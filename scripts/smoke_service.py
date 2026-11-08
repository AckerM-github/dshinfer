"""端到端冒烟：打我们自己的服务（不是 llama-server），验证流式/非流式/用量/指标。"""

import json
import time

import httpx

BASE = "http://127.0.0.1:8000"


def non_stream() -> None:
    body = {
        "model": "deepseek-v4-pro-qwen3.5-9b",
        "messages": [{"role": "user", "content": "用一句话解释什么是 KV cache"}],
        "max_tokens": 120,
        "temperature": 0.6,
    }
    t0 = time.perf_counter()
    r = httpx.post(f"{BASE}/v1/chat/completions", json=body, timeout=300)
    dt = time.perf_counter() - t0
    d = r.json()
    print(f"[非流式] HTTP {r.status_code}  {dt:.2f}s")
    print("  content:", d["choices"][0]["message"]["content"][:200])
    print("  finish:", d["choices"][0]["finish_reason"], " usage:", d["usage"])


def stream() -> None:
    body = {
        "model": "deepseek-v4-pro-qwen3.5-9b",
        "messages": [{"role": "user", "content": "数到五，只输出数字"}],
        "max_tokens": 64,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    t0 = time.perf_counter()
    ttft = None
    pieces: list[str] = []
    usage = None
    finish = None
    with httpx.Client(timeout=300) as c:
        with c.stream("POST", f"{BASE}/v1/chat/completions", json=body) as r:
            print(f"[流式] HTTP {r.status_code}")
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    break
                ch = json.loads(payload)
                if ch.get("usage"):
                    usage = ch["usage"]
                for c0 in ch.get("choices", []):
                    delta = c0.get("delta") or {}
                    if delta.get("content"):
                        if ttft is None:
                            ttft = time.perf_counter() - t0
                        pieces.append(delta["content"])
                    if c0.get("finish_reason"):
                        finish = c0["finish_reason"]
    dt = time.perf_counter() - t0
    print(f"  TTFT={ttft * 1000:.0f} ms  总={dt:.2f}s  finish={finish}  usage={usage}")
    print("  content:", "".join(pieces)[:200])


def metrics() -> None:
    txt = httpx.get(f"{BASE}/metrics", timeout=30).text
    keep = [
        ln for ln in txt.splitlines()
        if ln.startswith(("dshinfer_requests_total", "dshinfer_tokens_total", "dshinfer_ttft_seconds_count", "dshinfer_inflight"))
    ]
    print("[指标]")
    for ln in keep[:12]:
        print("  ", ln)


if __name__ == "__main__":
    non_stream()
    stream()
    metrics()
