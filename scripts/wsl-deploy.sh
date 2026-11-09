#!/usr/bin/env bash
# wsl-deploy.sh — git 单源部署：同步仓库 → 构建镜像 → 起容器 → 等 /healthz
#
#   bash scripts/wsl-deploy.sh [选项]
#
# 设计：代码只有一份事实来源（git 仓库），WSL 里那份是 clone 出来的运行副本。
# 每次部署 = git 同步 + docker/ops.sh up --build。不再手工 cp，避免两份代码漂移。
#
# 选项：
#   --fresh          备份现有目录后重新 clone（首次迁移用；旧目录改名为 <dir>.bak.<时间戳>）
#   --no-build       跳过镜像构建，只重启容器
#   --no-up          只同步代码，不动容器
#   --source PATH    首次 clone 的来源（默认 /mnt/d/llm/infer，也可以填 GitHub 地址）
#   --branch NAME    分支（默认 main）
#   --dir PATH       目标目录（默认 $HOME/infer）
#   -h, --help       显示本帮助
#
# 环境变量同选项：REPO_DIR / SOURCE / BRANCH / DEPLOY_BUILD / DEPLOY_UP
set -eo pipefail

# 本脚本会 git pull 到自己：先把原始参数与自身指纹存下来，同步后若自己变了就 re-exec
# （bash 按字节偏移继续读同一个文件，边拉边执行会读到错位的内容）
ORIG_ARGS=("$@")
SELF_SUM=$(sha256sum "$0" 2>/dev/null | cut -d' ' -f1 || true)

REPO_DIR="${REPO_DIR:-$HOME/infer}"
SOURCE="${SOURCE:-/mnt/d/llm/infer}"
BRANCH="${BRANCH:-main}"
DO_BUILD="${DEPLOY_BUILD:-1}"
DO_UP="${DEPLOY_UP:-1}"
FRESH=0

hr()    { echo '--------------------------------------------------------------'; }
head1() { hr; echo "== $*"; }
ok()    { echo "  [OK]   $*"; }
warn()  { echo "  [WARN] $*"; }
die()   { echo "  [FAIL] $*"; exit 1; }

# WSL 里 docker 通常要 sudo（docs/docker.md 10.2 装的是 apt 的 docker.io）
SUDO=""
if ! docker info >/dev/null 2>&1; then
  sudo -n docker info >/dev/null 2>&1 && SUDO=sudo
fi
d() { $SUDO docker "$@"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --fresh)    FRESH=1 ;;
    --no-build) DO_BUILD=0 ;;
    --no-up)    DO_UP=0 ;;
    --source)   shift; SOURCE="${1:?--source 需要路径}" ;;
    --branch)   shift; BRANCH="${1:?--branch 需要名字}" ;;
    --dir)      shift; REPO_DIR="${1:?--dir 需要路径}" ;;
    -h|--help)  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)          die "未知参数：$1（-h 看用法）" ;;
  esac
  shift
done

# ---------------------------------------------------------------- 1/4 前置

head1 "1/4 前置检查"
command -v git >/dev/null 2>&1 || die "git 不可用：sudo apt install -y git"
ok "$(git --version)"
command -v docker >/dev/null 2>&1 || die "docker 不可用（见 docs/docker.md 10.2）"
ok "$(docker --version 2>/dev/null)"
[ -e "$SOURCE" ] || [ "$FRESH" = 0 ] || die "来源 $SOURCE 不存在，用 --source 指定"
echo "  运行副本：$REPO_DIR"
echo "  来源    ：$SOURCE（仅首次 clone 用）"

# ---------------------------------------------------------------- 2/4 同步代码

head1 "2/4 同步代码"

if [ -e "$REPO_DIR" ] && [ ! -d "$REPO_DIR/.git" ]; then
  if [ "$FRESH" = 0 ]; then
    die "$REPO_DIR 不是 git 仓库（旧的 cp 副本？）。用 --fresh 迁移：会改名备份后重新 clone"
  fi
fi

if [ "$FRESH" = 1 ] && [ -e "$REPO_DIR" ]; then
  BACKUP="$REPO_DIR.bak.$(date +%Y%m%d-%H%M%S)"
  mv "$REPO_DIR" "$BACKUP"
  ok "旧目录已备份：$BACKUP"
fi

