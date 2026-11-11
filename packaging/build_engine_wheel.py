#!/usr/bin/env python3
"""把 vendor/llama_cpp + packaging/native/<platform>/ 组装成一个平台 wheel。

没有构建后端、没有编译：wheel 就是一个 zip，里面放
  llama_cpp/*.py               ← 逐字节来自 vendor/llama_cpp/（源码真相源）
  llama_cpp/lib/*              ← packaging/native/<platform>/ 里的原生库
  dshinfer_engine-<v>.dist-info/{METADATA,WHEEL,RECORD}

用法：
  python packaging/build_engine_wheel.py --platform win_amd64
  python packaging/build_engine_wheel.py --platform manylinux_2_35_x86_64
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
VENDOR = PROJECT / "vendor" / "llama_cpp"
ENGINE = ROOT / "engine"
DIST = ROOT / "dist"

PLATFORMS = ("win_amd64", "manylinux_2_35_x86_64")
NATIVE_GLOBS = {
    "win_amd64": ("*.dll",),
    "manylinux_2_35_x86_64": ("*.so", "*.so.*"),
}

# 超过这个大小的文件用 STORED：DLL/SO 本身熵很高，deflate 收益个位数百分比，
# 却要花几分钟 CPU。小文件仍然压缩。
STORE_THRESHOLD = 64 * 1024 * 1024


def sha256_b64(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8 << 20), b""):
            h.update(chunk)
    return base64.urlsafe_b64encode(h.digest()).rstrip(b"=").decode("ascii")


def read_meta() -> dict:
    with (ENGINE / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]


def build_metadata(meta: dict) -> str:
    readme = (ENGINE / meta.get("readme", "README.md")).read_text(encoding="utf-8")
    lines = [
        "Metadata-Version: 2.1",
        f"Name: {meta['name']}",
        f"Version: {meta['version']}",
        f"Summary: {meta.get('description', '')}",
        f"License: {meta.get('license', {}).get('text', 'MIT')}",
        f"Requires-Python: {meta.get('requires-python', '>=3.10')}",
        "Classifier: License :: OSI Approved :: MIT License",
        "Classifier: Programming Language :: Python :: 3",
        "Classifier: Topic :: Scientific/Engineering :: Artificial Intelligence",
        "Description-Content-Type: text/markdown",
    ]
    for dep in meta.get("dependencies", []):
        lines.append(f"Requires-Dist: {dep}")
    for name, url in meta.get("urls", {}).items():
        lines.append(f"Project-URL: {name}, {url}")
    return "\n".join(lines) + "\n\n" + readme


def build_wheel(platform: str, version: str | None, out_dir: Path) -> Path:
    meta = read_meta()
    version = version or meta["version"]
    dist_name = meta["name"].replace("-", "_")
    wheel_name = f"{dist_name}-{version}-py3-none-{platform}.whl"

    native_dir = ROOT / "native" / platform
    if not native_dir.is_dir():
        raise SystemExit(f"缺少原生库目录：{native_dir}（先跑 packaging/stage_native.py）")

    natives: list[Path] = []
    for pattern in NATIVE_GLOBS[platform]:
        natives.extend(p for p in native_dir.glob(pattern) if p.is_file())
    natives = sorted(set(natives))
    if not natives:
        raise SystemExit(f"{native_dir} 里没有可打包的库")

    sources = sorted(VENDOR.glob("*.py"))
    if len(sources) < 15:
        raise SystemExit(f"vendor/llama_cpp 里只有 {len(sources)} 个 .py，预期 15 个")

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / wheel_name
    dist_info = f"{dist_name}-{version}.dist-info"
    record: list[tuple[str, str, int]] = []

    print(f"打包 {wheel_name}")
    print(f"  Python : {len(sources)} 个 .py  ← {VENDOR}")
    print(f"  原生库 : {len(natives)} 个      ← {native_dir}")

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for src in sources:
            arc = f"llama_cpp/{src.name}"
            zf.write(src, arc, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
            record.append((arc, sha256_file(src), src.stat().st_size))

        for src in natives:
            arc = f"llama_cpp/lib/{src.name}"
            big = src.stat().st_size > STORE_THRESHOLD
            zf.write(
                src,
                arc,
                compress_type=zipfile.ZIP_STORED if big else zipfile.ZIP_DEFLATED,
                compresslevel=None if big else 9,
            )
            record.append((arc, sha256_file(src), src.stat().st_size))

        payloads = {
            f"{dist_info}/METADATA": build_metadata(meta).encode("utf-8"),
            f"{dist_info}/WHEEL": (
                "Wheel-Version: 1.0\n"
                "Generator: dshinfer-build 1.0\n"
                "Root-Is-Purelib: false\n"
                f"Tag: py3-none-{platform}\n"
            ).encode("utf-8"),
        }
        for name in (f"{dist_info}/NOTICE.md", f"{dist_info}/README.md"):
            src = ENGINE / Path(name).name
            if src.is_file():
                payloads[name] = src.read_bytes()

        for arc, data in payloads.items():
            zf.writestr(arc, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
            record.append((arc, sha256_b64(data), len(data)))

        record_path = f"{dist_info}/RECORD"
        lines = [f"{path},sha256={digest},{size}" for path, digest, size in record]
        lines.append(f"{record_path},,")
        zf.writestr(record_path, "\n".join(lines) + "\n", compress_type=zipfile.ZIP_DEFLATED)

    size_gb = out.stat().st_size / 1024 / 1024 / 1024
    print(f"产物：{out}  ({size_gb:.2f} GB)")
    print(f"sha256={hashlib.sha256(out.read_bytes()).hexdigest()}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--platform", required=True, choices=PLATFORMS)
    ap.add_argument("--version", help="默认取 packaging/engine/pyproject.toml")
    ap.add_argument("--out", type=Path, default=DIST)
    args = ap.parse_args(argv)
    build_wheel(args.platform, args.version, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
