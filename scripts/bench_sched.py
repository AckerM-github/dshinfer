"""调度策略实测：优先级到底救不救得了交互式请求？

场景（模拟线上最常见的混合负载）：
  · 8 个「离线批量」任务先把 4 个槽位占满（每个生成 ~120 token）
  · 1 秒后来了 3 个「交互式对话」（每个只生成 ~16 token）
  · 对照组：所有人 priority=0（等于先到先服务）
  · 实验组：对话 priority=9

对比的是**对话请求的等待时间**——也就是用户感知的「卡不卡」。

顺带测前缀亲和：同一段长系统提示词连发三次，看 prompt_tokens 是否被缓存吃掉。
引擎（llama-server）只把「真正重算的 token」计入 tokens_evaluated，所以这个数字
就是缓存命中与否的直接证据。
"""

from __future__ import annotations

import asyncio
import statistics
import time

import httpx

BASE = "http://127.0.0.1:8000"

LONG_PROMPT = "请写一段关于分布式系统的说明，尽量详细，覆盖一致性、分区、复制三个话题。"
SHORT_PROMPT = "用一句话解释什么是缓存。"


async def one(
    client: httpx.AsyncClient, name: str, prompt: str, max_tokens: int, priority: int, tenant: str
) -> dict:
    t0 = time.perf_counter()
    first: float | None = None
    text: list[str] = []
    async with client.stream(
        "POST",
        f"{BASE}/v1/chat/completions",
        headers={"X-Priority": str(priority), "X-Tenant": tenant},
        json={
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "stream": True,
        },
    ) as r:
        if r.status_code != 200:
            body = await r.aread()
            return {"name": name, "status": r.status_code, "error": body[:120].decode()}
        async for line in r.aiter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            import json

            chunk = json.loads(payload)
            for ch in chunk.get("choices", []):
                d = ch.get("delta") or {}
                if d.get("content"):
                    if first is None:
                        first = time.perf_counter()
                    text.append(d["content"])
    done = time.perf_counter()
    return {
        "name": name,
        "status": 200,
        "wait": (first or done) - t0,
        "total": done - t0,
        "chars": len("".join(text)),
    }


async def phase(label: str, chat_priority: int) -> dict:
    async with httpx.AsyncClient(timeout=300) as c:
        batch = [
            asyncio.create_task(
                one(c, f"batch{i}", LONG_PROMPT, 120, 0, f"batch{i}")
            )
            for i in range(8)
        ]
        await asyncio.sleep(1.0)
        t_submit = time.perf_counter()
        chats = [
            asyncio.create_task(one(c, f"chat{i}", SHORT_PROMPT, 16, chat_priority, "chat"))
            for i in range(3)
        ]
        chat_res = await asyncio.gather(*chats)
        await asyncio.gather(*batch)
        waits = [r["wait"] for r in chat_res if r["status"] == 200]
        totals = [r["total"] for r in chat_res if r["status"] == 200]
        print(
            f"[{label}] 对话请求 等待均值 {statistics.mean(waits):5.2f}s  "
            f"最大 {max(waits):5.2f}s  端到端均值 {statistics.mean(totals):5.2f}s"
        )
        _ = t_submit
        return {"wait_mean": statistics.mean(waits), "total_mean": statistics.mean(totals)}


async def prefix_cache_demo() -> None:
    async with httpx.AsyncClient(timeout=300) as c:
        sysmsg = "你是一个严谨的技术助手。" * 120  # 约 1500 字，稳定前缀
        print("\n[前缀缓存] 同一段系统提示词连发 3 次（同租户、同优先级）")
        for i in range(3):
            r = await c.post(
                f"{BASE}/v1/chat/completions",
                json={
                    "messages": [
                        {"role": "system", "content": sysmsg},
                        {"role": "user", "content": f"第 {i} 个问题：一句话说明什么是 KV cache。"},
                    ],
                    "max_tokens": 24,
                    "temperature": 0.0,
                },
            )
            u = r.json().get("usage", {})
            cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
            print(
                f"  第 {i + 1} 次: prompt_tokens={u.get('prompt_tokens')}  "
                f"cached={cached}  "
                f"completion_tokens={u.get('completion_tokens')}"
            )


async def main() -> None:
    print("=" * 66)
    print("对照组：所有请求 priority=0（先到先服务）")
    print("=" * 66)
    base = await phase("FIFO ", 0)
    print()
    print("=" * 66)
    print("实验组：对话 priority=9，批量 priority=0")
    print("=" * 66)
    prio = await phase("PRIO ", 9)
    print()
    print(f"对话等待：{base['wait_mean']:.2f}s → {prio['wait_mean']:.2f}s "
          f"（{base['wait_mean'] / max(prio['wait_mean'], 1e-9):.1f}x 改善）")
    await prefix_cache_demo()


if __name__ == "__main__":
    asyncio.run(main())
