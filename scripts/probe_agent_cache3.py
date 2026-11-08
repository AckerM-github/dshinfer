"""外接 agent 的正确姿势：每个会话带 `X-Session-Id`，槽位就钉住不换，KV 缓存能接力。

对比 `probe_agent_cache2.py`（不带会话号，靠轮转）：那个是反面教材。
"""

from __future__ import annotations

import asyncio
import collections
import os

import httpx

BASE = "http://127.0.0.1:8000"
MODEL = "deepseek-v4-pro-qwen3.5-9b"
SYSTEM = "你是一个严谨的助手。回答要短，不超过 30 字。" + ("背景资料。" * 40)
SESSIONS = int(os.environ.get("SESSIONS", "6"))
STEPS = ["一加一等于几", "再加二呢", "再乘三呢"]


async def one_session(client: httpx.AsyncClient, idx: int, stats: dict) -> None:
    sid = f"agent-{idx}"
    messages = [{"role": "system", "content": SYSTEM}]
    for step, q in enumerate(STEPS, 1):
        messages.append({"role": "user", "content": q})
        r = await client.post(
            f"{BASE}/v1/chat/completions",
            headers={"X-Session-Id": sid, "X-Tenant": "agent"},
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

    print(f"{SESSIONS} 个会话（各带 X-Session-Id）并发，每会话 3 步：\n")
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
