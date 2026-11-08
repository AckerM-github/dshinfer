"""模拟外接 agent 的多步对话，看 KV 缓存能不能跨步命中。

agent 的每一步都是「上一轮的完整对话 + 新工具结果」，所以 prompt 是严格变长的。
如果服务端能命中缓存，第 2 步之后每步只算增量；如果 slot 被换掉，就要整段重算。

两种工况：
  solo  —— 单会话连续 4 步（只有它在用服务）
  mixed —— 两个会话交替各 4 步（模拟两个 agent 并发）
"""

from __future__ import annotations

import asyncio
import json

import httpx

BASE = "http://127.0.0.1:8000"
MODEL = "deepseek-v4-pro-qwen3.5-9b"
SYSTEM = "你是一个严谨的助手。回答要短，不超过 30 字。" + ("背景资料。" * 40)


async def turn(client: httpx.AsyncClient, messages: list[dict], tag: str) -> tuple[str, dict]:
    r = await client.post(
        f"{BASE}/v1/chat/completions",
        json={"model": MODEL, "messages": messages, "max_tokens": 24, "temperature": 0},
        timeout=300,
    )
    r.raise_for_status()
    d = r.json()
    text = d["choices"][0]["message"]["content"] or ""
    usage = d["usage"]
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    print(
        f"  {tag:<10} prompt={usage['prompt_tokens']:>4}  cached={cached:>4}  "
        f"重算={usage['prompt_tokens'] - cached:>4}  completion={usage['completion_tokens']:>3}"
    )
    return text, usage


async def session(client: httpx.AsyncClient, name: str, questions: list[str]) -> None:
    messages = [{"role": "system", "content": SYSTEM}]
    for i, q in enumerate(questions, 1):
        messages.append({"role": "user", "content": q})
        text, _ = await turn(client, messages, f"{name}-step{i}")
        messages.append({"role": "assistant", "content": text})


async def main() -> None:
    qs = ["一加一等于几", "再加二呢", "再乘三呢", "最后减四呢"]
    async with httpx.AsyncClient() as client:
        print("== solo：单会话 4 步 ==")
        await session(client, "A", qs)

        print("\n== mixed：两个会话交替 4 步 ==")
        a = asyncio.create_task(session(client, "A", qs))
        await asyncio.sleep(0.05)
        b = asyncio.create_task(session(client, "B", qs))
        await asyncio.gather(a, b)

    st = (await client.get(f"{BASE}/admin/stats")).json()
    print("\n" + json.dumps(st.get("scheduler", {}), ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
