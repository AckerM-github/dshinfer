"""模型管理器：同一时刻只常驻一个模型，按需切换。

为什么只留一个：8 GB 显存里 Q4 权重就要 5.24 GB，两个模型起不来。Ollama 在同样
的显存约束下也是"换模型就卸载旧的"，这里显式把这层做成可观测的状态机：

    idle ──ensure(id)──> loading ──ok──> ready ──ensure(id2)──> stopping ──> loading …

它自己实现 Engine 协议（start/stop/stream/abort/health），所以 service 层完全
不需要知道"模型是可换的"这件事 —— 换模型只影响谁来处理 stream。
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Callable

import structlog

from ..config import Settings
from ..registry import ModelEntry, ModelNotFound, ModelRegistry
from .base import Engine, EngineError, GenRequest, TokenEvent

log = structlog.get_logger()

__all__ = ["EngineManager"]

Builder = Callable[[str], Engine]


class EngineManager:
    def __init__(self, settings: Settings, registry: ModelRegistry, builder: Builder) -> None:
        self._s = settings
        self._registry = registry
        self._builder = builder
        self._lock = asyncio.Lock()
        self._engine: Engine | None = None
        self._entry: ModelEntry | None = None
        self._loading = False
        self._last_error: str | None = None
        self._load_seconds: float = 0.0

    # ---- 状态 ----
    @property
    def name(self) -> str:
        if self._entry is None:
            return f"{self._s.engine}:none"
        return f"{self._s.engine}:{self._entry.id}"

    @property
    def current_id(self) -> str | None:
        return self._entry.id if self._entry else None

    @property
    def current(self) -> ModelEntry | None:
        return self._entry

    @property
    def load_seconds(self) -> float:
        return self._load_seconds

    async def start(self) -> None:
        await self.ensure(self._s.model_name)

    async def ensure(self, model_id: str) -> ModelEntry:
        """确保 model_id 已加载；已是当前模型则直接返回。"""
        try:
            entry = self._registry.resolve(model_id)
        except ModelNotFound:
            if self._s.engine != "mock":
                raise
            # mock 引擎不读文件，给一个占位条目让整条链路照常跑通
            entry = ModelEntry(id=model_id or "mock", path=f"mock://{model_id}", size_bytes=0)

        async with self._lock:
            if self._entry is not None and self._entry.path == entry.path and self._engine:
                return self._entry

            self._loading = True
            self._last_error = None
            t0 = asyncio.get_running_loop().time()
            try:
                if self._engine is not None:
                    log.info("model_unload", model=self._entry.id if self._entry else None)
                    await self._engine.stop()
                    self._engine = None
                    self._entry = None

                log.info("model_load", model=entry.id, path=entry.path, size_gb=entry.size_gb)
                engine = self._builder(entry.path)
                await engine.start()
                self._engine = engine
                self._entry = entry
                self._load_seconds = round(asyncio.get_running_loop().time() - t0, 2)
                log.info("model_ready", model=entry.id, load_s=self._load_seconds)
                return entry
            except Exception as exc:  # noqa: BLE001 - 要把原因带回给调用方
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.error("model_load_failed", model=entry.id, error=self._last_error)
                raise EngineError(f"加载模型失败: {self._last_error}") from exc
            finally:
                self._loading = False

    async def stop(self) -> None:
        async with self._lock:
            if self._engine is not None:
                await self._engine.stop()
            self._engine = None
            self._entry = None

    # ---- Engine 协议 ----
    async def stream(self, req: GenRequest) -> AsyncIterator[TokenEvent]:
        engine = self._engine
        if engine is None:
            raise EngineError("模型未加载")
        async for ev in engine.stream(req):
            yield ev

    async def abort(self, request_id: str) -> None:
        if self._engine is not None:
            await self._engine.abort(request_id)

    async def health(self) -> dict[str, object]:
        inner: dict[str, object] = {}
        if self._engine is not None:
            try:
                inner = await self._engine.health()
            except Exception as exc:  # noqa: BLE001
                inner = {"ready": False, "error": str(exc)}
        return {
            "engine": self._s.engine,
            "model": self.current_id,
            "loading": self._loading,
            "last_error": self._last_error,
            "load_seconds": self._load_seconds,
            **inner,
        }
