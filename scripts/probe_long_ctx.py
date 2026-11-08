"""长上下文探针：确认服务在 n_ctx=32768 / 单 slot 下能吃下 agent 级提示词。

用 repeat 次数的中文填充把 prompt 顶到指定 token 量级，然后问一个需要
"数数"的问题——模型数错没关系，我们要的是 usage.prompt_tokens 与是否 200。
"""

import argparse
import json
import time
import urllib.request

FILLER = "这是一段用于测试长上下文的填充文本，它没有任何实际含义。"


def post(url: str, payload: dict, timeout: float = 900.0) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json; charset=utf-8"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000/v1")
    ap.add_argument("--repeat", type=int, default=400, help="填充句重复次数")
    ap.add_argument("--max-tokens", type=int, default=24)
    args = ap.parse_args()

    prompt = FILLER * args.repeat + "\n\n忽略上面的内容，只回答两个字：收到"
    payload = {
        "model": "deepseek-v4-pro-qwen3.5-9b",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": args.max_tokens,
        "stream": False,
    }
    t0 = time.perf_counter()
    try:
        out = post(f"{args.base}/chat/completions", payload)
    except urllib.error.HTTPError as e:  # noqa: F821
        print("HTTP", e.code, e.read().decode("utf-8", "replace")[:400])
        return
    dt = time.perf_counter() - t0
    usage = out["usage"]
    print(f"prompt_tokens={usage['prompt_tokens']} completion={usage['completion_tokens']} "
          f"cached={usage['prompt_tokens_details']['cached_tokens']} elapsed={dt:.1f}s "
          f"prefill_speed={usage['prompt_tokens'] / dt:.0f} tok/s")
    print("answer:", out["choices"][0]["message"]["content"].strip()[:80])


if __name__ == "__main__":
    main()
