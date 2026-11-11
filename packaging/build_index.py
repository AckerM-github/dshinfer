#!/usr/bin/env python3
"""为 packaging/dist/ 里的引擎 wheel 生成一个 PEP 503 静态索引。

输出（默认 packaging/index/）：
  simple/index.html                 ← 包列表
  simple/dshinfer-engine/index.html ← 该包的 wheel 链接

链接是绝对的，指向 --base-url（默认是 GitHub Release 的下载前缀），这样这个目录
可以直接扔到任何静态托管（GitHub Pages / 内网 nginx / S3）当私源用：

  pip install dshinfer-engine --extra-index-url https://<host>/whl/
"""

from __future__ import annotations

import argparse
import hashlib
import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
OUT = ROOT / "index"

DEFAULT_BASE = "https://github.com/AckerM-github/dshinfer/releases/download/engine-v0.3.36.post1/"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def page(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n<html>\n<head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title></head>\n<body>\n"
        f"<h1>{html.escape(title)}</h1>\n{body}</body>\n</html>\n"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default=DEFAULT_BASE, help="release 资产的下载前缀，需以 / 结尾")
    ap.add_argument("--dist", type=Path, default=DIST)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)

    wheels = sorted(p for p in args.dist.glob("*.whl") if p.is_file())
    if not wheels:
        raise SystemExit(f"{args.dist} 里没有 wheel")

    base = args.base_url if args.base_url.endswith("/") else args.base_url + "/"
    by_pkg: dict[str, list[Path]] = {}
    for w in wheels:
        # PEP 503 归一化：-_. 折叠成 -，全部小写（dshinfer_engine → dshinfer-engine）
        raw = w.name.split("-")[0]
        by_pkg.setdefault(re.sub(r"[-_.]+", "-", raw).lower(), []).append(w)

    pkg = sorted(by_pkg)[0]
    simple = args.out / "simple"
    pkg_dir = simple / pkg
    pkg_dir.mkdir(parents=True, exist_ok=True)

    links = []
    for w in by_pkg[pkg]:
        digest = sha256(w)
        name = html.escape(w.name)
        href = base + w.name
        links.append(
            f'<a href="{html.escape(href)}#sha256={digest}" '
            f'data-requires-python="&gt;=3.10">{name}</a><br>\n'
        )

    (pkg_dir / "index.html").write_text(
        page(f"Links for {pkg}", "".join(links)), encoding="utf-8", newline="\n"
    )
    (simple / "index.html").write_text(
        page(f"Simple index", f'<a href="{pkg}/">{html.escape(pkg)}</a><br>\n'),
        encoding="utf-8",
        newline="\n",
    )

    total = sum(w.stat().st_size for w in wheels) / 1024 / 1024 / 1024
    print(f"{len(wheels)} 个 wheel（{total:.2f} GB）")
    for w in wheels:
        print(f"  {w.name}  {w.stat().st_size / 1024 / 1024 / 1024:.2f} GB")
    print(f"索引：{simple / 'index.html'}")
    print(f"用法：pip install dshinfer-engine --extra-index-url <host>/simple/")
    print(f"注意：链接前缀是 {base}（用 --base-url 改）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
