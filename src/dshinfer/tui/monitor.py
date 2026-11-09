"""运行监视器：7 个视图，2 秒刷新一次，终端版的 strata。

视图（按 1–7 或 ← → 切换）：
    1 Overview   一眼看完：tok/s、显存、GPU、并发、上下文占用、MoE、系统内存、最近请求
    2 Serving    服务层视角：调度器队列、在飞、直方图（TTFT / 端到端 / 排队）
    3 Requests   单请求时间线：最近 N 条的 queued → TTFT → decode → total
    4 Engine     引擎视角：llama-server 的全部原始指标（含投机解码）
    5 Hardware   硬件明细：每张卡的 NVML 字段、CPU 每核占用、系统内存
    6 Logs       引擎日志尾巴（容器里是 /var/log/dshinfer/llama-server.log）
    7 Connect    对外接口：URL、端点清单、模型元数据、怎么 curl

数据来源都是本机回环 + NVML，所以面板本身不引入任何常驻进程（不用 Prometheus +
Grafana，那套要多 300–500 MB 常驻内存，而 WSL 只有 7.5 GB）。q 退出时会问一句：
后台继续跑（只关面板）还是把引擎停掉。

界面上的三个"活得像个真面板"的细节：
  · 顶栏有阶段条（空闲 / 加载 / 生成 / 排队 / 错误）和 [LIVE] / [STALE Ns] / [FROZEN] 状态，
    采样失败时保留上一帧、只把状态标成 STALE，不会整屏闪成 0；
  · 宽度 < 100 列自动进窄屏紧凑布局（KPI 两列、趋势图降为单行、表格砍列）；
  · t 键循环三套配色（strata 黑绿 / ocean 暗蓝 / paper 浅色）。
"""

from __future__ import annotations

import asyncio
import re
import time
import urllib.request
from pathlib import Path

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from . import live
from .preset import Preset
from .theme import STRATA, THEMES, label as theme_label, markup_color, next_theme
from .widgets import (
    DASH,
    Kpi,
    Spark,
    bar,
    fill_table,
    fmt_bytes,
    fmt_int,
    fmt_mib,
    fmt_ms,
    fmt_pct,
    fmt_rate,
    fmt_seconds,
    meter,
    set_active_theme,
    short_id,
    theme_tone,
    tone_for,
)

# 顶栏/状态条按终端宽度排版：CJK 与 emoji 占两列，不能只看 len()
_TAG_RE = re.compile(r"\[/?[a-zA-Z0-9_#\- ]*\]")
_WIDE_RANGES = ((0x1100, 0x115F), (0x2E80, 0xA4CF), (0xAC00, 0xD7A3), (0xF900, 0xFAFF),
                (0xFE30, 0xFE6F), (0xFF00, 0xFF60), (0xFFE0, 0xFFE6), (0x1F300, 0x1FAFF))


def plain_width(text: str) -> int:
    """去掉 Textual/Rich 标记后按终端列宽算长度（CJK 算两列）。"""
    total = 0
    for ch in _TAG_RE.sub("", text):
        code = ord(ch)
        total += 2 if any(low <= code <= high for low, high in _WIDE_RANGES) else 1
    return total


VIEWS = [
    ("overview", "概览"),
    ("serving", "服务"),
    ("requests", "请求"),
    ("engine", "引擎"),
    ("hardware", "硬件"),
    ("logs", "日志"),
    ("connect", "连接"),
]

# 引擎指标的中文名（顺序就是 Engine 视图里的显示顺序，未知项排在最后）
ENGINE_LABELS: list[tuple[str, str]] = [
    ("prompt_tokens_total", "处理的 prompt 令牌"),
    ("prompt_tokens_cached_total", "其中命中前缀缓存"),
    ("tokens_predicted_total", "生成的令牌"),
    ("tokens_predicted_seconds_total", "累计生成耗时（秒）"),
    ("prompt_seconds_total", "累计预填充耗时（秒）"),
    ("n_decode_total", "decode 次数"),
    ("n_tokens_max", "单请求最长令牌"),
    ("n_busy_slots_per_decode", "每次 decode 占用的 slot"),
    ("requests_processing", "处理中的请求"),
    ("requests_deferred", "被推迟的请求"),
    ("predicted_tokens_seconds", "生成速度（引擎 gauge）"),
    ("prompt_tokens_seconds", "预填充速度（引擎 gauge）"),
    ("spec_decode_num_draft_tokens_total", "投机解码：草稿令牌"),
    ("spec_decode_num_accepted_tokens_total", "投机解码：被接受"),
]


def _tone_rate(value: float | None) -> str:
    """生成速度：本机 32K 配置单流约 40 t/s，4 并发单条约 33 t/s。"""
    if not value:
        return "dim"
    if value >= 30:
        return theme_tone("green")
    if value >= 10:
        return theme_tone("yellow")
    return theme_tone("red")


#: 阶段条，顺序即显示顺序（对标 Strata 的 Model state）
PHASES = ("空闲", "加载", "生成", "排队", "错误")


def phase_index(snap: live.Snapshot) -> int:
    """把三路读数收敛成一个阶段：0 空闲 / 1 加载 / 2 生成 / 3 排队 / 4 错误。

    判定顺序：引擎报了错误 → 错误；有请求真在算 → 生成；只是排队 → 排队；
    引擎 ready=False（正在读权重）→ 加载；其余视为空闲。
    """
    health = snap.health or {}
    engine = health.get("engine")
    engine = engine if isinstance(engine, dict) else {}
    if engine.get("last_error"):
        return 4
    if (snap.slots.get("processing") or 0) > 0 or (snap.service_req.get("inflight") or 0) > 0:
        return 2
    if (snap.service_req.get("queue_depth") or 0) > 0:
        return 3
    if engine.get("loading") or engine.get("ready") is False:
        return 1
    return 0


