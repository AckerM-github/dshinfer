"""Phase 3 压测：四个并发档位 × 两个端点。

对照的两个端点：
  A. 我们的服务   POST http://127.0.0.1:8000/v1/chat/completions   （OpenAI 协议，带调度）
  B. 引擎原生     POST http://127.0.0.1:8124/completion            （llama-server 直连，无调度）

为什么不用 vLLM 对照：vLLM 需要 Linux + 较新的 CUDA，本机是 Windows + 8 GB 显存，
9B 模型放不进去。拿 llama-server 原生接口做对照反而更干净——它能把「服务层到底加了多少开销」
这一项单独分离出来，而这正是我们这几周在写的东西。

跑法：uv run python scripts/bench_load.py

两端点默认指本机原生进程的 8000 / 8124，可改：
  --service / --engine（或 DSHINFER_BENCH_SERVICE / DSHINFER_BENCH_ENGINE）
  --levels 1,2,4,8   --per-level 12   --max-tokens 64   --targets service,engine

容器里跑（推荐）：引擎只监听容器内的 127.0.0.1:8124，宿主机直连不到，
所以用 docker exec 把本文件喂进容器的 venv python：
  docker exec -i dshinfer /opt/venv/bin/python - < scripts/bench_load.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time

import httpx

from dshinfer.config import get_settings
from dshinfer.service import render_prompt
from dshinfer.schemas import ChatMessage

SERVICE = os.environ.get("DSHINFER_BENCH_SERVICE", "http://127.0.0.1:8000")
ENGINE = os.environ.get("DSHINFER_BENCH_ENGINE", "http://127.0.0.1:8124")

PROMPT = "用三句话解释什么是 KV cache，以及它为什么能加速生成。"
MAX_TOKENS = 64
LEVELS = (1, 2, 4, 8)
PER_LEVEL = 12


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="四档并发 x 两个端点的压测")
    ap.add_argument("--service", default=SERVICE, help="服务层地址（默认 %(default)s）")
    ap.add_argument("--engine", default=ENGINE, help="llama-server 直连地址（默认 %(default)s）")
    ap.add_argument("--levels", default="1,2,4,8", help="并发档位，逗号分隔（默认 %(default)s）")
    ap.add_argument("--per-level", type=int, default=PER_LEVEL, help="每档请求数（默认 %(default)s）")
    ap.add_argument("--max-tokens", type=int, default=MAX_TOKENS, help="每请求生成上限（默认 %(default)s）")
    ap.add_argument("--targets", default="service,engine", help="跑哪些端点（默认 %(default)s）")
    return ap.parse_args(argv)

def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(round((len(s) - 1) * q)))
    return s[idx]


async def _one_service(c: httpx.AsyncClient) -> dict:
    t0 = time.perf_counter()
    first = None
    tokens = 0
    async with c.stream(
        "POST",
        f"{SERVICE}/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": PROMPT}],
            "max_tokens": MAX_TOKENS,
            "temperature": 0.0,
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    ) as r:
        if r.status_code != 200:
            return {"ok": False, "ttft": 0.0, "e2e": time.perf_counter() - t0, "tokens": 0}
        async for line in r.aiter_lines():
            if not line.startswith("data: "):
                continue
            body = line[6:]
            if body == "[DONE]":
                break
            ch = json.loads(body)
            for choice in ch.get("choices", []):
                if (choice.get("delta") or {}).get("content"):
                    if first is None:
                        first = time.perf_counter()
                    tokens += 1
    end = time.perf_counter()
    return {"ok": True, "ttft": (first or end) - t0, "e2e": end - t0, "tokens": tokens}


async def _one_engine(c: httpx.AsyncClient, prompt: str) -> dict:
    t0 = time.perf_counter()
    first = None
    tokens = 0
    async with c.stream(
        "POST",
        f"{ENGINE}/completion",
        json={
            "prompt": prompt,
            "stream": True,
            "n_predict": MAX_TOKENS,
            "temperature": 0.0,
            "cache_prompt": True,
        },
    ) as r:
        if r.status_code != 200:
            return {"ok": False, "ttft": 0.0, "e2e": time.perf_counter() - t0, "tokens": 0}
        async for line in r.aiter_lines():
            if not line.startswith("data: "):
                continue
            ch = json.loads(line[6:])
            if ch.get("stop"):
                break
            if ch.get("content"):
                if first is None:
                    first = time.perf_counter()
                tokens += 1
    end = time.perf_counter()
    return {"ok": True, "ttft": (first or end) - t0, "e2e": end - t0, "tokens": tokens}


async def run_level(level: int, target: str, prompt: str) -> dict:
    sem = asyncio.Semaphore(level)
    t0 = time.perf_counter()

    async def worker(client: httpx.AsyncClient):
        async with sem:
            if target == "service":
                return await _one_service(client)
            return await _one_engine(client, prompt)

    async with httpx.AsyncClient(timeout=600) as c:
        # 预热一次，避免首请求的 kernel / 缓存噪声
        if target == "service":
            await _one_service(c)
        else:
            await _one_engine(c, prompt)
        wall0 = time.perf_counter()
        results = await asyncio.gather(*[worker(c) for _ in range(PER_LEVEL)])
        wall = time.perf_counter() - wall0

    ok = [r for r in results if r["ok"]]
    ttfts = [r["ttft"] for r in ok]
    e2es = [r["e2e"] for r in ok]
    toks = sum(r["tokens"] for r in ok)
    _ = t0
    return {
        "level": level,
        "ok": len(ok),
        "wall": wall,
        "rps": len(ok) / wall,
        "tps": toks / wall,
        "ttft_p50": statistics.median(ttfts) if ttfts else 0,
        "ttft_p95": _pct(ttfts, 0.95),
        "e2e_p50": statistics.median(e2es) if e2es else 0,
        "e2e_p95": _pct(e2es, 0.95),
    }


async def main(argv: list[str] | None = None) -> None:
    global SERVICE, ENGINE, PER_LEVEL, MAX_TOKENS, LEVELS
    args = _parse_args(argv)
    SERVICE = args.service.rstrip("/")
    ENGINE = args.engine.rstrip("/")
    PER_LEVEL = args.per_level
    MAX_TOKENS = args.max_tokens
    LEVELS = tuple(int(x) for x in args.levels.replace(" ", "").split(",") if x)
    targets = tuple(t.strip() for t in args.targets.split(",") if t.strip())
    s = get_settings()
    prompt = render_prompt(
        [ChatMessage(role="user", content=PROMPT)],
        template_path=s.chat_template_path,
        enable_thinking=False,
    )
    print(f"每档 {PER_LEVEL} 个请求，max_tokens={MAX_TOKENS}\n")
    print("| 并发 | 端点 | 成功 | 墙钟(s) | req/s | tok/s | TTFT p50 | TTFT p95 | E2E p50 | E2E p95 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    rows = []
    for level in LEVELS:
        for target in targets:
            r = await run_level(level, target, prompt)
            rows.append((target, r))
            name = "dshinfer" if target == "service" else "llama-server 原生"
            print(
                f"| {level} | {name} | {r['ok']}/{PER_LEVEL} | {r['wall']:.2f} | "
                f"{r['rps']:.2f} | {r['tps']:.1f} | {r['ttft_p50'] * 1000:.0f} ms | "
                f"{r['ttft_p95'] * 1000:.0f} ms | {r['e2e_p50'] * 1000:.0f} ms | "
                f"{r['e2e_p95'] * 1000:.0f} ms |"
            )
    # 服务层相对原生的开销（只跑了单边就跳过）
    if not ({"service", "engine"} <= set(targets)):
        return

    print("\n| 并发 | 吞吐损失 | TTFT 增加 |")
    print("|---|---|---|")
    for level in LEVELS:
        svc = next(r for t, r in rows if t == "service" and r["level"] == level)
        eng = next(r for t, r in rows if t == "engine" and r["level"] == level)
        loss = (eng["tps"] - svc["tps"]) / eng["tps"] * 100 if eng["tps"] else 0
        add = (svc["ttft_p50"] - eng["ttft_p50"]) * 1000
        print(f"| {level} | {loss:+.1f}% | {add:+.0f} ms |")


if __name__ == "__main__":
    asyncio.run(main())
