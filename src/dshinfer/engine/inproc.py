"""进程内 llama.cpp 引擎：用 ctypes 绑定直接调 `llama.dll`。

和 `llamacpp_server.py` 的分工
------------------------------
    llamacpp_server.py   把 `llama-server.exe` 当子进程黑盒 —— 调度、KV 分配、
                         多 slot 全是它的；我们只发 HTTP 收 token。
    inproc.py            在同一个进程里调 libllama —— 省掉 HTTP 与进程间拷贝，
                         KV / batch / 采样器全部可见。

收益与代价（都是实测得到的，不是理论）
-------------------------------------
收益：
  * 每请求少 20–30 ms（不再过 socket）
  * 一个 `llama_context` 就在手里，PagedAttention / 自定义 batch 这类实验才有可能
代价：
  * **并发 = 1**。一个 context 一次只能跑一个 decode，这里用 `threading.Lock` 串行化。
    llama-server 的 4 slot + `--kv-unified` 到这里就没了（`-np` 对它无效）。
  * **崩溃连坐**。C 层 access violation 会带走整个进程，靠 `supervise.ps1` 重启。
  * 没有 `/metrics` `/slots` `/id_slot`。指标只能在 Python 侧算。
  * 前缀复用走 `Llama.generate()` 内部的 `kv_cache_seq_rm`（`llama_cpp/llama.py:916-963`），
    语义和 llama-server 的 `cache_prompt` 一样，但只能逐 token 比较。

为什么不用它的 `create_chat_completion`
---------------------------------------
prompt 已经由服务层 `render_prompt()` 渲染好了（模板来自 GGUF 元数据），
再走一次 chat 接口会二次套模板。所以这里只用 `create_completion`（裸文本续写）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator

import structlog

from ..config import Settings
from .base import EngineError, GenRequest, TokenEvent, Usage

log = structlog.get_logger(__name__)

# src/dshinfer/engine/inproc.py -> 仓库根
_REPO_ROOT = Path(__file__).resolve().parents[3]
# vendor/llama_cpp：我们移植进来自己维护的那份（不存在时退回 pip 装的）
_VENDOR = _REPO_ROOT / "vendor"

# ggml_type 的数值，对应 llama_cpp/llama_cpp.py:96-101 那组常量
_GGML_TYPES = {
    "f32": 0,
    "f16": 1,
    "q4_0": 2,
    "q4_1": 3,
    "q5_0": 6,
    "q5_1": 7,
    "q8_0": 8,
}


def _ggml_type(name: str) -> int | None:
    """KV cache 类型名 -> ggml_type。None 表示用引擎默认（f16）。"""
    if not name or name.lower() in ("f16", "default"):
        return None
    try:
        return _GGML_TYPES[name.lower()]
    except KeyError:
        log.warning("inproc.unknown_cache_type", value=name)
        return None


class InprocEngine:
    """把同进程的 `llama_cpp.Llama` 包装成 `Engine`。"""

    name = "llama.cpp-inproc"

    def __init__(self, settings: Settings, gguf_path: str | None = None) -> None:
        self._s = settings
        # 由 EngineManager 在换模型时指定；缺省退回配置里的单模型路径
        self._gguf_path = gguf_path or settings.gguf_path
        self._llm: Any = None
        self._ready = False
        # 串行化放在线程里而不是 asyncio.Lock：即使消费者提前退出
        # （客户端断开、GeneratorExit），下一个请求也会等这条线程真正结束，
        # 不会两个线程同时进同一个 llama_context。
        self._thread_lock = threading.Lock()
        self._cancelled: set[str] = set()

    # ---------------------------------------------------------------- 生命周期

    async def start(self) -> None:
        model = Path(self._gguf_path)
        if not model.exists():
            raise EngineError(f"GGUF 不存在：{model}")
        t0 = time.monotonic()
        self._llm = await asyncio.to_thread(self._load, model)
        self._ready = True
        log.info(
            "inproc.ready",
            model=model.name,
            load_s=round(time.monotonic() - t0, 2),
            n_ctx=self._llm.n_ctx(),
        )

    def _load(self, model: Path) -> Any:
        # DLL 目录要在 import 之前给：llama_cpp 在 import 时就把 libllama 加载了
        if self._s.inproc_lib_dir:
            os.environ.setdefault("LLAMA_CPP_LIB_PATH", self._s.inproc_lib_dir)
        if _VENDOR.is_dir():
            vendor = str(_VENDOR)
            if vendor not in sys.path:
                sys.path.insert(0, vendor)  # 优先我们移植的那份，而不是 site-packages
        try:
            import llama_cpp
        except ImportError as exc:  # pragma: no cover - 环境问题
            raise EngineError(
                "import llama_cpp 失败：需要 vendor/llama_cpp 或 pip install llama-cpp-python"
            ) from exc

        if self._s.n_parallel > 1:
            log.warning(
                "inproc.single_sequence",
                n_parallel=self._s.n_parallel,
                hint="进程内引擎一次只能跑一个 decode，并发请求会在锁上排队",
            )

        return llama_cpp.Llama(
            model_path=str(model),
            n_ctx=self._s.n_ctx,
            n_batch=self._s.n_batch,
            n_ubatch=self._s.n_ubatch,
            n_threads=self._s.n_threads or None,
            n_gpu_layers=self._s.n_gpu_layers,
            flash_attn=self._s.flash_attn,
            type_k=_ggml_type(self._s.cache_type_k),
            type_v=_ggml_type(self._s.cache_type_v),
            verbose=False,
        )

    async def stop(self) -> None:
        llm, self._llm = self._llm, None
        self._ready = False
        if llm is not None:
            await asyncio.to_thread(llm.close)
            log.info("inproc.stopped")

    # ---------------------------------------------------------------- 生成

    async def stream(self, req: GenRequest) -> AsyncIterator[TokenEvent]:
        if not self._ready or self._llm is None:
            raise EngineError("引擎未就绪")

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._cancelled.discard(req.request_id)

        def emit(item: Any) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, item)

        def worker() -> None:
            try:
                with self._thread_lock:
                    self._run(req, emit)
            except BaseException as exc:  # noqa: BLE001 - 跨线程带回给调用方
                emit(("error", exc))
            finally:
                emit(None)

        asyncio.ensure_future(asyncio.to_thread(worker))

        try:
            while True:
                item = await queue.get()
                if item is None:
                    return
                kind = item[0]
                if kind == "error":
                    raise EngineError(f"进程内引擎异常: {item[1]}")
                if kind == "final":
                    finish, usage = item[1], item[2]
                    yield TokenEvent(text="", finish_reason=finish, usage=usage)
                    return
                yield TokenEvent(text=item[1])
        finally:
            # 消费者提前走了：让线程在下一个 token 处停下（不 join，免得卡住事件循环）
            self._cancelled.add(req.request_id)

    def _run(self, req: GenRequest, emit: Any) -> None:
        llm = self._llm
        assert llm is not None

        t0 = time.monotonic()
        # special=True 必须给：模板里的 <|im_start|> / <think> 是特殊 token，
        # 不加会退化成逐字节切分（实测同一 prompt 19 → 34 个 token），
        # 于是 prompt_tokens 虚高、和 _input_ids 的前缀比对也永远对不上（cached 恒 0）。
        tokens: list[int] = llm.tokenize(
            req.prompt.encode("utf-8"), add_bos=True, special=True
        )
        cached = self._prefix_hit(tokens)

        remaining = self._s.n_ctx - len(tokens)
        if remaining <= 1:
            raise EngineError(
                f"上下文不足：prompt {len(tokens)} token，n_ctx {self._s.n_ctx}"
            )
        max_tokens = min(req.max_tokens, remaining - 1)

        first: float | None = None
        last: float | None = None
        n = 0
        finish = "length"

        for chunk in llm.create_completion(
            prompt=req.prompt,
            max_tokens=max_tokens,
            temperature=req.temperature,
            top_p=req.top_p,
            stop=req.stop or [],
            seed=req.seed,
            stream=True,
        ):
            if req.request_id in self._cancelled:
                finish = "cancelled"
                break
            choice = (chunk.get("choices") or [{}])[0]
            if choice.get("finish_reason"):
                finish = str(choice["finish_reason"])
            text = choice.get("text") or ""
            if not text:
                continue
            now = time.monotonic()
            if first is None:
                first = now
            last = now
            n += 1
            emit(("tok", text))

        # 一个 chunk 约等于一个 token（llama_cpp 默认逐 token 吐），
        # 拿不到精确 token 数时这个近似足够算 usage。
        first_t = first or t0
        last_t = last or first_t
        if finish == "cancelled":
            pass
        elif n >= max_tokens:
            finish = "length"
        elif finish not in ("stop", "length"):
            finish = "stop"

        emit(
            (
                "final",
                finish,
                Usage(
                    prompt_tokens=len(tokens),
                    completion_tokens=max(n, 1),
                    cached_tokens=cached,
                    evaluated_tokens=max(len(tokens) - cached, 0),
                    prompt_ms=round((first_t - t0) * 1000, 3),
                    decode_ms=round((last_t - first_t) * 1000, 3),
                ),
            )
        )

    def _prefix_hit(self, tokens: list[int]) -> int:
        """估算这次有多少 prompt token 不用重算。

        `Llama.generate()` 会算最长公共前缀并 `kv_cache_seq_rm` 掉尾部
        （`llama_cpp/llama.py:916-963`），这里只是把同一件事数出来填
        `usage.cached_tokens`。整段命中时引擎会回放最后一个 token 刷新 logits，
        所以最多算 `len(tokens) - 1`。
        """
        llm = self._llm
        try:
            prev = llm._input_ids.tolist()  # noqa: SLF001 - 绑定层没有公开等价物
        except Exception:  # noqa: BLE001
            return 0
        n = 0
        for a, b in zip(prev, tokens):
            if a != b:
                break
            n += 1
        return max(0, min(n, len(tokens) - 1))

    async def abort(self, request_id: str) -> None:
        # 打标记；线程在下一个 token 处退出循环（一次 decode 约 25 ms，够快）
        self._cancelled.add(request_id)

    async def health(self) -> dict[str, object]:
        llm = self._llm
        if llm is None:
            return {"engine": self.name, "ready": False, "reason": "not loaded"}
        return {
            "engine": self.name,
            "ready": self._ready,
            "model": Path(self._gguf_path).name,
            "n_ctx": llm.n_ctx(),
            "n_gpu_layers": self._s.n_gpu_layers,
            "concurrency": 1,
        }
