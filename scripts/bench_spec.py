"""投机解码实测：无投机 vs 草稿模型 vs n-gram。

直接打 llama-server 的 /completion，用服务端返回的 timings 计时（不含 HTTP/SSE 开销），
每种配置跑同一个 prompt、同一个 n_predict、temperature=0（贪心），保证输出可比。

用法：
    uv run python scripts/bench_spec.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

LLAMA = Path(r"D:\llm\runtime\llama.cpp")
SERVER = LLAMA / "llama-server.exe"
MODEL = Path(r"D:\llm\models\gguf\DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic.Q4_K_M.gguf")
DRAFT = Path(r"D:\llm\models\gguf\draft\Qwen3.5-0.8B-Q8_0.gguf")
PORT = 8130

BASE_ARGS = [
    "-m", str(MODEL),
    "-c", "8192",
    "-b", "2048",
    "-ub", "512",
    "-ngl", "99",
    "-fa", "on",
    "--no-webui",
    "--host", "127.0.0.1",
    "--port", str(PORT),
]

PROMPT = (
    "你是一个严谨的技术助手。请用中文分点解释：为什么大模型推理时，"
    "生成阶段的速度主要由显存带宽决定，而预填充阶段主要由算力决定？"
    "请给出具体数字和推导过程。"
)

CONFIGS: list[tuple[str, list[str]]] = [
    ("baseline", []),
    ("draft-4", ["-md", str(DRAFT), "--spec-type", "draft-simple", "--spec-draft-n-max", "4", "--spec-draft-ngl", "99"]),
    ("draft-8", ["-md", str(DRAFT), "--spec-type", "draft-simple", "--spec-draft-n-max", "8", "--spec-draft-ngl", "99"]),
    ("draft-12", ["-md", str(DRAFT), "--spec-type", "draft-simple", "--spec-draft-n-max", "12", "--spec-draft-ngl", "99"]),
    ("ngram", ["--spec-type", "ngram-simple"]),
]


def _http_json(url: str, payload: dict | None = None, timeout: float = 600.0) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST" if data else "GET"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode())


def wait_health(timeout: float = 240.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if _http_json(f"http://127.0.0.1:{PORT}/health", timeout=2).get("status") == "ok":
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.0)
    return False


def kill_stale() -> None:
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" | "
        f"Where-Object {{ $_.ExecutablePath -eq '{SERVER}' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=60)


def run_one(name: str, extra: list[str], n_predict: int = 128) -> dict[str, float] | None:
    kill_stale()
    time.sleep(0.5)
    log = open(Path(r"D:\llm\runtime") / f"spec_{name}.log", "wb")  # noqa: SIM115
    t0 = time.time()
    proc = subprocess.Popen(
        [str(SERVER), *BASE_ARGS, *extra], stdout=log, stderr=subprocess.STDOUT
    )
    try:
        if not wait_health():
            print(f"{name:<10} 启动失败，日志见 spec_{name}.log")
            return None
        load_s = time.time() - t0
        body = {
            "prompt": PROMPT,
            "n_predict": n_predict,
            "temperature": 0.0,
            "cache_prompt": False,
        }
        # 预热一次（不计分），避免首次请求的显存/页表抖动
        _http_json(f"http://127.0.0.1:{PORT}/completion", {**body, "n_predict": 8})
        r = _http_json(f"http://127.0.0.1:{PORT}/completion", body)
        t = r.get("timings", {})
        return {
            "load_s": load_s,
            "prompt_n": t.get("prompt_n", 0),
            "pp_tps": t.get("prompt_per_second", 0.0),
            "gen_n": t.get("predicted_n", 0),
            "tg_tps": t.get("predicted_per_second", 0.0),
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        kill_stale()
        time.sleep(1.0)


def main() -> int:
    if not DRAFT.exists():
        print(f"缺草稿模型：{DRAFT}")
        return 2
    print(f"模型 {MODEL.name}")
    print(f"草稿 {DRAFT.name}")
    print(f"prompt {len(PROMPT)} 字，n_predict=128，temperature=0\n")
    rows = []
    for name, extra in CONFIGS:
        got = run_one(name, extra)
        if got is None:
            continue
        rows.append((name, got))
        print(
            f"{name:<10} load={got['load_s']:5.1f}s  pp={got['prompt_n']:4d} tok @ {got['pp_tps']:7.1f} t/s"
            f"  tg={got['gen_n']:4d} tok @ {got['tg_tps']:5.2f} t/s"
        )
    if rows:
        base = next((r for n, r in rows if n == "baseline"), None)
        if base:
            print("\n相对基线：")
            for name, r in rows:
                if name == "baseline" or not base["tg_tps"]:
                    continue
                print(f"  {name:<10} tg ×{r['tg_tps'] / base['tg_tps']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
