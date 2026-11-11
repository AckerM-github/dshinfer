"""一次快照（Snapshot）：三路数据源拼成 UI 直接可读的扁平结构。

为什么要有这一层：
  · UI 每 2 秒要一次全部数据，三路（NVML / 系统 / 服务层 /metrics）各有各的
    超时与失败方式，视图层不该关心"哪一路挂了"；
  · 引擎在服务进程内，所以只有一个 /metrics：在这里统一成 rate / totals / slots / cache
    几个字典，视图层不用知道指标名；
  · 所有采集都降级：任何一路失败只记一句 errors，绝不抛异常——面板挂在屏幕上
    跑几个小时，不能因为一次 curl 超时整屏崩掉。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

try:  # 采集层是 teammate 独立实现的纯标准库模块；缺失时面板退化成"没有硬件数据"
    from . import probe  # type: ignore
except Exception:  # pragma: no cover - 只在 probe.py 还没合入时走到
    probe = None  # type: ignore

from ..gguf import moe_summary, read_meta

ENGINE_GAUGE_KEYS = (
    "predicted_tokens_seconds",
    "prompt_tokens_seconds",
    "requests_processing",
    "requests_deferred",
    "n_busy_slots_per_decode",
)

TOKENS = {
    "input": "input",
    "output": "output",
    "cached": "cached",
}


# ---------------------------------------------------------------- HTTP

def fetch(url: str, timeout: float = 0.8) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - 本机回环
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return None


def fetch_json(url: str, timeout: float = 1.0) -> dict | None:
    body = fetch(url, timeout=timeout)
    if not body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------- Prometheus 文本

def parse_prom(text: str | None) -> dict[str, float]:
    """把 Prometheus 文本格式摊平成 {"名字{标签}": 值}，同时登记裸名字（取最后一个）。

    刻意不用 prometheus_client 的解析器：引擎那边吐的指标名里带冒号
    （llamacpp:xxx），不是合法的客户端指标名，官方解析器会拒收。
    """
    out: dict[str, float] = {}
    if not text:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, raw = line.rpartition(" ")
        if not key or not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        out[key] = value
        bare = key.split("{", 1)[0]
        out[bare] = value
    return out


def series(metrics: dict[str, float], name: str, **labels: str) -> float | None:
    """按 名字 + 标签子串 取值：dshinfer_tokens_total{direction="output"}。"""
    if not labels:
        return metrics.get(name)
    needles = [f'{k}="{v}"' for k, v in labels.items()]
    prefix = name + "{"
    for key, value in metrics.items():
        if not key.startswith(prefix):
            continue
        if all(needle in key for needle in needles):
            return value
    return None


def histogram(metrics: dict[str, float], name: str) -> dict[str, float] | None:
    """由 _bucket/_count/_sum 复算 {count, mean_s, p50_s, p95_s}（桶内线性插值）。"""
    count = metrics.get(name + "_count")
    total = metrics.get(name + "_sum")
    if not count or total is None:
        return None
    buckets: list[tuple[float, float]] = []
    prefix = name + "_bucket{"
    for key, value in metrics.items():
        if not key.startswith(prefix):
            continue
        le = key.split('le="', 1)[-1].rstrip('"}')
        try:
            bound = float(le)
        except ValueError:
            continue
        buckets.append((bound, value))
    buckets.sort()
    return {
        "count": count,
        "mean_s": total / count if count else 0.0,
        "p50_s": _quantile(buckets, count, 0.50),
        "p95_s": _quantile(buckets, count, 0.95),
    }


def _quantile(buckets: list[tuple[float, float]], count: float, q: float) -> float:
    if not buckets or not count:
        return 0.0
    target = q * count
    previous_bound, previous_count = 0.0, 0.0
    for bound, cumulative in buckets:
        if cumulative >= target:
            span = cumulative - previous_count
            if span <= 0:
                return bound
            ratio = (target - previous_count) / span
            return previous_bound + (bound - previous_bound) * ratio
        previous_bound, previous_count = bound, cumulative
    return buckets[-1][0]


# ---------------------------------------------------------------- 速率

_lock = threading.Lock()
_state: dict[str, float | None] = {
    "tokens": None,
    "secs": None,
    "wall": None,
    "wall_tokens": None,
    "ewma": None,
    "agg": None,
    "agg_stamp": None,
}
_MIN_WALL_GAP = 0.5   # 墙钟
_MIN_SECS_GAP = 0.1   # 引擎累计生成秒数
_EWMA_ALPHA = 0.6
_AGG_ALPHA = 0.45
_AGG_IDLE_S = 8.0     # 连续多久没有新 token 才算真的空闲（把聚合读数压到 0）


def engine_rate(
    tokens: float | None, secs: float | None, now: float | None = None
) -> dict[str, float | None]:
    """三个视角的生成速度。

    instant   引擎侧：Δ已生成 token / Δ引擎累计生成秒数 —— 单条 decode 的真实速度，
              等价于 llama-bench 的 tg；
    window    instant 的 EWMA，用来稳住"切 slot 期间 gauge 读 0"的抖动；
    aggregate 全服务聚合：Δ已生成 token / Δ墙钟 的 EWMA，并发下会高于 instant；
              换批间隙没有新 token 时保持上一次读数，空闲超过 _AGG_IDLE_S 才归零。
    """
    stamp = time.monotonic() if now is None else now
    out: dict[str, float | None] = {"instant": None, "window": None, "aggregate": None}
    with _lock:
        prev_tokens, prev_secs = _state["tokens"], _state["secs"]
        prev_wall, prev_wall_tokens = _state["wall"], _state["wall_tokens"]
        if (
            tokens is not None
            and secs is not None
            and prev_tokens is not None
            and prev_secs is not None
            and secs > prev_secs
            and tokens >= prev_tokens
            and secs - prev_secs >= _MIN_SECS_GAP
        ):
            instant = (tokens - prev_tokens) / (secs - prev_secs)
            if 0 <= instant < 10_000:
                out["instant"] = instant
                previous_ewma = _state["ewma"]
                _state["ewma"] = (
                    instant
                    if previous_ewma is None
                    else previous_ewma + _EWMA_ALPHA * (instant - previous_ewma)
                )
                _state["tokens"] = tokens
                _state["secs"] = secs
        elif prev_tokens is None or (secs is not None and tokens is not None):
            _state["tokens"] = tokens
            _state["secs"] = secs
        out["window"] = _state["ewma"]
        if tokens is not None:
            if prev_wall is None or prev_wall_tokens is None or tokens < prev_wall_tokens:
                # 第一次采样，或者引擎重启把计数器归零了：重建基线，别硬算出负数。
                _state["wall"] = stamp
                _state["wall_tokens"] = tokens
                _state["agg"] = None
                _state["agg_stamp"] = None
            elif stamp - prev_wall >= _MIN_WALL_GAP:
                if tokens > prev_wall_tokens:
                    raw = (tokens - prev_wall_tokens) / (stamp - prev_wall)
                    previous_agg = _state["agg"]
                    _state["agg"] = (
                        raw
                        if previous_agg is None
                        else previous_agg + _AGG_ALPHA * (raw - previous_agg)
                    )
                    _state["agg_stamp"] = stamp
                _state["wall"] = stamp
                _state["wall_tokens"] = tokens
            # 换批间隙常常 2 秒一个新 token 都没有，直接算 Δtoken/Δ墙钟 会跳到 0，
            # 看着像服务停了。所以聚合值做 EWMA，只有确实空闲超过 _AGG_IDLE_S 才归零。
            last_agg = _state["agg"]
            if last_agg is not None:
                idle_since = _state["agg_stamp"] or stamp
                out["aggregate"] = 0.0 if stamp - idle_since > _AGG_IDLE_S else last_agg
    return out


def reset_rate() -> None:
    with _lock:
        for key in _state:
            _state[key] = None


# ---------------------------------------------------------------- 快照

@dataclass(slots=True)
class Snapshot:
    ts: float = 0.0
    model: str = ""
    ctx_limit: int = 0
    service_url: str = ""
    engine: str = ""
    backend: str = "none"
    hardware_error: str | None = None
    gpus: list[dict] = field(default_factory=list)
    cpu: dict = field(default_factory=dict)
    mem: dict = field(default_factory=dict)
    engine_metrics: dict[str, float] = field(default_factory=dict)
    service_metrics: dict[str, float] = field(default_factory=dict)
    rate: dict[str, float | None] = field(default_factory=dict)
    hist: dict[str, dict[str, float]] = field(default_factory=dict)
    traces: list[dict] = field(default_factory=list)
    # 最近一批 trace 里最大的 (prompt + completion)，用来显示"当前最长的请求用了多少上下文"
    ctx_used: float = 0.0
    health: dict = field(default_factory=dict)
    model_info: dict = field(default_factory=dict)
    tokens: dict[str, int] = field(default_factory=dict)
    totals: dict[str, float] = field(default_factory=dict)
    slots: dict[str, float] = field(default_factory=dict)
    cache: dict[str, float] = field(default_factory=dict)
    service_req: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def gpu(self) -> dict:
        return self.gpus[0] if self.gpus else {}

    @property
    def tok_s(self) -> float:
        """主数字：优先 EWMA，没有就退回累计 token / 累计生成秒数。"""
        window = self.rate.get("window") or 0.0
        if window > 0:
            return window
        secs = self.totals.get("decode_seconds") or 0.0
        total = self.totals.get("tokens_predicted") or 0.0
        if secs > 0 and total > 0:
            return total / secs
        return 0.0


def _as_dict(obj, *names: str) -> dict:
    """把 probe 的 dataclass 摊平成 dict（字段名缺失就跳过，兼容将来的改名）。"""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return {k: v for k, v in obj.items() if k in names or not names}
    out: dict = {}
    for name in names or dir(obj):
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if callable(value) or (name.startswith("_") and name not in names):
            continue
        out[name] = value
    return out


GPU_FIELDS = (
    "index",
    "name",
    "driver",
    "total_mib",
    "used_mib",
    "free_mib",
    "util_pct",
    "mem_util_pct",
    "temp_c",
    "power_w",
    "power_limit_w",
    "pcie_gen",
    "pcie_width",
    "fan_pct",
    "clock_core_mhz",
    "clock_mem_mhz",
)
CPU_FIELDS = ("name", "cores", "usage_pct", "per_core_pct", "load1")
MEM_FIELDS = ("total_mib", "used_mib", "available_mib")


def load_model_info(gguf_path: str | Path | None) -> dict:
    """模型元数据只在启动时读一次（GGUF 头，毫秒级）：架构、层数、头数、MoE、文件大小。"""
    info: dict = {
        "path": str(gguf_path or ""),
        "arch": "",
        "n_layers": 0,
        "n_ctx_train": 0,
        "n_embd": 0,
        "n_head": 0,
        "n_head_kv": 0,
        "n_ff": 0,
        "file_size_bytes": 0,
        "moe": {
            "layers": 0,
            "experts_total": 0,
            "experts_active": 0,
            "experts_per_layer": 0,
            "used_per_layer": 0,
            "is_moe": False,
        },
    }
    if not gguf_path:
        return info
    try:
        meta = read_meta(str(gguf_path))
    except Exception:
        meta = None
    if meta is not None:
        for key in (
            "arch",
            "n_layers",
            "n_ctx_train",
            "n_embd",
            "n_head",
            "n_head_kv",
            "n_ff",
            "file_size_bytes",
        ):
            value = getattr(meta, key, None)
            if value is not None:
                info[key] = value
    try:
        info["moe"] = moe_summary(meta)
    except Exception:
        pass
    return info


def sample_snapshot(
    *,
    service_url: str,
    model: str,
    ctx_limit: int,
    engine: str,
    model_info: dict | None = None,
    trace_limit: int = 20,
) -> Snapshot:
    """采一次全部数据。永不抛异常；读不到的项留空并记进 errors。"""
    snap = Snapshot(
        ts=time.time(),
        model=model,
        ctx_limit=ctx_limit,
        service_url=service_url,
        engine=engine,
        model_info=model_info or {},
    )
    if probe is not None:
        try:
            hardware = probe.sample_hardware()
        except Exception as exc:  # 采集层自己的兜底之外再兜一次
            hardware = None
            snap.hardware_error = f"硬件采集失败：{exc}"
        if hardware is not None:
            snap.backend = getattr(hardware, "backend", "none") or "none"
            snap.hardware_error = snap.hardware_error or getattr(hardware, "error", None)
            gpus = getattr(hardware, "gpus", []) or []
            snap.gpus = [_as_dict(gpu, *GPU_FIELDS) for gpu in gpus]
            snap.cpu = _as_dict(getattr(hardware, "cpu", None), *CPU_FIELDS)
            snap.mem = _as_dict(getattr(hardware, "mem", None), *MEM_FIELDS)
    else:
        snap.hardware_error = "probe 模块未就绪（没有安装 tui 采集层）"

    # inproc 把引擎搬进了服务进程，所以"引擎指标"就是服务层指标：
    # 不再有第二个 /metrics 端点，也不再区分 llamacpp: 前缀。
    snap.service_metrics = parse_prom(fetch(service_url.rstrip("/") + "/metrics"))
    if not snap.service_metrics:
        snap.errors.append("服务层指标读不到（/metrics）")

    health = fetch_json(service_url.rstrip("/") + "/healthz")
    if health is None:
        snap.errors.append("服务层 /healthz 连不上")
    else:
        snap.health = health

    traces = fetch_json(service_url.rstrip("/") + f"/admin/traces?limit={trace_limit}")
    if isinstance(traces, dict) and isinstance(traces.get("data"), list):
        snap.traces = traces["data"]
    else:
        snap.errors.append("/admin/traces 读不到（面板服务层可能不是本轮代码）")

    for item in snap.traces:
        tokens = item.get("tokens") or {}
        used = float(tokens.get("prompt") or 0) + float(tokens.get("completion") or 0)
        snap.ctx_used = max(snap.ctx_used, used)

    for name, key in (("input", "input"), ("output", "output"), ("cached", "cached")):
        value = series(snap.service_metrics, "dshinfer_tokens_total", direction=key)
        snap.tokens[name] = int(value or 0)

    decode_seconds = snap.service_metrics.get("dshinfer_decode_seconds_total") or 0.0
    requests_done = series(snap.service_metrics, "dshinfer_requests_total") or 0.0
    snap.totals = {
        "tokens_predicted": float(snap.tokens["output"]),
        "prompt_tokens": float(snap.tokens["input"]),
        "prompt_cached": float(snap.tokens["cached"]),
        "n_decode": requests_done,
        "decode_seconds": decode_seconds,
        "n_tokens_max": 0.0,
        "spec_draft": 0.0,
        "spec_accepted": 0.0,
    }
    snap.slots = {
        "processing": snap.service_metrics.get("dshinfer_inflight_requests") or 0.0,
        "deferred": snap.service_metrics.get("dshinfer_queue_depth") or 0.0,
        "busy": 1.0 if (snap.service_metrics.get("dshinfer_inflight_requests") or 0.0) else 0.0,
        # inproc 一个 context 一次只跑一个 decode，"序列数"恒 1
        "parallel": 1.0,
    }
    prompt_total = snap.totals["prompt_tokens"]
    snap.cache = {
        "hit_tokens": snap.totals["prompt_cached"],
        "hit_ratio": (snap.totals["prompt_cached"] / prompt_total * 100.0)
        if prompt_total
        else 0.0,
    }
    snap.service_req = {
        "total": series(snap.service_metrics, "dshinfer_requests_total") or 0.0,
        "inflight": snap.service_metrics.get("dshinfer_inflight_requests") or 0.0,
        "queue_depth": snap.service_metrics.get("dshinfer_queue_depth") or 0.0,
        "dispatched": series(snap.service_metrics, "dshinfer_scheduler_events_total", event="dispatch")
        or 0.0,
        "enqueued": series(snap.service_metrics, "dshinfer_scheduler_events_total", event="enqueue")
        or 0.0,
    }
    for name, key in (
        ("ttft", "dshinfer_ttft_seconds"),
        ("request", "dshinfer_request_seconds"),
        ("queue", "dshinfer_queue_wait_seconds"),
    ):
        value = histogram(snap.service_metrics, key)
        if value:
            snap.hist[name] = value

    snap.rate = engine_rate(
        tokens=snap.totals.get("tokens_predicted"),
        secs=snap.totals.get("decode_seconds"),
        now=time.monotonic(),
    )
    return snap


# ---------------------------------------------------------------- 日志尾巴

def read_log(
    path: str | Path | None, offset: int = 0, max_bytes: int = 64 * 1024
) -> tuple[list[str], int]:
    """从偏移处读新增内容（只读尾巴，避免一次把整个日志读进内存）。"""
    if not path:
        return [], offset
    try:
        file_path = Path(path).expanduser()
        if not file_path.is_file():
            return [], offset
        size = file_path.stat().st_size
        if size < offset:  # 被轮转/截断过，从头开始
            offset = 0
        start = max(offset, size - max_bytes)
        with file_path.open("rb") as handle:
            handle.seek(start)
            chunk = handle.read()
        text = chunk.decode("utf-8", "replace")
        lines = text.splitlines()
        if start > offset and lines:
            lines = lines[1:]  # 起始位置可能落在半行上
        return lines, size
    except OSError:
        return [], offset
