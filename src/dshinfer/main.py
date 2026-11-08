"""进程入口：python -m dshinfer.main  或  dshinfer / uvicorn dshinfer.api:app"""

from __future__ import annotations

import uvicorn

from .api import create_app
from .config import get_settings


def run() -> None:
    s = get_settings()
    uvicorn.run(
        create_app(s),
        host=s.host,
        port=s.port,
        log_config=None,  # 用我们自己的 structlog
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    run()
