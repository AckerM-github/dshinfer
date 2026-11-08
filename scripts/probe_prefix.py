"""前缀缓存实验：并发时缓存到底命不命中，以及"先热一下"有没有用。

三种打法，同一批 4 条请求、同一段长 system prompt：
  A 直接并发            —— 现状
  B 先发一条 max_tokens=1 的预热请求，再并发   —— 服务层能做的"绕法"
  C 逐条串行            —— 上界（每条都能吃到上一条的缓存）

看两个数：每条请求的 cached prompt token，以及 TTFT。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

import httpx

SYSTEM = (
    "你是一名严谨的技术助理。回答时必须先给结论，再给三条理由，最后给一个注意事项。"
    "不要使用感叹号，不要编造来源，遇到不确定的信息要明确说不知道。"
    "下面的背景资料仅供理解上下文，不要逐字复述：" + "青石镇的铁匠铺每天清晨最先亮灯，" * 20
)

QUESTIONS = [
    "为什么显存带宽决定生成速度？",
    "前缀缓存省掉了什么？",
    "批处理为什么提高吞吐？",
    "KV cache 怎么估算？",
]


async def one(client: httpx.AsyncClient, base: str, q: str, n_predict: int) -> dict:
    payload = {
        "model": "deepseek-v4-pro-qwen3.5-9b",
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": q}],
        "max_tokens": n_predict,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    t0 = time.perf_counter()
    ttft = None
    tokens = 0
    cached = 0
    async with client.stream("POST", f"{base}/v1/chat/completions", json=payload, timeout=300) as r:
        r.raise_for_status()
        async for line in r.aiter_lines():
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            for ch in chunk.get("choices") or []:
                piece = (ch.get("delta") or {}).get("content") or ""
                if piece and ttft is None:
                    ttft = time.perf_counter() - t0
                if piece:
                    tokens += 1
            if chunk.get("usage"):
                cached = (chunk["usage"].get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    return {
        "ttft_ms": round((ttft or 0) * 1000),
        "tokens": tokens,
        "cached": cached,
        "wall_s": round(time.perf_counter() - t0, 2),
    }


def show(tag: str, rows: list[dict]) -> None:
    print(f"{tag}")
    for i, r in enumerate(rows):
        print(f"  #{i}  TTFT {r['ttft_ms']:>5}ms  tokens {r['tokens']:>4}  cached {r['cached']:>4}  wall {r['wall_s']}s")
    print(
        f"  -> TTFT 中位 {statistics.median(r['ttft_ms'] for r in rows):.0f} ms, "
        f"命中 {sum(1 for r in rows if r['cached'])}/{len(rows)}, "
        f"缓存省下 {sum(r['cached'] for r in rows)} token\n"
    )


async def main_async(args: argparse.Namespace) -> None:
    base = f"http://127.0.0.1:{args.port}"
    n = args.num
    async with httpx.AsyncClient() as client:
        await one(client, base, "热身", 4)  # 让模板/分词稳定，不计入统计

        rows = await asyncio.gather(*(one(client, base, QUESTIONS[i % 4], args.n_predict) for i in range(n)))
        show("A 直接并发", rows)

        # 清空不了 KV，用不同的问题文本 + 不同系统提示做 B，避免吃到 A 的缓存
        sys_b = SYSTEM + "（第二轮）"
        q_b = [q + "（请展开）" for q in QUESTIONS]

        async def one_b(q: str, n_predict: int) -> dict:
            payload = {
                "model": "deepseek-v4-pro-qwen3.5-9b",
                "messages": [{"role": "system", "content": sys_b}, {"role": "user", "content": q}],
                "max_tokens": n_predict,
                "temperature": 0,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            t0 = time.perf_counter()
            ttft = None
            tokens = 0
            cached = 0
            async with client.stream("POST", f"{base}/v1/chat/completions", json=payload, timeout=300) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    for ch in chunk.get("choices") or []:
                        piece = (ch.get("delta") or {}).get("content") or ""
                        if piece and ttft is None:
                            ttft = time.perf_counter() - t0
                        if piece:
                            tokens += 1
                    if chunk.get("usage"):
                        cached = (chunk["usage"].get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            return {"ttft_ms": round((ttft or 0) * 1000), "tokens": tokens, "cached": cached,
                    "wall_s": round(time.perf_counter() - t0, 2)}

        # 预热：一条只生成 1 个 token 的请求，把 600 token 前缀压进 KV
        await one_b(q_b[0], 1)
        rows_b = await asyncio.gather(*(one_b(q_b[i % 4], args.n_predict) for i in range(n)))
        show("B 先预热再并发", rows_b)

        rows_c = []
        for i in range(n):
            rows_c.append(await one_b(q_b[i % 4] + f"（{i}）", args.n_predict))
        show("C 逐条串行", rows_c)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("-n", "--num", type=int, default=4)
    ap.add_argument("--n-predict", type=int, default=96)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
