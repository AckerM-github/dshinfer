"""TUI 的显示零件与格式化工具。

设计约束：
  · 只用 Textual 自带的 Static / DataTable 等，曲线用块字符自己画，不引第三方渲染库；
  · 颜色语义集中在 tone_for()，别在视图里散落 ["red"] 之类的魔法字面量；
  · 所有格式化函数必须容忍 None（面板最常见的情况就是"这一项读不到"）。
"""

from __future__ import annotations

from textual.widgets import DataTable, Static

from .theme import readable

# ---------------------------------------------------------------- 格式化

DASH = "—"


def fmt_rate(value: float | None, unit: str = "") -> str:
    if value is None:
        return DASH
    return f"{value:,.1f}{unit}"


def fmt_int(value: float | int | None) -> str:
    if value is None:
        return DASH
    return f"{int(value):,}"


def fmt_mib(mib: float | None) -> str:
    """显存/内存统一用 MiB 表达；超过 1024 MiB 再补一个 GiB 读数，方便和 nvidia-smi 对。"""
    if mib is None:
        return DASH
    if mib >= 1024:
        return f"{mib:,.0f} MiB ({mib / 1024:,.2f} GiB)"
    return f"{mib:,.0f} MiB"


def fmt_pct(value: float | None, digits: int = 0) -> str:
    if value is None:
        return DASH
    return f"{value:.{digits}f}%"


def fmt_ms(value: float | None) -> str:
    if value is None:
        return DASH
    if value >= 1000:
        return f"{value / 1000:,.2f} s"
    return f"{value:,.0f} ms"


def fmt_seconds(value: float | None) -> str:
    if value is None:
        return DASH
    if value < 60:
        return f"{value:,.1f} s"
    minutes, seconds = divmod(int(value), 60)
    if minutes < 60:
        return f"{minutes} m {seconds:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} m"


def fmt_bytes(size: int | None) -> str:
    if size is None:
        return DASH
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:,.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:,.1f} TiB"


def short_id(text: str, keep: int = 10) -> str:
    return text if len(text) <= keep else text[: keep - 1] + "…"


# ---------------------------------------------------------------- 色阶

#: 当前主题名（MonitorApp 切主题时更新）：浅色主题要把黄/绿换成深色版才看得清。
_ACTIVE_THEME = "strata"


def set_active_theme(name: str) -> None:
    global _ACTIVE_THEME
    _ACTIVE_THEME = name or "strata"


def theme_tone(tone: str) -> str:
    """按当前主题修正一个语义色名。"""
    return readable(_ACTIVE_THEME, tone)


def tone_for(pct: float | None, warn: float = 75.0, danger: float = 90.0) -> str:
    if pct is None:
        return "dim"
    if pct >= danger:
        return theme_tone("red")
    if pct >= warn:
        return theme_tone("yellow")
    return theme_tone("green")


def tone_of_latency(ms: float | None) -> str:
    """延迟：TTFT 500 ms 以内绿、2 s 以内黄、再往上红（本机实测基线）。"""
    if ms is None:
        return "dim"
    if ms >= 2000:
        return "red"
    if ms >= 500:
        return theme_tone("yellow")
    return theme_tone("green")


# ---------------------------------------------------------------- 条形

_FULL = "█"
_PARTIALS = " ▏▎▍▌▋▊▉"
_EMPTY = "░"


def bar(pct: float | None, width: int = 16, tone: str | None = None) -> str:
    """八分之一分辨率的横向条：整数个实心块 + 一个分数块 + 余下的空槽。"""
    if pct is None:
        return f"[dim]{_EMPTY * width}[/]"
    pct = max(0.0, min(100.0, float(pct)))
    eighths = round(pct / 100.0 * width * 8)
    full, part = divmod(eighths, 8)
    body = _FULL * full
    if part:
        body += _PARTIALS[part]
    body += _EMPTY * max(0, width - full - (1 if part else 0))
    colour = tone or tone_for(pct)
    return f"[{colour}]{body}[/]"


def meter(
    label: str,
    value: float | None,
    total: float | None,
    width: int = 14,
    unit: str = "mib",
) -> str:
    """一行「标签 + 条 + 百分比 + 读数」。value/total 任一缺失就整体降级成一行文本。

    unit 决定读数怎么格式化：mib（内存/显存）或 tokens（上下文占用）。
    """
    if value is None or not total:
        return f"[dim]{label}[/]  {DASH}"
    pct = value / total * 100.0
    if unit == "tokens":
        reading = f"{fmt_int(value)} / {fmt_int(total)}"
    else:
        reading = f"{fmt_mib(value)} / {fmt_mib(total)}"
    return (
        f"[dim]{label}[/]  {bar(pct, width)}  "
        f"[{tone_for(pct)}]{pct:5.1f}%[/]  [dim]{reading}[/]"
    )


# ---------------------------------------------------------------- 零件


class Kpi(Static):
    """一张指标卡：标题在外框，主数字 + 单位 + 副标题在框内。"""

    def __init__(self, title: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = title

    def set(
        self,
        main: str,
        unit: str = "",
        sub: str = "",
        tone: str = "cyan",
    ) -> None:
        unit_part = f" [dim]{unit}[/]" if unit else ""
        sub_part = f"\n[dim]{sub}[/]" if sub else ""
        self.update(f"[bold {theme_tone(tone)}]{main}[/]{unit_part}{sub_part}")


_SPARK_BLOCKS = " ▁▂▃▄▅▆▇█"


def spark_line(values: list[float], width: int, top: float | None = None) -> str:
    """一行块状 sparkline：以窗口最大值为满格，新数据靠右（对标 Strata 的曲线）。"""
    data = [float(v) for v in values][-max(1, width):]
    if not data:
        return "[dim]" + "·" * max(1, width) + "[/]"
    ceiling = top if top and top > 0 else max(data)
    if ceiling <= 0:
        ceiling = 1.0
    chars = []
    for value in data:
        ratio = max(0.0, min(1.0, value / ceiling))
        index = int(round(ratio * (len(_SPARK_BLOCKS) - 1)))
        chars.append(_SPARK_BLOCKS[index])
    return "".join(chars).rjust(max(1, width))


class Spark(Static):
    """带标题的曲线卡：一行块状 sparkline + 一行摘要，宽度自适应。"""

    def __init__(self, title: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = title

    def set_data(
        self,
        values: list[float],
        summary: str = "",
        tone: str = "cyan",
        top: float | None = None,
    ) -> None:
        width = max(8, (self.size.width or 40) - 2)
        body = spark_line(list(values)[-width:], width, top)
        text = f"[dim]{summary}[/]\n" if summary else ""
        self.update(f"{text}[{tone}]{body}[/]")


def ensure_columns(table: DataTable, columns: list[str]) -> None:
    """只在列数/列名变了时重建列，避免每 2 秒清一次列导致的闪烁。"""
    current = [str(col.label) for col in table.columns.values()]
    if current == columns:
        return
    table.clear(columns=True)
    for name in columns:
        table.add_column(name, key=name)


def fill_table(table: DataTable, columns: list[str], rows: list[tuple]) -> None:
    ensure_columns(table, columns)
    table.clear(columns=False)
    for row in rows:
        table.add_row(*row)


def kv_rows(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """把 (名称, 值) 列表补成两列表格的行（缺值统一显示破折号）。"""
    return [(name, value if value else DASH) for name, value in pairs]
