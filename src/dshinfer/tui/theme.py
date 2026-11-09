"""监视器配色主题：三套，监视器里按 t 循环。

为什么用 Textual 的 Theme 而不是自己写 class 切换：
  · App.theme 变化时 Textual 会重算全部 CSS 变量并重绘，不用重建控件、不丢曲线历史；
  · 颜色只写在 app.tcss 里一次（全部 $变量），加一套主题只要在这里多一条记录；
  · 富文本（Rich markup）里没法写 $primary，所以 _MARKUP 给每套主题配一个 ANSI 色名
    （终端里 green / cyan / blue 本来就跟着配色方案走）。

三套：
  strata  黑底绿字、双层边框 —— 对标 Strata 的终端观感（默认）
  ocean   原来的暗蓝（深蓝底 + 青蓝强调）
  paper   浅色，白天/投屏用
"""

from __future__ import annotations

from textual.theme import Theme

STRATA = "strata"
OCEAN = "ocean"
PAPER = "paper"

#: t 键的循环顺序，第一项是默认主题
THEME_ORDER: tuple[str, ...] = (STRATA, OCEAN, PAPER)

#: 主题名 → 富文本里用的 ANSI 色名（标题、主数字的高亮色）
THEME_MARKUP: dict[str, str] = {
    STRATA: "green",
    OCEAN: "cyan",
    PAPER: "blue",
}

#: 主题名 → 给用户看的中文名
THEME_LABEL: dict[str, str] = {
    STRATA: "黑绿",
    OCEAN: "暗蓝",
    PAPER: "浅色",
}

THEMES: tuple[Theme, ...] = (
    Theme(
        name=STRATA,
        primary="#3ddc8a",
        secondary="#1f8f5f",
        accent="#6ef2b0",
        foreground="#c9dbd0",
        background="#050807",
        surface="#0a0f0c",
        panel="#0d1512",
        success="#3ddc8a",
        warning="#e5c07b",
        error="#ff6b6b",
        dark=True,
        variables={
            "card-border": "#1d3a2c",
            "card-bg": "#070c0a",
        },
    ),
    Theme(
        name=OCEAN,
        primary="#3fa7ff",
        secondary="#1f6feb",
        accent="#69d2ff",
        foreground="#cfd6de",
        background="#0b0e12",
        surface="#0d1117",
        panel="#11161c",
        success="#3ddc8a",
        warning="#e5c07b",
        error="#ff6b6b",
        dark=True,
        variables={
            "card-border": "#232b34",
            "card-bg": "#0f141a",
        },
    ),
    Theme(
        name=PAPER,
        primary="#1f6feb",
        secondary="#0b4bb3",
        accent="#0d7a5f",
        foreground="#20242a",
        background="#f7f6f2",
        surface="#ffffff",
        panel="#ecebe4",
        success="#0d7a5f",
        warning="#a86f00",
        error="#c0392b",
        dark=False,
        variables={
            "card-border": "#cfccc0",
            "card-bg": "#ffffff",
        },
    ),
)


#: 浅色主题下要把"亮点色"换成深色版，否则黄/绿字在白底上几乎看不见
_PAPER_TONES: dict[str, str] = {
    "green": "dark_green",
    "cyan": "blue",
    "yellow": "dark_goldenrod",
    "red": "dark_red",
    "white": "black",
}


def readable(theme_name: str, tone: str) -> str:
    """把语义色名换成当前主题下可读的 Rich 颜色名（浅色主题用深色变体）。"""
    if theme_name == PAPER:
        return _PAPER_TONES.get(tone, tone)
    return tone


def markup_color(theme_name: str) -> str:
    """当前主题对应的 ANSI 色名（用于 Rich markup）。"""
    return THEME_MARKUP.get(theme_name, "white")


def label(theme_name: str) -> str:
    return THEME_LABEL.get(theme_name, theme_name)


def next_theme(current: str) -> str:
    try:
        index = THEME_ORDER.index(current)
    except ValueError:
        return THEME_ORDER[0]
    return THEME_ORDER[(index + 1) % len(THEME_ORDER)]
