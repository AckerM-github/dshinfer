#!/usr/bin/env bash
# ops.sh — dshinfer 容器运维入口（一个脚本收掉日常命令）
#
#   bash docker/ops.sh <命令> [参数]
#
# 自动适配：在 WSL2 里叠加 docker/compose.wsl.yaml，需要时自动套 sudo。
# 也适用于 Linux + nvidia-container-toolkit 的主机（只读 docker/compose.yaml）。
#
# 命令：
#   up [--build]    构建（可选）并后台启动，等到 /healthz 就绪
#   down            停止并删除容器（保留镜像与日志卷）
#   stop | start    停 / 起
#   restart         重启
#   status          容器、/healthz、显存、磁盘占用总览
#   check           7 项体检：(docker/容器/healthz/API/权重/显存/磁盘)，全通过 exit 0
#   logs [N] [-e] [-f]   服务日志（-e 引擎日志，-f 跟随）
#   bench [args]    停服务 → 一次性容器跑 llama-bench → 自动拉起服务
#   models          打印权重清单并校验目标目录里的文件
#   shell           进入容器 bash
#   tui             容器里打开终端面板（Textual，需要 TTY；q 退出）
set -eo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

CONTAINER=dshinfer
IMAGE=dshinfer:local
HTTP=http://127.0.0.1:8000
BENCH_MODEL="${BENCH_MODEL:-/models/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic.Q4_K_M.gguf}"

is_wsl() { grep -qi microsoft /proc/version 2>/dev/null; }

if [ -n "${OPS_COMPOSE_FILES:-}" ]; then
  COMPOSE_FILES=$OPS_COMPOSE_FILES
elif is_wsl; then
  COMPOSE_FILES="-f docker/compose.yaml -f docker/compose.wsl.yaml"
else
  COMPOSE_FILES="-f docker/compose.yaml"
fi

SUDO=""
if ! docker info >/dev/null 2>&1; then
  if sudo -n docker info >/dev/null 2>&1; then
    SUDO=sudo
  fi
fi

dc() { $SUDO docker compose $COMPOSE_FILES "$@"; }
d()  { $SUDO docker "$@"; }

# 权重目录：优先取容器 /models 的实际挂载源 —— 那才是容器真正读的目录。
# 直接写 $HOME/models 在 sudo 下会变成 /root/models（sudo 重置 HOME），校验就全 MISSING。
container_models_dir() {
  d inspect -f '{{range .Mounts}}{{if eq .Destination "/models"}}{{.Source}}{{end}}{{end}}' "$CONTAINER" 2>/dev/null | head -n 1
}
if [ -z "${MODELS_DIR:-}" ]; then
  MODELS_DIR=$(container_models_dir)
  [ -n "$MODELS_DIR" ] || MODELS_DIR="$HOME/models"
fi

hr()    { echo '--------------------------------------------------------------'; }
head1() { hr; echo "== $*"; }
ok()    { echo "  [OK]   $*"; }
warn()  { echo "  [WARN] $*"; }
bad()   { echo "  [FAIL] $*"; }
die()   { bad "$*"; exit 1; }

healthz() { curl -fsS --max-time 5 "$HTTP/healthz" 2>/dev/null; }
is_up()   { d ps --filter "name=^/$CONTAINER$" --format '{{.Names}}' 2>/dev/null | grep -q "$CONTAINER"; }

nvidia_smi_bin() {
  if command -v nvidia-smi >/dev/null 2>&1; then echo nvidia-smi
  elif [ -x /usr/lib/wsl/lib/nvidia-smi ]; then echo /usr/lib/wsl/lib/nvidia-smi
  fi
}

# ---------------------------------------------------------------- up / down

cmd_up() {
  local build=0
  for a in "$@"; do [ "$a" = "--build" ] && build=1; done
  if [ "$build" = 1 ]; then
    head1 "构建镜像"
    dc build
  fi
  head1 "启动容器"
  dc up -d
  echo "  等待 $HTTP/healthz（引擎冷加载约 20–30 s，首次更久）..."
  local t0=$SECONDS
  while [ $((SECONDS - t0)) -lt 300 ]; do
    if curl -fsS --max-time 3 "$HTTP/healthz" >/dev/null 2>&1; then
      ok "就绪，用时 $((SECONDS - t0)) s"
      status_brief
      return 0
    fi
    sleep 2
  done
  bad "300 s 内未就绪，看日志：bash docker/ops.sh logs 80"
  return 1
}

