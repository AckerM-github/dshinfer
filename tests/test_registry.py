"""注册表 + GGUF 头部解析。用现场写出来的最小 GGUF 文件测，不依赖那 5.6 GB 的大文件。"""

from __future__ import annotations

import struct

from dshinfer.gguf import read_meta
from dshinfer.registry import ModelNotFound, ModelRegistry

T_U32, T_STR, T_U64 = 4, 8, 10


def _gguf(path, *, arch="qwen35", layers=33, ctx=262144, embd=4096, version=3):
    """写一个只有元数据、没有张量的 GGUF。"""
    kvs = [
        ("general.architecture", T_STR, arch),
        (f"{arch}.block_count", T_U32, layers),
        (f"{arch}.context_length", T_U32, ctx),
        (f"{arch}.embedding_length", T_U32, embd),
        ("tokenizer.ggml.model", T_STR, "gpt2"),
    ]

    def wstr(s: str) -> bytes:
        b = s.encode("utf-8")
        return struct.pack("<Q", len(b)) + b

    out = bytearray(b"GGUF")
    out += struct.pack("<I", version)
    out += struct.pack("<Q", 0)  # tensor_count
    out += struct.pack("<Q", len(kvs))
    for key, typ, val in kvs:
        out += wstr(key)
        out += struct.pack("<I", typ)
        out += wstr(val) if typ == T_STR else struct.pack("<I", val)
    path.write_bytes(bytes(out))
    return path


def test_read_meta_parses_header(tmp_path):
    p = _gguf(tmp_path / "demo.Q4_K_M.gguf")
    meta = read_meta(p)
    assert meta is not None
    assert meta.arch == "qwen35"
    assert meta.n_layers == 33
    assert meta.n_ctx_train == 262144
    assert meta.n_embd == 4096


def test_read_meta_rejects_garbage(tmp_path):
    p = tmp_path / "bad.gguf"
    p.write_bytes(b"NOTGGUF" + b"\x00" * 32)
    assert read_meta(p) is None


def test_registry_scans_directory(tmp_path):
    _gguf(tmp_path / "a.gguf", arch="llama", layers=32)
    _gguf(tmp_path / "b.gguf", arch="qwen35", layers=33)
    reg = ModelRegistry(str(tmp_path))
    ids = [e.id for e in reg.scan()]
    assert sorted(ids) == ["a", "b"]
    entry = reg.get("b")
    assert entry is not None and entry.n_layers == 33 and entry.size_gb >= 0


def test_registry_default_id_wins(tmp_path):
    target = _gguf(tmp_path / "long-name.Q4_K_M.gguf")
    _gguf(tmp_path / "other.gguf")
    reg = ModelRegistry(str(tmp_path), default_path=str(target), default_id="friendly-name")
    ids = {e.id for e in reg.scan()}
    assert "friendly-name" in ids and "long-name.Q4_K_M" not in ids
    assert reg.resolve("friendly-name").path == str(target)


def test_registry_resolve_unknown_raises(tmp_path):
    _gguf(tmp_path / "a.gguf")
    reg = ModelRegistry(str(tmp_path))
    try:
        reg.resolve("does-not-exist")
    except ModelNotFound as exc:
        assert "does-not-exist" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应当抛 ModelNotFound")


def test_registry_picks_up_new_file_without_restart(tmp_path):
    reg = ModelRegistry(str(tmp_path))
    assert reg.scan() == []
    _gguf(tmp_path / "late.gguf")
    assert [e.id for e in reg.scan()] == ["late"]
