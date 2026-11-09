"""内置监控面板：GET /dashboard（页面）+ GET /admin/live（快照 JSON）。

为什么自己写而不是上 Grafana：数据源都在进程里/容器里，加一套 Prometheus+Grafana
要多吃 300–500 MB 内存，而这个项目跑在 7.5 GB 的 WSL 里、显存只剩 2 GB。
这里零新依赖，只用标准库 + 已有的 prometheus_client。

三路数据，全部以「拿不到就降级成 null」的方式采集，任何一路坏掉页面仍能开：

1. 本进程的 Prometheus registry（metrics.py）——请求数、令牌、TTFT/延迟分位、在飞、排队；
2. llama-server 的 /metrics（它的名字带 llamacpp: 前缀，不是合法 Prometheus 命名，
   所以直接按文本解析）——实时 tok/s、prompt tok/s、processing/deferred 请求数；
3. NVML——ctypes 直接打开 libnvidia-ml.so.1。WSL 的容器里是 /usr/lib/wsl/lib/ 下的
   宿主驱动库（compose 把整个 /usr/lib/wsl 挂进来了），没有 nvidia-smi 二进制也能读
   显存/利用率/温度/功耗。

前端只做两件事：每 2 s 拉一次 /admin/live，把数值和历史曲线画到 canvas。历史曲线在
浏览器内存里维护（最多 120 个点 ≈ 4 分钟），服务端不存时序——这样不用引入任何存储。
"""

from __future__ import annotations

import ctypes
import threading
import time
import urllib.request
from typing import Any

from .metrics import REGISTRY

STARTED = time.time()

# ---------------------------------------------------------------- GPU（NVML）


class _MemInfo(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


class _UtilRates(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


_NVML_PATHS = (
    "/usr/lib/wsl/lib/libnvidia-ml.so.1",  # WSL：容器里 bind mount 进来的宿主驱动
    "/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1",
    "/usr/local/nvidia/lib64/libnvidia-ml.so.1",
    "libnvidia-ml.so.1",
    "libnvidia-ml.so",
)

_nvml_lock = threading.Lock()
_nvml_lib: Any = None
_nvml_handle: Any = None
_nvml_error: str | None = None
_nvml_tried = False

_gpu_cache: dict[str, Any] | None = None
_gpu_cache_at = 0.0


def _nvml_open() -> Any:
    """第一次用时打开 NVML；失败就记住原因，不再反复试、不刷日志。"""
    global _nvml_lib, _nvml_handle, _nvml_error, _nvml_tried
    if _nvml_tried:
        return _nvml_lib
    with _nvml_lock:
        if _nvml_tried:
            return _nvml_lib
        _nvml_tried = True
        for path in _NVML_PATHS:
            try:
                lib = ctypes.CDLL(path)
                if lib.nvmlInit_v2() != 0:
                    raise OSError("nvmlInit_v2 失败")
                handle = ctypes.c_void_p()
                if lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle)) != 0:
                    raise OSError("拿不到 GPU 0 的 handle")
                _nvml_lib, _nvml_handle = lib, handle
                return lib
            except Exception:
                continue
        _nvml_error = "NVML 不可用"
        return None


def gpu_info(ttl: float = 1.5) -> dict[str, Any] | None:
    """显存/利用率/温度/功耗。拿不到返回 None（原生 Windows、无驱动容器都属正常）。"""
    global _gpu_cache, _gpu_cache_at
    now = time.monotonic()
    if _gpu_cache is not None and now - _gpu_cache_at < ttl:
        return _gpu_cache
    lib = _nvml_open()
    if lib is None:
        return None
    try:
        h = _nvml_handle
        mem = _MemInfo()
        lib.nvmlDeviceGetMemoryInfo(h, ctypes.byref(mem))
        util = _UtilRates()
        lib.nvmlDeviceGetUtilizationRates(h, ctypes.byref(util))
        name = ctypes.create_string_buffer(96)
        lib.nvmlDeviceGetName(h, name, ctypes.c_uint(96))
        temp = ctypes.c_uint(0)
        lib.nvmlDeviceGetTemperature(h, 0, ctypes.byref(temp))
        power = ctypes.c_uint(0)
        limit = ctypes.c_uint(0)
        lib.nvmlDeviceGetPowerUsage(h, ctypes.byref(power))
        lib.nvmlDeviceGetEnforcedPowerLimit(h, ctypes.byref(limit))
        info = {
            "name": name.value.decode("utf-8", "replace"),
            "total_mib": round(mem.total / 1048576, 1),
            "used_mib": round(mem.used / 1048576, 1),
            "free_mib": round(mem.free / 1048576, 1),
            "util_pct": int(util.gpu),
            "mem_util_pct": int(util.memory),
            "temp_c": int(temp.value),
            "power_w": round(power.value / 1000, 1),
            "power_limit_w": round(limit.value / 1000, 1),
        }
    except Exception:
        return None
    _gpu_cache, _gpu_cache_at = info, now
    return info