cmd_down()   { head1 "停止并删除容器"; dc down; }
cmd_stop()   { head1 "停止容器"; dc stop; }
cmd_start()  { head1 "启动容器（不重建）"; dc start; }
cmd_restart(){ head1 "重启容器"; dc restart; }

status_brief() {
  local st
  st=$(d ps --filter "name=^/$CONTAINER$" --format '  {{.Names}}  {{.Status}}  {{.Ports}}' 2>/dev/null)
  [ -n "$st" ] && echo "$st" || warn "容器未运行"
}

# ---------------------------------------------------------------- status

cmd_status() {
  head1 "容器"
  if is_up; then
    status_brief
  else
    warn "容器未运行（bash docker/ops.sh up）"
  fi

  head1 "/healthz"
  local hz
  if hz=$(healthz) && [ -n "$hz" ]; then
    if command -v python3 >/dev/null 2>&1; then
      echo "$hz" | python3 -m json.tool 2>/dev/null | sed 's/^/  /' || echo "  $hz"
    else
      echo "  $hz"
    fi
  else
    warn "取不到 $HTTP/healthz（容器停了，或引擎还在加载）"
  fi

  head1 "显存"
  local smi
  smi=$(nvidia_smi_bin || true)
  if [ -n "$smi" ]; then
    $smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader | sed 's/^/  /'
  else
    warn "宿主找不到 nvidia-smi（容器内的 llama-server 仍在用 GPU，不影响）"
  fi

  head1 "资源 / 磁盘"
  if is_up; then
    d stats --no-stream --format '  {{.Name}}  CPU {{.CPUPerc}}  MEM {{.MemUsage}}' "$CONTAINER" 2>/dev/null || true
  fi
  d system df 2>/dev/null | sed 's/^/  /' || true
  echo "  权重目录 $MODELS_DIR："
  df -h "$MODELS_DIR" 2>/dev/null | tail -1 | sed 's/^/    /' || true
}

# ---------------------------------------------------------------- check

