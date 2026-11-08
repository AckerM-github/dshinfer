"""Mock 引擎：不加载任何权重，按固定速率吐字。

它的作用是把「服务层」和「模型」解耦：API、流式、取消、背压、指标先在它上面跑通，
之后换上 llama.cpp 后端，上层代码不需要改。
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from ..config import Settings
from .base import GenRequest, TokenEvent, Usage


class MockEngine:
    name = "mock"

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._aborted: set[str] = set()

    async def start(self) -> None:  # 没有权重要加载
        return None

    async def stop(self) -> None:
        self._aborted.clear()

    async def abort(self, request_id: str) -> None:
        self._aborted.add(request_id)

    async def health(self) -> dict[str, object]:
        return {"engine": self.name, "ready": True}

    async def stream(self, req: GenRequest) -> AsyncIterator[TokenEvent]:
        self._aborted.discard(req.request_id)
        await asyncio.sleep(self._s.mock_ttft_ms / 1000)

        text = self._s.mock_response
        interval = 1.0 / max(self._s.mock_tokens_per_second, 1.0)
        out = ""
        n = 0
        finish: str | None = None

        for i in range(0, len(text), 2):  # 两个汉字一片，近似一个 token
            if req.request_id in self._aborted:
                return  # 客户端已断开：不发收尾包，直接结束
            if n >= req.max_tokens:
                finish = "length"
                break

            piece = text[i : i + 2]
            candidate = out + piece
            # 停止串可能出现在这一片的中间：只吐到它之前，不能把停止串发出去
            cut = min(
                (candidate.find(s) for s in req.stop if s and s in candidate),
                default=-1,
            )
            if cut != -1:
                head = candidate[max(cut, len(out)) : cut]
                if head:
                    out = candidate[:cut]
                    n += 1
                    yield TokenEvent(text=head)
                finish = "stop"
                break

            out = candidate
            n += 1
            yield TokenEvent(text=piece)
            await asyncio.sleep(interval)

        if req.request_id in self._aborted:
            return
        yield TokenEvent(
            text="",
            finish_reason=finish or "stop",
            usage=Usage(prompt_tokens=max(len(req.prompt) // 3, 1), completion_tokens=n),
        )
