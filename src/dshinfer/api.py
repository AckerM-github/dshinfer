"""HTTP 层：OpenAI 兼容的 /v1/chat/completions + 健康检查 + 指标。

路由只做三件事：校验、准入、把服务层的事件流转成 SSE 或 JSON。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import orjson
import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel

from .config import Settings, get_settings
from .dashboard import PAGE as DASHBOARD_PAGE
from .dashboard import live as live_snapshot
from .engine import EngineError, EngineManager, build_engine
from .logging import configure_logging
from .metrics import render
from .registry import ModelNotFound, ModelRegistry
from .schemas import ChatCompletionRequest
from .service import ChatService, QueueFull
from .trace import RequestTrace

log = structlog.get_logger()

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _error(status: int, message: str, etype: str, code: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": etype, "code": code}}, status_code=status
    )


class LoadModelRequest(BaseModel):
    id: str


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()
    configure_logging(s.log_level)

    registry = ModelRegistry(
        s.models_dir,
        default_path=s.gguf_path,
        default_id=s.model_name,
        extra_paths=(s.gguf_path,),
    )
    manager = EngineManager(s, registry, lambda path: build_engine(s, gguf_path=path))
    service = ChatService(manager, s)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.start()
        log.info("server_start", model=s.model_name, engine=s.engine, port=s.port)
        try:
            yield
        finally:
            await service.stop()
            log.info("server_stop")

    app = FastAPI(
        title="dshinfer",
        version="0.1.0",
        description="DeepSeek-V4-Pro-Qwen3.5-9B 推理服务层",
        lifespan=lifespan,
    )
    app.state.service = service
    app.state.settings = s

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError):
        # OpenAI 客户端习惯 400 + error 对象，而不是 FastAPI 默认的 422 列表
        detail = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        return _error(400, detail or "请求格式错误", "invalid_request_error", "invalid_request")

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        t0 = time.perf_counter()
        response = await call_next(request)
        log.info(
            "http",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
        )
        return response

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "engine": await manager.health()}

    @app.get("/v1/models")
    async def list_models():
        loaded = manager.current
        entries = registry.scan()
        if entries:
            data = [e.to_dict(loaded=bool(loaded and loaded.path == e.path)) for e in entries]
            # context_length 是"这个服务当前能给你多少"，与模型自带的 n_ctx_train
            # （训练上下文 262144）不是一回事——受本机显存与 KV 量化档限制。
            for row in data:
                row["context_length"] = s.max_context_tokens
        else:
            # mock 引擎 / 模型目录还没建：至少把配置里的默认模型报出来
            data = [
                {
                    "id": s.model_name,
                    "object": "model",
                    "created": 0,
                    "owned_by": "local",
                    "loaded": loaded is not None,
                    "context_length": s.max_context_tokens,
                }
            ]
        return {"object": "list", "data": data}

    @app.get("/admin/models")
    async def admin_models():
        loaded = manager.current
        return {
            "loaded": loaded.id if loaded else None,
            "state": await manager.health(),
            "data": [
                e.to_dict(loaded=bool(loaded and loaded.path == e.path))
                for e in registry.scan()
            ],
        }

    @app.post("/admin/models/load")
    async def load_model(body: LoadModelRequest):
        """热切换模型：卸载当前、加载目标。有请求在飞时拒绝，避免把流打断。"""
        stats = await service.stats()
        if stats.get("inflight"):
            return _error(409, "有请求正在处理，等它们结束后再切换模型", "conflict", "busy")
        try:
            entry = await manager.ensure(body.id)
        except ModelNotFound as exc:
            return _error(404, str(exc), "invalid_request_error", "model_not_found")
        except EngineError as exc:
            return _error(500, str(exc), "server_error", "model_load_failed")
        return {"loaded": entry.to_dict(loaded=True), "load_seconds": manager.load_seconds}

    @app.post("/admin/models/unload")
    async def unload_model():
        stats = await service.stats()
        if stats.get("inflight"):
            return _error(409, "有请求正在处理，等它们结束后再卸载", "conflict", "busy")
        await manager.stop()
        return {"loaded": None}

    @app.get("/admin/stats")
    async def admin_stats():
        return await service.stats()

    @app.get("/admin/traces")
    async def admin_traces(limit: int = 20):
        """最近 N 条请求的时间线摘要（新的在前）。进程内环形缓冲，重启即清空。"""
        return {"data": service.traces.recent(max(1, min(limit, 200)))}

    @app.get("/admin/traces/{trace_id}")
    async def admin_trace(trace_id: str):
        """单条请求的完整时间线：每个打点的绝对偏移 + 派生 span。"""
        tr = service.traces.get(trace_id)
        if tr is None:
            return _error(404, f"没有这条 trace：{trace_id}", "invalid_request_error", "trace_not_found")
        return tr.detail()

    @app.get("/metrics")
    async def metrics():
        body, ctype = render()
        return Response(content=body, media_type=ctype)

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard_page():
        """内置监控面板（单页，无外部依赖）。数据走 /admin/live。"""
        return HTMLResponse(DASHBOARD_PAGE)

    @app.get("/admin/live")
    async def admin_live():
        """面板用的一次性快照：服务层指标 + 引擎 /metrics + GPU（NVML）。

        引擎抓取和 NVML 都是同步调用，放线程池里跑，别卡住事件循环。
        """
        engine_url = (
            f"http://127.0.0.1:{s.engine_port}/metrics" if s.engine == "llamacpp" else None
        )
        return await asyncio.to_thread(
            live_snapshot,
            engine_url=engine_url,
            model=s.model_name,
            context_tokens=s.n_ctx,
            max_context_tokens=s.max_context_tokens,
            engine_kind=s.engine,
            max_concurrency=s.max_concurrency,
            queue_limit=s.queue_limit,
            health=await manager.health(),
        )

    @app.post("/v1/chat/completions")
    async def chat_completions(req: ChatCompletionRequest, request: Request):
        if req.n != 1:
            return _error(400, "只支持 n=1", "invalid_request_error", "unsupported_n")

        # 优先级/租户走请求头：OpenAI 协议里没有这两个概念，但线上必须能区分
        # 「交互式对话」和「离线批量」——否则一个批处理任务就能把对话延迟拖到几秒。
        try:
            priority = int(request.headers.get("x-priority", "0"))
        except ValueError:
            priority = 0
        tenant = request.headers.get("x-tenant", "default")[:64]
        # 会话号决定槽位亲和：同一段多轮对话必须落在同一个 slot 上，
        # 否则每一步都要把整段历史重新预填充（实测 6 会话并发时命中率 0–29%）。
        session = request.headers.get("x-session-id", "")[:64]

        # 时间线从「请求进到路由」开始记：客户端可以自带 X-Request-Id 方便对齐日志，
        # 不给就用服务端生成的 chatcmpl- 号。
        trace = RequestTrace(
            id=request.headers.get("x-request-id", "")[:64], tenant=tenant, priority=priority
        )
        prep = service.prepare(
            req, priority=priority, tenant=tenant, session=session, trace=trace
        )
        try:
            grant = await service.admit(prep)
        except QueueFull as e:
            return _error(429, str(e), "rate_limit_error", "queue_full")

        if not trace.id:
            trace.id = grant.req_id

        if req.stream:
            return StreamingResponse(
                _sse(service.stream_chat(prep, grant)),
                media_type="text/event-stream",
                headers={**SSE_HEADERS, "X-Request-Id": trace.id},
            )

        try:
            payload = await service.complete_chat(prep, grant)
        except EngineError as e:
            return _error(503, str(e), "engine_error", "engine_unavailable")
        except TimeoutError:
            return _error(504, "生成超时", "timeout_error", "request_timeout")
        # Server-Timing 让 curl -D 直接看到排队/首字/总耗时，不用去翻 /admin/traces
        return JSONResponse(
            payload,
            headers={
                "X-Request-Id": trace.id,
                "Server-Timing": _server_timing(trace),
            },
        )

    return app


def _server_timing(trace: RequestTrace) -> str:
    """RFC 8672 的 Server-Timing：只报三个最关心的阶段，单位毫秒。"""
    spans = trace.summary()["spans_ms"]
    return ", ".join(
        f"{name};dur={spans[key]}"
        for name, key in (("queue", "queue"), ("ttft", "ttft"), ("total", "total"))
        if key in spans
    )


async def _sse(gen):
    """把 chunk 字典序列化成 SSE；[DONE] 是 OpenAI 客户端的结束信号。"""
    try:
        async for chunk in gen:
            yield b"data: " + orjson.dumps(chunk) + b"\n\n"
        yield b"data: [DONE]\n\n"
    except asyncio.CancelledError:  # 客户端断开
        raise
    except Exception as e:  # 流已开始，只能以错误帧结束
        log.exception("stream_failed", error=str(e))
        yield b"data: " + orjson.dumps(
            {"error": {"message": str(e), "type": "server_error"}}
        ) + b"\n\n"
        yield b"data: [DONE]\n\n"


app = create_app()