cmd_check() {
  local rc=0 sev msg
  step() { head1 "$1"; }

  step "1/7 docker"
  if command -v docker >/dev/null 2>&1; then
    ok "$(docker --version 2>/dev/null)"
  else
    bad "docker 不在 PATH"; rc=1
  fi

  step "2/7 容器"
  if d inspect "$CONTAINER" >/dev/null 2>&1; then
    local state
    state=$(d inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$CONTAINER" 2>/dev/null)
    case "$state" in
      "running healthy") ok "running / healthy" ;;
      "running starting") ok "running，healthcheck 还在 start-period（240 s 内属正常）" ;;
      running*)         warn "running / $state" ;;
      *)                bad "状态：$state"; rc=1 ;;
    esac
  else
    bad "容器 $CONTAINER 不存在"; rc=1
  fi

  step "3/7 /healthz 与引擎"
  if hz=$(healthz) && [ -n "$hz" ]; then
    local summary
    summary=$(printf '%s' "$hz" | python3 -c 'import json,sys
d = json.load(sys.stdin)
e = d.get("engine") or {}
u = e.get("upstream") or {}
print("%s | engine=%s model=%s ready=%s loading=%s load_s=%s upstream=%s" % (
    d.get("status"), e.get("engine"), e.get("model"), e.get("ready"),
    e.get("loading"), e.get("load_seconds"), u.get("status")))
if e.get("last_error"):
    print("last_error:", e["last_error"])' 2>/dev/null || echo "$hz")
    echo "  $summary"
    case "$summary" in
      *"ready=True"*) ok "引擎就绪" ;;
      *) warn "引擎未就绪（加载中或上次失败）" ;;
    esac
  else
    bad "取不到 $HTTP/healthz"; rc=1
  fi

  step "4/7 OpenAI 接口"
  local models_json n
  if models_json=$(curl -fsS --max-time 8 "$HTTP/v1/models" 2>/dev/null); then
    n=$(printf '%s' "$models_json" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("data", [])))' 2>/dev/null || echo "?")
    ok "/v1/models 列出 $n 个模型"
  else
    bad "/v1/models 不响应"; rc=1
  fi

  step "5/7 权重校验（models/models.json）"
  if [ -f scripts/fetch-model.sh ]; then
    # 日志用 mktemp：固定路径 /tmp/xxx 在 sudo 下会踩 fs.protected_regular ——
    # 同一个文件先前被普通用户建过（0644、属主 linc）时，root 带 O_CREAT 打开它会被
    # 内核直接拒绝（Permission denied），于是校验脚本其实跑通了、这里却报 FAIL。
    local clog=""
    clog=$(mktemp "${TMPDIR:-/tmp}/ops-check-models.XXXXXX" 2>/dev/null) || clog=""
    if [ -n "$clog" ]; then
      if bash scripts/fetch-model.sh --check --target "$MODELS_DIR" > "$clog" 2>&1; then
        grep -c '^OK' "$clog" | sed 's/^/  /' | { read -r c; ok "$c 个文件大小与 sha256 全部通过"; }
        grep '^OK' "$clog" | sed 's/^/    /'
      else
        bad "校验失败，详见 $clog"; tail -5 "$clog" | sed 's/^/    /'; rc=1
      fi
      rm -f "$clog"
    else
      # 连 mktemp 都不行就直接跑，不打日志
      if bash scripts/fetch-model.sh --check --target "$MODELS_DIR"; then
        ok "权重校验通过（未落日志）"
      else
        bad "权重校验失败"; rc=1
      fi
    fi
  else
    warn "scripts/fetch-model.sh 不存在，跳过"
  fi

  step "6/7 显存"
  local smi line used total pct
  smi=$(nvidia_smi_bin || true)
  if [ -n "$smi" ]; then
    line=$($smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits | head -1)
    used=$(echo "$line" | cut -d, -f1 | tr -d ' ')
    total=$(echo "$line" | cut -d, -f2 | tr -d ' ')
    if [ -n "$used" ] && [ -n "$total" ] && [ "$total" -gt 0 ]; then
      pct=$((used * 100 / total))
      echo "  已用 $used / $total MiB（$pct%）"
      if [ "$pct" -ge 95 ]; then
        warn "显存接近上限，长上下文请求可能 OOM"
      else
        ok "显存有余量"
      fi
    fi
  else
    warn "宿主找不到 nvidia-smi，跳过"
  fi

  step "7/7 磁盘"
  local avail
  avail=$(df -P "$MODELS_DIR" 2>/dev/null | tail -1 | awk '{print $4}')
  if [ -n "$avail" ]; then
    echo "  $MODELS_DIR 剩余 $((avail / 1024 / 1024)) GiB"
    if [ "$avail" -lt 5242880 ]; then warn "剩余不足 5 GiB，下一个模型可能放不下"; fi
  else
    warn "读不到 $MODELS_DIR 所在分区"
  fi

  hr
  if [ "$rc" = 0 ]; then
    ok "体检通过"
  else
    bad "体检有问题（exit $rc）"
  fi
  return "$rc"
}

# ---------------------------------------------------------------- logs

cmd_logs() {
  local n=50 engine=0 follow=0 a
  for a in "$@"; do
    case "$a" in
      -e|--engine) engine=1 ;;
      -f|--follow) follow=1 ;;
      *) n=$a ;;
    esac
  done
  if [ "$engine" = 1 ]; then
    head1 "引擎日志 /var/log/dshinfer/llama-server.log（最后 $n 行）"
    if [ "$follow" = 1 ]; then d exec "$CONTAINER" tail -f -n "$n" /var/log/dshinfer/llama-server.log
    else d exec "$CONTAINER" tail -n "$n" /var/log/dshinfer/llama-server.log; fi
  else
    head1 "服务日志（最后 $n 行）"
    if [ "$follow" = 1 ]; then dc logs -f --tail "$n"; else dc logs --tail "$n" --no-log-prefix 2>&1 | tail -n "$n"; fi
  fi
}

# ---------------------------------------------------------------- bench

