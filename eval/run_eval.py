"""跑评测集：把 eval/cases.jsonl 打到本地服务上，判定、汇总、存档。

用法：
    uv run python eval/run_eval.py                # 打 http://127.0.0.1:8000
    uv run python eval/run_eval.py --only tool-*  # 只跑某几类
    uv run python eval/run_eval.py --base-url http://127.0.0.1:8000 -c 2

判定在 judge.py 里，这里是"发请求 + 记账 + 出表"。
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from judge import Resp, judge  # noqa: E402

HERE = Path(__file__).parent
CASES = HERE / "cases.jsonl"
RESULTS = HERE / "results"


def load_cases(patterns: list[str]) -> list[dict]:
    rows = []
    for line in CASES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    if patterns:
        rows = [r for r in rows if any(fnmatch.fnmatch(r["id"], p) for p in patterns)]
    return [build_messages(r) for r in rows]


def needle_prompt(total: int = 60, pos: int = 41) -> str:
    """长上下文"针"：60 行无关填充里藏一句暗号，问第 pos 行是什么。

    用生成而不是静态文本，是为了以后能改长度做 4K/8K/16K 的衰减曲线。
    """
    lines = []
    for i in range(1, total + 1):
        if i == pos:
            lines.append(f"第{i}行: 暗号是 青鸟-7391，只在这一行出现。")
        else:
            lines.append(f"第{i}行: 这是第{i}行的填充内容，与问题无关。")
    body = "\n".join(lines)
    return (
        "下面是一份不带编号之外的结构的清单。\n" + body + f"\n\n问：第{pos}行的暗号是什么？只回答暗号本身。"
    )


def build_messages(case: dict) -> dict:
    """把带生成器的用例展开成普通 messages。"""
    gen = case.get("generate")
    if gen == "needle":
        case = {**case, "messages": [{"role": "user", "content": needle_prompt()}]}
    return case


async def run_case(client: httpx.AsyncClient, base_url: str, case: dict, sem: asyncio.Semaphore) -> dict:
    payload: dict = {
        "model": "deepseek-v4-pro-qwen3.5-9b",  # 服务端会用自己的 model_name 覆盖
        "messages": case["messages"],
        "temperature": 0,
        "top_p": 1,
        "max_tokens": case.get("max_tokens", 128),
        "stream": False,
    }
    if case.get("tools"):
        payload["tools"] = case["tools"]
        payload["tool_choice"] = "auto"

    async with sem:
        t0 = time.perf_counter()
        try:
            r = await client.post(f"{base_url}/v1/chat/completions", json=payload, timeout=180)
            dt = time.perf_counter() - t0
            r.raise_for_status()
            body = r.json()
        except Exception as e:  # noqa: BLE001 - 单条失败不该中断整轮
            dt = time.perf_counter() - t0
            return {
                "id": case["id"],
                "category": case["category"],
                "ok": False,
                "detail": f"请求失败: {type(e).__name__}: {e}",
                "latency_s": round(dt, 3),
            }

    choice = body["choices"][0]
    msg = choice.get("message") or {}
    resp = Resp(
        content=msg.get("content") or "",
        tool_calls=msg.get("tool_calls") or [],
        finish_reason=choice.get("finish_reason"),
    )
    ok, detail = judge(resp, case["check"])
    usage = body.get("usage") or {}
    comp = usage.get("completion_tokens") or 0
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    return {
        "id": case["id"],
        "category": case["category"],
        "ok": ok,
        "detail": detail,
        "latency_s": round(dt, 3),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": comp,
        "cached_tokens": cached,
        "tps": round(comp / dt, 1) if dt > 0 and comp else 0.0,
        "answer": (resp.content or "")[:200],
        # 失败时留下完整回答，否则无法复现"为什么判错"
        "answer_full": None if ok else (resp.content or ""),
        "tool_calls": [tc.get("function", {}).get("name") for tc in resp.tool_calls],
        "finish_reason": resp.finish_reason,
    }


def summarize(rows: list[dict]) -> None:
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)

    print(f"{'类别':<10}{'用例':>4}{'通过':>6}{'平均延迟':>10}{'平均 tok/s':>12}")
    print("-" * 44)
    for cat, items in by_cat.items():
        ok = sum(1 for i in items if i["ok"])
        lat = statistics.mean(i["latency_s"] for i in items)
        tps = statistics.mean(i["tps"] for i in items if i.get("tps"))
        print(f"{cat:<10}{len(items):>4}{ok:>4}/{len(items):<2}{lat:>9.2f}s{tps:>11.1f}")

    total_ok = sum(1 for r in rows if r["ok"])
    all_lat = statistics.mean(r["latency_s"] for r in rows)
    print("-" * 44)
    print(f"{'总体':<10}{len(rows):>4}{total_ok:>4}/{len(rows):<2}{all_lat:>9.2f}s")

    failed = [r for r in rows if not r["ok"]]
    if failed:
        print("\n失败明细：")
        for r in failed:
            print(f"  ✗ {r['id']:<18} {r['detail']}")
            if r.get("answer"):
                print(f"      答: {r['answer'][:100]!r}")


async def main_async(args: argparse.Namespace) -> int:
    cases = load_cases(args.only)
    if not cases:
        print("没有匹配的用例", file=sys.stderr)
        return 2

    base_url = args.base_url.rstrip("/")
    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient() as client:
        try:
            h = await client.get(f"{base_url}/healthz", timeout=5)
            h.raise_for_status()
        except Exception as e:  # noqa: BLE001
            print(f"服务不可用 {base_url}: {e}", file=sys.stderr)
            return 2

        t0 = time.perf_counter()
        rows = await asyncio.gather(*(run_case(client, base_url, c, sem) for c in cases))
        wall = time.perf_counter() - t0

    rows = sorted(rows, key=lambda r: [c["id"] for c in cases].index(r["id"]))
    summarize(rows)
    total_ok = sum(1 for r in rows if r["ok"])
    print(f"\n墙钟 {wall:.1f}s，并发 {args.concurrency}")

    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = {
        "stamp": stamp,
        "base_url": base_url,
        "cases": len(rows),
        "passed": total_ok,
        "wall_s": round(wall, 1),
        "rows": rows,
    }
    payload = json.dumps(out, ensure_ascii=False, indent=2)
    (RESULTS / f"{stamp}.json").write_text(payload, encoding="utf-8")
    (HERE / "latest.json").write_text(payload, encoding="utf-8")
    print(f"结果已存 eval/results/{stamp}.json")
    return 0 if total_ok == len(rows) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("-c", "--concurrency", type=int, default=2)
    ap.add_argument("--only", nargs="*", default=[], help="glob，如 tool-*")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