# ---------------------------------------------------------------- 引擎指标

_ENGINE_PREFIX = "llamacpp:"


def engine_metrics(url: str | None, timeout: float = 0.8) -> dict[str, float]:
    """抓 llama-server 的 /metrics。mock 引擎没有这个端点，返回空 dict。"""
    if not url:
        return {}
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception:
        return {}
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        head, _, value = line.rpartition(" ")
        if not head.startswith(_ENGINE_PREFIX):
            continue
        try:
            out[head[len(_ENGINE_PREFIX) :]] = float(value)
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------- 服务层指标


def _quantile(buckets: list[tuple[float, float]], q: float) -> float | None:
    """从累积直方图桶里插值出分位数（Prometheus histogram_quantile 的简化版）。"""
    if not buckets:
        return None
    total = buckets[-1][1]
    if total <= 0:
        return None
    rank = q * total
    prev_le, prev_c = 0.0, 0.0
    for le, c in buckets:
        if c >= rank:
            if le == float("inf"):
                return prev_le
            span = c - prev_c
            if span <= 0:
                return le
            return round(prev_le + (le - prev_le) * (rank - prev_c) / span, 4)
        prev_le, prev_c = le, c
    return buckets[-1][0]


def service_metrics() -> dict[str, Any]:
    """把本进程的 registry 摊平成面板要的形状（只取认识的指标，不做通用暴露格式解析）。"""
    by_status: dict[str, float] = {}
    tokens: dict[str, float] = {}
    events: dict[str, float] = {}
    inflight = 0.0
    queued = 0.0
    raws: dict[str, dict[str, Any]] = {}

    for metric in REGISTRY.collect():
        for sample in metric.samples:
            name = sample.name
            labels = sample.labels
            value = float(sample.value)
            if name == "dshinfer_requests_total":
                key = str(labels.get("status", "?"))
                by_status[key] = by_status.get(key, 0.0) + value
            elif name == "dshinfer_tokens_total":
                direction = str(labels.get("direction", "?"))
                tokens[direction] = tokens.get(direction, 0.0) + value
            elif name == "dshinfer_inflight_requests":
                inflight = value
            elif name == "dshinfer_queue_depth":
                queued = value
            elif name == "dshinfer_scheduler_events_total":
                events[str(labels.get("event", "?"))] = value
            elif name.endswith(("_bucket", "_count", "_sum")):
                base = name.rsplit("_", 1)[0]
                slot = raws.setdefault(base, {"count": 0.0, "sum": 0.0, "buckets": []})
                if name.endswith("_bucket"):
                    le = str(labels.get("le", "inf"))
                    slot["buckets"].append(
                        (float("inf") if le == "+Inf" else float(le), value)
                    )
                elif name.endswith("_count"):
                    slot["count"] = value
                else:
                    slot["sum"] = value

    hist: dict[str, Any] = {}
    for base, slot in raws.items():
        buckets = sorted(slot["buckets"])
        count = slot["count"]
        hist[base] = {
            "count": count,
            "mean_s": round(slot["sum"] / count, 4) if count else None,
            "p50_s": _quantile(buckets, 0.5),
            "p95_s": _quantile(buckets, 0.95),
        }

    return {
        "inflight": inflight,
        "queued": queued,
        "requests_total": round(sum(by_status.values()), 1),
        "requests_by_status": by_status,
        "tokens": tokens,
        "scheduler_events": events,
        "histograms": hist,
    }


