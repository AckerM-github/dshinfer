from __future__ import annotations

from ..config import Settings
from .base import Engine, EngineError, GenRequest, TokenEvent, Usage
from .manager import EngineManager
from .mock import MockEngine

__all__ = [
    "Engine",
    "EngineError",
    "EngineManager",
    "GenRequest",
    "TokenEvent",
    "Usage",
    "MockEngine",
    "build_engine",
]


def build_engine(settings: Settings, gguf_path: str | None = None) -> Engine:
    """按配置选择后端。gguf_path 由 EngineManager 在切换模型时传入。"""
    if settings.engine == "mock":
        return MockEngine(settings)
    if settings.engine in ("llamacpp", "llama-server"):
        from .llamacpp_server import LlamaServerEngine

        return LlamaServerEngine(settings, gguf_path=gguf_path)
    if settings.engine in ("inproc", "llama.cpp-inproc"):
        from .inproc import InprocEngine

        return InprocEngine(settings, gguf_path=gguf_path)
    raise EngineError(f"未知引擎: {settings.engine}")
