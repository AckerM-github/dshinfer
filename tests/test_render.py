"""模板渲染回归：带历史时不能退回裸 ChatML。

背景：`chat_template.jinja` 里写的是 `{% if message.tool_calls and
message.tool_calls is iterable %}`。如果给模板喂的是 `dict`（缺 tool_calls 键），
jinja 的 `is iterable` 会对 Undefined 调 iter() 抛 UndefinedError，整段渲染失败，
`render_prompt` 退回最朴素的 ChatML —— 那个版本没有 `<think></think>` 护栏，
模型就会自己开一个思考块，把真正的回答挤掉（实测 96 个 token 全是思考）。
"""

from __future__ import annotations

import os

import pytest

from dshinfer.config import get_settings
from dshinfer.schemas import ChatMessage
from dshinfer.service import render_prompt

TPL = get_settings().chat_template_path

pytestmark = pytest.mark.skipif(not os.path.exists(TPL), reason="chat template 不在本机")


def _history() -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content="你是助手"),
        ChatMessage(role="user", content="q1"),
        ChatMessage(role="assistant", content="a1"),
        ChatMessage(role="user", content="q2"),
    ]


def test_think_guard_present_with_history():
    prompt = render_prompt(_history(), template_path=TPL, enable_thinking=False)
    assert prompt.endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n")


def test_thinking_enabled_opens_block():
    prompt = render_prompt(_history(), template_path=TPL, enable_thinking=True)
    assert prompt.endswith("<|im_start|>assistant\n<think>\n")
