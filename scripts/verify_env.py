#!/usr/bin/env python3
"""verify_env.py — 两端配置一致性自检（Windows 原生 / 容器 / 云）。

同一份源码在三种形态里跑，真正的判据不是「配置文件看起来一样」，而是
「跑起来之后自报的生效配置一样」。本脚本读 GET /admin/config，只取跨机器可比的
键（路径本来就不该一样，所以不进比对），把结果写成 JSON；两份 JSON 一 compare
就知道有没有漂移。

用法：

    # 采集（宿主侧 / 容器侧各跑一次，JSON 可以跨机器拷）
    python3 scripts/verify_env.py --base http://127.0.0.1:8000 --json-out host.json
    docker exec -i dshinfer /opt/venv/bin/python - \
        --base http://127.0.0.1:8000 --json-out /tmp/c.json < scripts/verify_env.py

    # 比对
    python3 scripts/verify_env.py --compare host.json container.json

退出码：0 = 一致；1 = 有差异；2 = 采集失败（服务没起 / 端点不通）。
只用标准库 urllib —— 镜像里的 python 不需要额外装包。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

# 这些键必须两端完全一致：它们决定了「同样输入给出同样输出」的前提
CONTRACT_KEYS = [
    "engine",
    "model_name",
    "gguf_file",
    "gguf_bytes",
    "n_ctx",
    "cache_type_k",
    "cache_type_v",
    "n_parallel",
    "max_concurrency",
    "kv_unified",
    "n_batch",
    "n_ubatch",
    "flash_attn",
    "slot_pinning",
    "fit",
    "n_gpu_layers",
    "enable_thinking",
]

# 只报告、不参与判定（各形态天然不同或随运行时变化）
INFO_KEYS = ["platform"]


def _get(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def collect(base: str, timeout: float = 10.0) -> dict:
    base = base.rstrip("/")
    cfg = _get(base + "/admin/config", timeout)
    health = _get(base + "/healthz", timeout)
    engine = health.get("engine") or {}
    return {
        "base": base,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "healthy": health.get("status") == "ok" and bool(engine.get("ready", True)),
        "engine_state": {
            "engine": engine.get("engine"),
            "model": engine.get("model"),
            "load_seconds": engine.get("load_seconds"),
        },
        "contract": {k: cfg.get(k) for k in CONTRACT_KEYS},
        "info": {k: cfg.get(k) for k in INFO_KEYS},
        "raw_extra": {
            k: v for k, v in cfg.items() if k not in CONTRACT_KEYS and k not in INFO_KEYS
        },
    }


def _load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def compare(paths: list[str]) -> int:
    reports = [(_load(p), p) for p in paths]
    diffs = []
    for key in CONTRACT_KEYS:
        values = [rep["contract"].get(key) for rep, _ in reports]
        if any(v != values[0] for v in values):
            diffs.append((key, values))

    engine_names = [rep["engine_state"].get("engine") for rep, _ in reports]
    if any(v != engine_names[0] for v in engine_names):
        diffs.append(("engine_state.engine", engine_names))

    width = max(len(k) for k in [k for k, _ in diffs] + ["字段"])
    print("== 两端配置一致性自检 ==")
    for rep, path in reports:
        print(
            "  %-28s healthy=%s engine=%s model=%s"
            % (path, rep["healthy"], rep["engine_state"].get("engine"), rep["engine_state"].get("model"))
        )
    if not diffs:
        print("  [OK] %d 个契约键完全一致" % len(CONTRACT_KEYS))
        return 0
    print("  [FAIL] %d 处不一致：" % len(diffs))
    print("  %-*s  %s" % (width, "字段", "  ".join("值%d" % i for i in range(len(reports)))))
    for key, values in diffs:
        print("  %-*s  %s" % (width, key, "  ".join(str(v) for v in values)))
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="dshinfer 两端配置一致性自检")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="服务层地址")
    ap.add_argument("--timeout", type=float, default=10.0, help="单次请求超时（秒）")
    ap.add_argument("--json-out", help="把采集结果写到这个文件")
    ap.add_argument("--compare", nargs="+", metavar="JSON", help="比对两份或更多采集结果")
    ap.add_argument("--quiet", action="store_true", help="采集模式不打印 JSON")
    args = ap.parse_args(argv)

    if args.compare:
        return compare(args.compare)

    try:
        report = collect(args.base, args.timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print("采集失败：%s（%s）" % (args.base, exc), file=sys.stderr)
        return 2

    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    if not args.quiet:
        print(text)
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
