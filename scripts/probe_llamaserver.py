"""直接打 llama-server 的 /completion，验证「我们渲染的 prompt + 真模型」这条路。

用法：先手动起 llama-server（端口 8124），再跑这个脚本。
"""

import json
import sys
import time

import httpx

sys.path.insert(0, r"D:\llm\infer\src")

from dshinfer.schemas import ChatMessage  # noqa: E402
from dshinfer.service import render_prompt  # noqa: E402

TEMPLATE = r"D:\llm\runtime\chat_template.jinja"
BASE = "http://127.0.0.1:8124"


def ask(question: str, max_tokens: int = 128, thinking: bool = False) -> None:
    msgs = [ChatMessage(role="user", content=question)]
    prompt = render_prompt(msgs, template_path=TEMPLATE, enable_thinking=thinking)
    print(f"\n=== {question!r}  (thinking={thinking})")
    print("prompt:", repr(prompt))

    payload = {
        "prompt": prompt,
        "n_predict": max_tokens,
        "temperature": 0.6,
        "top_p": 0.9,
        "stream": True,
        "cache_prompt": True,
    }
    t0 = time.perf_counter()
    ttft = None
    pieces: list[str] = []
    stats = {}
    with httpx.Client(timeout=None) as c:
        with c.stream("POST", f"{BASE}/completion", json=payload) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                chunk = json.loads(line[6:])
                if chunk.get("stop"):
                    stats = chunk
                    break
                text = chunk.get("content") or ""
                if text and ttft is None:
                    ttft = time.perf_counter() - t0
                if text:
                    pieces.append(text)
    dt = time.perf_counter() - t0
    out = "".join(pieces)
    n = stats.get("tokens_predicted", len(pieces))
    print("输出:", out[:300])
    print(
        f"TTFT={ttft * 1000:.0f} ms  总耗时={dt:.2f} s  tokens={n}  "
        f"速度={n / dt:.1f} tok/s  prompt_tokens={stats.get('tokens_evaluated')}"
    )


if __name__ == "__main__":
    ask("你是谁？用一个词回答。", 32)
    ask("1+1 等于几？只回答数字。", 16)
