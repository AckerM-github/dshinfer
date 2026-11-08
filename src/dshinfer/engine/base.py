"""引擎契约：服务层只认识这几个类型，不关心底层是 mock、llama.cpp 还是自研 kernel。

换后端 = 新写一个 Engine 实现，上层一行不改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol, runtime_checkable


class EngineError(RuntimeError):
    """引擎层的可预期错误（加载失败、显存不足、上下文超限……）。"""


@dataclass(slots=True)
class GenRequest:
    request_id: str
    prompt: str  # 已经渲染好模板的纯文本
    max_tokens: int
    temperature: float = 0.0
    top_p: float = 1.0
    stop: list[str] = field(default_factory=list)
    seed: int | None = None
    # 调度器希望这条请求落到哪个槽位（引擎支持就照办，不支持就忽略）
    slot: int | None = None


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 其中有多少是命中前缀缓存、没有真正重算的（OpenAI 的 prompt_tokens_details.cached_tokens）
    cached_tokens: int = 0
    # 这次真正喂进前向的 prompt token 数（prompt_tokens - cached_tokens）
    evaluated_tokens: int = 0
    # 引擎自己报的耗时（llama-server 的 timings，毫秒）。缺省 0 表示后端没提供。
    # 这两个数比服务层测的 TTFT/decode 更"干净"：不含 SSE 编码和网络。
    prompt_ms: float = 0.0
    decode_ms: float = 0.0


@dataclass(slots=True)
class TokenEvent:
    text: str
    token_id: int | None = None
    finish_reason: str | None = None  # "stop" | "length" | None
    usage: Usage | None = None


@runtime_checkable
class Engine(Protocol):
    """所有后端的统一接口。

    生命周期：start() → stream()* → stop()
    stream() 是异步生成器；客户端断开时上层会调用 abort(request_id)。
    """

    name: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def stream(self, req: GenRequest) -> AsyncIterator[TokenEvent]: ...

    async def abort(self, request_id: str) -> None: ...

    async def health(self) -> dict[str, object]: ...
