"""探针：这个 9B 量化模型到底会不会按模板发工具调用？

直接打 :8124/completion（绕开我们的服务层），只验证模型能力：
  1. 能不能按 <tool_call><function=...><parameter=...> 格式吐出来
  2. 参数能不能解析成 JSON
  3. 拿到工具结果后能不能继续（多步）
  4. 不需要工具时会不会瞎调
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import jinja2

TEMPLATE = Path(r"D:\llm\runtime\chat_template.jinja").read_text(encoding="utf-8")
URL = "http://127.0.0.1:8124/completion"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的当前天气",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名，例如 杭州"},
                    "unit": {"type": "string", "enum": ["c", "f"], "description": "温度单位"},
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "四则运算计算器",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
        },
    },
]

CALL_RE = re.compile(r"<tool_call>\s*<function=([^>\s]+)>(.*?)</function>\s*</tool_call>", re.S)
PARAM_RE = re.compile(r"<parameter=([^>\s]+)>\s*(.*?)\s*</parameter>", re.S)


def render(messages, tools=None) -> str:
    env = jinja2.Environment(trim_blocks=True, lstrip_blocks=True)
    tpl = env.from_string(TEMPLATE)
    return tpl.render(
        messages=messages,
        tools=tools,
        add_generation_prompt=True,
        enable_thinking=False,
        bos_token="",
        eos_token="<|im_end|>",
    )


def parse(text: str):
    m = CALL_RE.search(text)
    if not m:
        return None
    name, body = m.group(1), m.group(2)
    args = {k: v.strip() for k, v in PARAM_RE.findall(body)}
    return {"name": name, "arguments": args}


def ask(prompt: str, tools, stop=None) -> str:
    payload = {
        "prompt": prompt,
        "temperature": 0,
        "n_predict": 220,
        "stop": stop or ["<|im_end|>", "<|im_start|>"],
        "cache_prompt": True,
    }
    with httpx.Client(timeout=120) as c:
        r = c.post(URL, json=payload)
        r.raise_for_status()
        return r.json()["content"]


def main() -> None:
    cases = [
        ("单工具", "杭州今天天气怎么样？", TOOLS),
        ("要算数", "帮我算一下 128 * 7 + 96 等于多少", TOOLS),
        ("不需要工具", "用一句话解释什么是 KV cache。", TOOLS),
        ("双工具歧义", "北京现在几度？顺便算一下 45/9", TOOLS),
        ("无工具", "杭州今天天气怎么样？", None),
    ]

    print("=" * 72)
    for label, text, tools in cases:
        msgs = [{"role": "user", "content": text}]
        out = ask(render(msgs, tools), tools)
        parsed = parse(out)
        print(f"\n【{label}】{text}")
        print(f"  原始输出: {out.strip()[:300]!r}")
        print(f"  解析结果: {parsed}")
        if label == "不需要工具" and parsed:
            print("  !! 不该调用却调用了")

    print("\n" + "=" * 72)
    print("多步：先查天气，再把工具结果喂回去")
    msgs = [{"role": "user", "content": "杭州今天天气怎么样？适合跑步吗？"}]
    first = ask(render(msgs, TOOLS), TOOLS)
    print(f"  第 1 轮: {first.strip()[:200]!r}")
    call = parse(first)
    if not call:
        print("  没发出工具调用，后续无法进行")
        return
    result = '{"city": "杭州", "temp_c": 26, "condition": "多云", "wind": "3级"}'
    msgs = msgs + [
        {"role": "assistant", "content": first},
        {"role": "tool", "content": f"<tool_response>\n{result}\n</tool_response>"},
    ]
    second = ask(render(msgs, TOOLS), TOOLS)
    print(f"  第 2 轮: {second.strip()[:300]!r}")
    print(f"  又调工具了？ {parse(second)}")

    print("\n" + "=" * 72)
    print("稳定性：同一问题跑 6 次，看格式合规率")
    ok = 0
    for i in range(6):
        out = ask(render([{"role": "user", "content": "杭州今天天气怎么样？"}], TOOLS), TOOLS)
        p = parse(out)
        ok += bool(p)
        print(f"  #{i+1} {'✓' if p else '✗'} {p if p else out.strip()[:80]!r}")
    print(f"  合规 {ok}/6")


if __name__ == "__main__":
    main()
