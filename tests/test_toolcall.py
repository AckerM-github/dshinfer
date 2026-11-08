"""工具调用解析：模型的 XML 格式 → OpenAI tool_calls。"""

from __future__ import annotations

import json

from dshinfer.toolcall import parse_tool_calls, strip_tool_calls

SINGLE = """<tool_call>
<function=get_weather>
<parameter=city>
杭州
</parameter>
<parameter=unit>
c
</parameter>
</function>
</tool_call>"""


def test_single_call():
    text, calls = parse_tool_calls(SINGLE)
    assert text == ""
    assert len(calls) == 1
    assert calls[0]["type"] == "function"
    assert calls[0]["function"]["name"] == "get_weather"
    assert calls[0]["id"].startswith("call_")
    assert json.loads(calls[0]["function"]["arguments"]) == {"city": "杭州", "unit": "c"}


def test_text_before_call_is_kept():
    text, calls = parse_tool_calls("好的，我查一下。\n" + SINGLE)
    assert text == "好的，我查一下。"
    assert calls[0]["function"]["name"] == "get_weather"


def test_two_calls_in_one_turn():
    two = SINGLE + "\n" + SINGLE.replace("get_weather", "calculator").replace("杭州", "1+1")
    text, calls = parse_tool_calls(two)
    assert text == ""
    assert [c["function"]["name"] for c in calls] == ["get_weather", "calculator"]


def test_multiline_and_numeric_args():
    raw = """<tool_call>
<function=calculator>
<parameter=expression>
128 * 7
+ 96
</parameter>
<parameter=precision>
2
</parameter>
</function>
</tool_call>"""
    _, calls = parse_tool_calls(raw)
    args = json.loads(calls[0]["function"]["arguments"])
    assert args["precision"] == 2  # 数字被还原成 int，不是字符串
    assert args["expression"].startswith("128 * 7")


def test_json_style_fallback():
    raw = '<tool_call>{"name": "get_weather", "arguments": {"city": "北京"}}</tool_call>'
    _, calls = parse_tool_calls(raw)
    assert calls[0]["function"]["name"] == "get_weather"
    assert json.loads(calls[0]["function"]["arguments"])["city"] == "北京"


def test_no_call():
    text, calls = parse_tool_calls("就是一段普通话，没有任何工具调用。")
    assert calls == []
    assert text == "就是一段普通话，没有任何工具调用。"


def test_strip_helper():
    assert strip_tool_calls("前言" + SINGLE) == "前言"
