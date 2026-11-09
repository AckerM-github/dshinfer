"""GGUF 头部解析：稠密 / MoE / 损坏文件，全部用现场写出来的最小文件测。

不依赖 models 目录里的真实模型 —— 那 5 GB 文件不能进 CI。
合成的文件只有头部（没有张量段），read_meta 本来也只读头部。
"""

from __future__ import annotations

import struct
from pathlib import Path

from dshinfer.gguf import GgufMeta, moe_summary, read_meta

# 与 gguf.py 的 _value 类型表对齐：4=uint32, 8=string, 10=uint64
T_U32, T_STR, T_U64 = 4, 8, 10


def _wstr(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<Q", len(b)) + b


def _gguf(path: Path, kvs: list[tuple[str, int, object]], *, version: int = 3) -> Path:
    """写一个只有元数据、没有张量的 GGUF v3 文件。"""
    out = bytearray(b"GGUF")
    out += struct.pack("<I", version)
    out += struct.pack("<Q", 0)  # tensor_count，头部解析不看张量
    out += struct.pack("<Q", len(kvs))
    for key, typ, val in kvs:
        out += _wstr(key)
        out += struct.pack("<I", typ)
        if typ == T_STR:
            out += _wstr(str(val))
        elif typ == T_U64:
            out += struct.pack("<Q", int(val))
        elif typ == T_U32:
            out += struct.pack("<I", int(val))
        else:  # pragma: no cover - 测试辅助不该走到这里
            raise AssertionError(f"未支持的类型 {typ}")
    path.write_bytes(bytes(out))
    return path


def _dense_kvs(arch: str = "qwen35") -> list[tuple[str, int, object]]:
    """稠密（qwen35 混合注意力）模型的典型头部键。"""
    return [
        ("general.architecture", T_STR, arch),
        (f"{arch}.block_count", T_U32, 33),
        (f"{arch}.context_length", T_U32, 262144),
        (f"{arch}.embedding_length", T_U32, 4096),
        (f"{arch}.feed_forward_length", T_U32, 12288),
        (f"{arch}.attention.head_count", T_U32, 16),
        (f"{arch}.attention.head_count_kv", T_U32, 4),
        ("tokenizer.ggml.model", T_STR, "gpt2"),
    ]


def _moe_kvs(arch: str = "deepseek4") -> list[tuple[str, int, object]]:
    return _dense_kvs(arch) + [
        (f"{arch}.expert_count", T_U32, 64),
        # expert_used_count 也用 u64 写一次，验证 int 类型不分宽度都能取到
        (f"{arch}.expert_used_count", T_U64, 8),
        (f"{arch}.expert_feed_forward_length", T_U32, 1024),
    ]


# ---- 正常解析 ----


def test_read_meta_parses_dense_model(tmp_path):
    p = _gguf(tmp_path / "dense.Q4_K_M.gguf", _dense_kvs())
    meta = read_meta(p)
    assert isinstance(meta, GgufMeta)
    assert meta.arch == "qwen35"
    assert meta.n_layers == 33
    assert meta.n_ctx_train == 262144
    assert meta.n_embd == 4096
    assert meta.tensor_count == 0
    # 新增的宽度字段
    assert meta.n_ff == 12288
    assert meta.n_head == 16
    assert meta.n_head_kv == 4
    # 稠密模型没有 MoE 键，降级成 0 而不是报错
    assert meta.n_experts == 0
    assert meta.n_experts_used == 0
    assert meta.n_expert_ff == 0
    # 文件大小来自 stat，不是文件头
    assert meta.file_size_bytes == p.stat().st_size


def test_read_meta_parses_moe_model(tmp_path):
    p = _gguf(tmp_path / "moe.gguf", _moe_kvs())
    meta = read_meta(p)
    assert meta is not None
    assert meta.arch == "deepseek4"
    assert meta.n_layers == 33
    assert meta.n_experts == 64
    assert meta.n_experts_used == 8
    assert meta.n_expert_ff == 1024
    assert meta.n_ff == 12288  # resident 稠密 FFN 也照常读

    summary = moe_summary(meta)
    assert summary == {
        "layers": 33,
        "experts_total": 33 * 64,
        "experts_active": 33 * 8,
        "experts_per_layer": 64,
        "used_per_layer": 8,
        "is_moe": True,
    }


def test_read_meta_accepts_str_path(tmp_path):
    """registry 传的是 Path，但 API 也可能传字符串，两种都得能读。"""
    p = _gguf(tmp_path / "dense.gguf", _dense_kvs())
    meta = read_meta(str(p))
    assert meta is not None and meta.n_layers == 33


def test_to_dict_keeps_legacy_keys_and_adds_new(tmp_path):
    p = _gguf(tmp_path / "moe.gguf", _moe_kvs())
    d = read_meta(p).to_dict()  # type: ignore[union-attr]
    # 旧键一个不少（registry / API 已经在用）
    for key in ("arch", "n_layers", "n_ctx_train", "n_embd", "tensor_count"):
        assert key in d
    # 新键全部就位
    assert d["n_ff"] == 12288
    assert d["n_head"] == 16
    assert d["n_head_kv"] == 4
    assert d["n_experts"] == 64
    assert d["n_experts_used"] == 8
    assert d["n_expert_ff"] == 1024
    assert d["file_size_bytes"] == p.stat().st_size


# ---- 降级：任何异常都返回 None ----


def test_read_meta_rejects_bad_magic(tmp_path):
    p = tmp_path / "bad.gguf"
    p.write_bytes(b"NOTGGUF" + b"\x00" * 32)
    assert read_meta(p) is None


def test_read_meta_rejects_unsupported_version(tmp_path):
    p = _gguf(tmp_path / "v1.gguf", _dense_kvs(), version=1)
    assert read_meta(p) is None


def test_read_meta_truncated_header_returns_none(tmp_path):
    full = _gguf(tmp_path / "full.gguf", _dense_kvs()).read_bytes()
    p = tmp_path / "cut.gguf"
    # 砍在 kv 段中间（文件头 24 字节之后），字符串/数值必然读不全
    p.write_bytes(full[: len(full) // 2])
    assert read_meta(p) is None
    # 连 tensor_count 都没读完
    p.write_bytes(full[:10])
    assert read_meta(p) is None


def test_read_meta_missing_arch_returns_none(tmp_path):
    p = _gguf(tmp_path / "noarch.gguf", [("general.name", T_STR, "x")])
    assert read_meta(p) is None


def test_read_meta_missing_file_returns_none(tmp_path):
    assert read_meta(tmp_path / "nope.gguf") is None


# ---- moe_summary 的边界 ----


def test_moe_summary_none_is_not_moe():
    zero = {
        "layers": 0,
        "experts_total": 0,
        "experts_active": 0,
        "experts_per_layer": 0,
        "used_per_layer": 0,
        "is_moe": False,
    }
    assert moe_summary(None) == zero


def test_moe_summary_dense_is_not_moe(tmp_path):
    meta = read_meta(_gguf(tmp_path / "dense.gguf", _dense_kvs()))
    assert moe_summary(meta) == {
        "layers": 0,
        "experts_total": 0,
        "experts_active": 0,
        "experts_per_layer": 0,
        "used_per_layer": 0,
        "is_moe": False,
    }


def test_moe_summary_without_used_count(tmp_path):
    """只写了 expert_count 的模型也要能报总专家数，激活数落 0。"""
    kvs = _dense_kvs("llama") + [("llama.expert_count", T_U32, 16)]
    meta = read_meta(_gguf(tmp_path / "partial.gguf", kvs))
    summary = moe_summary(meta)
    assert summary["is_moe"] is True
    assert summary["experts_total"] == 33 * 16
    assert summary["experts_active"] == 0
