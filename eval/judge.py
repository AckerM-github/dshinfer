"""判定器：把一条用例的检查规则落到模型返回上。

设计原则：
1. **规则优先于模型**——能用字符串/正则/JSON 判的，绝不再调一次模型（省时间、可复现）。
2. 判定函数只吃 `Resp`，不碰 HTTP，方便单测。
3. 判定失败时返回可读原因，报告里能直接看出是哪条规则没过。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Resp:
    """一次请求里判定需要的全部内容。"""

    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None


def _strip_fences(text: str) -> str:
    """去掉 ```json ... ``` 包裹，模型经常不听话。"""
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)
    return m.group(1) if m else t


def judge(resp: Resp, check: dict[str, Any]) -> tuple[bool, str]:
    """返回 (是否通过, 说明)。"""
    kind = check.get("type")
    text = resp.content or ""

    if kind == "contains_all":
        missing = [v for v in check["values"] if v not in text]
        return (not missing, f"缺少 {missing}" if missing else "全部命中")

    if kind == "contains_any":
        hit = [v for v in check["values"] if v in text]
        return (bool(hit), f"命中 {hit}" if hit else f"未命中 {check['values']}")

    if kind == "exact":
        got = text.strip()
        want = check["value"]
        return (got == want, f"期望 {want!r} 实得 {got!r}")

    if kind == "regex":
        # 同时试原文与 strip 后的版本：模型常带首尾空白/空行
        ok = re.search(check["value"], text) is not None or re.search(check["value"], text.strip()) is not None
        return (ok, f"正则 {'匹配' if ok else '不匹配'} {check['value']}")

    if kind == "json":
        body = _strip_fences(text)
        try:
            obj = json.loads(body)
        except json.JSONDecodeError as e:
            return (False, f"不是合法 JSON: {e}")
        if not isinstance(obj, dict):
            return (False, f"顶层不是对象，而是 {type(obj).__name__}")
        missing = [k for k in check.get("keys", []) if k not in obj]
        return (not missing, f"缺字段 {missing}" if missing else "字段齐全")

    if kind == "tool_call":
        if not resp.tool_calls:
            return (False, f"没有 tool_calls（正文 {text[:60]!r}）")
        tc = resp.tool_calls[0]
        fn = tc.get("function", {})
        name = fn.get("name")
        if name != check["name"]:
            return (False, f"调用了 {name}，期望 {check['name']}")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            return (False, f"参数不是 JSON: {fn.get('arguments')!r}")
        for k, v in (check.get("args_contain") or {}).items():
            if str(v) not in str(args.get(k, "")):
                return (False, f"参数 {k}={args.get(k)!r} 不含 {v!r}")
        return (True, f"{name}({json.dumps(args, ensure_ascii=False)})")

    if kind == "no_tool_call":
        if resp.tool_calls:
            return (False, f"不该调用工具，却调用了 {resp.tool_calls[0].get('function', {}).get('name')}")
        want = check.get("contains")
        if want and want not in text:
            return (False, f"正文不含 {want!r}")
        return (True, "未调工具且答案正确")

    if kind == "length_between":
        n = len(text.strip())
        lo, hi = check["min"], check["max"]
        return (lo <= n <= hi, f"长度 {n}，要求 {lo}–{hi}")

    if kind == "python_runs":
        return _judge_python(text, check)

    return (False, f"未知判定类型 {kind}")


def extract_code(text: str) -> str:
    """从回答里抠出第一段 Python 代码；没围栏就整段当代码。"""
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    if m:
        return m.group(1)
    # 没有围栏时，从第一个 def/import/class 开始截到结尾
    m = re.search(r"(?m)^(?:def |import |from |class )", text)
    return text[m.start():] if m else text


def _judge_python(text: str, check: dict[str, Any]) -> tuple[bool, str]:
    """真的把代码跑起来：代码 + 隐藏断言拼成一个文件，看输出是否包含期望串。

    这是唯一能证明"代码对不对"的判定方式，字符串匹配只能证明"写了 def"。
    """
    code = extract_code(text)
    if not code.strip():
        return (False, "回答里没有代码")
    script = code + "\n\n" + check["test"]
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "candidate.py"
        path.write_text(script, encoding="utf-8")
        try:
            p = subprocess.run(
                [sys.executable, "-I", str(path)],
                capture_output=True,
                text=True,
                timeout=check.get("timeout_s", 15),
                cwd=td,
            )
        except subprocess.TimeoutExpired:
            return (False, "代码执行超时")
    out = (p.stdout or "") + (p.stderr or "")
    want = check.get("expect", "OK")
    if p.returncode != 0:
        tail = (p.stderr or "").strip().splitlines()[-1:] or ["无输出"]
        return (False, f"执行失败: {tail[0][:120]}")
    ok = want in out
    return (ok, f"输出 {'含' if ok else '不含'} {want!r}: {out.strip()[:80]!r}")
