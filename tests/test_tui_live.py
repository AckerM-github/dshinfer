"""live.py 里三个速率口径的单元测试（不依赖 Textual，也不碰真引擎）。"""

from __future__ import annotations

import pytest

from dshinfer.tui import live


@pytest.fixture(autouse=True)
def _clean_rate() -> None:
    live.reset_rate()
    yield
    live.reset_rate()


def test_first_sample_is_empty() -> None:
    assert live.engine_rate(100.0, 10.0, now=0.0) == {
        "instant": None,
        "window": None,
        "aggregate": None,
    }


def test_steady_stream() -> None:
    live.engine_rate(100.0, 10.0, now=100.0)
    rate = live.engine_rate(180.0, 12.0, now=102.0)
    assert rate["instant"] == pytest.approx(40.0)      # 80 token / 2 引擎秒
    assert rate["window"] == pytest.approx(40.0)
    assert rate["aggregate"] == pytest.approx(40.0)    # 80 token / 2 墙钟秒


def test_aggregate_holds_across_batch_boundary() -> None:
    """换批间隙 2 秒没有新 token：聚合值不能塌成 0，单条口径应当报 None。"""
    live.engine_rate(100.0, 10.0, now=100.0)
    live.engine_rate(180.0, 12.0, now=102.0)
    idle = live.engine_rate(180.0, 12.0, now=104.0)
    assert idle["aggregate"] == pytest.approx(40.0)
    assert idle["instant"] is None
    assert idle["window"] == pytest.approx(40.0)


def test_aggregate_decays_to_zero_when_really_idle() -> None:
    live.engine_rate(100.0, 10.0, now=100.0)
    live.engine_rate(180.0, 12.0, now=102.0)
    live.engine_rate(180.0, 12.0, now=104.0)          # 仍然只有 2 秒空闲
    dead = live.engine_rate(180.0, 12.0, now=120.0)   # 空闲超过 8 秒
    assert dead["aggregate"] == 0.0


def test_short_intervals_are_ignored() -> None:
    """面板可能被连点刷新：0.2 秒内的两次采样不能算出荒唐的速率。"""
    live.engine_rate(100.0, 10.0, now=100.0)
    live.engine_rate(180.0, 12.0, now=102.0)
    spammed = live.engine_rate(200.0, 12.2, now=102.2)
    assert spammed["aggregate"] == pytest.approx(40.0)
    assert spammed["instant"] == pytest.approx(100.0)


def test_counter_reset_drops_rate() -> None:
    """引擎重启后计数器归零：不能算出负数，也不能沿用旧基线报正数。"""
    live.engine_rate(900.0, 30.0, now=100.0)
    reset = live.engine_rate(0.0, 0.0, now=102.0)
    assert reset["aggregate"] is None
    assert reset["window"] is None
    again = live.engine_rate(60.0, 1.5, now=104.0)
    assert again["instant"] == pytest.approx(40.0)
    assert again["aggregate"] == pytest.approx(30.0)