_rate_lock = threading.Lock()
_rate_state: dict[str, float | None] = {
    "tokens": None, "secs": None, "wall": None, "wall_tokens": None,
    "ewma": None, "agg": None,
}
# 面板每 2 s 拉一次，但脚本/截图工具可能在毫秒内连打十几次快照——那种间隔里
# 令牌数几乎不变，差分出来是 0 或离谱的峰。要求最小采样间隔，太近就沿用上一次。
_MIN_WALL_GAP = 0.5   # 聚合吞吐的最小墙钟间隔（秒）
_MIN_SECS_GAP = 0.1   # 单条 decode 速度的最小生成时间间隔（秒）


def engine_rate(metrics: dict[str, Any]) -> dict[str, Any]:
    """按引擎累计计数器算"最近吞吐"。

    llama.cpp 的 `predicted_tokens_seconds` 是个采样值（某些时刻会读到 0），
    所以再自己做一次差分：Δtokens / Δ(生成秒数)，得到与墙钟无关的真实速度，
    并用 EWMA 平滑两次采样之间的抖动。
    """
    tokens = metrics.get("tokens_predicted_total")
    secs = metrics.get("tokens_predicted_seconds_total")
    now = time.time()
    with _rate_lock:
        st = _rate_state
        inst: float | None = None
        if tokens is not None and secs is not None:
            # 单条 decode 速度：Δtokens / Δ(引擎累计生成秒数)，与墙钟无关
            if st["tokens"] is not None and st["secs"] is not None:
                dsec = secs - st["secs"]          # type: ignore[operator]
                if dsec >= _MIN_SECS_GAP:
                    dtok = tokens - st["tokens"]  # type: ignore[operator]
                    inst = max(0.0, dtok / dsec)
                    st["ewma"] = inst if st["ewma"] is None else st["ewma"] * 0.4 + inst * 0.6
                    st["tokens"], st["secs"] = tokens, secs
            else:
                st["tokens"], st["secs"] = tokens, secs
            # 聚合吞吐：Δtokens / Δ墙钟，并发时明显高于单条
            if st["wall_tokens"] is not None and st["wall"] is not None:
                dwall = now - st["wall"]
                if dwall >= _MIN_WALL_GAP:
                    agg = (tokens - st["wall_tokens"]) / dwall  # type: ignore[operator]
                    st["agg"] = max(0.0, agg) if st["agg"] is None else st["agg"] * 0.5 + max(0.0, agg) * 0.5
                    st["wall"], st["wall_tokens"] = now, tokens
            else:
                st["wall"], st["wall_tokens"] = now, tokens
        return {
            "instant": None if inst is None else round(inst, 2),
            "window": None if st["ewma"] is None else round(st["ewma"], 2),
            "aggregate": None if st["agg"] is None else round(st["agg"], 2),
        }


