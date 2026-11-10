"""全部配置集中在这里，用 pydantic-settings 从环境变量 / .env 读取。

约定：环境变量前缀 DSHINFER_，例如 DSHINFER_ENGINE=llamacpp

「一套源码，三种形态」（Windows 原生 / 容器 / 云上 Linux）共用同一份调优参数：
仓库里的 config/base.env 会被自动加载；容器里则是镜像内的 /app/runtime/base.env
（由 docker/Dockerfile 把同一份文件拷进去）。机器相关的路径不进 base.env：

    config/base.env    调优参数（上下文、KV 量化、并发、调度），与机器无关
    config/docker.env  容器内路径（/models、/opt/llama/...）
    本机 .env          宿主机路径（不进 git，模板 .env.example）

优先级（高到低）：进程环境变量 > 本机 .env > config/base.env > 本文件里的平台默认值。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 这个 build 用的量化档；换模型只改这一处（config/docker.env 与 .env.example 里同一名字）
DEFAULT_MODEL_FILE = "DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic.Q4_K_M.gguf"

_IS_WINDOWS = os.name == "nt"
# src/dshinfer/config.py -> 仓库根（源码跑时用来找 config/base.env）
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _env_files() -> tuple[Path, ...]:
    """按优先级从低到高排好的 dotenv 文件（同一个键，后面的覆盖前面的）。"""
    candidates = (
        # 镜像里：Dockerfile 拷进来的两份契约
        Path("/app/runtime/base.env"),
        Path("/app/runtime/docker.env"),
        # 源码跑（Windows 原生 / WSL / 云上手跑）：仓库里的同一份契约
        _REPO_ROOT / "config" / "base.env",
        # 本机路径覆盖（不进 git）
        Path(".env"),
    )
    return tuple(p for p in candidates if p.is_file())


def _default_models_dir() -> str:
    """权重目录：Windows 走本地盘；容器里是 compose 的挂载点；裸 Linux 用 ~/models。"""
    if _IS_WINDOWS:
        return r"D:\llm\models\gguf"
    if Path("/models").is_dir():
        return "/models"
    return str(Path.home() / "models")


def _default_gguf_path() -> str:
    return str(Path(_default_models_dir()) / DEFAULT_MODEL_FILE)


def _default_llama_server_path() -> str:
    """引擎可执行文件：Windows 用本地 shim；Linux/容器用镜像摊平后的构建。"""
    if _IS_WINDOWS:
        return r"D:\llm\runtime\llama.cpp\llama-server.exe"
    for candidate in ("/opt/llama/bin/llama-server", "/usr/local/bin/llama-server"):
        if Path(candidate).exists():
            return candidate
    return "/opt/llama/bin/llama-server"


def _default_engine_log_path() -> str:
    if _IS_WINDOWS:
        return r"D:\llm\runtime\llama-server.log"
    for directory in (Path("/var/log/dshinfer"), Path.home() / ".local" / "state" / "dshinfer"):
        if directory.is_dir() and os.access(directory, os.W_OK):
            return str(directory / "llama-server.log")
    return str(Path.home() / ".local" / "state" / "dshinfer" / "llama-server.log")


def _default_chat_template_path() -> str:
    if _IS_WINDOWS:
        return r"D:\llm\runtime\chat_template.jinja"
    for candidate in (
        "/app/runtime/chat_template.jinja",
        str(_REPO_ROOT / "docker" / "chat_template.jinja"),
    ):
        if Path(candidate).is_file():
            return candidate
    return "/app/runtime/chat_template.jinja"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DSHINFER_", env_file=_env_files(), extra="ignore"
    )

    # ---- 服务 ----
    app_name: str = "dshinfer"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"

    # ---- 模型 ----
    model_name: str = "deepseek-v4-pro-qwen3.5-9b"
    model_revision: str = "xtools-at/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic"
    max_context_tokens: int = 65536
    # 从 GGUF 元数据抠出来的 jinja 模板；缺失时服务层退回朴素 ChatML
    chat_template_path: str = _default_chat_template_path()
    # True = 让模型先输出思考过程（会混在正文里）；False = 直接给答案
    enable_thinking: bool = False

    # ---- 引擎 ----
    engine: str = "mock"  # mock | llamacpp
    # 模型目录：注册表扫描这里的 *.gguf（新增文件重启后自动出现在 /v1/models）
    models_dir: str = _default_models_dir()
    gguf_path: str = _default_gguf_path()
    n_gpu_layers: int = -1  # -1 = 全部放 GPU
    # 上下文总额。配合 kv_unified 时这是「单个请求最多能用多少」，不是「四个人分」。
    # 32K 对 agent（DSH）偏紧：系统提示 + 工具定义就吃掉 24–28K，剩下 4–8K
    # 既要放历史又要放回答，开思考必被截断。实测本机可上到 64K：
    #   KV 用 q8_0 量化后每 token 16 KB，64K ≈ 1.0 GB（与 32K/f16 同量级），
    #   加载后显存 6,727 MiB（32K/f16 时 6,161），pp/tg 无变化，PPL 7.7457 vs 7.7389。
    n_ctx: int = 65536
    # KV 缓存量化：f16（默认）/ q8_0 / q4_0。q8_0 把每 token 的 KV 减半，质量损失
    # 在 PPL 上是 +0.09%，换取上下文翻倍——本机 8 GB 显存下这是最划算的一档。
    # 注意：33 层里有 26 层线性注意力（recurrent），那部分的循环状态不参与这种量化。
    cache_type_k: str = "q8_0"
    cache_type_v: str = "q8_0"
    # llama-server 的 --parallel（slot 数）。注意：配合 kv_unified 时这里不再是
    # 「把 32K 切四份」，而是「最多四个并发请求共享一个 32K 的 KV 池」——单个长请求
    # 仍能用满 32K，多条短请求则并行跑。实测 4 路并发聚合 110 tok/s（单流 42）。
    n_parallel: int = 4
    # 统一 KV 池（--kv-unified）。这是 llama.cpp 版的「按需分配」：不再给每个
    # slot 预留 n_ctx/n_parallel，显存按实际占用走。没有它，4 个 slot 就是
    # 4 × 8K，长上下文和并发二选一。
    kv_unified: bool = True
    n_batch: int = 512
    # 微批（micro-batch）大小。预填充时的实际并行宽度，比 n_batch 更影响
    # pp 速度：实测 1024 比默认 512 快约 10%（2170 → 2400 tok/s），再往上
    # 收益消失，显存占用线性增长。
    n_ubatch: int = 1024
    # FlashAttention。本机 40 系（sm_89）支持；关掉会明显变慢且更费显存。
    flash_attn: bool = True
    # llama.cpp 的 --fit 自动降配（我们这份 llama.cpp 构建里默认开启）：显存不够时
    # 它会自己少 offload 几层到 GPU，并且**不报错、不改 /props 里的 n_ctx**。
    # 实测 8 GB + n_ctx=65536 时它会选 30/34 层上卡，tg 从 42.1 掉到 26.8 tok/s
    # （-36%），而短上下文也一样慢——这就是过去「64K 掉速 35%」的真正原因（不是
    # 深度、不是带宽）。n_ctx 是我们显式钉死的，所以默认关掉：让「装不下」显式失败，
    # 而不是悄悄变慢。显存更小的卡若加载 OOM，把它设回 true。
    fit: bool = False
    n_threads: int = 0  # 0 = 让 llama.cpp 自己决定

    # ---- llama-server 子进程 ----
    llama_server_path: str = _default_llama_server_path()
    engine_port: int = 8124
    engine_log_path: str = _default_engine_log_path()
    engine_start_timeout_s: float = 180.0

    # ---- 并发与背压 ----
    # 服务层同时在飞的上限。必须 ≤ n_parallel，否则请求会排在调度器里等一个
    # 永远空不出来的 slot。配合 kv_unified 时 4 路并发是纯赚：单流只慢 20%，
    # 聚合吞吐 42 → 110 tok/s。
    max_concurrency: int = 4
    queue_limit: int = 64
    queue_timeout_s: float = 30.0
    request_timeout_s: float = 180.0
    # 把调度器分到的 slot 作为 id_slot 传给 llama-server，让「同一会话」固定落在
    # 同一个槽位上。llama.cpp 的 prompt cache 是**按槽位**保存的：换了槽位就等于冷启动，
    # 整段历史重新预填充。实测 6 个 agent 会话各 3 步：
    #   不钉（8 槽轮转）：命中率 0% / 29% / 29%，每步重算 157–194 token
    #   钉死（按会话号）：命中率 100%，每步只重算 22–24 token（就是新增的那几句）
    # 代价：请求固定在一个槽位，引擎不能替它挑槽——单流 -3% 吞吐，换来多步对话
    # 每步省下几百毫秒的预填充，值。
    slot_pinning: bool = True
    # 前缀复用的最小块（llama-server 的 --cache-reuse N）。
    # 默认 0 = 关闭。注意：在本机这个模型上它**无效**——33 层里有 26 层是线性
    # 注意力（recurrent），循环层只有「跑过的状态」，没有可切块复用的 KV，所以
    # 不同 prompt 共享 system 前缀时 cache_n 恒为 0（0/256/钉 slot 三种组合都试过）。
    # 留着这个开关是给纯 Transformer 模型用的（那边它确实能让共享前缀命中）。
    cache_reuse: int = 0

    # ---- 调度 ----
    # 老化窗口：排队每过这么多秒，等效优先级 +1，防止低优先级请求饿死
    aging_s: float = 2.0
    # 单租户同时在飞 / 排队的上限（0 = 不限制）。
    # 注意：默认值给 0，因为「单租户配额」一旦小于 max_concurrency，就等于给所有
    # 单客户端场景静默砍半吞吐（实测并发 4 时吞吐掉 42%）。多租户部署应设成
    # max_concurrency // 租户数，并在压测里确认尾延迟。
    per_tenant_inflight: int = 0
    per_tenant_queue: int = 0

    # ---- 生成默认值 ----
    default_max_tokens: int = 512
    default_temperature: float = 0.7
    default_top_p: float = 0.9

    # ---- mock 引擎（没有模型时也能把整条链路跑通）----
    mock_ttft_ms: float = 120.0
    mock_tokens_per_second: float = 40.0
    mock_response: str = (
        "这是一个 mock 引擎的输出，用来验证服务层的流式、取消、并发和指标。"
        "它不加载任何权重，只按固定速率吐字。"
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
