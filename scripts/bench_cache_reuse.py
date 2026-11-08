"""--cache-reuse 的 A/B：共享前缀到底能不能复用，省多少。

llama.cpp 默认（cache-reuse=0）只复用「上一条 prompt 恰好是新 prompt 的完整前缀」，
所以多轮对话能命中，而"同一段 system + 不同问题"一律重算。这个脚本用同一批
请求对比 cache-reuse=0 与 256，看 cache_n 与 TTFT。

用法：uv run python scripts/bench_cache_reuse.py
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
import urllib.request
from pathlib import Path

EXE = Path(r"D:\llm\runtime\llama.cpp\llama-server.exe")
MODEL = Path(r"D:\llm\models\gguf\DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic.Q4_K_M.gguf")

SYSTEM = (
    "你是一名严谨的技术助理。回答时必须先给结论，再给三条理由，最后给一个注意事项。"
    "不要使用感叹号，不要编造来源，遇到不确定的信息要明确说不知道。下面的背景资料仅供理解上下文，不要逐字复述："
    + "青石镇的铁匠铺每天清晨最先亮灯，炉火映得半条街都是红的。" * 12
)
QUESTIONS = [
    "为什么显存带宽决定生成速度？",
    "前缀缓存省掉了什么？",
    "批处理为什么提高吞吐？",
    "KV cache 怎么估算？",
]


def prompt(q: str) -> str:
    return (
        f"<|im_start|>system\n{SYSTEM}<|im_end|>\n"
        f"<|im_start|>user\n{q}<|im_end|>\n"
        f"<|im_start|>assistant\n<think>\n\n</think>\n\n"
    )


def post(port: int, body: dict, timeout: float = 240) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/completion",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode())


def wait_health(port: int, timeout: float = 240) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:  # noqa: S310
                if r.status == 200:
                    return
        except Exception:  # noqa: BLE001
            time.sleep(1)
    raise RuntimeError(f"engine on {port} did not become healthy")


def run_round(port: int, n_predict: int) -> None:
    # 串行 4 条：共享 600 token 前缀，问题不同
    serial = []
    for q in QUESTIONS:
        t0 = time.perf_counter()
        d = post(port, {"prompt": prompt(q), "n_predict": n_predict, "temperature": 0,
                        "cache_prompt": True, "stream": False})
        tm = d.get("timings") or {}
        serial.append({
            "wall_ms": round((time.perf_counter() - t0) * 1000),
            "prompt_n": tm.get("prompt_n"),
            "cache_n": tm.get("cache_n") or 0,
            "ttft_ms": round((tm.get("prompt_ms") or 0)),
        })
    # 并发 4 条：走 /completion 的同一批（urllib 是阻塞的，用线程池）
    from concurrent.futures import ThreadPoolExecutor

    def one(q: str) -> dict:
        t0 = time.perf_counter()
        d = post(port, {"prompt": prompt(q), "n_predict": n_predict, "temperature": 0,
                        "cache_prompt": True, "stream": False})
        tm = d.get("timings") or {}
        return {
            "wall_ms": round((time.perf_counter() - t0) * 1000),
            "prompt_n": tm.get("prompt_n"),
            "cache_n": tm.get("cache_n") or 0,
        }

    with ThreadPoolExecutor(max_workers=4) as ex:
        conc = list(ex.map(one, QUESTIONS))

    for tag, rows in (("串行", serial), ("并发", conc)):
        caches = [r["cache_n"] for r in rows]
        print(f"  {tag}: cache_n={caches} 命中 {sum(1 for c in caches if c)}/{len(caches)}"
              f"  wall 中位 {statistics.median(r['wall_ms'] for r in rows):.0f} ms")
        if tag == "串行":
            print(f"        prompt_n={[r['prompt_n'] for r in rows]}  prefill 耗时={[r['ttft_ms'] for r in rows]} ms")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-predict", type=int, default=48)
    ap.add_argument("--ports", type=int, nargs=2, default=[8136, 8137])
    args = ap.parse_args()

    for port, reuse in zip(args.ports, (0, 256), strict=False):
        cmd = [
            str(EXE), "-m", str(MODEL), "--host", "127.0.0.1", "--port", str(port),
            "-c", "32768", "-b", "2048", "-ub", "1024", "-fa", "on", "-ngl", "99",
            "-np", "4", "--kv-unified", "--no-webui",
        ]
        if reuse:
            cmd += ["--cache-reuse", str(reuse)]
        print(f"\n=== cache-reuse={reuse} (port {port}) ===")
        log = open(Path(r"D:\llm\runtime") / f"cacheReuse_{reuse}.log", "wb")  # noqa: SIM115
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)  # noqa: S603
        try:
            wait_health(port)
            time.sleep(1)
            run_round(port, args.n_predict)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()
            time.sleep(2)


if __name__ == "__main__":
    main()
