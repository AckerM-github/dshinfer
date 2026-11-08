"""发一条请求，把它的时间线打印成瀑布图。

用法：
    uv run python scripts/trace_one.py
    uv run python scripts/trace_one.py --stream
    uv run python scripts/trace_one.py --question "为什么前缀缓存没用"

做的事：带 `X-Request-Id` 发请求 → 读 `Server-Timing` 响应头 → 再拉
`/admin/traces/{id}`，把服务层记录的每个打点画成条形图。排障时看这个比看
Prometheus 直方图快得多：一眼能分出「排队 3 秒」和「预填充 3 秒」。
"""

from __future__ import annotations

import argparse
import asyncio
import time

import httpx

BAR_WIDTH = 48


def draw(detail: dict) -> None:
    marks = detail.get("marks_ms") or {}
    order = ["received", "rendered", "admitted", "engine_start", "first_token", "last_token", "done"]
    present = [(name, marks[name]) for name in order if name in marks]
    total = present[-1][1] if present else 1.0
    total = max(total, 1.0)

    print(f"\n  时间线（总 {total:.0f} ms，每格 ≈ {total / BAR_WIDTH:.1f} ms）")
    for i, (name, offset) in enumerate(present):
        end = present[i + 1][1] if i + 1 < len(present) else total
        start_col = round(offset / total * BAR_WIDTH)
        length = max(1, round((end - offset) / total * BAR_WIDTH))
        bar = " " * start_col + "█" * length
        print(f"  {name:<13}{offset:>8.0f} ms  {bar:<{BAR_WIDTH}}  +{end - offset:.0f} ms")


async def main_async(args: argparse.Namespace) -> None:
    base = f"http://127.0.0.1:{args.port}"
    rid = f"trace-{int(time.time())}"
    payload = {
        "model": "deepseek-v4-pro-qwen3.5-9b",
        "messages": [{"role": "user", "content": args.question}],
        "max_tokens": args.max_tokens,
        "temperature": 0,
        "stream": args.stream,
    }
    if args.stream:
        payload["stream_options"] = {"include_usage": True}

    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=300) as client:
        if args.stream:
            text = ""
            async with client.stream(
                "POST", f"{base}/v1/chat/completions", json=payload, headers={"X-Request-Id": rid}
            ) as r:
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    if line[6:].strip() == "[DONE]":
                        break
                    import json as _json

                    chunk = _json.loads(line[6:])
                    for ch in chunk.get("choices") or []:
                        text += (ch.get("delta") or {}).get("content") or ""
            print(f"流式完成，{time.perf_counter() - t0:.2f}s，正文 {len(text)} 字：{text[:60]}")
        else:
            r = await client.post(
                f"{base}/v1/chat/completions", json=payload, headers={"X-Request-Id": rid}
            )
            r.raise_for_status()
            data = r.json()
            print(f"非流式完成，{time.perf_counter() - t0:.2f}s")
            print(f"  X-Request-Id : {r.headers.get('x-request-id')}")
            print(f"  Server-Timing: {r.headers.get('server-timing')}")
            print(f"  usage        : {data['usage']}")
            print(f"  正文         : {(data['choices'][0]['message']['content'] or '')[:60]}")

        detail_r = await client.get(f"{base}/admin/traces/{rid}")
        if detail_r.status_code != 200:
            print(f"取 trace 失败：{detail_r.status_code} {detail_r.text[:200]}")
            return
        detail = detail_r.json()

    draw(detail)
    spans = detail.get("spans_ms") or {}
    print("\n  派生 span：" + "  ".join(f"{k}={v}ms" for k, v in spans.items()))
    print(f"  token      ：{detail.get('tokens')}")
    print(f"  引擎自报   ：{detail.get('engine_ms')}（prompt/decode，毫秒）")
    print(f"  状态       ：{detail.get('status')} / {detail.get('finish_reason')}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--question", default="用一句话解释什么是 KV cache")
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--stream", action="store_true")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
