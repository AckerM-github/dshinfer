"""流式开销剖析：同一批请求，分别打服务层与引擎，看差额到底花在哪。

用法：
    uv run python scripts/profile_stream.py                 # 4 路并发，服务层
    uv run python scripts/profile_stream.py --port 8124     # 直连引擎（对照组）
    uv run python scripts/profile_stream.py -n 1            # 单流

测的四个数：TTFT、解码 tok/s、cached prompt token、墙钟。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

import httpx

# 600 字左右的长前缀：够长到让 prompt cache 有意义，又不会把预填充拖成主导
SYSTEM = (
    "你是一名严谨的技术助理。回答时必须先给结论，再给三条理由，最后给一个注意事项。"
    "不要使用感叹号，不要编造来源，遇到不确定的信息要明确说不知道。"
    "下面的背景资料仅供理解上下文，不要逐字复述：" + "青石镇的铁匠铺每天清晨最先亮灯，" * 20
)

QUESTIONS = [
    "为什么 GPU 的显存带宽决定了大模型生成速度？",
    "前缀缓存为什么能省掉预填充？",
    "为什么批处理能提高吞吐但不降低单条延迟？",
    "KV cache 的显存占用怎么估算？",
]


def engine_prompt(q: str) -> str:
    return (
        "<|im_start|>system\n" + SYSTEM + "<|im_end|>\n"
        "<|im_start|>user\n" + q + "<|im_end|>\n"
        "<|im_start|>assistant\n<think>\n\n</think>\n\n"
    )


async def one(client: httpx.AsyncClient, base: str, q: str, n_predict: int, direct: bool) -> dict:
    t0 = time.perf_counter()
    ttft = None
    tokens = 0
    cached = 0

    if direct:
        body = {"prompt": engine_prompt(q), "n_predict": n_predict, "temperature": 0, "stream": True}
        async with client.stream("POST", f"{base}/completion", json=body, timeout=300) as r:
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                chunk = json.loads(line[6:])
                piece = chunk.get("content") or ""
                if piece and ttft is None:
                    ttft = time.perf_counter() - t0
                if piece:
                    tokens += 1
                if chunk.get("stop"):
                    t = chunk.get("timings") or {}
                    cached = t.get("cache_n") or 0
                    break
    else:
        payload = {
            "model": "deepseek-v4-pro-qwen3.5-9b",
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": q},
            ],
            "max_tokens": n_predict,
            "temperature": 0,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
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
                u = chunk.get("usage")
                if u:
                    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0

    dt = time.perf_counter() - t0
    return {
        "ttft_ms": round((ttft or 0) * 1000),
        "tokens": tokens,
        "decode_tps": round(tokens / max(dt - (ttft or 0), 1e-6), 1),
        "wall_s": round(dt, 2),
        "cached": cached,
    }


async def main_async(args: argparse.Namespace) -> None:
    base = f"http://127.0.0.1:{args.port}"
    direct = args.port == 8124
    async with httpx.AsyncClient() as client:
        # 预热：让前缀进缓存，避免第一轮把预填充算进解码
        await one(client, base, "热身问题", 8, direct)

        t0 = time.perf_counter()
        rows = await asyncio.gather(
            *(one(client, base, QUESTIONS[i % len(QUESTIONS)], args.n_predict, direct) for i in range(args.num))
        )
        wall = time.perf_counter() - t0

    total = sum(r["tokens"] for r in rows)
    agg = total / wall
    print(f"端点 {base}  并发 {args.num}  直连={direct}")
    print(f"{'#':>2} {'TTFT':>7} {'tokens':>7} {'tok/s':>7} {'wall':>6} {'cached':>7}")
    for i, r in enumerate(rows):
        print(f"{i:>2} {r['ttft_ms']:>6}ms {r['tokens']:>7} {r['decode_tps']:>7} {r['wall_s']:>6}s {r['cached']:>7}")
    print("-" * 42)
    print(
        f"聚合 {agg:.1f} tok/s（{total} token / {wall:.2f}s），"
        f"TTFT 中位 {statistics.median(r['ttft_ms'] for r in rows):.0f} ms，"
        f"缓存命中 {sum(1 for r in rows if r['cached'])}/{len(rows)}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("-n", "--num", type=int, default=4)
    ap.add_argument("--n-predict", type=int, default=128)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
