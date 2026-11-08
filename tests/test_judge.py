"""判定器的单测：判定规则写错会让整份评测失真，所以每条规则都要有正反例。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
from judge import Resp, extract_code, judge  # noqa: E402


def test_contains_all_and_any():
    assert judge(Resp(content="沸点是 100 摄氏度"), {"type": "contains_all", "values": ["100"]})[0]
    assert not judge(Resp(content="大概九十多"), {"type": "contains_all", "values": ["100"]})[0]
    assert judge(Resp(content="周六"), {"type": "contains_any", "values": ["星期六", "周六"]})[0]
    assert not judge(Resp(content="周日"), {"type": "contains_any", "values": ["周六"]})[0]


def test_exact_strips_whitespace():
    assert judge(Resp(content="  我知道了\n"), {"type": "exact", "value": "我知道了"})[0]
    assert not judge(Resp(content="我知道了。"), {"type": "exact", "value": "我知道了"})[0]


def test_regex_tolerates_trailing_newline():
    check = {"type": "regex", "value": "^(?:(?![0-9]).){1,60}$"}
    assert judge(Resp(content="四季\n"), check)[0]
    assert not judge(Resp(content="一年有4个季节"), check)[0]


def test_json_strips_code_fence():
    ok, _ = judge(Resp(content='```json\n{"name": "张三", "age": 30}\n```'), {"type": "json", "keys": ["name", "age"]})
    assert ok
    bad, detail = judge(Resp(content='{"name": "张三"}'), {"type": "json", "keys": ["name", "age"]})
    assert not bad and "age" in detail
    assert not judge(Resp(content="这不是 JSON"), {"type": "json", "keys": []})[0]


def test_tool_call_and_no_tool_call():
    tc = [{"function": {"name": "get_weather", "arguments": '{"city": "杭州"}'}}]
    ok, _ = judge(Resp(tool_calls=tc), {"type": "tool_call", "name": "get_weather", "args_contain": {"city": "杭州"}})
    assert ok
    assert not judge(Resp(tool_calls=tc), {"type": "tool_call", "name": "calculator"})[0]
    assert not judge(Resp(content="杭州"), {"type": "tool_call", "name": "get_weather"})[0]

    assert judge(Resp(content="中国的首都是北京。"), {"type": "no_tool_call", "contains": "北京"})[0]
    assert not judge(Resp(tool_calls=tc, content="北京"), {"type": "no_tool_call"})[0]


def test_length_between():
    assert judge(Resp(content="时间是一条河"), {"type": "length_between", "min": 4, "max": 10})[0]
    assert not judge(Resp(content="短"), {"type": "length_between", "min": 4, "max": 10})[0]


def test_extract_code_from_fence_and_bare():
    assert "def f" in extract_code("说明\n```python\ndef f():\n    return 1\n```\n后记")
    assert extract_code("def f():\n    return 1").startswith("def f")


@pytest.mark.parametrize(
    "code,ok",
    [
        ("def solve(nums):\n    return [x*x for x in nums if x % 2 == 0]", True),
        ("def solve(nums):\n    return nums", False),
    ],
)
def test_python_runs_executes_real_code(code, ok):
    check = {
        "type": "python_runs",
        "test": 'assert solve([1,2,3,4]) == [4,16]\nprint("OK")',
    }
    got, detail = judge(Resp(content=f"```python\n{code}\n```"), check)
    assert got is ok, detail


def test_python_runs_catches_syntax_error():
    got, detail = judge(
        Resp(content='```python\ndef f(:\n    pass\n```'),
        {"type": "python_runs", "test": "print('OK')"},
    )
    assert not got and "执行失败" in detail


def test_unknown_check_type_is_failure_not_exception():
    ok, detail = judge(Resp(content="x"), {"type": "does_not_exist"})
    assert not ok and "未知判定类型" in detail
