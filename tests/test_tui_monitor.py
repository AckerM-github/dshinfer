"""monitor.py 纯函数（阶段条、请求表指标、宽度计算）与 widgets.spark_line 的单测。

都不启动 Textual App，只喂普通 dict / 假快照。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dshinfer.tui.monitor import (
    _token_counts,
    phase_index,
    plain_width,
    trace_cache_pct,
    trace_tok_s,
)
from dshinfer.tui.widgets import spark_line


def _snap(*, health=None, slots=None, service_req=None):
    return SimpleNamespace(
        health=health if health is not None else {"engine": {"ready": True}},
        slots=slots or {},
        service_req=service_req or {},
    )


def test_plain_width_counts_cjk_as_two_columns() -> None:
    assert plain_width("abc") == 3
    assert plain_width("模型") == 4


def test_plain_width_strips_markup() -> None:
    # 闭合标签 "[ / ]" 里没有字母，正则必须用 * 才能剥掉，否则宽度虚高
    assert plain_width("[bold]tok/s[/]") == 5
    assert plain_width("[dim]模型[/] 上下文") == 4 + 1 + 6


def test_phase_idle() -> None:
    assert phase_index(_snap()) == 0


def test_phase_generating() -> None:
    assert phase_index(_snap(slots={"processing": 2})) == 2
    assert phase_index(_snap(service_req={"inflight": 1})) == 2


def test_phase_queued() -> None:
    assert phase_index(_snap(service_req={"queue_depth": 3})) == 3


def test_phase_loading() -> None:
    assert phase_index(_snap(health={"engine": {"loading": True, "ready": False}})) == 1


def test_phase_error_wins() -> None:
    snap = _snap(
        health={"engine": {"last_error": "CUDA OOM", "ready": False}},
        slots={"processing": 4},
    )
    assert phase_index(snap) == 4


def test_token_counts_accepts_both_names() -> None:
    assert _token_counts({"tokens": {"prompt": 10, "completion": 5, "cached": 2}}) == (10, 5, 2)
    assert _token_counts({"tokens": {"input": 7, "output": 3}}) == (7, 3, 0)
    assert _token_counts({}) == (0, 0, 0)


def test_trace_tok_s_uses_decode_span() -> None:
    item = {"tokens": {"prompt": 100, "completion": 50}, "spans_ms": {"decode": 1000}}
    assert trace_tok_s(item) == pytest.approx(50.0)


def test_trace_tok_s_falls_back_to_total_minus_ttft() -> None:
    item = {
        "tokens": {"completion": 40},
        "spans_ms": {"ttft": 500, "total": 2500},
    }
    assert trace_tok_s(item) == pytest.approx(20.0)


def test_trace_tok_s_without_output_is_none() -> None:
    assert trace_tok_s({"tokens": {"prompt": 9}, "spans_ms": {"decode": 100}}) is None


def test_trace_cache_pct() -> None:
    assert trace_cache_pct({"tokens": {"prompt": 100, "cached": 100}}) == pytest.approx(50.0)
    assert trace_cache_pct({"tokens": {"prompt": 0, "cached": 0}}) is None


def test_spark_line_length_and_right_alignment() -> None:
    line = spark_line([1.0, 2.0], 6)
    assert len(line) == 6
    assert line.startswith("    ")          # 旧的少，右边留给新数据
    assert line[4] == "▄"               # 1 是窗口最大值 2 的一半 → 半格
    assert line[5] == "█"               # 满格


def test_spark_line_flat_series_is_flat() -> None:
    line = spark_line([5.0] * 8, 8)
    assert set(line) == {"█"}


def test_spark_line_empty_and_zero() -> None:
    assert "·" in spark_line([], 4)
    assert spark_line([0.0, 0.0], 2) == "  "


def test_spark_line_respects_top() -> None:
    assert spark_line([50.0], 1, top=100.0) == "▄"
