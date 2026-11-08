"""通过服务层测多轮对话的 TTFT / 速度 / 前缀缓存命中。

用法：uv run python scripts/probe_perf_service.py
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx

BASE = "http://127.0.0.1:8000"
MODEL = "deepseek-v4-pro-qwen3.5-9b"
SYSTEM = "你是一个严谨的中文技术助手。请始终使用简体中文回答，给出数字与推导，不要空泛。" * 40


async def turn(client: httpx.AsyncClient, messages: list[dict]) -> dict:
    body = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": 96,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    t0 = time.perf_counter()
    ttft = None
    text = ""
    usage = {}
    async with client.stream("POST", f"{BASE}/v1/chat/completions", json=body) as r:
        r.raise_for_status()
        async for line in r.aiter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            if chunk.get("usage"):
                usage = chunk["usage"]
            for ch in chunk.get("choices") or []:
                piece = (ch.get("delta") or {}).get("content") or ""
                if piece and ttft is None:
                    ttft = time.perf_counter() - t0
                text += piece
    total = time.perf_counter() - t0
    out_tok = usage.get("completion_tokens") or 0
    return {
        "ttft_ms": round((ttft or 0) * 1000),
        "total_s": round(total, 2),
        "out_tokens": out_tok,
        "tok_per_s": round(out_tok / total, 1) if total else 0,
        "prompt_tokens": usage.get("prompt_tokens"),
        "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
        "head": text[:40].replace("\n", " "),
    }


async def main() -> None:
    messages = [{"role": "system", "content": SYSTEM}]
    async with httpx.AsyncClient(timeout=300) as client:
        for i, q in enumerate(
            [
                "什么是 KV cache？",
                "它和注意力机制是什么关系？",
                "为什么它能省算力？",
            ],
            start=1,
        ):
            messages.append({"role": "user", "content": q})
            r = await turn(client, messages)
            print(
                f"第{i}轮  prompt={r['prompt_tokens']:>5}  cached={r['cached_tokens']:>5}  "
                f"TTFT={r['ttft_ms']:>5} ms  {r['out_tokens']} tok / {r['total_s']} s  "
                f"= {r['tok_per_s']} tok/s   {r['head']!r}"
            )
            messages.append({"role": "assistant", "content": r["head"]})


if __name__ == "__main__":
    asyncio.run(main())
