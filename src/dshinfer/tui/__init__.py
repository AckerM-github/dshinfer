"""终端监控面板（Textual TUI）：启动向导 + 运行监视器。

模块分工：

    preset.py   预设数据模型：向导产出、可保存复用、可导出 compose 环境变量
    probe.py    系统原生数据采集（NVML / procfs / Win32），纯标准库
    live.py     把 probe + 引擎/服务指标拼成一次快照 Snapshot
    widgets.py  显示零件：KPI 卡、条形表、表格工具、格式化
    wizard.py   10 步启动向导
    monitor.py  7 视图运行监视器
    app.tcss    样式

入口：python -m dshinfer.tui（或 pip 装出来的 dshinfer-tui 命令）。
"""

from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """延迟导入 __main__，避免 import dshinfer.tui 时就把 textual 拉起来。"""
    from .__main__ import main as _main

    return _main(argv)
