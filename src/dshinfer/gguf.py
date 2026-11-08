"""只读 GGUF 头部，拿到架构 / 层数 / 训练上下文 / 宽度。

为什么要自己写：模型注册表要对目录里每个 .gguf 报出"这是什么模型、能吃多长上下文"，
而 llama-cpp-python 的绑定版本落后于新架构（认不出 qwen35）。头部解析只依赖文件格式，
不依赖引擎版本，所以永远可用，而且只读前几 MB —— 5.6 GB 的文件也是毫秒级。

格式（GGUF v2/v3）：
  magic "GGUF" | version u32 | tensor_count u64 | kv_count u64 | kv_count 个 (key, type, value) | 张量信息 …
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

__all__ = ["GgufMeta", "read_meta"]

_MAGIC = b"GGUF"

_U8, _I8, _U16, _I16, _U32, _I32, _F32, _BOOL, _STR, _ARR, _U64, _I64, _F64 = range(13)

_FIXED = {
    _U8: "<B",
    _I8: "<b",
    _U16: "<H",
    _I16: "<h",
    _U32: "<I",
    _I32: "<i",
    _F32: "<f",
    _BOOL: "<?",
    _U64: "<Q",
    _I64: "<q",
    _F64: "<d",
}

_MAX_STR = 1 << 22  # 单个字符串上限 4 MB（chat template 也就几十 KB）


class GgufError(ValueError):
    """头部损坏 / 不是 GGUF。"""


def _exact(f, n: int) -> bytes:
    b = f.read(n)
    if len(b) != n:
        raise GgufError("文件被截断")
    return b


def _string(f) -> str:
    (n,) = struct.unpack("<Q", _exact(f, 8))
    if n > _MAX_STR:
        raise GgufError(f"字符串长度异常: {n}")
    return _exact(f, n).decode("utf-8", "replace")


def _skip(f, t: int) -> None:
    fmt = _FIXED.get(t)
    if fmt is not None:
        f.seek(struct.calcsize(fmt), 1)
    elif t == _STR:
        (n,) = struct.unpack("<Q", _exact(f, 8))
        f.seek(n, 1)
    elif t == _ARR:
        (et,) = struct.unpack("<I", _exact(f, 4))
        (n,) = struct.unpack("<Q", _exact(f, 8))
        fmt = _FIXED.get(et)
        if fmt is not None:
            f.seek(struct.calcsize(fmt) * n, 1)
        else:
            for _ in range(n):
                _skip(f, et)
    else:
        raise GgufError(f"未知值类型 {t}")


def _value(f, t: int):
    fmt = _FIXED.get(t)
    if fmt is not None:
        return struct.unpack(fmt, _exact(f, struct.calcsize(fmt)))[0]
    if t == _STR:
        return _string(f)
    # 数组 / 其它类型对我们没用，跳过
    _skip(f, t)
    return None


@dataclass(frozen=True)
class GgufMeta:
    arch: str
    n_layers: int
    n_ctx_train: int
    n_embd: int
    tensor_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "arch": self.arch,
            "n_layers": self.n_layers,
            "n_ctx_train": self.n_ctx_train,
            "n_embd": self.n_embd,
            "tensor_count": self.tensor_count,
        }


def read_meta(path: str | Path) -> GgufMeta | None:
    """读头部；任何格式问题都返回 None（注册表里降级成"未知"而不是整站 500）。"""
    p = Path(path)
    try:
        with p.open("rb") as f:
            if _exact(f, 4) != _MAGIC:
                return None
            (version,) = struct.unpack("<I", _exact(f, 4))
            if version not in (2, 3):
                return None
            (tensor_count,) = struct.unpack("<Q", _exact(f, 8))
            (kv_count,) = struct.unpack("<Q", _exact(f, 8))

            kv: dict[str, object] = {}
            for _ in range(kv_count):
                key = _string(f)
                (vtype,) = struct.unpack("<I", _exact(f, 4))
                kv[key] = _value(f, vtype)

        arch = kv.get("general.architecture")
        if not isinstance(arch, str):
            return None

        def pick(name: str, default: int = 0) -> int:
            v = kv.get(f"{arch}.{name}")
            return int(v) if isinstance(v, int) else default

        return GgufMeta(
            arch=arch,
            n_layers=pick("block_count"),
            n_ctx_train=pick("context_length"),
            n_embd=pick("embedding_length"),
            tensor_count=int(tensor_count),
        )
    except (OSError, GgufError, struct.error):
        return None
