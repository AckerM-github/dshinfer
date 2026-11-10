"""形态隔离守卫：一套源码要能同时跑三种形态，谁都不许偷偷依赖对方的路径。

    Windows 原生（机器 A）  : D 盘上的 runtime / models
    容器与云（机器 B）      : /models、/opt/llama/...

守卫全部是静态文本检查（不启动 docker、不联网），跑得快，目的是让「把本机路径
塞回配置里」这类回归在测试阶段就红掉。端到端的清白性验证见 docs/deploy.md。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DOCKER = ROOT / "docker"
CONFIG = ROOT / "config"

# 只有这两个模块允许写 Windows 默认值：它们就是「平台默认值」的所在地
WINDOWS_DEFAULT_ALLOWLIST = {"config.py", "preset.py"}

_BS = chr(92)  # backslash
_D = chr(36)   # dollar sign
WINDOWS_PATH = re.compile("(?<![A-Za-z])[A-Za-z]:[" + _BS + "/]")
MOUNT_ROOT = re.compile("/mnt/")

TUNING_KEYS = [
    "DSHINFER_ENGINE",
    "DSHINFER_N_CTX",
    "DSHINFER_CACHE_TYPE_K",
    "DSHINFER_CACHE_TYPE_V",
    "DSHINFER_N_PARALLEL",
    "DSHINFER_MAX_CONCURRENCY",
    "DSHINFER_KV_UNIFIED",
    "DSHINFER_N_BATCH",
    "DSHINFER_N_UBATCH",
    "DSHINFER_FLASH_ATTN",
    "DSHINFER_SLOT_PINNING",
    "DSHINFER_N_GPU_LAYERS",
    "DSHINFER_FIT",
    "DSHINFER_ENABLE_THINKING",
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """去掉整行注释和行尾注释，别把说明文字里的旧路径当成真实依赖。"""
    lines = []
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


def test_base_env_is_the_only_tuning_source():
    base = _read(CONFIG / "base.env")
    for key in TUNING_KEYS:
        assert key + "=" in base, "config/base.env 缺 " + key
    compose = _read(DOCKER / "compose.yaml")
    for key in TUNING_KEYS:
        assert key + "=" not in compose, (
            "docker/compose.yaml 又抄了一遍 " + key + "：调优值只能写在 config/base.env；"
            "覆盖层要临时改动请写 environment:（compose 里它优先于 env_file）"
        )
    assert "../config/base.env" in compose
    assert "../config/docker.env" in compose


def test_base_env_has_no_machine_paths():
    for lineno, raw in enumerate(_read(CONFIG / "base.env").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, _, value = line.partition("=")
        assert key.startswith("DSHINFER_"), "config/base.env:%d 键名要以 DSHINFER_ 开头" % lineno
        assert not WINDOWS_PATH.search(value), "config/base.env:%d 混进了 Windows 路径" % lineno
        assert not MOUNT_ROOT.search(value), "config/base.env:%d 混进了 /mnt 路径" % lineno
        assert not value.startswith("/"), (
            "config/base.env:%d 混进了宿主机绝对路径 %r（路径属于 docker.env / .env）" % (lineno, value)
        )


def test_docker_env_only_mentions_container_paths():
    allowed = ("/models", "/opt/llama", "/app", "/var/log/dshinfer")
    for lineno, raw in enumerate(_read(CONFIG / "docker.env").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, _, value = line.partition("=")
        assert not WINDOWS_PATH.search(value), "config/docker.env:%d 混进了 Windows 路径" % lineno
        assert not MOUNT_ROOT.search(value), "config/docker.env:%d 混进了 /mnt 路径" % lineno
        if value.startswith("/"):
            assert value.startswith(allowed), (
                "config/docker.env:%d 的 %s=%s 不在镜像约定路径里" % (lineno, key, value)
            )


def test_compose_base_layer_has_no_host_paths():
    text = _strip_comments(_read(DOCKER / "compose.yaml"))
    assert not WINDOWS_PATH.search(text), "docker/compose.yaml 不许出现 Windows 路径"
    assert not MOUNT_ROOT.search(text), "docker/compose.yaml 不许出现 /mnt 路径"
    assert _BS not in text, "docker/compose.yaml 不许出现反斜杠（Windows 路径的指纹）"
    assert _D + "{MODELS_DIR:-/models}:/models:ro" in text, "权重目录要用中性占位 /models，由覆盖层给真实目录"


def test_wsl_overlay_only_holds_machine_specifics():
    text = _strip_comments(_read(DOCKER / "compose.wsl.yaml"))
    assert not WINDOWS_PATH.search(text)
    assert not MOUNT_ROOT.search(text), "WSL 覆盖层不许把 /mnt 里的权重挂给容器（9p 太慢）"
    assert "MODELS_DIR" in text
    assert "DSHINFER_N_CTX" in text


def test_dockerfile_ships_the_contract_and_no_host_paths():
    text = _strip_comments(_read(DOCKER / "Dockerfile"))
    assert not MOUNT_ROOT.search(text)
    assert not WINDOWS_PATH.search(text)
    assert "config/base.env" in text and "config/docker.env" in text


def test_sources_do_not_hardcode_host_paths():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name in WINDOWS_DEFAULT_ALLOWLIST:
            continue
        text = _read(path)
        if WINDOWS_PATH.search(text) or MOUNT_ROOT.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, (
        "这些源文件混进了宿主路径（平台默认值只允许写在 config.py / preset.py）："
        + ", ".join(offenders)
    )


def test_env_example_only_overrides_paths():
    body = _strip_comments(_read(ROOT / ".env.example"))
    for key in TUNING_KEYS:
        assert key + "=" not in body, (
            ".env.example 不要再抄调优项 " + key + "（唯一来源是 config/base.env）"
        )
    assert "DSHINFER_MODELS_DIR=" in body


def test_settings_defaults_follow_platform():
    from dshinfer.config import Settings

    s = Settings(engine="mock", log_level="error", _env_file=None)
    if os.name == "nt":
        assert s.models_dir.startswith("D:")
        assert s.llama_server_path.endswith("llama-server.exe")
    else:
        assert not s.models_dir.startswith("D:")
        assert not s.llama_server_path.endswith(".exe")
        assert s.llama_server_path.startswith("/")


def test_verify_env_is_stdlib_only():
    text = _read(ROOT / "scripts" / "verify_env.py")
    assert "urllib" in text
    for banned in ("import requests", "import httpx"):
        assert banned not in text, "verify_env.py 要能在镜像与宿主两端裸跑，只能用标准库"
