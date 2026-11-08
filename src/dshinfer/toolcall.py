"""把模型吐出来的工具调用文本，翻译成 OpenAI 的 `tool_calls` 结构。

模型真正认识的格式（来自 GGUF 里的 chat template）是 XML：

    <tool_call>
    <function=get_weather>
    <parameter=city>
    杭州
    </parameter>
    </function>
    </tool_call>

注意三个坑：
  1. 一轮可能吐**多个** `<tool_call>`，必须全部解析，不能只取第一个。
  2. 参数值是原样文本，可能多行、可能本身就是 JSON——不能一律 `json.loads`。
  3. 模型常在工具调用**前面**说一句人话（模板允许），必须把这句留在 content 里。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

__all__ = ["parse_tool_calls", "strip_tool_calls"]

_CALL_RE = re.compile(r"<tool_call>\s*<function=([^>\s]+)>(.*?)</function>\s*</tool_call>", re.S)
_PARAM_RE = re.compile(r"<parameter=([^>\s]+)>\s*(.*?)\s*</parameter>", re.S)
# 兜底：有些模型/模板会吐 JSON 形式
_JSON_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def _coerce(value: str) -> Any:
    """参数值尽量还原成 JSON 类型，还原不了就当字符串。"""
    v = value.strip()
    if v == "":
        return ""
    if v in ("true", "false"):
        return v == "true"
    if v == "null":
        return None
    if len(v) <= 64 and re.fullmatch(r"-?\d+(\.\d+)?", v):
        return float(v) if "." in v else int(v)
    if v[:1] in "[{" or v[:1] in "\"'":
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return v
    return v


def parse_tool_calls(text: str) -> tuple[str, list[dict[str, Any]]]:
    """返回 (正文, tool_calls)。正文 = 去掉所有工具块之后剩下的文字。"""
    calls: list[dict[str, Any]] = []
    spans: list[tuple[int, int]] = []

    for m in _CALL_RE.finditer(text):
        name, body = m.group(1), m.group(2)
        args = {k: _coerce(v) for k, v in _PARAM_RE.findall(body)}
        calls.append(_make_call(name, args))
        spans.append((m.start(), m.end()))

    for m in _JSON_CALL_RE.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        try:
            obj = json.loads(m.group(1))
        except ValueError:
            continue
        name = obj.get("name") or obj.get("function", {}).get("name")
        args = obj.get("arguments") or obj.get("parameters") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"input": args}
        if name:
            calls.append(_make_call(name, args))
            spans.append((m.start(), m.end()))

    if not spans:
        return text.strip(), []

    spans.sort()
    parts, cursor = [], 0
    for s, e in spans:
        parts.append(text[cursor:s])
        cursor = e
    parts.append(text[cursor:])
    return "".join(parts).strip(), calls


def strip_tool_calls(text: str) -> str:
    return parse_tool_calls(text)[0]


def _make_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "call_" + uuid.uuid4().hex[:24],
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }
