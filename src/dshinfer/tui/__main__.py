"""python -m dshinfer.tui / dshinfer-tui 的入口。

三种用法：

    dshinfer-tui                     启动向导，走完 10 步直接进监视器
    dshinfer-tui --monitor           跳过向导，用最近一次保存的预设直接看盘
    dshinfer-tui --snapshot          不画界面，打印一次 JSON 快照（给脚本/CI 用）
    dshinfer-tui --dump-env          打印这份预设对应的 compose 环境变量片段

--snapshot 是刻意做的：TUI 要有终端才能跑，但"数据层通不通"必须能在任何地方验证，
所以把采集逻辑和渲染彻底分开，快照随时可以非交互地打出来。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import live
from .preset import Preset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dshinfer-tui",
        description="dshinfer 终端面板：启动向导 + 7 视图运行监视器（Textual）",
    )
    parser.add_argument("--preset", metavar="PATH", help="预设 JSON 路径（默认用最近保存的一份）")
    parser.add_argument("--monitor", action="store_true", help="跳过向导，直接进监视器")
    parser.add_argument("--wizard", action="store_true", help="只跑向导，不进监视器")
    parser.add_argument("--snapshot", action="store_true", help="打印一次 JSON 快照后退出")
    parser.add_argument("--dump-env", action="store_true", help="打印 compose 环境变量片段后退出")
    parser.add_argument("--local-env", action="store_true", help="--dump-env 时导出本机（非容器）路径")
    parser.add_argument("--service-url", help="服务层地址，默认 http://<host>:<port>")
    parser.add_argument("--interval", type=float, default=2.0, help="刷新间隔秒（默认 2）")
    return parser


def _load_preset(args: argparse.Namespace) -> Preset:
    if args.preset:
        return Preset.load(Path(args.preset))
    return Preset.latest()


def _service_url(args: argparse.Namespace, preset: Preset) -> str:
    if args.service_url:
        return args.service_url
    host = "127.0.0.1" if preset.host in ("", "0.0.0.0", "::") else preset.host
    return f"http://{host}:{preset.port}"


def _print_snapshot(args: argparse.Namespace, preset: Preset) -> int:
    snapshot = live.sample_snapshot(
        service_url=_service_url(args, preset),
        model=preset.model_stem,
        ctx_limit=preset.n_ctx,
        engine=preset.engine,
        model_info=live.load_model_info(preset.gguf_path),
    )
    payload = {
        "ts": snapshot.ts,
        "model": snapshot.model,
        "ctx_limit": snapshot.ctx_limit,
        "engine": snapshot.engine,
        "backend": snapshot.backend,
        "hardware_error": snapshot.hardware_error,
        "gpus": snapshot.gpus,
        "cpu": snapshot.cpu,
        "mem": snapshot.mem,
        "rate": snapshot.rate,
        "tokens": snapshot.tokens,
        "totals": snapshot.totals,
        "slots": snapshot.slots,
        "cache": snapshot.cache,
        "service_req": snapshot.service_req,
        "hist": snapshot.hist,
        "traces": len(snapshot.traces),
        "health": snapshot.health,
        "model_info": snapshot.model_info,
        "errors": snapshot.errors,
    }
    # dataclass + float 混着来，default 兜住 GPU 字段里的 None
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if not snapshot.errors else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        preset = _load_preset(args)
    except (OSError, ValueError) as exc:
        print(f"预设读不出来：{exc}", file=sys.stderr)
        return 2

    if args.dump_env:
        print(preset.env_block(container=not args.local_env))
        return 0

    if args.snapshot:
        return _print_snapshot(args, preset)

    try:
        from textual.app import App  # noqa: F401  - 只用来给出准确缺依赖提示
    except ImportError:
        print(
            "缺少 Textual：请安装 TUI 依赖（uv sync --extra tui 或 pip install 'dshinfer[tui]'）",
            file=sys.stderr,
        )
        return 3

    if not args.monitor:
        from .wizard import WizardApp

        wizard = WizardApp(preset)
        result = wizard.run()
        if result is None:
            print("向导已取消，未启动监视器。", file=sys.stderr)
            return 1
        preset = result
        if args.wizard:
            print(f"预设已保存：{preset.name}")
            return 0
    elif args.wizard:
        print("--monitor 与 --wizard 不能同时用", file=sys.stderr)
        return 2

    from .monitor import MonitorApp

    monitor = MonitorApp(
        preset,
        service_url=_service_url(args, preset),
        interval=args.interval,
    )
    monitor.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