def phase_line(snap: live.Snapshot, *, compact: bool = False) -> str:
    """一行阶段条：当前阶段反色高亮，其余压暗；右侧跟"本请求已用令牌"。"""
    active = phase_index(snap)
    chips = " ".join(
        f"[bold reverse] {name} [/]" if idx == active else f"[dim]{name}[/]"
        for idx, name in enumerate(PHASES)
    )
    used = snap.totals.get("n_tokens_max") or 0.0
    total = snap.ctx_limit or 0
    pct = used / total * 100 if total else 0.0
    if compact:
        progress = f"[dim]{used:,.0f}/{total / 1024:,.0f}K[/] [{tone_for(min(100.0, pct * 4))}]{pct:,.0f}%[/]"
    else:
        progress = (
            f"[dim]单请求最长令牌[/] [bold]{used:,.0f}[/] / {total:,.0f}"
            f"  [{tone_for(min(100.0, pct * 4))}]{pct:,.1f}%[/]"
        )
    return f"{chips}   {progress}"


def _token_counts(item: dict) -> tuple[float, float, float]:
    """trace 里的令牌三元组 (输入, 输出, 命中缓存)。

    服务层现在的字段名是 prompt / completion / cached，早期版本叫 input / output，
    面板两种都认，避免中间版本混跑时整列变成「—」。
    """
    tokens = item.get("tokens") or {}
    prompt = tokens.get("prompt", tokens.get("input")) or 0
    output = tokens.get("completion", tokens.get("output")) or 0
    cached = tokens.get("cached") or 0
    return prompt, output, cached


def trace_tok_s(item: dict) -> float | None:
    """单条请求的生成速度：优先 decode 段，退化到 (总耗时 - TTFT)。"""
    spans = item.get("spans_ms") or {}
    _, output, _ = _token_counts(item)
    if not output:
        return None
    decode_ms = spans.get("decode")
    if decode_ms:
        return output / (decode_ms / 1000.0)
    total_ms = spans.get("total")
    ttft_ms = spans.get("ttft") or 0.0
    if total_ms and total_ms - ttft_ms > 0:
        return output / ((total_ms - ttft_ms) / 1000.0)
    return None


def trace_cache_pct(item: dict) -> float | None:
    """前缀缓存命中率：命中令牌 / (输入 + 命中)。"""
    prompt, _, cached = _token_counts(item)
    total = prompt + cached
    return cached / total * 100.0 if total else None


class QuitScreen(ModalScreen[str]):
    """q 退出时的三选一：后台继续跑 / 停掉引擎 / 取消。"""

    BINDINGS = [
        Binding("escape", "cancel", "取消"),
        Binding("q", "cancel", "取消"),
    ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("[bold]退出监视器[/]", classes="help-title"),
            Static(
                "面板只是一个读数窗口，关掉它不会影响服务。\n"
                "要不要顺带把引擎也停掉（省显存、下次请求会重新加载 8–22 秒）？",
                classes="help-body",
            ),
            OptionList(
                Option("后台继续跑（只关面板）", id="keep"),
                Option("停掉引擎并退出（释放显存）", id="stop"),
                Option("取消", id="cancel"),
                id="quit-choices",
            ),
            id="help-box",
        )

    def on_mount(self) -> None:
        self.query_one("#quit-choices", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id or "cancel")

    def action_cancel(self) -> None:
        self.dismiss("cancel")