if [ ! -d "$REPO_DIR/.git" ]; then
  git clone -q --branch "$BRANCH" "$SOURCE" "$REPO_DIR" || die "clone 失败：$SOURCE"
  ok "已 clone $SOURCE → $REPO_DIR（分支 $BRANCH）"
  # .env 只服务「Windows 原生运行」，容器配置来自 compose；老副本里有就顺手带过来
  for cand in "$REPO_DIR".bak.*/.env "$SOURCE/.env"; do
    if [ -f "$cand" ] && [ ! -f "$REPO_DIR/.env" ]; then
      cp "$cand" "$REPO_DIR/.env" && echo "  已带上 $(basename "$(dirname "$cand")")/.env（容器不读它）"
      break
    fi
  done
else
  cd "$REPO_DIR"
  git rev-parse --verify --quiet "refs/heads/$BRANCH" >/dev/null || \
    die "本地没有分支 $BRANCH（当前 $(git rev-parse --abbrev-ref HEAD)）"
  BEFORE=$(git rev-parse HEAD)
  git pull --ff-only origin "$BRANCH" || die "git pull 失败：先处理 $REPO_DIR 里的本地改动（git status）"
  AFTER=$(git rev-parse HEAD)
  if [ "$BEFORE" = "$AFTER" ]; then
    ok "已是最新：$(git log -1 --oneline)"
  else
    ok "已更新：$(git log -1 --oneline)"
    git log --oneline "$BEFORE..$AFTER" | sed 's/^/    /'
  fi
fi

cd "$REPO_DIR"
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  warn "运行副本有未提交改动（下面这些，重建镜像时会被打进镜像）："
  git status --porcelain | sed 's/^/    /'
fi
echo "  当前版本：$(git rev-parse --short HEAD) $(git log -1 --format=%s)"

# 刚 pull 下来的版本里可能就包含本脚本的新实现；bash 已经按旧偏移读过一段了，
# 继续执行会读到错位内容（实测表现为汇总行少 sudo、输出串行）。
NEW_SELF="$REPO_DIR/scripts/wsl-deploy.sh"
if [ -f "$NEW_SELF" ] && [ -n "$SELF_SUM" ] && [ "${DEPLOY_REEXEC:-0}" != 1 ]; then
  NEW_SUM=$(sha256sum "$NEW_SELF" 2>/dev/null | cut -d' ' -f1 || true)
  if [ -n "$NEW_SUM" ] && [ "$NEW_SUM" != "$SELF_SUM" ]; then
    ok "部署脚本自身有新版本，用它重新执行一遍"
    exec env DEPLOY_REEXEC=1 bash "$NEW_SELF" "${ORIG_ARGS[@]}"
  fi
fi

# ---------------------------------------------------------------- 3/4 ~ 4/4 构建 + 启动

if [ "$DO_UP" = 0 ]; then
  head1 "3/4 跳过构建与启动（--no-up）"
  exit 0
fi

if [ "$DO_BUILD" = 1 ]; then
  head1 "3/4 构建镜像（未改动的层会命中缓存，通常几秒）"
  bash docker/ops.sh up --build
else
  head1 "3/4 跳过构建（--no-build）"
  bash docker/ops.sh up
fi

head1 "4/4 结果"
# /healthz 已就绪，但容器自带的 healthcheck 有 interval/start-period，此刻常是 starting；
# 等它变 healthy 再打印，省得下次看到「Up 4 seconds (health: starting)」又怀疑一次。
for _i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  _health=$(d inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' dshinfer 2>/dev/null || true)
  [ "$_health" = healthy ] && break
  [ "$_health" = none ] && break
  sleep 2
done
if [ "${_health:-}" = healthy ]; then
  ok "容器 healthcheck 已转 healthy"
else
  warn "容器 healthcheck 仍是 ${_health:-?}（服务 /healthz 已就绪，可能还在 start-period 内）"
fi
echo "  代码版本：$(git rev-parse --short HEAD)"
echo "  镜像    ：$(d images dshinfer:local --format '{{.ID}}  {{.CreatedSince}}  {{.Size}}' 2>/dev/null | head -1)"
echo "  容器    ：$(d ps --filter name=dshinfer --format '{{.Status}}' 2>/dev/null | head -1)"
echo "  下一步  ：bash docker/ops.sh check   # 7 项体检"
