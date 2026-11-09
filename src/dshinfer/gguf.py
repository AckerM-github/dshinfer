"""只读 GGUF 头部，拿到架构 / 层数 / 训练上下文 / 宽度 / MoE 专家规模。

为什么要自己写：模型注册表要对目录里每个 .gguf 报出"这是什么模型、能吃多长上下文"，
而 llama-cpp-python 的绑定版本落后于新架构（认不出 qwen35）。头部解析只依赖文件格式，
不依赖引擎版本，所以永远可用，而且只读前几 MB —— 5.6 GB 的文件也是毫秒级。

MoE（混合专家）模型的专家数也在头部里：{arch}.expert_count / expert_used_count /
expert_feed_forward_length。终端面板的 Overview 用它显示"显存里的 expert 数"。
注意这两个键只有 MoE 模型才有；稠密模型（包括 qwen35 这种注意力 + 线性注意力的混合
架构）读到 0，moe_summary() 会降级成 is_moe=False，不要当成错误。

格式（GGUF v2/v3）：
  magic "GGUF" | version u32 | tensor_count u64 | kv_count u64 | kv_count 个 (key, type, value) | 张量信息 …
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

__all__ = ["GgufMeta", "moe_summary", "read_meta"]

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
    # 旧字段全部保留在前，新增字段都带默认值：老代码按位置构造也不会坏。
    arch: str
    n_layers: int
    n_ctx_train: int
    n_embd: int
    tensor_count: int
    n_ff: int = 0
    n_head: int = 0
    n_head_kv: int = 0
    n_experts: int = 0
    n_experts_used: int = 0
    n_expert_ff: int = 0
    file_size_bytes: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "arch": self.arch,
            "n_layers": self.n_layers,
            "n_ctx_train": self.n_ctx_train,
            "n_embd": self.n_embd,
            "tensor_count": self.tensor_count,
            "n_ff": self.n_ff,
            "n_head": self.n_head,
            "n_head_kv": self.n_head_kv,
            "n_experts": self.n_experts,
            "n_experts_used": self.n_experts_used,
            "n_expert_ff": self.n_expert_ff,
            "file_size_bytes": self.file_size_bytes,
        }


def moe_summary(meta: GgufMeta | None) -> dict[str, int | bool]:
    """把头部里的每层专家数摊平成"整模型一共多少 expert / 每 token 激活多少"。

    Overview 要的是"显存里的 expert 数"，所以按层数累加：layers * expert_count。
    非 MoE（meta 为 None、没写 expert_count、或稠密模型）统一返回全 0 + is_moe=False，
    不抛异常 —— 面板只关心"有没有、有多少"，解析失败不该让整个页面挂掉。
    """
    zero: dict[str, int | bool] = {
        "layers": 0,
        "experts_total": 0,
        "experts_active": 0,
        "experts_per_layer": 0,
        "used_per_layer": 0,
        "is_moe": False,
    }
    if meta is None or meta.n_experts <= 0:
        return zero
    return {
        "layers": meta.n_layers,
        "experts_total": meta.n_layers * meta.n_experts,
        "experts_active": meta.n_layers * meta.n_experts_used,
        "experts_per_layer": meta.n_experts,
        "used_per_layer": meta.n_experts_used,
        "is_moe": True,
    }


def read_meta(path: str | Path) -> GgufMeta | None:
    """读头部；任何格式问题都返回 None（注册表里降级成"未知"而不是整站 500）。"""
    p = Path(path)
    try:
        # 文件大小由 stat 拿（头部里没有），TUI 用来算"显存/磁盘占用"。
        file_size_bytes = p.stat().st_size
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
            n_ff=pick("feed_forward_length"),
            n_head=pick("attention.head_count"),
            n_head_kv=pick("attention.head_count_kv"),
            # MoE 三件套：稠密模型没有这些键，pick 落回 0 即"不是 MoE"。
            n_experts=pick("expert_count"),
            n_experts_used=pick("expert_used_count"),
            n_expert_ff=pick("expert_feed_forward_length"),
            file_size_bytes=file_size_bytes,
        )
    except (OSError, GgufError, struct.error):
        return None
