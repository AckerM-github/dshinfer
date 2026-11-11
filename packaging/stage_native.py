#!/usr/bin/env python3
"""把原生库摊进 packaging/native/<platform>/，供 build_engine_wheel.py 打包。

两个来源：
  --from-dir   Windows：从 runtime 目录拷贝 *.dll
  --from-wheel Linux：从 llama-cpp-python 的 manylinux wheel 里取 .so
               （为什么不是 llama.cpp 官方 tar.gz，见 docs 里那一节：官方 Linux 包是
                GGML_BACKEND_DL=ON 编的，与本绑定 0.3.36 的 ctypes 调用约定对不上，
                decode 会跳 0x100 崩溃；manylinux wheel 里的那份才是静态后端、逐字节配套的）

用法：
  python packaging/stage_native.py --platform win_amd64 --from-dir D:\\llm\\runtime\\llama-inproc\\bin
  python packaging/stage_native.py --platform manylinux_2_35_x86_64 --from-wheel D:\\...\\llama_cpp_python-0.3.36-py3-none-manylinux_2_35_x86_64.whl
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NATIVE = ROOT / "native"

# Linux 要保留的文件名。wheel 是 zip，装出来没有符号链接，所以下面每个"别名"都得是实文件；
# 这七个是 libllama / libmtmd 的 DT_NEEDED 闭包加上 ctypes 直接打开的名字，多一个都不带
# （把 .so / .so.0 / .so.0.x.y 三份全带上会让 922 MB 的 libggml-cuda 复制三遍）。
LINUX_KEEP = (
    "libllama.so",        # ctypes 按 base name 找的就是它
    "libllama.so.0",      # libmtmd 的 DT_NEEDED
    "libggml.so.0",       # libllama 的 DT_NEEDED
    "libggml-base.so.0",  # 所有人共用
    "libggml-cpu.so.0",   # libggml 的 DT_NEEDED（静态后端，不是 arch 插件）
    "libggml-cuda.so.0",  # libggml 的 DT_NEEDED
    "libmtmd.so.0",       # 多模态，libllama 可选依赖
)


def stage_from_dir(dest: Path, src: Path) -> list[Path]:
    files = sorted(p for p in src.glob("*.dll") if p.is_file())
    if not files:
        raise SystemExit(f"{src} 里没有 .dll")
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(f, dest / f.name)
    return [dest / f.name for f in files]


def stage_from_wheel(dest: Path, wheel: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with zipfile.ZipFile(wheel) as zf:
        names = {
            Path(n).name: n
            for n in zf.namelist()
            if "llama_cpp/lib/" in n and not n.endswith("/")
        }
        missing = [k for k in LINUX_KEEP if k not in names]
        if missing:
            raise SystemExit(f"{wheel.name} 里缺 {missing}")
        for keep in LINUX_KEEP:
            with zf.open(names[keep]) as src, (dest / keep).open("wb") as out:
                shutil.copyfileobj(src, out, length=8 << 20)
            written.append(dest / keep)
    return sorted(written)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--platform", required=True, choices=["win_amd64", "manylinux_2_35_x86_64"])
    ap.add_argument("--from-dir", type=Path, help="Windows：拷贝 *.dll 的目录")
    ap.add_argument("--from-wheel", type=Path, help="Linux：llama-cpp-python 的 manylinux wheel")
    ap.add_argument("--dest", type=Path, help="默认 packaging/native/<platform>")
    args = ap.parse_args(argv)

    dest = args.dest or (NATIVE / args.platform)
    if dest.exists():
        shutil.rmtree(dest)

    if args.platform == "win_amd64":
        if not args.from_dir:
            raise SystemExit("win_amd64 需要 --from-dir")
        files = stage_from_dir(dest, args.from_dir)
    else:
        if not args.from_wheel:
            raise SystemExit("manylinux 需要 --from-wheel")
        files = stage_from_wheel(dest, args.from_wheel)

    total = sum(f.stat().st_size for f in files)
    for f in files:
        print(f"  {f.name:32s} {f.stat().st_size / 1024 / 1024:9.1f} MB")
    print(f"{len(files)} 个文件，合计 {total / 1024 / 1024 / 1024:.2f} GB → {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
