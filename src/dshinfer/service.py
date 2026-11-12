"""服务层：把「API 请求」翻译成「引擎请求」，再把引擎吐出的事件翻译成 OpenAI 格式。

这里是所有工程策略的落点：
  · 准入调度（优先级 / 老化 / 租户配额 / 前缀亲和，见 scheduler.py）
  · 首字延迟 / 端到端延迟 / 排队等待 / 令牌数 埋点
  · 客户端断开 → 立刻 abort 引擎，不浪费算力
  · 流式与非流式共用同一条事件流，保证两者行为一致
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import AsyncIterator

import jinja2
import structlog

from .config import Settings
from .engine import Engine, GenRequest, TokenEvent
from .metrics import DECODE_SECONDS, INFLIGHT, LATENCY, REQUESTS, TOKENS, TTFT
from .scheduler import Grant, QueueFull, Scheduler
from .schemas import ChatCompletionRequest
from .toolcall import parse_tool_calls
from .trace import RequestTrace, TraceStore

log = structlog.get_logger()

__all__ = ["ChatService", "QueueFull", "Prepared", "render_prompt"]

# "<tool_call" 的长度减一：工具标记可能横跨 chunk，先扣住这么多字符不吐
# 10 = len("<tool_call>") - 1：保证任何部分匹配的标记都还留在未发出的尾巴里，
# 否则前缀被当成正文吐出去后，尾部拼不回来，parse_tool_calls 会漏掉这个调用。
_TAIL_HOLD = 10


def _new_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex[:24]


def _raise(msg: str) -> None:
    raise RuntimeError(msg)


@lru_cache(maxsize=4)
def _load_template(path: str) -> jinja2.Template | None:
    """加载 GGUF 里自带的 chat 模板（jinja2 语法，和 transformers 用的是同一份）。"""
    p = Path(path)
    if not p.exists():
        return None
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)
    env.filters["tojson"] = lambda x, **k: json.dumps(x, ensure_ascii=False)
    env.globals["raise_exception"] = _raise
    return env.from_string(p.read_text(encoding="utf-8"))


def render_prompt(
    messages,
    *,
    template_path: str = "",
    enable_thinking: bool = False,
    tools: list[dict] | None = None,
) -> str:
    """把 messages 渲染成模型真正认识的模板。

    优先用模型自带模板（`chat_template.jinja`，从 GGUF 元数据里抠出来的），
    这样 `<|im_start|>` / `<think>` / 工具调用格式都和训练时一致；
    模板缺失或渲染失败时退回最朴素的 ChatML。
    """
    tpl = _load_template(template_path) if template_path else None
    if tpl is not None:
        try:
            # 直接传 pydantic 对象而不是 dict：模板里写的是
            # `{% if message.tool_calls and message.tool_calls is iterable %}`，
            # dict 缺这个键时 jinja 的 `is iterable` 会对 Undefined 调 iter()，
            # 抛 UndefinedError 让整个模板渲染失败、退回到没有 <think> 护栏的
            # 裸 ChatML —— 模型就会自己开一个思考块，把正文挤掉。
            return tpl.render(
                messages=messages,
                add_generation_prompt=True,
                tools=tools or None,
                enable_thinking=enable_thinking,
            )
        except Exception as exc:  # 模板里 raise_exception 之类的
            log.warning("chat_template_failed", error=str(exc))

    parts: list[str] = []
    for m in messages:
        role = "system" if m.role == "developer" else m.role
        parts.append(f"<|im_start|>{role}\n{m.content or ''}<|im_end|>\n")
    parts.append("<|im_start|>assistant\n")
    return "".join(parts)


@dataclass(slots=True)
class Prepared:
    """准入之前就把渲染和参数解析做完：前缀哈希要在调度时就能用上。"""

    prompt: str
    prefix_key: str
    priority: int
    tenant: str
    model: str
    max_tokens: int
    temperature: float
    top_p: float
    stop: list[str]
    seed: int | None
    include_usage: bool
    tools: list[dict] | None = None
    # 这条请求的时间线；None 表示调用方没开 trace（比如内部调用）
    trace: RequestTrace | None = None


class ThinkSplitter:
    """把 `<think>…</think>` 里的推理过程从正文里剥出来。

    模板在开思考时以 `<think>\\n` 结尾，模型接着写推理，最后自己吐 `</think>`。
    不剥的话这段推理会原样进 `content`：实测「小明比小红大 3 岁…」的回答前 200 字
    全是英文推导，答案被挤到 282 字之后——在 DSH 里看起来就像模型在胡言乱语。

    标记可能横跨两个 chunk，所以未闭合时留 `len(MARK)-1` 个字符在手里。
    """

    MARK = "</think>"
    HOLD = len(MARK) - 1

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.closed = not enabled
        self.buf = ""

    def feed(self, text: str) -> list[tuple[str, str]]:
        """喂一段新文本，返回 [(kind, text)]，kind ∈ {reasoning, content}。"""
        if not self.enabled or self.closed:
            return [("content", text)] if text else []
        self.buf += text
        idx = self.buf.find(self.MARK)
        if idx >= 0:
            head, tail = self.buf[:idx], self.buf[idx + len(self.MARK) :]
            self.buf = ""
            self.closed = True
            out: list[tuple[str, str]] = []
            if head:
                out.append(("reasoning", head))
            if tail:
                out.append(("content", tail.lstrip("\n")))
            return out
        if len(self.buf) > self.HOLD:
            head, self.buf = self.buf[: -self.HOLD], self.buf[-self.HOLD :]
            return [("reasoning", head)]
        return []

    def flush(self) -> list[tuple[str, str]]:
        """收尾：没等到 `</think>` 就把剩下的按推理算，别丢字。"""
        if not self.buf:
            return []
        rest, self.buf = self.buf, ""
        return [("content" if self.closed else "reasoning", rest)]


def _prefix_key(prompt: str, session: str = "") -> str:
    """槽位亲和键：同一个会话尽量落到同一个 slot，才能吃到 KV 缓存。

    优先用调用方给的会话号（`X-Session-Id`）——外接 agent 的每一步都是
    「上一轮对话 + 新内容」，同一会话必须复用同一个 slot 才不会整段重算。
    没给会话号时退回提示词前 512 字符：多轮对话的系统提示词在最前面、
    其后内容逐轮变长，这个指纹在一段对话内是稳定的。
    """
    if session:
        return hashlib.sha1(f"s:{session}".encode("utf-8")).hexdigest()[:16]
    return hashlib.sha1(prompt[:512].encode("utf-8")).hexdigest()[:16]


def _tools_for(req: ChatCompletionRequest) -> list[dict] | None:
    """按 tool_choice 决定要不要把工具 schema 拼进提示词。

    - 没给 tools → None（模板就不会拼工具段）
    - tool_choice="none" → None（明确禁止调工具）
    - tool_choice 指定函数名 → 只拼这一个（相当于强制）
    - 其余（auto / 缺省）→ 全拼，模型自己决定
    """
    if not req.tools:
        return None
    choice = req.tool_choice
    if choice == "none":
        return None
    tools = [t.model_dump(exclude_none=True) for t in req.tools]
    if isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name")
        if name:
            picked = [t for t in tools if t["function"]["name"] == name]
            return picked or tools
    return tools


class ChatService:
    def __init__(
        self, engine: Engine, settings: Settings, traces: TraceStore | None = None
    ) -> None:
        self.engine = engine
        self.s = settings
        self.traces = traces or TraceStore()
        self.scheduler = Scheduler(
            max_concurrency=settings.max_concurrency,
            queue_limit=settings.queue_limit,
            queue_timeout_s=settings.queue_timeout_s,
            aging_s=settings.aging_s,
            per_tenant_inflight=settings.per_tenant_inflight,
            per_tenant_queue=settings.per_tenant_queue,
        )

    async def start(self) -> None:
        await self.engine.start()
        log.info("engine_ready", engine=self.engine.name)

    async def stop(self) -> None:
        await self.engine.stop()

    # ---------------- 准入 ----------------

    def prepare(
        self,
        req: ChatCompletionRequest,
        *,
        priority: int = 0,
        tenant: str = "default",
        session: str = "",
        trace: RequestTrace | None = None,
    ) -> Prepared:
        tools = _tools_for(req)
        prompt = render_prompt(
            req.messages,
            template_path=self.s.chat_template_path,
            enable_thinking=self.s.enable_thinking,
            tools=tools,
        )
        if trace is not None:
            trace.mark("rendered")
        return Prepared(
            prompt=prompt,
            prefix_key=_prefix_key(prompt, session),
            priority=priority,
            tenant=tenant,
            model=req.model or self.s.model_name,
            max_tokens=req.token_budget or self.s.default_max_tokens,
            temperature=(
                req.temperature if req.temperature is not None else self.s.default_temperature
            ),
            top_p=req.top_p if req.top_p is not None else self.s.default_top_p,
            stop=req.stop_list,
            seed=req.seed,
            include_usage=bool(req.stream_options and req.stream_options.include_usage),
            tools=tools,
            trace=trace,
        )

    async def admit(self, prep: Prepared) -> Grant:
        """拿槽位；拿不到抛 QueueFull（路由层据此返回 429）。"""
        grant = await self.scheduler.acquire(
            _new_id(),
            priority=prep.priority,
            tenant=prep.tenant,
            prefix_key=prep.prefix_key,
        )
        INFLIGHT.inc()
        if prep.trace is not None:
            # 队列等待 = 从进路由到拿到槽位；包含调度器的老化/配额判断
            prep.trace.mark("admitted")
        return grant

    def _release(self, grant: Grant) -> None:
        INFLIGHT.dec()
        self.scheduler.release(grant)

    # ---------------- 对外接口 ----------------

    async def stats(self) -> dict[str, object]:
        return {
            "inflight": self.scheduler.stats()["inflight"],
            "queued": self.scheduler.stats()["queued"],
            "max_concurrency": self.s.max_concurrency,
            "queue_limit": self.s.queue_limit,
            "scheduler": self.scheduler.stats(),
            "engine": await self.engine.health(),
        }

    async def stream_chat(
        self, prep: Prepared, grant: Grant, *, always_usage: bool = False
    ) -> AsyncIterator[dict]:
        """流式：逐块产出 OpenAI 的 chat.completion.chunk。

        调用前必须先 admit(prep) 拿到 grant：这样「队列满了」能在响应头发出去之前变成 429。
        """
        cid = grant.req_id
        created = int(time.time())
        model = prep.model
        include_usage = prep.include_usage

        tr = prep.trace
        if tr is not None and not tr.id:
            tr.id = cid
        if tr is not None:
            tr.model = model
            tr.tenant = prep.tenant
            tr.priority = prep.priority

        started = time.perf_counter()
        first_token_at: float | None = None
        completion_tokens = 0
        usage = None
        status = "ok"
        finished = False

        gen = GenRequest(
            request_id=cid,
            prompt=prep.prompt,
            max_tokens=prep.max_tokens,
            temperature=prep.temperature,
            top_p=prep.top_p,
            stop=prep.stop,
            seed=prep.seed,
        )

        tool_mode = bool(prep.tools)
        buf: list[str] = []
        body_parts: list[str] = []
        splitter = ThinkSplitter(self.s.enable_thinking)
        emitted = 0
        pending_finish: str | None = None
        tool_calls: list[dict] = []

        try:
            # 开头先给 role，和 OpenAI 一致
            yield {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }

            async with asyncio.timeout(self.s.request_timeout_s):
                if tr is not None:
                    tr.mark("engine_start")
                async for ev in self.engine.stream(gen):
                    if ev.usage is not None:
                        usage = ev.usage
                    if ev.text:
                        completion_tokens += 1
                        if tool_mode:
                            # 带工具时必须先攒着：`<tool_call>` 可能横跨多个 chunk，
                            # 只能先放出去尾巴之前的部分，剩下的等确定不是工具块再吐。
                            # 发之前还要过 ThinkSplitter：开思考时推理段属于
                            # reasoning_content，不能当正文流出去。
                            buf.append(ev.text)
                            joined = "".join(buf)
                            cut = joined.find("<tool_call>")
                            if cut < 0:
                                cut = max(len(joined) - _TAIL_HOLD, 0)
                            if cut > emitted:
                                for kind, piece in splitter.feed(joined[emitted:cut]):
                                    if not piece:
                                        continue
                                    if first_token_at is None:
                                        first_token_at = time.perf_counter()
                                        TTFT.observe(first_token_at - started)
                                        if tr is not None:
                                            tr.mark("first_token")
                                    if kind == "content":
                                        body_parts.append(piece)
                                    delta = (
                                        {"reasoning_content": piece}
                                        if kind == "reasoning"
                                        else {"content": piece}
                                    )
                                    yield {
                                        "id": cid,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": model,
                                        "choices": [
                                            {"index": 0, "delta": delta, "finish_reason": None}
                                        ],
                                    }
                                emitted = cut
                        else:
                            for kind, piece in splitter.feed(ev.text):
                                if not piece:
                                    continue
                                if first_token_at is None:
                                    first_token_at = time.perf_counter()
                                    TTFT.observe(first_token_at - started)
                                    if tr is not None:
                                        tr.mark("first_token")
                                delta = {"reasoning_content": piece} if kind == "reasoning" else {"content": piece}
                                yield {
                                    "id": cid,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": model,
                                    "choices": [
                                        {"index": 0, "delta": delta, "finish_reason": None}
                                    ],
                                }
                    if ev.finish_reason is not None:
                        finished = True
                        pending_finish = ev.finish_reason

            if tr is not None:
                tr.mark("last_token")

            if tool_mode:
                joined = "".join(buf)
                tail = joined[emitted:]
                cut_tool = tail.find("<tool_call>")
                visible_tail = tail if cut_tool < 0 else tail[:cut_tool]
                for kind, piece in [*splitter.feed(visible_tail), *splitter.flush()]:
                    if not piece:
                        continue
                    if kind == "content":
                        body_parts.append(piece)
                    delta = (
                        {"reasoning_content": piece} if kind == "reasoning" else {"content": piece}
                    )
                    yield {
                        "id": cid,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    }
                body = "".join(body_parts)
                if cut_tool >= 0:
                    body += tail[cut_tool:]
                _, tool_calls = parse_tool_calls(body)
                if tool_calls:
                    yield {
                        "id": cid,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {"index": i, **call} for i, call in enumerate(tool_calls)
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                    pending_finish = "tool_calls"

            yield {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": pending_finish or "stop"}
                ],
            }

            if include_usage or always_usage:
                u = usage or _usage_from(gen.prompt, completion_tokens)
                yield {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": u.prompt_tokens,
                        "completion_tokens": u.completion_tokens,
                        "total_tokens": u.prompt_tokens + u.completion_tokens,
                        # OpenAI 同名字段：命中前缀缓存、没有真正重算的 token
                        "prompt_tokens_details": {"cached_tokens": u.cached_tokens},
                    },
                }
        except TimeoutError:
            status = "timeout"
            raise
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            if not finished:
                # 客户端断开 / 超时 / 异常：通知引擎停下，别继续占算力
                await self.engine.abort(cid)
            LATENCY.observe(time.perf_counter() - started)
            TOKENS.labels(direction="output").inc(completion_tokens)
            if usage is not None:
                TOKENS.labels(direction="input").inc(usage.prompt_tokens)
                TOKENS.labels(direction="cached").inc(usage.cached_tokens)
                if usage.decode_ms > 0:
                    DECODE_SECONDS.inc(usage.decode_ms / 1000.0)
            REQUESTS.labels(endpoint="chat", status=status, stream="true").inc()
            self._release(grant)
            if tr is not None:
                tr.mark("done")
                tr.status = status
                tr.finish_reason = pending_finish
                if usage is not None:
                    tr.tokens = {
                        "prompt": usage.prompt_tokens,
                        "completion": usage.completion_tokens,
                        "cached": usage.cached_tokens,
                    }
                    tr.engine_ms = {
                        "prompt": round(usage.prompt_ms, 1),
                        "decode": round(usage.decode_ms, 1),
                    }
                else:
                    tr.tokens = {"completion": completion_tokens}
                self.traces.add(tr)
                # 一行结构化日志 = 一条完整时间线，grep 一个 id 就能复盘
                log.info("request_trace", **tr.summary())
            log.info(
                "request_done",
                id=cid,
                status=status,
                completion_tokens=completion_tokens,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            )

    async def complete_chat(self, prep: Prepared, grant: Grant) -> dict:
        """非流式：消费同一条事件流，拼成完整响应。

        准入由调用方（api 路由）统一做一次，这里不再 admit——否则槽位会被重复占用。
        """
        text: list[str] = []
        reasoning: list[str] = []
        calls: list[dict] = []
        finish_reason = "stop"
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        cid = model = None
        created = int(time.time())

        async for chunk in self.stream_chat(prep, grant, always_usage=True):
            cid = chunk["id"]
            model = chunk["model"]
            created = chunk["created"]
            if chunk.get("usage"):
                usage = chunk["usage"]
            for ch in chunk["choices"]:
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    text.append(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning.append(delta["reasoning_content"])
                if delta.get("tool_calls"):
                    calls.extend(delta["tool_calls"])
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]

        content = "".join(text)
        # 有工具调用时，正文里通常只剩一句「我查一下」加换行，去掉尾巴更干净
        message: dict = {
            "role": "assistant",
            "content": (content.strip() if calls else content) or None,
        }
        think = "".join(reasoning).strip()
        if think:
            # 非标准字段，但 deepseek / vllm / llama.cpp 都用这个名字，客户端一般会忽略
            message["reasoning_content"] = think
        if calls:
            clean_calls = []
            for call in calls:
                call = dict(call)
                call.pop("index", None)
                clean_calls.append(call)
            message["tool_calls"] = clean_calls

        return {
            "id": cid,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage,
        }


def _usage_from(prompt: str, completion_tokens: int):
    from .engine import Usage

    return Usage(prompt_tokens=max(len(prompt) // 3, 1), completion_tokens=completion_tokens)
