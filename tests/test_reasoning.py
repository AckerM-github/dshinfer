"""思考段剥离：`<think>…</think>` 里的推理不能混进正文。"""

from __future__ import annotations

import pytest

from dshinfer.service import ThinkSplitter


def test_disabled_passes_through() -> None:
    sp = ThinkSplitter(enabled=False)
    assert sp.feed("你好</think>世界") == [("content", "你好</think>世界")]


def test_split_in_one_chunk() -> None:
    sp = ThinkSplitter(enabled=True)
    out = sp.feed("先算 10+2=12</think>\n\n答案是 15")
    kinds = [k for k, _ in out]
    assert kinds == ["reasoning", "content"]
    assert out[0][1] == "先算 10+2=12"
    assert out[1][1] == "答案是 15"


def test_marker_split_across_chunks() -> None:
    sp = ThinkSplitter(enabled=True)
    got: list[tuple[str, str]] = []
    for piece in ["推理第一段", "</th", "ink>", "正文"]:
        got += sp.feed(piece)
    got += sp.flush()
    reasoning = "".join(t for k, t in got if k == "reasoning")
    content = "".join(t for k, t in got if k == "content")
    assert reasoning == "推理第一段"
    assert content == "正文"


def test_holdback_does_not_lose_text() -> None:
    """没闭合时不能丢字符：拼接结果必须等于原文减掉标记。"""
    sp = ThinkSplitter(enabled=True)
    pieces = ["abc", "def", "ghi"]
    got: list[tuple[str, str]] = []
    for p in pieces:
        got += sp.feed(p)
    got += sp.flush()
    assert "".join(t for _, t in got) == "abcdefghi"


def test_no_marker_flush_is_reasoning() -> None:
    sp = ThinkSplitter(enabled=True)
    got = sp.feed("只有思考没有结论") + sp.flush()
    assert all(k == "reasoning" for k, _ in got)
    assert "".join(t for _, t in got) == "只有思考没有结论"


def test_tool_mode_text_survives() -> None:
    """开思考时，正文里不能出现 `<think>` 或 `</think>`，但工具块要留下。"""
    sp = ThinkSplitter(enabled=True)
    text = "思考中</think>我查一下<tool_call><function=calc></function></tool_call>"
    parts = sp.feed(text) + sp.flush()
    body = "".join(t for k, t in parts if k == "content")
    assert "</think>" not in body
    assert "<tool_call>" in body
    assert "我查一下" in body


def test_disabled_keeps_text_verbatim() -> None:
    """关思考时原样透传：模型本不该吐标记，吐了也不猜它。"""
    sp = ThinkSplitter(enabled=False)
    parts = sp.feed("abc") + sp.flush()
    assert parts == [("content", "abc")]
