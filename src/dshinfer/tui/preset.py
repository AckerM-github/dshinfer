"""启动向导的产物：一份可保存、可复用、可直接变成容器环境变量的运行预设。

为什么单独一个模块：向导（wizard.py）负责交互，监视器（monitor.py）只读预设，
两边都不该知道对方的界面细节；预设还要能被非交互路径（--dump-env / --monitor）
复用，所以它不能挂在任何一个 Screen 上。

落地方式（用户要求「向导要真落地」）：

    ~/.config/dshinfer/presets/<name>.json    预设本体（Windows 在 %APPDATA%）
    ~/.config/dshinfer/presets/<name>.env     docker compose 可直接用的环境变量片段

本机（Windows 原生跑）用 to_env(container=False) 得到 D: 路径版本；容器里跑
用 to_env(container=True) 得到 /models 与镜像内固定路径版本。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

DEFAULT_MODELS_DIR_WIN = r"D:\llm\models\gguf"
DEFAULT_LOG_WIN = r"D:\llm\runtime\service.log"
DEFAULT_MODELS_DIR_POSIX = "~/models"
DEFAULT_LOG_POSIX = "/var/log/dshinfer/service.log"

# 容器里这些路径是 Dockerfile / compose 约定好的，不要改
CONTAINER_MODELS_DIR = "/models"
CONTAINER_LOG_PATH = "/var/log/dshinfer/service.log"
CONTAINER_CHAT_TEMPLATE = "/app/runtime/chat_template.jinja"


def presets_dir() -> Path:
    override = os.environ.get("DSHINFER_PRESETS_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base) if base else Path.home() / ".config"
    return root / "dshinfer" / "presets"


def in_container() -> bool:
    """是不是跑在镜像里（而不是宿主机原生跑）。

    判据用 /.dockerenv：这是 Docker 给每个容器放的空文件，宿主机上不存在；
    比"某个路径在不在"更稳（/models 是挂载点，宿主机的 WSL 里也可能有同名目录）。
    """
    return Path("/.dockerenv").exists()


def default_models_dir() -> str:
    if os.name == "nt":
        return DEFAULT_MODELS_DIR_WIN
    if in_container():
        return CONTAINER_MODELS_DIR
    return DEFAULT_MODELS_DIR_POSIX


def default_log_path() -> str:
    if os.name == "nt":
        return DEFAULT_LOG_WIN
    if in_container():
        return CONTAINER_LOG_PATH
    return DEFAULT_LOG_POSIX


def discover_models(models_dir: str | os.PathLike[str]) -> list[Path]:
    """扫描模型目录下的 *.gguf（含 draft/ 之类的一级子目录），按体积从大到小。

    刻意不用 registry.py 的实现：那边只扫一层，而向导要连草稿模型一起看到。
    """
    root = Path(models_dir).expanduser()
    if not root.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(root.glob("*.gguf")):
        found.append(path)
    for path in sorted(root.glob("*/*.gguf")):
        found.append(path)
    found.sort(key=lambda p: (-_size(p), p.name))
    return found


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


@dataclass(slots=True)
class Preset:
    """一次运行的全部可调项。字段与 Settings（pydantic）一一对应，导出时不丢项。"""

    name: str = "default"
    model_file: str = ""
    models_dir: str = ""
    # 上下文与 KV 量化：8 GB 显存下这两个是一对（32K/q8_0 = 41 t/s、64K/q8_0 = 26 t/s）
    n_ctx: int = 32768
    cache_type_k: str = "q8_0"
    cache_type_v: str = "q8_0"
    # 多模态投影文件，空字符串 = 不支持图片输入
    mmproj: str = ""
    # inproc 引擎一个 context 一次只跑一个 decode，所以序列数 = 1、并发 = 1
    n_parallel: int = 1
    max_concurrency: int = 1
    n_batch: int = 2048
    n_ubatch: int = 1024
    flash_attn: bool = True
    n_gpu_layers: int = -1
    engine: str = "inproc"
    service_log_path: str = ""
    host: str = "127.0.0.1"
    port: int = 8000

    # ------------------------------------------------------------ 构造

    @classmethod
    def default(cls, name: str = "default") -> "Preset":
        preset = cls(
            name=name,
            models_dir=default_models_dir(),
            service_log_path=default_log_path(),
        )
        candidates = discover_models(preset.models_dir)
        if candidates:
            preset.model_file = candidates[0].name
        return preset

    @property
    def gguf_path(self) -> str:
        if not self.model_file:
            return ""
        if os.path.isabs(self.model_file):
            return self.model_file
        return str(Path(self.models_dir).expanduser() / self.model_file)

    @property
    def model_stem(self) -> str:
        if not self.model_file:
            return "（未选择模型）"
        return Path(self.model_file).stem

    # ------------------------------------------------------------ 校验

    def problems(self) -> list[str]:
        """返回「会让人跑不起来」的问题；提示级别的不算（比如 64K 掉速）。"""
        issues: list[str] = []
        if not self.model_file:
            issues.append("还没选模型文件")
        elif not Path(self.gguf_path).is_file():
            issues.append(f"模型文件不存在：{self.gguf_path}")
        if self.models_dir and not Path(self.models_dir).expanduser().is_dir():
            issues.append(f"模型目录不存在：{self.models_dir}")
        if self.n_ctx <= 0:
            issues.append("上下文长度必须为正")
        if self.n_parallel < 1 or self.max_concurrency < 1:
            issues.append("并发必须 >= 1")
        if self.max_concurrency > self.n_parallel:
            issues.append(
                f"服务层并发 {self.max_concurrency} 大于引擎序列数 {self.n_parallel}，"
                "多出来的请求会一直排队"
            )
        if self.n_batch < self.n_ubatch:
            issues.append("n_batch 不能小于 n_ubatch")
        return issues

    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.n_ctx > 32768:
            out.append(
                f"上下文 {self.n_ctx:,} 超过 32K：本机这是常规档位的上限，"
                "64K 生成速度会掉约 35%（40 → 26 t/s）"
            )
        if self.n_parallel > 1:
            out.append(
                "inproc 引擎一个 context 一次只跑一个 decode，序列数 >1 只会多占 KV，不会更快"
            )
        if self.cache_type_k == "f16" and self.n_ctx > 32768:
            out.append("上下文 >32K 还用 f16 KV 很可能直接 OOM，建议 q8_0")
        if not self.flash_attn:
            out.append("关掉 FlashAttention 会更慢也更费显存（sm_89 支持它）")
        return out

    # ------------------------------------------------------------ 序列化

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Preset":
        allowed = {f.name for f in fields(cls)}
        clean = {k: v for k, v in (data or {}).items() if k in allowed}
        return cls(**clean)

    def save(self, path: str | os.PathLike[str] | None = None) -> Path:
        target = Path(path).expanduser() if path else presets_dir() / f"{self.name}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return target

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "Preset":
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def latest(cls) -> "Preset":
        """没有 --preset 时的兜底：目录里最新的预设，没有就用默认值。"""
        files = sorted(
            presets_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        if files:
            try:
                return cls.load(files[0])
            except (OSError, ValueError, TypeError):
                pass
        return cls.default()

    # ------------------------------------------------------------ 导出环境变量

    def to_env(self, container: bool = True) -> list[tuple[str, str]]:
        """按 compose / Dockerfile 的键名导出，顺序固定（方便 diff 与贴进 yaml）。

        只导出 Settings 里真的存在的键：inproc 把引擎搬进了服务进程，
        LLAMA_SERVER_PATH / ENGINE_LOG_PATH / KV_UNIFIED 这些已经不存在了。
        """
        if container:
            models_dir = CONTAINER_MODELS_DIR
            gguf = f"{CONTAINER_MODELS_DIR}/{Path(self.model_file).name}"
            host = "0.0.0.0"
        else:
            models_dir = self.models_dir
            gguf = self.gguf_path
            host = self.host
        pairs: list[tuple[str, str]] = [
            ("DSHINFER_ENGINE", self.engine),
            ("DSHINFER_MODELS_DIR", models_dir),
            ("DSHINFER_GGUF_PATH", gguf),
            ("DSHINFER_HOST", host),
            ("DSHINFER_PORT", str(self.port)),
            ("DSHINFER_N_CTX", str(self.n_ctx)),
            ("DSHINFER_CACHE_TYPE_K", self.cache_type_k),
            ("DSHINFER_CACHE_TYPE_V", self.cache_type_v),
            ("DSHINFER_N_PARALLEL", str(self.n_parallel)),
            ("DSHINFER_MAX_CONCURRENCY", str(self.max_concurrency)),
            ("DSHINFER_N_BATCH", str(self.n_batch)),
            ("DSHINFER_N_UBATCH", str(self.n_ubatch)),
            ("DSHINFER_FLASH_ATTN", _bool(self.flash_attn)),
            ("DSHINFER_N_GPU_LAYERS", str(self.n_gpu_layers)),
        ]
        if container:
            pairs.append(("DSHINFER_CHAT_TEMPLATE_PATH", CONTAINER_CHAT_TEMPLATE))
        if self.mmproj:
            pairs.append(("DSHINFER_MMPROJ_PATH", self.mmproj))
        return pairs

    def env_block(self, container: bool = True) -> str:
        lines = [f"      {key}: \"{value}\"" for key, value in self.to_env(container)]
        return "\n".join(lines)


def _bool(value: bool) -> str:
    return "true" if value else "false"


def write_env_file(preset: Preset, container: bool = True) -> Path:
    target = presets_dir() / f"{preset.name}.env"
    target.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# 由 dshinfer-wizard 生成，可直接贴进 docker/compose.yaml 的 environment:\n"
        "# 或 docker compose --env-file（那种情况下去掉键前缀改写请自行调整）\n"
    )
    target.write_text(header + preset.env_block(container) + "\n", encoding="utf-8")
    return target