class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "关闭"),
        Binding("q", "close", "关闭"),
        Binding("question_mark", "close", "关闭"),
    ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("[bold]监视器按键[/]", classes="help-title"),
            Static(
                "1–7 或 ← →   切换视图（概览 / 服务 / 请求 / 引擎 / 硬件 / 日志 / 连接）\n"
                "空格         冻结 / 解冻（冻结时停止采样，画面定住方便看数）\n"
                "t            换配色（黑绿 / 暗蓝 / 浅色）\n"
                "r            立刻刷新一次\n"
                "q            退出（可只关面板，也可顺带停引擎）\n"
                "?            本帮助\n\n"
                "宽度 < 100 列自动进入紧凑布局：KPI 两列、趋势图降为单行、表格砍列。\n"
                "采样失败会保留上一帧，顶栏状态条从 [LIVE] 变成 [STALE Ns]。\n"
                "所有曲线只保留最近约 4 分钟（120 点 × 2 秒），不落盘。",
                classes="help-body",
            ),
            Static("[dim]Esc 返回[/]", classes="help-hint"),
            id="help-box",
        )

    def action_close(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------- 各视图


class OverviewView(VerticalScroll):
    def __init__(self) -> None:
        super().__init__(id="view-overview")
        self._tok_history: list[float] = []
        self._vram_history: list[float] = []

    def compose(self) -> ComposeResult:
        yield Static("", id="phase-line")
        with Horizontal(id="row-kpi", classes="row"):
            yield Kpi("生成速度", id="k-tok", classes="kpi")
            yield Kpi("显存", id="k-vram", classes="kpi")
            yield Kpi("GPU 负载", id="k-gpu", classes="kpi")
            yield Kpi("并发", id="k-conc", classes="kpi")
        with Horizontal(id="row-spark", classes="row"):
            yield Spark("生成速度 tok/s", id="s-tok", classes="spark")
            yield Spark("显存 MiB", id="s-vram", classes="spark")
        yield Static("", id="ctx-line")
        yield Static("", id="model-line")
        yield Static("", id="mem-line")
        yield Static("[bold]最近请求[/]", classes="section-title")
        yield DataTable(id="recent", classes="mini")

    def _compact(self) -> bool:
        return bool(getattr(self.app, "compact", False))

    def apply(self, snap: live.Snapshot) -> None:
        tok = snap.tok_s
        rate = snap.rate
        gpu = snap.gpu
        compact = self._compact()
        self._tok_history.append(tok)
        self._tok_history = self._tok_history[-120:]
        used = gpu.get("used_mib") or 0.0
        self._vram_history.append(used)
        self._vram_history = self._vram_history[-120:]

        self.query_one("#phase-line", Static).update(phase_line(snap, compact=compact))
        self.query_one("#k-tok", Kpi).set(
            f"{tok:,.1f}",
            "tok/s",
            f"单条 {fmt_rate(rate.get('instant'))} · 聚合 {fmt_rate(rate.get('aggregate'))}"
            if compact
            else (
                f"单条 {fmt_rate(rate.get('instant'))} · 聚合 {fmt_rate(rate.get('aggregate'))}"
                f" · 已生成 {fmt_int(snap.totals.get('tokens_predicted'))} tok"
            ),
            _tone_rate(tok),
        )
        total_mib = gpu.get("total_mib") or 0.0
        if total_mib:
            vram_sub = (
                f"共 {total_mib:,.0f} MiB · 余 {gpu.get('free_mib') or 0:,.0f} MiB"
                if compact
                else f"共 {total_mib:,.0f} MiB · 余 {fmt_mib(gpu.get('free_mib'))}"
            )
        else:
            vram_sub = snap.hardware_error or "读不到显存（NVML 不可用）"
        self.query_one("#k-vram", Kpi).set(
            f"{used:,.0f}",
            "MiB",
            vram_sub,
            tone_for(used / total_mib * 100 if total_mib else None),
        )
        util = gpu.get("util_pct")
        temp = gpu.get("temp_c")
        power = gpu.get("power_w")
        if compact:
            gpu_sub = f"{fmt_int(temp)} °C · {fmt_int(power)} W"
        else:
            pcie = (
                f"PCIe {gpu.get('pcie_gen')}x{gpu.get('pcie_width')}"
                if gpu.get("pcie_gen") and gpu.get("pcie_width")
                else "PCIe ?"
            )
            gpu_sub = f"{fmt_int(temp)} °C · {fmt_int(power)} W · {pcie}"
        self.query_one("#k-gpu", Kpi).set(fmt_pct(util), "", gpu_sub, tone_for(util))
        processing = snap.slots.get("processing") or 0.0
        inflight = snap.service_req.get("inflight") or 0.0
        queue_depth = snap.service_req.get("queue_depth", 0) or 0
        conc_sub = (
            f"在飞 {inflight:,.0f} · 排队 {queue_depth:,.0f}"
            if compact
            else (
                f"服务层在飞 {inflight:,.0f} · 排队 {queue_depth:,.0f}"
                f" · 引擎推迟 {snap.slots.get('deferred', 0):,.0f}"
            )
        )
        self.query_one("#k-conc", Kpi).set(
            f"{processing:,.0f}",
            "在飞",
            conc_sub,
            "cyan" if processing else "dim",
        )

        tone = markup_color(self.app.theme)
        spark_tok = self.query_one("#s-tok", Spark)
        window = fmt_rate(rate.get("window"))
        mean_rate = self._mean(self._tok_history)
        spark_tok.set_data(
            self._tok_history,
            f"{window} tok/s · 均 {mean_rate:,.1f}"
            if compact
            else f"窗口 {window} tok/s · 60 s 均 {mean_rate:,.1f}",
            tone,
        )
        spark_vram = self.query_one("#s-vram", Spark)
        spark_vram.set_data(
            self._vram_history,
            f"{used:,.0f} MiB · 峰值 {max(self._vram_history or [0]):,.0f} MiB",
            tone,
            total_mib or None,
        )

        n_tokens_max = snap.totals.get("n_tokens_max") or 0.0
        cache_ratio = snap.cache.get("hit_ratio") or 0.0
        if compact:
            self.query_one("#ctx-line", Static).update(
                meter("上下文", n_tokens_max, snap.ctx_limit or None, 10, unit="tokens")
                + f"   [dim]缓存命中[/] [{tone_for(cache_ratio, 20, 60)}]{cache_ratio:,.0f}%[/]"
                + f"   [dim]请求[/] {fmt_int(snap.service_req.get('total'))}"
            )
        else:
            self.query_one("#ctx-line", Static).update(
                meter("上下文占用（单请求最长）", n_tokens_max, snap.ctx_limit or None, 18, unit="tokens")
                + f"   [dim]前缀缓存命中[/] [{tone_for(cache_ratio, 20, 60)}]{cache_ratio:,.0f}%[/]"
                + f"   [dim]已解码[/] {fmt_int(snap.totals.get('n_decode'))} 次"
                + f"   [dim]请求[/] {fmt_int(snap.service_req.get('total'))}"
            )
        self.query_one("#model-line", Static).update(self._model_line(snap, compact=compact))
        mem = snap.mem
        mem_tail = (
            f"   [dim]核心[/] {snap.cpu.get('cores') or DASH}"
            f"   [dim]{snap.backend}[/]"
            if compact
            else (
                f"   [dim]CPU[/] {snap.cpu.get('name') or DASH}"
                f"   [dim]核心[/] {snap.cpu.get('cores') or DASH}"
                f"   [dim]采集[/] {snap.backend}"
            )
        )
        self.query_one("#mem-line", Static).update(
            meter("系统内存", mem.get("used_mib"), mem.get("total_mib"), 10 if compact else 18)
            + mem_tail
        )

        rows = []
        for item in snap.traces[:8]:
            spans = item.get("spans_ms") or {}
            _, out_tokens, _ = _token_counts(item)
            rid = short_id(str(item.get("id", "")), 12)
            status = item.get("status", DASH)
            if compact:
                rows.append(
                    (
                        rid,
                        status,
                        fmt_ms(spans.get("ttft")),
                        fmt_ms(spans.get("total")),
                        fmt_rate(trace_tok_s(item)),
                    )
                )
            else:
                rows.append(
                    (
                        rid,
                        status,
                        fmt_ms(spans.get("queue")),
                        fmt_ms(spans.get("ttft")),
                        fmt_ms(spans.get("decode")),
                        fmt_ms(spans.get("total")),
                        fmt_rate(trace_tok_s(item)),
                        fmt_int(out_tokens),
                        fmt_pct(trace_cache_pct(item)),
                    )
                )
        columns = (
            ["请求", "状态", "TTFT", "总耗时", "tok/s"]
            if compact
            else ["请求", "状态", "排队", "TTFT", "decode", "总耗时", "tok/s", "输出", "缓存%"]
        )
        fill_table(self.query_one("#recent", DataTable), columns, rows)

    @staticmethod
    def _mean(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    @staticmethod
    def _model_line(snap: live.Snapshot, *, compact: bool = False) -> str:
        info = snap.model_info or {}
        moe = info.get("moe") or {}
        if compact:
            head = (
                f"[dim]模型[/] {short_id(snap.model, 22)}"
                f"   [dim]{info.get('arch') or DASH}[/]"
                f"   [dim]{info.get('n_layers') or DASH} 层[/]"
                f"   [dim]{fmt_bytes(info.get('file_size_bytes'))}[/]"
            )
        else:
            head = (
                f"[dim]模型[/] {snap.model}"
                f"   [dim]架构[/] {info.get('arch') or DASH}"
                f"   [dim]层数[/] {info.get('n_layers') or DASH}"
                f"   [dim]头[/] {info.get('n_head') or DASH}/{info.get('n_head_kv') or DASH}"
                f"   [dim]FFN[/] {info.get('n_ff') or DASH}"
                f"   [dim]文件[/] {fmt_bytes(info.get('file_size_bytes'))}"
            )
        if moe.get("is_moe"):
            moe_line = (
                f"[dim]MoE[/] [bold]{moe.get('experts_per_layer'):,}[/] 专家/层"
                f" · 每 token 激活 [bold]{moe.get('used_per_layer'):,}[/]"
                f" · 显存里共 [bold]{moe.get('experts_total'):,}[/] 组"
            )
        else:
            moe_line = "[dim]MoE[/] 非 MoE（稠密模型，没有 expert 概念）"
        return "\n".join([head, moe_line])


class ServingView(VerticalScroll):
    def __init__(self) -> None:
        super().__init__(id="view-serving")

    def compose(self) -> ComposeResult:
        yield Static("", id="serve-summary")
        yield Static("[bold]引擎与调度[/]", classes="section-title")
        yield DataTable(id="serve-engine")
        yield Static("[bold]服务层直方图[/]", classes="section-title")
        yield DataTable(id="serve-hist")

    def apply(self, snap: live.Snapshot) -> None:
        rate = snap.rate
        self.query_one("#serve-summary", Static).update(
            "[dim]单条 decode[/] "
            f"[bold]{fmt_rate(rate.get('instant'))}[/] tok/s"
            f"   [dim]EWMA[/] {fmt_rate(rate.get('window'))} tok/s"
            f"   [dim]聚合[/] {fmt_rate(rate.get('aggregate'))} tok/s"
            f"\n[dim]在飞[/] {fmt_int(snap.service_req.get('inflight'))}"
            f"   [dim]队列深度[/] {bar(min(100.0, (snap.service_req.get('queue_depth') or 0) / 8 * 100), 12)}"
            f"   [dim]调度 dispatch/enqueue[/] {fmt_int(snap.service_req.get('dispatched'))}/{fmt_int(snap.service_req.get('enqueued'))}"
        )
        engine_rows = [
            ("生成令牌", fmt_int(snap.totals.get("tokens_predicted"))),
            ("prompt 令牌", fmt_int(snap.totals.get("prompt_tokens"))),
            ("命中前缀缓存", f"{fmt_int(snap.cache.get('hit_tokens'))}（{snap.cache.get('hit_ratio', 0):,.0f}%）"),
            ("decode 次数", fmt_int(snap.totals.get("n_decode"))),
            ("单请求最长令牌", fmt_int(snap.totals.get("n_tokens_max"))),
            ("处理中 / 推迟", f"{snap.slots.get('processing', 0):,.0f} / {snap.slots.get('deferred', 0):,.0f}"),
            ("每次 decode 的 slot", fmt_rate(snap.slots.get("busy"), " 个")),
            ("投机解码 草稿/接受", f"{fmt_int(snap.totals.get('spec_draft'))} / {fmt_int(snap.totals.get('spec_accepted'))}"),
        ]
        fill_table(self.query_one("#serve-engine", DataTable), ["指标", "值"], engine_rows)

        hist_rows = []
        for key, label in (
            ("ttft", "TTFT 首字"),
            ("request", "端到端"),
            ("queue", "排队等待"),
        ):
            hist = snap.hist.get(key) or {}
            hist_rows.append(
                (
                    label,
                    fmt_ms(hist.get("p50_s", 0) * 1000 if hist else None),
                    fmt_ms(hist.get("p95_s", 0) * 1000 if hist else None),
                    fmt_ms(hist.get("mean_s", 0) * 1000 if hist else None),
                    fmt_int(hist.get("count")) if hist else DASH,
                )
            )
        fill_table(
            self.query_one("#serve-hist", DataTable),
            ["指标", "p50", "p95", "均值", "样本"],
            hist_rows,
        )


class RequestsView(VerticalScroll):
    def __init__(self) -> None:
        super().__init__(id="view-requests")

    def compose(self) -> ComposeResult:
        yield Static("", id="req-summary")
        yield Static("[bold]最近请求（服务层 trace，新的在上）[/]", classes="section-title")
        yield DataTable(id="req-table")

    def apply(self, snap: live.Snapshot) -> None:
        self.query_one("#req-summary", Static).update(
            f"[dim]在飞[/] {fmt_int(snap.service_req.get('inflight'))}"
            f"   [dim]排队[/] {fmt_int(snap.service_req.get('queue_depth'))}"
            f"   [dim]累计请求[/] {fmt_int(snap.service_req.get('total'))}"
            f"   [dim]trace 条数[/] {len(snap.traces)}"
        )
        compact = bool(getattr(self.app, "compact", False))
        rows = []
        for item in snap.traces[:24]:
            spans = item.get("spans_ms") or {}
            in_tokens, out_tokens, _ = _token_counts(item)
            rid = short_id(str(item.get("id", "")), 16)
            status = item.get("status", DASH)
            tok_s = fmt_rate(trace_tok_s(item))
            if compact:
                rows.append(
                    (
                        rid,
                        status,
                        fmt_ms(spans.get("ttft")),
                        fmt_ms(spans.get("total")),
                        tok_s,
                        fmt_pct(trace_cache_pct(item)),
                    )
                )
            else:
                rows.append(
                    (
                        rid,
                        status,
                        fmt_ms(spans.get("queue")),
                        fmt_ms(spans.get("ttft")),
                        fmt_ms(spans.get("decode")),
                        fmt_ms(spans.get("total")),
                        fmt_int(in_tokens),
                        fmt_int(out_tokens),
                        fmt_pct(trace_cache_pct(item)),
                        tok_s,
                    )
                )
        columns = (
            ["请求", "状态", "TTFT", "总耗时", "tok/s", "缓存%"]
            if compact
            else [
                "请求",
                "状态",
                "排队",
                "TTFT",
                "decode",
                "总耗时",
                "输入",
                "输出",
                "缓存%",
                "tok/s",
            ]
        )
        fill_table(self.query_one("#req-table", DataTable), columns, rows)


class EngineView(VerticalScroll):
    def __init__(self) -> None:
        super().__init__(id="view-engine")

    def compose(self) -> ComposeResult:
        yield Static("", id="engine-summary")
        yield Static("[bold]llama-server 原始指标[/]", classes="section-title")
        yield DataTable(id="engine-table")

    def apply(self, snap: live.Snapshot) -> None:
        ready = (snap.health.get("engine") or {}).get("ready") if snap.health else None
        load_seconds = (snap.health.get("engine") or {}).get("load_seconds") if snap.health else None
        last_error = (snap.health.get("engine") or {}).get("last_error") if snap.health else None
        state = "就绪" if ready else ("加载中" if ready is False else "未知")
        self.query_one("#engine-summary", Static).update(
            f"[dim]引擎[/] {snap.engine}"
            f"   [dim]状态[/] [{tone_for(100 if ready else 40)}]{state}[/]"
            f"   [dim]加载耗时[/] {fmt_seconds(load_seconds) if load_seconds else DASH}"
            f"   [dim]地址[/] {snap.engine_url}"
            + (f"\n[red]最近错误：{last_error}[/]" if last_error else "")
        )
        rows = []
        known = {key for key, _ in ENGINE_LABELS}
        for key, label in ENGINE_LABELS:
            rows.append((label, key, _engine_value(snap.engine_metrics.get(key))))
        for key in sorted(snap.engine_metrics):
            if key in known or "{" in key:
                continue
            rows.append((key, key, _engine_value(snap.engine_metrics[key])))
        fill_table(self.query_one("#engine-table", DataTable), ["指标", "原始键", "值"], rows)


def _engine_value(value: float | None) -> str:
    if value is None:
        return DASH
    if value >= 1000:
        return f"{value:,.0f}"
    if float(value).is_integer():
        return f"{value:,.0f}"
    return f"{value:,.2f}"


class HardwareView(VerticalScroll):
    def __init__(self) -> None:
        super().__init__(id="view-hardware")

    def compose(self) -> ComposeResult:
        yield Static("", id="hw-summary")
        yield Static("[bold]GPU（NVML）[/]", classes="section-title")
        yield DataTable(id="hw-gpu")
        yield Static("", id="hw-cpu")
        yield Static("", id="hw-mem")

    def apply(self, snap: live.Snapshot) -> None:
        cpu = snap.cpu
        mem = snap.mem
        self.query_one("#hw-summary", Static).update(
            f"[dim]采集后端[/] {snap.backend}"
            + (f"   [red]{snap.hardware_error}[/]" if snap.hardware_error else "")
        )
        rows = []
        for gpu in snap.gpus:
            for label, key in (
                ("GPU", "name"),
                ("驱动", "driver"),
                ("显存", "used_mib"),
                ("显存总量", "total_mib"),
                ("显存空闲", "free_mib"),
                ("GPU 利用率", "util_pct"),
                ("显存带宽占用", "mem_util_pct"),
                ("温度", "temp_c"),
                ("功耗", "power_w"),
                ("功耗上限", "power_limit_w"),
                ("PCIe", None),
                ("风扇", "fan_pct"),
                ("核心频率", "clock_core_mhz"),
                ("显存频率", "clock_mem_mhz"),
            ):
                if key is None:
                    value = f"gen {gpu.get('pcie_gen')} x{gpu.get('pcie_width')}"
                else:
                    raw = gpu.get(key)
                    value = _hw_value(key, raw)
                rows.append((f"{label}", str(value)))
        fill_table(self.query_one("#hw-gpu", DataTable), ["项目", "值"], rows or [("—", "没有可用的 GPU 数据")])

        per_core = cpu.get("per_core_pct") or []
        core_line = ""
        if per_core:
            chunks = [f"[dim]CPU{n:>2}[/] {bar(value, 8)} {value:3.0f}%" for n, value in enumerate(per_core)]
            core_line = "\n".join(chunks)
        self.query_one("#hw-cpu", Static).update(
            f"[dim]CPU[/] {cpu.get('name') or DASH}"
            f"   [dim]核心[/] {cpu.get('cores') or DASH}"
            f"   [dim]总占用[/] {fmt_pct(cpu.get('usage_pct'), 1)}"
            f"   [dim]1 分钟负载[/] {cpu.get('load1') if cpu.get('load1') is not None else DASH}"
            + (f"\n[dim]每核[/]\n{core_line}" if core_line else "")
        )
        self.query_one("#hw-mem", Static).update(
            meter("系统内存", mem.get("used_mib"), mem.get("total_mib"), 24)
        )


def _hw_value(key: str, raw) -> str:
    if raw is None:
        return DASH
    if key.endswith("_mib"):
        return fmt_mib(raw)
    if key in ("util_pct", "mem_util_pct", "fan_pct"):
        return f"{raw:,.0f}%"
    if key == "temp_c":
        return f"{raw:,.0f} °C"
    if key in ("power_w", "power_limit_w"):
        return f"{raw:,.0f} W"
    if key.endswith("_mhz"):
        return f"{raw:,.0f} MHz"
    return str(raw)


class LogsView(VerticalScroll):
    def __init__(self, log_path: str) -> None:
        super().__init__(id="view-logs")
        self.log_path = log_path
        self._offset = 0

    def compose(self) -> ComposeResult:
        yield Static("", id="logs-head")
        yield RichLog(id="logs", wrap=True, markup=False, max_lines=2000)

    def apply(self, snap: live.Snapshot) -> None:
        path = Path(self.log_path).expanduser() if self.log_path else None
        exists = bool(path and path.is_file())
        self.query_one("#logs-head", Static).update(
            f"[dim]日志[/] {self.log_path or '（未配置）'}"
            f"   [dim]存在[/] {'是' if exists else '否'}"
            "   [dim]捕获[/] "
            + ("是（容器内 tini 把引擎 stdout 落这里）" if exists else "引擎输出可能只进了 docker logs")
        )
        lines, offset = live.read_log(path, self._offset)
        self._offset = offset
        widget = self.query_one("#logs", RichLog)
        for line in lines[-200:]:
            widget.write(line)


class ConnectView(VerticalScroll):
    def __init__(self, preset: Preset) -> None:
        super().__init__(id="view-connect")
        self.preset = preset

    def compose(self) -> ComposeResult:
        yield Static("", id="connect-body")

    def apply(self, snap: live.Snapshot) -> None:
        info = snap.model_info or {}
        base = snap.service_url.rstrip("/")
        self.query_one("#connect-body", Static).update(
            "\n".join(
                [
                    "[bold]服务层（OpenAI 兼容）[/]",
                    f"  接口      {base}",
                    f"  模型 ID   {snap.model}",
                    f"  上下文    {snap.ctx_limit:,}（模型训练上下文 {info.get('n_ctx_train') or DASH:,}）",
                    f"  健康      {base}/healthz",
                    "",
                    "[bold]引擎（llama-server，仅容器内/本机可见）[/]",
                    f"  指标      {snap.engine_url}",
                    f"  日志      {self.preset.engine_log_path or DASH}",
                    "",
                    "[bold]端点[/]",
                    "  GET  /v1/models              模型列表（含 loaded 标记）",
                    "  POST /v1/chat/completions    对话（流式 / 非流式，带 Server-Timing）",
                    "  GET  /healthz                服务 + 引擎状态",
                    "  GET  /metrics                Prometheus 指标",
                    "  GET  /admin/traces?limit=N   最近 N 条请求时间线",
                    "  POST /admin/models/load      热切换模型（有在飞请求时拒绝）",
                    "  POST /admin/models/unload    卸载引擎（释放显存）",
                    "",
                    "[bold]常用命令[/]",
                    f"  curl -s {base}/healthz",
                    f"  curl -s {base}/v1/models",
                    f"  curl -s {base}/metrics | head",
                    f"  docker logs --tail 50 dshinfer",
                    "",
                    "[dim]面板自身不落盘、不开监听端口，只读本机回环与 NVML。[/]",
                ]
            )
        )


# ---------------------------------------------------------------- 主应用


#: 小于这个宽度就进紧凑布局（80 列是 SSH / 分屏 / 竖屏的常见宽度）
_COMPACT_WIDTH = 100


class MonitorApp(App[None]):
    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("1", "show_view(0)", "概览", show=False),
        Binding("2", "show_view(1)", "服务", show=False),
        Binding("3", "show_view(2)", "请求", show=False),
        Binding("4", "show_view(3)", "引擎", show=False),
        Binding("5", "show_view(4)", "硬件", show=False),
        Binding("6", "show_view(5)", "日志", show=False),
        Binding("7", "show_view(6)", "连接", show=False),
        Binding("left", "prev_view", "上一屏", show=False),
        Binding("right", "next_view", "下一屏", show=False),
        Binding("space", "toggle_freeze", "冻结"),
        Binding("t", "cycle_theme", "主题"),
        Binding("r", "refresh_now", "刷新"),
        Binding("question_mark", "help", "帮助"),
        Binding("q", "ask_quit", "退出"),
    ]

    def __init__(
        self,
        preset: Preset,
        *,
        engine_url: str | None = None,
        service_url: str | None = None,
        interval: float = 2.0,
    ) -> None:
        super().__init__()
        self.preset = preset
        self.engine_url = engine_url or f"http://127.0.0.1:{preset.engine_port}/metrics"
        self.service_url = service_url or f"http://{_loopback(preset.host)}:{preset.port}"
        self.interval = max(0.5, interval)
        self._started = time.monotonic()
        self._busy = False
        self._snap: live.Snapshot | None = None
        self._model_info = live.load_model_info(preset.gguf_path)
        self._current_view = 0
        self._compact = False
        self._frozen = False
        self._last_ok: float | None = None
        self._theme = STRATA
        # 主题必须在样式表解析之前注册：app.tcss 里的 $card-border 就是从这里来的，
        # 放到 on_mount 里会先被判定成 "undefined variable" 而整张样式表加载失败。
        for theme in THEMES:
            self.register_theme(theme)
        self.theme = self._theme
        set_active_theme(self._theme)

    @property
    def compact(self) -> bool:
        """供各视图判断要不要砍列/缩副标题（宽度 < 100 列时为真）。"""
        return self._compact

    def compose(self) -> ComposeResult:
        yield Static("", id="topbar")
        yield from (
            OverviewView(),
            ServingView(),
            RequestsView(),
            EngineView(),
            HardwareView(),
            LogsView(self.preset.engine_log_path),
            ConnectView(self.preset),
        )
        yield Static("", id="hintbar")

    def on_mount(self) -> None:
        self._compact = self.size.width < _COMPACT_WIDTH
        self.screen.set_class(self._compact, "compact")
        self._show_view(0)
        self.set_interval(self.interval, self._tick)
        self.set_interval(0.5, self._render_topbar)
        self.run_worker(self._tick(), thread=False)

    def on_resize(self, event: events.Resize) -> None:
        """窄屏/宽屏切换：只换 CSS 类和表格列，不重建控件（曲线历史要留着）。"""
        compact = event.size.width < _COMPACT_WIDTH
        if compact != self._compact:
            self._compact = compact
            self.screen.set_class(compact, "compact")
            if self._snap is not None:
                self._apply_snapshot(self._snap)
        self._render_topbar()

    # ------------------------------------------------------------ 刷新

    async def _tick(self) -> None:
        if self._busy or self._frozen:
            return
        self._busy = True
        snap: live.Snapshot | None = None
        try:
            snap = await asyncio.to_thread(
                live.sample_snapshot,
                engine_url=self.engine_url,
                service_url=self.service_url,
                model=self.preset.model_stem,
                ctx_limit=self.preset.n_ctx,
                engine=self.preset.engine,
                model_info=self._model_info,
                trace_limit=24,
            )
        except Exception as exc:  # 采集层自己会兜底，这里只防"意外中的意外"
            self.log.warning(f"快照失败：{exc}")
        finally:
            self._busy = False

        if snap is not None and snap.health:
            # 服务层答上了才算这一帧新鲜；引擎指标缺几项只是 degradation，不算 STALE
            self._last_ok = time.monotonic()
            self._snap = snap
            self._apply_snapshot(snap)
        elif self._snap is None:
            # 第一帧就没连上：至少把"HOST 一片空白"渲染成可读的 0 / 破折号
            self._snap = snap or live.Snapshot(
                model=self.preset.model_stem, ctx_limit=self.preset.n_ctx
            )
            self._apply_snapshot(self._snap)
        # 其余情况保留上一帧：顶栏状态条自己会从 [LIVE] 变成 [STALE Ns]
        self._render_topbar()

    def _apply_snapshot(self, snap: live.Snapshot) -> None:
        for view in (
            self.query_one(OverviewView),
            self.query_one(ServingView),
            self.query_one(RequestsView),
            self.query_one(EngineView),
            self.query_one(HardwareView),
            self.query_one(LogsView),
            self.query_one(ConnectView),
        ):
            try:
                view.apply(snap)
            except Exception as exc:  # 一个视图渲染挂了不该拖垮整屏
                self.log.warning(f"{type(view).__name__}.apply 失败: {exc}")

    # ------------------------------------------------------------ 顶栏

    def _status_chip(self) -> str:
        """[LIVE] / [STALE Ns] / [FROZEN] / 未连上 —— 一眼看出这屏数字有多新。"""
        if self._frozen:
            return "[bold yellow]FROZEN 冻结[/]"
        if self._last_ok is None:
            return "[bold red]● 未连上[/]"
        age = time.monotonic() - self._last_ok
        if age <= max(2.5 * self.interval, 6.0):
            return "[bold green]● LIVE[/]"
        return f"[bold yellow]● STALE {fmt_seconds(age)}[/]"

    def _phase_chip(self, snap: live.Snapshot) -> str:
        index = phase_index(snap)
        tone = ("green", "yellow", "cyan", "yellow", "red")[index]
        dot = "●" if index in (2, 3) else "○"
        return f"[{tone}]{dot} {PHASES[index]}[/]"

    def _render_topbar(self) -> None:
        snap = self._snap
        accent = markup_color(self.theme)
        view_name = VIEWS[self._current_view][1]
        width = self.size.width or 80
        tabs = " ".join(
            f"[bold {accent}]{idx + 1}[/] {label}"
            if idx == self._current_view
            else f"[dim]{idx + 1} {label}[/]"
            for idx, (_, label) in enumerate(VIEWS)
        )
        model = self.preset.model_stem
        ctx = f"{self.preset.n_ctx // 1024}K"
        status = self._status_chip()
        phase = self._phase_chip(snap) if snap is not None else ""
        tail = (phase + "  " if phase else "") + status
        brand = f" [bold {accent}]dshinfer[/]  [bold]{view_name}[/]"
        budget = max(24, width - 2)  # #topbar padding: 0 1
        gpu = snap.gpu if snap is not None else None

        # 段落按优先级排：数字越小越先被丢掉（宽度不够时保模型、砍引擎/GPU）
        def segments(model_text: str) -> list[tuple[str, str, int]]:
            rows = [
                ("url", f"  [dim]{self.service_url}[/]", 3),
                ("model", f"  [dim]模型[/] {model_text}", 9),
                ("ctx", f"  [dim]上下文[/] {ctx}", 5),
            ]
            if gpu:
                rows.append(
                    (
                        "gpu",
                        f"  [dim]GPU[/] {fmt_pct(gpu.get('util_pct'))}"
                        f" [dim]显存[/] {gpu.get('used_mib', 0):,.0f}"
                        f"/{gpu.get('total_mib', 0):,.0f} MiB",
                        1,
                    )
                )
            rows.append(("engine", f"  [dim]引擎[/] {self.preset.engine}", 2))
            return rows

        def assemble(model_text: str, keys: set[str]) -> str:
            return "".join(seg for key, seg, _ in segments(model_text) if key in keys)

        keys = {key for key, _, _ in segments(model)}
        widths = {key: plain_width(seg) for key, seg, _ in segments("")}
        base = plain_width(brand) + plain_width("  " + tail)
        model_text = model

        def needed() -> int:
            body = sum(widths[key] for key in keys if key != "model")
            if "model" in keys:
                body += widths["model"] + plain_width(model_text)
            return base + body

        # 宽度不够时的让位顺序：先砍 GPU → 引擎 → 地址 → 上下文，最后才截模型名
        for victim in ("gpu", "engine", "url", "ctx"):
            if needed() <= budget:
                break
            keys.discard(victim)
        while needed() > budget and "model" in keys and plain_width(model_text) > 12:
            excess = needed() - budget
            model_text = short_id(model, max(12, plain_width(model_text) - excess))
        if needed() > budget:
            keys.clear()
        head = brand + assemble(model_text, keys) + ("  " + tail if tail else "")
        self.query_one("#topbar", Static).update("\n".join([head, "  " + tabs]))
        hint = self.query_one("#hintbar", Static)
        hint.update(
            f" {view_name} · 1–7/←→ 视图 · 空格 {'解冻' if self._frozen else '冻结'}"
            f" · t 主题 · r 刷新 · ? 帮助 · q 退出"
            + (f"   [dim]{'；'.join(snap.errors)}[/]" if snap and snap.errors else "")
        )

    # ------------------------------------------------------------ 冻结 / 主题

    def action_toggle_freeze(self) -> None:
        self._frozen = not self._frozen
        if not self._frozen:
            self.run_worker(self._tick(), thread=False)
        self._render_topbar()

    def action_cycle_theme(self) -> None:
        self._theme = next_theme(self.theme)
        self.theme = self._theme
        set_active_theme(self._theme)
        self._render_topbar()
        self.notify(f"主题：{theme_label(self._theme)}", timeout=2)

    # ------------------------------------------------------------ 视图切换

    def _show_view(self, index: int) -> None:
        index %= len(VIEWS)
        self._current_view = index
        for idx, (key, _) in enumerate(VIEWS):
            widget = self.query_one(f"#view-{key}")
            widget.display = idx == index
        self._render_topbar()

    def action_show_view(self, index: int) -> None:
        self._show_view(index)

    def action_prev_view(self) -> None:
        self._show_view(self._current_view - 1)

    def action_next_view(self) -> None:
        self._show_view(self._current_view + 1)

    def action_refresh_now(self) -> None:
        live.reset_rate()
        self.run_worker(self._tick(), thread=False)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_ask_quit(self) -> None:
        self.push_screen(QuitScreen(), self._on_quit_choice)

    def _on_quit_choice(self, choice: str | None) -> None:
        if choice == "stop":
            self._stop_engine()
            self.exit()
        elif choice == "keep":
            self.exit()

    def _stop_engine(self) -> None:
        url = self.service_url.rstrip("/") + "/admin/models/unload"
        try:
            request = urllib.request.Request(url, method="POST", data=b"")
            urllib.request.urlopen(request, timeout=3).read()
        except Exception:
            pass


def _loopback(host: str) -> str:
    """0.0.0.0 不能当客户端地址用；面板自己连的永远是回环。"""
    return "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
