"""外接 agent 的并发压力：6 个会话各 3 步，看 4 个槽位够不够、缓存会不会互相挤掉。

结论看两列：
  cached  —— 这一步复用了多少 KV（理想值 ≈ prompt - 新增 token）
  重算    —— 实际要过的 token 数，它决定 TTFT
"""

from __future__ import annotations

import asyncio
import collections

import httpx

BASE = "http://127.0.0.1:8000"
MODEL = "deepseek-v4-pro-qwen3.5-9b"
SYSTEM = "你是一个严谨的助手。回答要短，不超过 30 字。" + ("背景资料。" * 40)
SESSIONS = 6
STEPS = ["一加一等于几", "再加二呢", "再乘三呢"]


async def one_session(client: httpx.AsyncClient, idx: int, stats: dict) -> None:
    messages = [{"role": "system", "content": SYSTEM}]
    for step, q in enumerate(STEPS, 1):
        messages.append({"role": "user", "content": q})
        r = await client.post(
            f"{BASE}/v1/chat/completions",
            json={"model": MODEL, "messages": messages, "max_tokens": 16, "temperature": 0},
            timeout=300,
        )
        r.raise_for_status()
        d = r.json()
        u = d["usage"]
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        stats[f"step{step}"].append((u["prompt_tokens"], cached))
        messages.append({"role": "assistant", "content": d["choices"][0]["message"]["content"] or ""})


async def main() -> None:
    stats: dict[str, list] = collections.defaultdict(list)
    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(one_session(client, i, stats) for i in range(SESSIONS)))
        sched = (await client.get(f"{BASE}/admin/stats")).json().get("scheduler", {})

    print(f"{SESSIONS} 个会话并发，每会话 3 步：\n")
    for step, rows in sorted(stats.items()):
        prompts = sum(p for p, _ in rows)
        cached = sum(c for _, c in rows)
        hits = sum(1 for _, c in rows if c > 0)
        print(
            f"  {step}: {hits}/{len(rows)} 条命中   "
            f"cached {cached}/{prompts} = {cached / prompts:.0%}   "
            f"每条重算 {[(p - c) for p, c in rows]}"
        )
    print(f"\n  调度器：{sched}")


if __name__ == "__main__":
    asyncio.run(main())
