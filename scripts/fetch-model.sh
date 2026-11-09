#!/usr/bin/env bash
# fetch-model.sh — 按 models/models.json 校验 / 下载 / 生成 SHA256SUMS
#
# 权重本体不进 git、不进镜像（GitHub 单文件 100 MB 硬限制、二进制不可 diff）。
# 进 git 的只有三样：models/models.json、models/SHA256SUMS、本脚本。
#
# 退出码：0 全部通过 / 2 用法或环境错 / 3 缺文件 / 4 大小不符 / 5 sha256 不符
#          / 6 清单无直链（需本地生成）或下载失败
set -eo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
MANIFEST="$ROOT/models/models.json"
SUMS="$ROOT/models/SHA256SUMS"
TARGET="${DSHINFER_MODELS_DIR:-$HOME/models}"
MODE=check
ONLY=""

usage() {
  cat <<'EOF'
用法: scripts/fetch-model.sh [模式] [--only ID] [--target DIR]

模式（默认 --check）:
  --check     校验目标目录里每个文件的大小与 sha256
  --download  下载清单里带 url 的模型（断点续传），下完即校验；
              无 url 的条目打印 provenance 并以 6 退出
  --sums      从清单重新生成 models/SHA256SUMS
  --list      列出清单

选项:
  --only ID     只处理某个 id
  --target DIR  权重目录（默认 $DSHINFER_MODELS_DIR，否则 ~/models）
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --check|--download|--sums|--list) MODE="${1#--}" ;;
    --only)   ONLY="$2"; shift ;;
    --target) TARGET="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

command -v python3 >/dev/null 2>&1 || { echo "需要 python3（解析 JSON 清单）" >&2; exit 2; }
[ -f "$MANIFEST" ] || { echo "找不到清单: $MANIFEST" >&2; exit 2; }

# 每行输出（字段用 0x1f 单元分隔符隔开，空字段不会被折叠）:
#   kind 0x1f id 0x1f relpath 0x1f bytes 0x1f sha256 0x1f url 0x1f note
records() {
  python3 - "$MANIFEST" "$ONLY" <<'PY'
import json
import sys

manifest, only = sys.argv[1], sys.argv[2]
with open(manifest, encoding="utf-8") as fh:
    data = json.load(fh)

rows = 0
for m in data.get("models", []):
    if only and m.get("id") != only:
        continue
    prov = m.get("provenance") or {}
    print("\x1f".join([
        str(prov.get("kind", "-")),
        m["id"],
        m["file"],
        str(m["bytes"]),
        m["sha256"],
        m.get("url") or "",
        (m.get("note") or "").replace("\x1f", " "),
    ]))
    rows += 1

if rows == 0:
    sys.exit("清单里没有匹配的模型（--only %r）" % only)
PY
}

explain() {
  python3 - "$MANIFEST" "$1" <<'PY'
import json
import sys

manifest, mid = sys.argv[1], sys.argv[2]
with open(manifest, encoding="utf-8") as fh:
    for m in json.load(fh).get("models", []):
        if m.get("id") == mid:
            print(json.dumps(m.get("provenance") or {}, ensure_ascii=False, indent=2))
PY
}

sha_of() { sha256sum "$1" | cut -d' ' -f1; }
size_of() { wc -c < "$1" | tr -d ' '; }
US=$(printf '\037')

case "$MODE" in
  list)
    echo "清单: $MANIFEST"
    echo "目标: $TARGET"
    records | while IFS="$US" read -r kind id rel bytes sha url note; do
      printf '%-22s %-11s %13s B  %s\n' "$id" "$kind" "$bytes" "$rel"
    done
    ;;

  sums)
    {
      records | while IFS="$US" read -r kind id rel bytes sha url note; do
        printf '%s  %s\n' "$sha" "$rel"
      done
    } | sort -k2 > "$SUMS.tmp"
    mv "$SUMS.tmp" "$SUMS"
    echo "已写入 $SUMS"
    cat "$SUMS"
    ;;

  download)
    rc=0
    while IFS="$US" read -r kind id rel bytes sha url note; do
      dest="$TARGET/$rel"
      mkdir -p "$(dirname "$dest")"
      if [ -f "$dest" ] && [ "$(size_of "$dest")" = "$bytes" ] && [ "$(sha_of "$dest")" = "$sha" ]; then
        echo "SKIP      $id（已存在且校验通过）"
        continue
      fi
      if [ -z "$url" ]; then
        echo "NO-URL    $id：清单里没有直链，需要本地量化生成。provenance:"
        explain "$id" | sed 's/^/            /'
        rc=6
        continue
      fi
      echo "GET       $id <- $url"
      if ! curl -fL --retry 3 --retry-delay 3 -C - -o "$dest.part" "$url"; then
        echo "FETCH-FAIL $id（保留 $dest.part 供续传）" >&2
        rc=6
        continue
      fi
      if [ "$(sha_of "$dest.part")" != "$sha" ]; then
        echo "SHA-FAIL  $id：下载内容与清单不符，已改名为 $dest.part.bad" >&2
        mv "$dest.part" "$dest.part.bad"
        rc=5
        continue
      fi
      mv "$dest.part" "$dest"
      echo "OK        $id"
    done < <(records)
    exit "$rc"
    ;;

  check)
    rc=0
    found=0
    while IFS="$US" read -r kind id rel bytes sha url note; do
      found=$((found + 1))
      dest="$TARGET/$rel"
      if [ ! -f "$dest" ]; then
        echo "MISSING        $id  ($dest)"
        rc=3
        continue
      fi
      actual_size=$(size_of "$dest")
      if [ "$actual_size" != "$bytes" ]; then
        echo "SIZE-MISMATCH  $id  期望 $bytes B，实际 $actual_size B  ($dest)"
        rc=4
        continue
      fi
      actual_sha=$(sha_of "$dest")
      if [ "$actual_sha" = "$sha" ]; then
        echo "OK             $id  $actual_size B  $dest"
      else
        echo "SHA-MISMATCH   $id  期望 $sha，实际 $actual_sha  ($dest)"
        rc=5
      fi
    done < <(records)
    echo "---"
    if [ "$rc" = 0 ]; then
      echo "检查 $found 个文件，全部通过（目标目录 $TARGET）"
    else
      echo "检查 $found 个文件，有问题（退出码 $rc，目标目录 $TARGET）"
    fi
    exit "$rc"
    ;;

  *)
    usage
    exit 2
    ;;
esac
