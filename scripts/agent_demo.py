"""最小 agent 循环：模型出工具调用 → 本地执行 → 结果回灌 → 模型出最终答案。

跑之前先起服务（会自带 llama-server）：
    $env:DSHINFER_ENGINE="llamacpp"; uv run uvicorn dshinfer.api:app --port 8000
然后：
    uv run python scripts/agent_demo.py
"""

from __future__ import annotations

import ast
import json
import time

import httpx

BASE = "http://127.0.0.1:8000"
MAX_STEPS = 6

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询城市当前天气（温度、天气、风力）",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "城市名，如 杭州"}},
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "四则运算计算器，支持 + - * / 和括号",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
        },
    },
]

WEATHER = {
    "杭州": {"temp_c": 26, "condition": "多云", "wind": "3级", "humidity": "68%"},
    "北京": {"temp_c": 34, "condition": "晴", "wind": "2级", "humidity": "31%"},
}

_OPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.USub: lambda a: -a,
}


def safe_eval(expr: str) -> float:
    """只允许数字和四则运算——别在生产里用 eval。"""
    def ev(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        raise ValueError(f"不支持的表达式片段: {ast.dump(node)}")

    return ev(ast.parse(expr, mode="eval").body)


def run_tool(name: str, args: dict) -> str:
    if name == "get_weather":
        city = args.get("city", "")
        data = WEATHER.get(city)
        if not data:
            return json.dumps({"error": f"没有 {city} 的天气数据"}, ensure_ascii=False)
        return json.dumps({"city": city, **data}, ensure_ascii=False)
    if name == "calculator":
        try:
            return json.dumps({"expression": args.get("expression"), "value": safe_eval(args["expression"])}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
    return json.dumps({"error": f"未知工具 {name}"}, ensure_ascii=False)


def chat(messages: list[dict]) -> dict:
    r = httpx.post(
        f"{BASE}/v1/chat/completions",
        json={"messages": messages, "tools": TOOLS, "temperature": 0, "max_tokens": 512},
        timeout=180,
    )
    r.raise_for_status()
    return r.json()


def main(question: str = "杭州和北京今天哪个更适合跑步？两地温差多少度？") -> None:
    messages: list[dict] = [{"role": "user", "content": question}]
    print(f"用户：{question}\n")

    t0 = time.perf_counter()
    for step in range(1, MAX_STEPS + 1):
        t = time.perf_counter()
        body = chat(messages)
        choice = body["choices"][0]
        msg = choice["message"]
        elapsed = (time.perf_counter() - t) * 1000

        if msg.get("content"):
            print(f"[第 {step} 步 · {elapsed:.0f} ms] 模型：{msg['content'].strip()}")

        calls = msg.get("tool_calls") or []
        if not calls:
            print(f"\n最终答案（{time.perf_counter() - t0:.1f}s，{step} 步，"
                  f"{body['usage']['total_tokens']} token）：\n{msg.get('content', '').strip()}")
            return

        # 把 assistant 这一轮（带 tool_calls）原样回灌，模型需要看到自己调了什么
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})

        for call in calls:
            fn = call["function"]["name"]
            args = json.loads(call["function"]["arguments"] or "{}")
            result = run_tool(fn, args)
            print(f"           ↳ 调用 {fn}({json.dumps(args, ensure_ascii=False)}) → {result}")
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

    print("超过最大步数，agent 没有收敛")


if __name__ == "__main__":
    import sys

    main(sys.argv[1] if len(sys.argv) > 1 else "杭州和北京今天哪个更适合跑步？两地温差多少度？")