def live(
    *,
    engine_url: str | None,
    model: str,
    context_tokens: int,
    max_context_tokens: int,
    engine_kind: str,
    max_concurrency: int,
    queue_limit: int,
    health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """一次快照：页面每 2 s 拿一份，所有字段都可能为 null。"""
    _engine = engine_metrics(engine_url)
    return {
        "ts": round(time.time(), 3),
        "uptime_s": round(time.time() - STARTED, 1),
        "model": {
            "id": model,
            # n_ctx 才是引擎这次真正跑的上下文；max_context_tokens 是模型训练上限，
            # 面板两个都显示，省得拿 65536 去套 32768 的部署。
            "context_tokens": context_tokens,
            "max_context_tokens": max_context_tokens,
            "engine": engine_kind,
            "max_concurrency": max_concurrency,
            "queue_limit": queue_limit,
        },
        "health": health or {},
        "gpu": gpu_info(),
        "engine_metrics": _engine,
        "engine_rate": engine_rate(_engine),
        "service": service_metrics(),
    }

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>dshinfer · 监控面板</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 18px 22px 40px; background: #0d1117; color: #c9d1d9;
         font: 13px/1.5 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
  header { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; margin-bottom: 16px; }
  .brand { font-size: 20px; font-weight: 650; letter-spacing: .3px; color: #e6edf3; }
  .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%;
         background: #6e7681; margin-left: 8px; vertical-align: middle; }
  .dot.ok { background: #3fb950; box-shadow: 0 0 8px #3fb95088; }
  .dot.bad { background: #f85149; }
  .meta { color: #8b949e; font-size: 12px; }
  .meta b { color: #c9d1d9; font-weight: 600; }
  .cards { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); }
  .card { background: #161b22; border: 1px solid #21262d; border-radius: 10px; padding: 12px 14px 8px; }
  .card label { display: block; color: #8b949e; font-size: 11px; letter-spacing: .08em;
                text-transform: uppercase; margin-bottom: 6px; }
  .big { font-size: 26px; font-weight: 600; color: #e6edf3; font-variant-numeric: tabular-nums; }
  .big small { font-size: 12px; font-weight: 400; color: #8b949e; margin-left: 4px; }
  .sub { color: #8b949e; font-size: 11px; min-height: 16px; margin-top: 2px; }
  canvas { display: block; width: 100%; height: 46px; margin-top: 8px; }
  .bar { height: 6px; border-radius: 3px; background: #21262d; overflow: hidden; margin-top: 8px; }
  .bar i { display: block; height: 100%; width: 0; border-radius: 3px;
           background: linear-gradient(90deg, #2f81f7, #d29922); transition: width .4s; }
  .grid2 { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); margin-top: 12px; }
  .panel { background: #161b22; border: 1px solid #21262d; border-radius: 10px; padding: 12px 14px; margin-top: 12px; }
  .grid2 .panel { margin-top: 0; }
  .panel h3 { margin: 0 0 10px; font-size: 12px; color: #8b949e; letter-spacing: .08em; text-transform: uppercase; }
  table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
  th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid #21262d; font-size: 12px; }
  th { color: #8b949e; font-weight: 500; }
  td.num, th.num { text-align: right; }
  td.dim { color: #8b949e; }
  .ok { color: #3fb950; } .warn { color: #d29922; } .bad { color: #f85149; }
  .empty { color: #6e7681; font-size: 12px; padding: 6px 0; }
  footer { color: #6e7681; font-size: 11px; margin-top: 18px; }
</style>
</head>
<body>
<header>
  <div class="brand">dshinfer<span class="dot" id="dot"></span></div>
  <div class="meta">
    模型 <b id="m-model">—</b> · 上下文 <b id="m-ctx">—</b> · 引擎 <b id="m-engine">—</b>
    · 运行 <b id="m-uptime">—</b> · <span id="m-state">连接中…</span>
  </div>
</header>

<section class="cards">
  <div class="card">
    <label>生成速度</label>
    <div class="big"><span id="v-pps">—</span><small>tok/s</small></div>
    <div class="sub" id="v-pps-sub">等待采样</div>
    <canvas id="c-pps"></canvas>
  </div>
  <div class="card">
    <label>显存</label>
    <div class="big"><span id="v-vram">—</span><small>/ <span id="v-vram-total">—</span> MiB</small></div>
    <div class="bar"><i id="v-vram-bar"></i></div>
    <canvas id="c-vram"></canvas>
  </div>
  <div class="card">
    <label>GPU 利用率</label>
    <div class="big"><span id="v-gpu">—</span><small>%</small></div>
    <div class="sub" id="v-gpu-sub">—</div>
    <canvas id="c-gpu"></canvas>
  </div>
  <div class="card">
    <label>并发</label>
    <div class="big"><span id="v-inflight">—</span><small>在飞</small></div>
    <div class="sub" id="v-queue-sub">—</div>
    <canvas id="c-inflight"></canvas>
  </div>
</section>

<section class="grid2">
  <div class="panel">
    <h3>延迟（服务层直方图）</h3>
    <table><thead><tr><th>指标</th><th class="num">p50</th><th class="num">p95</th><th class="num">均值</th><th class="num">样本</th></tr></thead>
      <tbody id="t-lat"><tr><td colspan="5" class="empty">还没有请求</td></tr></tbody></table>
  </div>
  <div class="panel">
    <h3>累计</h3>
    <table><tbody id="t-totals"></tbody></table>
  </div>
</section>

<section class="panel">
  <h3>引擎（llama-server）</h3>
  <table><tbody id="t-engine"></tbody></table>
</section>

<section class="panel">
  <h3>最近请求</h3>
  <table>
    <thead><tr><th>请求</th><th>模型</th><th class="num">队列</th><th class="num">TTFT</th>
      <th class="num">decode</th><th class="num">总耗时</th><th>状态</th></tr></thead>
    <tbody id="t-traces"><tr><td colspan="7" class="empty">还没有请求</td></tr></tbody>
  </table>
</section>

<footer>数据源：本进程 /metrics（服务层）· llama-server /metrics（引擎）· NVML（显存/GPU）·
  每 2 s 刷新，曲线保留最近 4 分钟 · <a href="/metrics" style="color:#2f81f7">原始指标</a> ·
  <a href="/admin/live" style="color:#2f81f7">快照 JSON</a></footer>

<script>
var MAXP = 120;
var H = { pps: [], vram: [], gpu: [], inflight: [] };
var RATE = null;  // 上一帧用于曲线补点的占位（服务端已给差分吞吐）

function fmt(v, d) {
  if (v === null || v === undefined || (typeof v === 'number' && !isFinite(v))) return '—';
  return Number(v).toFixed(d === undefined ? 1 : d);
}
function txt(id, s) { var e = document.getElementById(id); if (e) e.textContent = s; }
function ms(v) { return v === null || v === undefined ? '—' : (Number(v) * 1000).toFixed(0) + ' ms'; }

function spark(id, data, color, floorZero) {
  var cv = document.getElementById(id);
  if (!cv) return;
  var dpr = window.devicePixelRatio || 1;
  var w = cv.clientWidth || 200, h = cv.clientHeight || 46;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  }
  var g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  if (data.length < 2) return;
  var mn = Math.min.apply(null, data), mx = Math.max.apply(null, data);
  if (floorZero) mn = 0;
  if (mx - mn < 1e-9) mx = mn + 1;
  var pad = 3;
  function X(i) { return pad + (w - 2 * pad) * i / (data.length - 1); }
  function Y(v) { return h - pad - (h - 2 * pad) * (v - mn) / (mx - mn); }
  g.beginPath(); g.moveTo(X(0), Y(data[0]));
  for (var i = 1; i < data.length; i++) g.lineTo(X(i), Y(data[i]));
  g.strokeStyle = color; g.lineWidth = 1.6; g.stroke();
  g.lineTo(X(data.length - 1), h); g.lineTo(X(0), h); g.closePath();
  g.fillStyle = color + '22'; g.fill();
}
function push(key, v) {
  if (v === null || v === undefined || !isFinite(v)) return;
  H[key].push(v);
  if (H[key].length > MAXP) H[key].shift();
}

function render(d) {
  var em = d.engine_metrics || {};
  var sv = d.service || {};
  var gpu = d.gpu || null;
  var h = d.health || {};
  // /healthz 的 engine 是字符串（"llama-server"），字段与 ready 同级；
  // 兼容将来嵌套成对象的形态。
  var eng = (h.engine && typeof h.engine === 'object') ? h.engine : h;

  txt('m-model', d.model.id || '—');
  txt('m-ctx', (d.model.context_tokens || 0).toLocaleString());
  var mctx = document.getElementById('m-ctx');
  if (mctx) mctx.title = '引擎本轮的 n_ctx；模型训练上限 ' + (d.model.max_context_tokens || 0).toLocaleString();
  txt('m-engine', d.model.engine || '—');
  var up = d.uptime_s || 0;
  txt('m-uptime', up > 3600 ? (up / 3600).toFixed(1) + ' h' : (up / 60).toFixed(0) + ' min');
  var dot = document.getElementById('dot');
  var ready = eng.ready === true;
  dot.className = 'dot ' + (ready ? 'ok' : (eng.loading ? '' : 'bad'));
  txt('m-state', ready ? '引擎就绪' : (eng.loading ? '引擎加载中…' : '引擎未就绪'));

  // 引擎的 predicted_tokens_seconds 是采样值（空闲/边界会读到 0），
  // 主数字用服务端按计数器差分出的窗口吞吐（/admin/live 的 engine_rate.window）。
  var pps = em['predicted_tokens_seconds'];
  var ptot = em['tokens_predicted_total'];
  var er = d.engine_rate || {};
  // 引擎那两个 *_tokens_seconds 是采样值，空闲或刚切 slot 时会读到 0；
  // 这里改用累计计数器算平均（totals / total_seconds），永远有意义的数。
  var avgGen = (ptot && em['tokens_predicted_seconds_total'])
    ? ptot / em['tokens_predicted_seconds_total'] : null;
  var avgPrompt = (em['prompt_tokens_total'] && em['prompt_seconds_total'])
    ? em['prompt_tokens_total'] / em['prompt_seconds_total'] : null;
  // 优先级：服务端差分窗口 → 引擎采样 gauge（>0 才有意义）→ 累计均值
  var shown = er.window;
  if (shown === null || shown === undefined) shown = (pps > 0) ? pps : avgGen;
  var aggShown = er.aggregate;
  if (aggShown === null || aggShown === undefined) aggShown = '';
  else aggShown = fmt(aggShown, 1);
  txt('v-pps', fmt(shown, 1));
  txt('v-pps-sub', '单条 ' + fmt(shown, 1) + ' · 聚合 ' + aggShown
      + ' · 均值 生成 ' + fmt(avgGen, 1) + ' / prompt ' + fmt(avgPrompt, 0)
      + ' tok/s · 已生成 ' + fmt(ptot, 0) + ' tok');
  push('pps', shown);

  if (gpu) {
    txt('v-vram', fmt(gpu.used_mib, 0));
    txt('v-vram-total', fmt(gpu.total_mib, 0));
    var pct = gpu.total_mib ? (gpu.used_mib / gpu.total_mib) * 100 : 0;
    document.getElementById('v-vram-bar').style.width = pct.toFixed(1) + '%';
    txt('v-gpu', fmt(gpu.util_pct, 0));
    // WSL 下 nvmlDeviceGetUtilizationRates 偶尔会在满负载时读到 0，
    // 所以把显存带宽利用率也带上——token 生成是带宽密集的，它更能说明"确实在算"。
    txt('v-gpu-sub', fmt(gpu.temp_c, 0) + '°C · '
        + fmt(gpu.power_w, 0) + '/' + fmt(gpu.power_limit_w, 0) + ' W · 带宽 '
        + fmt(gpu.mem_util_pct, 0) + '% · ' + gpu.name);
    push('vram', gpu.used_mib);
    push('gpu', gpu.util_pct);
  } else {
    txt('v-vram', '—'); txt('v-vram-total', '—');
    txt('v-gpu', '—'); txt('v-gpu-sub', 'NVML 不可用（原生 Windows 或无驱动容器里正常）');
  }

  txt('v-inflight', fmt(sv.inflight, 0));
  txt('v-queue-sub', '排队 ' + fmt(sv.queued, 0) + ' / 上限 ' + fmt(d.model.queue_limit, 0)
      + ' · 并发上限 ' + fmt(d.model.max_concurrency, 0)
      + ' · 引擎处理中 ' + fmt(em['requests_processing'], 0)
      + ' · 延后 ' + fmt(em['requests_deferred'], 0));
  push('inflight', sv.inflight);

  spark('c-pps', H.pps, '#2f81f7', true);
  spark('c-vram', H.vram, '#d29922', false);
  spark('c-gpu', H.gpu, '#3fb950', true);
  spark('c-inflight', H.inflight, '#a371f7', true);

  // 延迟表
  var names = {
    'dshinfer_ttft_seconds': 'TTFT 首字',
    'dshinfer_request_seconds': '端到端',
    'dshinfer_queue_wait_seconds': '排队等待'
  };
  var rows = [];
  var hist = sv.histograms || {};
  Object.keys(names).forEach(function (k) {
    var v = hist[k];
    if (!v) return;
    rows.push('<tr><td>' + names[k] + '</td><td class="num">' + ms(v.p50_s) + '</td><td class="num">'
      + ms(v.p95_s) + '</td><td class="num">' + ms(v.mean_s) + '</td><td class="num dim">'
      + fmt(v.count, 0) + '</td></tr>');
  });
  document.getElementById('t-lat').innerHTML = rows.length ? rows.join('')
    : '<tr><td colspan="5" class="empty">还没有请求</td></tr>';

  // 累计表
  var tok = sv.tokens || {};
  var reqs = sv.requests_by_status || {};
  var reqRows = Object.keys(reqs).sort().map(function (k) {
    var cls = k.charAt(0) === '2' ? 'ok' : (k.charAt(0) === '4' ? 'warn' : 'bad');
    return '<span class="' + cls + '">' + k + '</span> ' + fmt(reqs[k], 0);
  }).join(' · ');
  var cached = em['prompt_tokens_cached_total'];
  var promptTok = em['prompt_tokens_total'];
  var cachePct = (cached !== undefined && promptTok) ? (cached / promptTok * 100) : null;
  var ev = sv.scheduler_events || {};
  var evRows = Object.keys(ev).sort().map(function (k) { return k + ' ' + fmt(ev[k], 0); }).join(' · ');
  document.getElementById('t-totals').innerHTML =
      '<tr><th>服务层请求</th><td class="num">' + fmt(sv.requests_total, 0) + '</td></tr>'
    + '<tr><th>按状态</th><td class="num">' + (reqRows || '—') + '</td></tr>'
    + '<tr><th>服务层令牌（输入 / 输出 / 命中缓存）</th><td class="num">'
      + fmt(tok.input, 0) + ' / ' + fmt(tok.output, 0) + ' / ' + fmt(tok.cached, 0) + '</td></tr>'
    + '<tr><th>引擎 prompt 令牌（含前缀缓存命中）</th><td class="num">'
      + fmt(promptTok, 0) + (cachePct === null ? '' : '（缓存命中 ' + cachePct.toFixed(0) + '%）')
      + '</td></tr>'
    + '<tr><th>调度事件</th><td class="num dim">' + (evRows || '—') + '</td></tr>';

  // 引擎表
  var monotonic = [
    ['prompt_tokens_total', '处理的 prompt 令牌'],
    ['prompt_tokens_cached_total', '其中命中前缀缓存'],
    ['tokens_predicted_total', '生成的令牌'],
    ['n_decode_total', 'decode 次数'],
    ['n_tokens_max', '单请求最长令牌数'],
    ['spec_decode_num_draft_tokens_total', '投机解码：草稿令牌'],
    ['spec_decode_num_accepted_tokens_total', '投机解码：被接受']
  ];
  var rows2 = monotonic.filter(function (p) { return em[p[0]] !== undefined; })
    .map(function (p) { return '<tr><th>' + p[1] + '（' + p[0] + '）</th><td class="num">'
      + fmt(em[p[0]], 0) + '</td></tr>'; });
  document.getElementById('t-engine').innerHTML = rows2.length ? rows2.join('')
    : '<tr><td class="empty">引擎没有提供 /metrics（mock 引擎或尚未就绪）</td></tr>';
}

function renderTraces(list) {
  var rows = (list || []).map(function (t) {
    var s = t.spans_ms || {};
    var cls = t.status === 'done' ? 'ok' : (t.status === 'error' ? 'bad' : 'warn');
    return '<tr><td>' + (t.id || '—').slice(0, 12) + '</td><td class="dim">' + (t.model || '—')
      + '</td><td class="num">' + fmt(s.queue, 0) + '</td><td class="num">' + fmt(s.ttft, 0)
      + '</td><td class="num">' + fmt(s.decode, 0) + '</td><td class="num">' + fmt(s.total, 0)
      + '</td><td class="' + cls + '">' + (t.status || '?') + '</td></tr>';
  });
  document.getElementById('t-traces').innerHTML = rows.length ? rows.join('')
    : '<tr><td colspan="7" class="empty">还没有请求</td></tr>';
}

async function tick() {
  try {
    var r = await fetch('/admin/live', { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    render(await r.json());
    var t = await fetch('/admin/traces?limit=8', { cache: 'no-store' });
    if (t.ok) renderTraces((await t.json()).data);
  } catch (e) {
    txt('m-state', '连不上服务：' + e.message);
    document.getElementById('dot').className = 'dot bad';
  }
}
tick();
setInterval(tick, 2000);
window.addEventListener('resize', function () {
  spark('c-pps', H.pps, '#2f81f7', true);
  spark('c-vram', H.vram, '#d29922', false);
  spark('c-gpu', H.gpu, '#3fb950', true);
  spark('c-inflight', H.inflight, '#a371f7', true);
});
</script>
</body>
</html>
"""