# 必须在顶层定义、用全局变量：EXIT trap 触发时 cmd_bench 的 local 已经消失
BENCH_WAS_UP=0
BENCH_STAY_DOWN=0
BENCH_RESTARTED=0
bench_restore() {
  if [ "${BENCH_WAS_UP:-0}" = 1 ] && [ "${BENCH_STAY_DOWN:-0}" = 0 ] && [ "${BENCH_RESTARTED:-0}" = 0 ]; then
    BENCH_RESTARTED=1
    echo "  重新拉起服务..."
    dc up -d >/dev/null
    local t0=$SECONDS
    while [ $((SECONDS - t0)) -lt 120 ]; do
      if curl -fsS --max-time 3 "$HTTP/healthz" >/dev/null 2>&1; then
        echo "  已就绪，用时 $((SECONDS - t0)) s"
        return 0
      fi
      sleep 2
    done
    echo "  容器起来了但 120 s 内未就绪，看日志：bash docker/ops.sh logs 80"
  fi
}

cmd_bench() {
  local stay_down=0 a
  local args=()
  for a in "$@"; do
    case "$a" in
      --stay-down) stay_down=1 ;;
      *) args+=("$a") ;;
    esac
  done
  [ "${#args[@]}" -eq 0 ] && args=(-p 512 -n 128 -ngl 99 -fa 1)

  d image inspect "$IMAGE" >/dev/null 2>&1 || die "镜像 $IMAGE 不存在，先跑 bash docker/ops.sh up --build"

  BENCH_WAS_UP=0
  BENCH_STAY_DOWN=$stay_down
  BENCH_RESTARTED=0
  is_up && BENCH_WAS_UP=1

  if [ "$BENCH_WAS_UP" = 1 ]; then
    head1 "先停服务释放显存"
    dc stop
  fi

  trap bench_restore EXIT

  local gpu_opts=()
  if is_wsl; then
    gpu_opts=(-v /usr/lib/wsl:/usr/lib/wsl:ro --device /dev/dxg
              -e LD_LIBRARY_PATH=/usr/lib/wsl/lib:/opt/llama/bin:/opt/llama/lib)
  else
    gpu_opts=(--gpus all)
  fi

  head1 "llama-bench $BENCH_MODEL ${args[*]}"
  local rc=0
  d run --rm -v "$MODELS_DIR:/models:ro" "${gpu_opts[@]}" "$IMAGE" \
    /opt/llama/bin/llama-bench -m "$BENCH_MODEL" "${args[@]}" || rc=$?
  bench_restore
  return "$rc"
}

# ---------------------------------------------------------------- models

cmd_models() {
  [ -f scripts/fetch-model.sh ] || die "scripts/fetch-model.sh 不在当前 checkout（$(pwd)）里"
  head1 "权重清单"
  bash scripts/fetch-model.sh --list
  hr
  head1 "校验 $MODELS_DIR"
  bash scripts/fetch-model.sh --check --target "$MODELS_DIR"
}

cmd_shell() { d exec -it "$CONTAINER" bash; }

# ---------------------------------------------------------------- tui

# 面板必须在容器里跑：引擎的 8014/metrics 只绑容器内的 127.0.0.1，宿主看不到；
# NVML 也只在容器里挂着（compose.wsl.yaml 把 /usr/lib/wsl 只读挂进去）。
cmd_tui() {
  if [ "$(d inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != "true" ]; then
    die "容器 $CONTAINER 没在跑：先 bash docker/ops.sh up"
  fi
  # -it 两个都要：Textual 要 TTY 才能接管屏幕；不带 -i 连按键都收不到
  d exec -it -e TERM="${TERM:-xterm-256color}" "$CONTAINER" \
    python -m dshinfer.tui --monitor "$@"
}

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

case "${1:-help}" in
  up)      shift; cmd_up "$@" ;;
  down)    cmd_down ;;
  stop)    cmd_stop ;;
  start)   cmd_start ;;
  restart) cmd_restart ;;
  status)  cmd_status ;;
  check)   cmd_check ;;
  logs)    shift; cmd_logs "$@" ;;
  bench)   shift; cmd_bench "$@" ;;
  models)  cmd_models ;;
  shell)   cmd_shell ;;
  tui)     shift; cmd_tui "$@" ;;
  help|-h|--help) usage ;;
  *) bad "未知命令：$1"; echo; usage; exit 2 ;;
esac
