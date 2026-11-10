"""llama.cpp 引擎适配器。

设计要点
--------
1. **进程内不加载模型**：llama.cpp 的 Python 绑定（llama-cpp-python）更新慢，
   遇到 `qwen35` 这种带线性注意力的新架构会直接报 unknown architecture。
   所以这里把官方 `llama-server.exe` 当成一个**子进程黑盒**：它负责权重、KV cache、
   CUDA kernel，我们只负责「发 prompt、收 token 流」。
2. **不用它的 chat 模板**：prompt 由服务层 `render_prompt()` 渲染好再传，
   走 `/completion` 而不是 `/v1/chat/completions`，避免双重套模板。
3. **取消 = 断开连接**：llama-server 在 HTTP 连接断开时会停止该槽位的生成，
   所以 `abort()` 只需要让流读取循环退出。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import AsyncIterator

import httpx
import structlog

from ..config import Settings
from .base import EngineError, GenRequest, TokenEvent, Usage

log = structlog.get_logger(__name__)


def _kill_stale(exe: Path) -> int:
    """杀掉上一次遗留的 llama-server（只认这个 exe 路径）。返回杀掉的数量。

    Windows 上父进程被 `Stop-Process -Force` 杀掉时子进程不会跟着死，它会继续
    占着 5 GB 显存，下一次加载直接 OOM。清理放在 spawn 之前，代价是一次
    PowerShell 调用（约 1 秒），换来「重启永远能起来」。
    """
    if os.name != "nt":
        return 0
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" | "
        f"Where-Object {{ $_.ExecutablePath -eq '{exe}' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    return sum(1 for line in r.stdout.splitlines() if line.strip().isdigit())


class LlamaServerEngine:
    """把一个 llama-server 子进程包装成 `Engine`。

    一个模型实例 = 一个进程；并发生成靠 llama-server 自己的 slots（`-np`）。
    """

    name = "llama-server"

    def __init__(self, settings: Settings, gguf_path: str | None = None) -> None:
        self._s = settings
        # 由 EngineManager 指定；缺省退回配置里的单模型路径
        self._gguf_path = gguf_path or settings.gguf_path
        self._proc: subprocess.Popen[bytes] | None = None
        self._client: httpx.AsyncClient | None = None
        self._cancelled: set[str] = set()
        self._ready = False
        self._base_url = f"http://127.0.0.1:{settings.engine_port}"

    # ---------------------------------------------------------------- 生命周期

    async def start(self) -> None:
        model = Path(self._gguf_path)
        if not model.exists():
            raise EngineError(f"GGUF 不存在：{model}")
        exe = Path(self._s.llama_server_path)
        if not exe.exists():
            raise EngineError(f"llama-server 不存在：{exe}")

        # 上次被强杀时子进程会变成孤儿，继续占着 8 GB 显存里的 5 GB。
        # 这里的清理只针对我们自己那份 exe，不会误杀别人的 llama.cpp。
        killed = _kill_stale(exe)
        if killed:
            log.warning("engine.stale_killed", count=killed, exe=str(exe))

        cmd = [
            str(exe),
            "-m", str(model),
            "--host", "127.0.0.1",
            "--port", str(self._s.engine_port),
            "-c", str(self._s.n_ctx),
            "-b", str(self._s.n_batch),
            "-ub", str(self._s.n_ubatch),
            "-fa", "on" if self._s.flash_attn else "off",
            "-ngl", str(self._s.n_gpu_layers),
            "-np", str(max(1, self._s.n_parallel)),
            "--no-webui",
            "--metrics",
        ]
        if self._s.n_threads > 0:
            cmd += ["-t", str(self._s.n_threads)]
        # --fit 会自动降配（少 offload 几层）且不留任何痕迹，等于把「装不下」
        # 伪装成「就这么慢」。n_ctx 是显式配置，宁可加载失败也别偷偷降级。
        if not self._s.fit:
            cmd += ["-fit", "off"]
        # 统一 KV 池：让 n_parallel 个 slot 共享 n_ctx 总额，而不是各预留一份。
        # 这是 llama.cpp 里最接近 PagedAttention"按需分配"的东西。
        if self._s.kv_unified:
            cmd.append("--kv-unified")
        # KV 量化：q8_0 让每 token 的 KV 减半，从而在同样的显存里把上下文翻倍
        # （32K/f16 ≈ 64K/q8_0）。质量代价 PPL +0.09%，速度无变化。
        for flag, ctype in (("-ctk", self._s.cache_type_k), ("-ctv", self._s.cache_type_v)):
            if ctype and ctype.lower() not in ("f16", "default", ""):
                cmd += [flag, ctype]
        # 跨请求前缀复用：不开这个，只有「上一条 prompt 完整包含在新 prompt 开头」
        # 才会命中，共享一段 system 前缀的不同问题全部重算。
        if self._s.cache_reuse > 0:
            cmd += ["--cache-reuse", str(self._s.cache_reuse)]

        log.info("engine.spawn", cmd=cmd[:6], gguf=str(model))
        # 不继承我们的 stdout：llama-server 日志很吵，单独写到文件便于排查
        self._log_file = open(self._s.engine_log_path, "wb")  # noqa: SIM115
        self._proc = subprocess.Popen(cmd, stdout=self._log_file, stderr=subprocess.STDOUT)

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=None),
        )
        await self._wait_ready()

    async def _wait_ready(self) -> None:
        """等 /health 变绿。模型加载 5 GB 大概 10–40 秒。"""
        deadline = time.monotonic() + self._s.engine_start_timeout_s
        last_err: str = ""
        while time.monotonic() < deadline:
            assert self._proc is not None
            if self._proc.poll() is not None:
                raise EngineError(
                    f"llama-server 启动即退出（code={self._proc.returncode}），"
                    f"日志见 {self._s.engine_log_path}"
                )
            try:
                r = await self._client.get("/health")  # type: ignore[union-attr]
                if r.status_code == 200 and r.json().get("status") == "ok":
                    self._ready = True
                    log.info("engine.ready", url=self._base_url)
                    return
            except Exception as exc:  # 进程还没监听端口
                last_err = str(exc)
            await asyncio.sleep(0.5)
        raise EngineError(f"llama-server 在 {self._s.engine_start_timeout_s}s 内未就绪：{last_err}")

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                await asyncio.to_thread(self._proc.wait, 10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            log.info("engine.stopped")
        self._proc = None
        self._ready = False

    # ---------------------------------------------------------------- 生成

    async def stream(self, req: GenRequest) -> AsyncIterator[TokenEvent]:
        if not self._ready or self._client is None:
            raise EngineError("引擎未就绪")
        payload = {
            "prompt": req.prompt,
            "stream": True,
            "n_predict": req.max_tokens,
            "temperature": req.temperature,
            "top_p": req.top_p,
            "stop": req.stop or [],
            "cache_prompt": True,          # 复用前缀 KV（llama-server 自己算最长公共前缀）
            "return_tokens": False,
        }
        if req.slot is not None:
            # 调度器给的前缀亲和槽位：同一段系统提示词尽量回到同一个 slot，
            # 这样它能命中 llama-server 的 prompt cache，少重算几千个 token。
            payload["id_slot"] = req.slot
        prompt_tokens = 0
        completion_tokens = 0
        cached_tokens = 0
        evaluated_tokens = 0
        prompt_ms = 0.0
        decode_ms = 0.0
        finish = "stop"
        try:
            async with self._client.stream("POST", "/completion", json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")[:400]
                    raise EngineError(f"llama-server {resp.status_code}: {body}")
                async for line in resp.aiter_lines():
                    if req.request_id in self._cancelled:
                        finish = "cancelled"
                        break
                    if not line.startswith("data: "):
                        continue  # 心跳行 "data: " 或空行
                    chunk = json.loads(line[6:])
                    if chunk.get("stop"):
                        prompt_tokens = chunk.get("tokens_evaluated", prompt_tokens)
                        completion_tokens = chunk.get("tokens_predicted", completion_tokens)
                        # timings.prompt_n = 这次真正算的 token 数，timings.cache_n = 命中缓存跳过的
                        timings = chunk.get("timings") or {}
                        evaluated_tokens = int(timings.get("prompt_n", evaluated_tokens) or 0)
                        cached_tokens = int(timings.get("cache_n", cached_tokens) or 0)
                        # 引擎自报耗时：prompt_ms 含前缀比对 + 前向，predicted_ms 是纯解码
                        prompt_ms = float(timings.get("prompt_ms") or 0.0)
                        decode_ms = float(timings.get("predicted_ms") or 0.0)
                        if chunk.get("stopped_eos") or chunk.get("stopped_word"):
                            finish = "stop"
                        elif chunk.get("stopped_limit"):
                            finish = "length"
                        continue
                    text = chunk.get("content") or ""
                    if not text:
                        continue
                    completion_tokens += 1  # llama-server 不给逐片 token 数，按片近似
                    yield TokenEvent(text=text, finish_reason=None)
        finally:
            self._cancelled.discard(req.request_id)

        yield TokenEvent(
            text="",
            finish_reason=finish,
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=max(completion_tokens, 1),
                cached_tokens=cached_tokens,
                evaluated_tokens=evaluated_tokens or max(prompt_tokens - cached_tokens, 0),
                prompt_ms=prompt_ms,
                decode_ms=decode_ms,
            ),
        )

    async def abort(self, request_id: str) -> None:
        # 只是打标记；真正让 GPU 停下来的是 stream() 退出 async with 后连接被关闭
        self._cancelled.add(request_id)

    async def health(self) -> dict[str, object]:
        if self._client is None:
            return {"engine": self.name, "ready": False, "reason": "no client"}
        try:
            r = await self._client.get("/health")
            return {"engine": self.name, "ready": r.status_code == 200, "upstream": r.json()}
        except Exception as exc:
            return {"engine": self.name, "ready": False, "reason": str(exc)}
