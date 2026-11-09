"""probe 数据层的测试：解析、直方图、差分、降级、HTTP。

原则：不依赖真实 GPU / 真实引擎。HTTP 用标准库 http.server 在本机随机端口起
一个假服务；NVML 部分只断言「不抛异常 + 形状正确 + 缺项为 None」，因为 CI 上
没有显卡——真机输出放在交付说明里，不放测试里。
"""

from __future__ import annotations

import ast
import contextlib
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from dshinfer.tui import probe
from dshinfer.tui.probe import (
    CpuInfo,
    GpuInfo,
    Hardware,
    RateTracker,
    SysMem,
    engine_metrics,
    fetch,
    fetch_json,
    histogram,
    parse_prom,
    sample_cpu,
    sample_hardware,
    sample_mem,
    service_metrics,
)


# ------------------------------------------------------------------ 假 HTTP 服务


@contextlib.contextmanager
def serve(routes: dict[str, tuple[int, str]]):
    """在 127.0.0.1 的随机端口上起一个只读的假服务，yield base url。"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
            entry = routes.get(self.path)
            if entry is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status, body = entry
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:  # 别把访问日志打到测试输出里
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


ENGINE_TEXT = """# HELP llamacpp:tokens_predicted_total Number of tokens predicted
# TYPE llamacpp:tokens_predicted_total counter
llamacpp:tokens_predicted_total 128
llamacpp:tokens_predicted_seconds_total 4.5
llamacpp:kv_cache_usage_ratio 0.42
llamacpp:requests_processing 1
"""

SERVICE_TEXT = """# HELP dshinfer_inflight_requests 正在处理的请求
# TYPE dshinfer_inflight_requests gauge
dshinfer_inflight_requests 3
dshinfer_queue_depth 2
dshinfer_requests_total{endpoint="/v1/chat/completions",status="200",stream="true"} 10
dshinfer_requests_total{endpoint="/v1/chat/completions",status="500",stream="false"} 2
dshinfer_requests_total{endpoint="/v1/models",status="200",stream="false"} 5
dshinfer_tokens_total{direction="prompt"} 100
dshinfer_tokens_total{direction="completion"} 40
dshinfer_scheduler_events_total{event="admit"} 7
dshinfer_scheduler_events_total{event="preempt"} 1
dshinfer_ttft_seconds_bucket{le="0.05"} 1
dshinfer_ttft_seconds_bucket{le="0.1"} 3
dshinfer_ttft_seconds_bucket{le="+Inf"} 4
dshinfer_ttft_seconds_count 4
dshinfer_ttft_seconds_sum 0.3
"""


# ------------------------------------------------------------------- parse_prom


def test_parse_prom_ignores_comments_and_blank_lines() -> None:
    prom = parse_prom(ENGINE_TEXT)
    assert set(prom) == {
        "llamacpp:tokens_predicted_total",
        "llamacpp:tokens_predicted_seconds_total",
        "llamacpp:kv_cache_usage_ratio",
        "llamacpp:requests_processing",
    }
    assert prom["llamacpp:tokens_predicted_total"] == 128.0


def test_parse_prom_canonicalizes_labels() -> None:
    prom = parse_prom('m{b="2",a="1"} 3\nplain 4\nm{a="x y"} 5\n')
    assert prom['m{a="1",b="2"}'] == 3.0
    assert prom["plain"] == 4.0
    assert prom['m{a="x y"}'] == 5.0


def test_parse_prom_unescapes_and_reexcapes_labels() -> None:
    prom = parse_prom('m{path="C:\\\\tmp\\\\a",q="say \\"hi\\""} 1\n')
    # 标签值恢复成原始字符，再规范化时重新转义，最终是一个稳定的样本键
    # 输入里 \\ 表示一个反斜杠字符；规范化后按语义重新转义，所以键里是两个反斜杠
    assert prom['m{path="C:\\\\tmp\\\\a",q="say \\"hi\\""}'] == 1.0


def test_parse_prom_ignores_timestamp_and_bad_values() -> None:
    prom = parse_prom("m 5 1700000000000\nbad NaN? 1\nnovalue \n")
    assert prom["m"] == 5.0
    assert "bad" not in prom


def test_parse_prom_handles_empty_and_none() -> None:
    assert parse_prom(None) == {}
    assert parse_prom("") == {}
    assert parse_prom("\n# 只有注释\n") == {}


def test_parse_prom_keeps_float_inf() -> None:
    prom = parse_prom("m 1\ninf_metric +Inf\n")
    assert prom["inf_metric"] == float("inf")


# ------------------------------------------------------------------- histogram


def _prom_hist() -> dict[str, float]:
    return parse_prom(
        "\n".join(
            [
                'h_bucket{le="0.1"} 5',
                'h_bucket{le="0.5"} 15',
                'h_bucket{le="1.0"} 20',
                'h_bucket{le="+Inf"} 20',
                "h_count 20",
                "h_sum 8",
            ]
        )
    )


def test_histogram_count_mean_and_quantiles() -> None:
    result = histogram(_prom_hist(), "h")
    assert result["count"] == 20.0
    assert result["mean_s"] == pytest.approx(0.4)
    # p50: rank=10，落在 (0.1, 0.5] 桶内，线性插值 0.1 + 0.4*(10-5)/10 = 0.3
    assert result["p50_s"] == pytest.approx(0.3)
    # p95: rank=19，落在 (0.5, 1.0] 桶内，0.5 + 0.5*(19-15)/5 = 0.9
    assert result["p95_s"] == pytest.approx(0.9)


def test_histogram_last_inf_bucket_returns_previous_bound() -> None:
    prom = parse_prom('h_bucket{le="0.1"} 0\nh_bucket{le="+Inf"} 10\nh_count 10\n')
    result = histogram(prom, "h")
    assert result["count"] == 10.0
    assert result["p50_s"] == pytest.approx(0.1)
    assert result["mean_s"] is None


def test_histogram_missing_count_uses_largest_bucket() -> None:
    prom = parse_prom('h_bucket{le="1"} 4\nh_bucket{le="2"} 9\n')
    result = histogram(prom, "h")
    assert result["count"] == 9.0
    assert result["p50_s"] is not None


def test_histogram_absent_or_zero_returns_none_shape() -> None:
    expected = {"count": 0.0, "mean_s": None, "p50_s": None, "p95_s": None}
    assert histogram(None, "h") == expected
    assert histogram({}, "h") == expected
    assert histogram(parse_prom("other 1"), "h") == expected
    assert histogram(parse_prom("h_count 0"), "h") == expected


def test_histogram_ignores_lookalike_names() -> None:
    prom = parse_prom('h_bucket_other{le="1"} 3\nh_count 0\n')
    assert histogram(prom, "h")["count"] == 0.0


# ----------------------------------------------------------------------- fetch


def test_fetch_reads_body_and_rejects_bad_targets() -> None:
    with serve({"/metrics": (200, "hello")}) as base:
        assert fetch(base + "/metrics") == "hello"
        # 404 → urlopen 抛 HTTPError → None
        assert fetch(base + "/missing") is None
        # 不带 scheme 的配置写法也要能用
        assert fetch(base.removeprefix("http://") + "/metrics") == "hello"
        # 端口没人监听 → None（短超时，不拖慢 UI）
        assert fetch("127.0.0.1:1/metrics", timeout=0.3) is None
    assert fetch(None) is None
    assert fetch("") is None


def test_fetch_json_parses_and_degrades() -> None:
    with serve({"/admin/live": (200, '{"ok": 1}'), "/bad": (200, "not json")}) as base:
        assert fetch_json(base + "/admin/live") == {"ok": 1}
        assert fetch_json(base + "/bad") is None
        assert fetch_json(base + "/missing") is None
    assert fetch_json(None) is None


def test_engine_metrics_strips_llamacpp_prefix() -> None:
    with serve({"/metrics": (200, ENGINE_TEXT)}) as base:
        metrics = engine_metrics(base + "/metrics")
    assert metrics["tokens_predicted_total"] == 128.0
    assert metrics["tokens_predicted_seconds_total"] == 4.5
    assert metrics["kv_cache_usage_ratio"] == 0.42
    assert all(not name.startswith("llamacpp:") for name in metrics)


def test_engine_metrics_handles_missing_endpoint() -> None:
    with serve({}) as base:
        assert engine_metrics(base + "/metrics") == {}
    assert engine_metrics(None) == {}


def test_service_metrics_shapes_and_aggregation() -> None:
    with serve({"/metrics": (200, SERVICE_TEXT)}) as base:
        data = service_metrics(base + "/metrics")
    assert data["available"] is True
    assert data["inflight"] == 3.0
    assert data["queued"] == 2.0
    assert data["requests_total"] == 17.0
    assert data["requests_by_status"] == {"200": 15.0, "500": 2.0}
    assert data["tokens"] == {"prompt": 100.0, "completion": 40.0}
    assert data["scheduler_events"] == {"admit": 7.0, "preempt": 1.0}
    ttft = data["histograms"]["dshinfer_ttft_seconds"]
    assert ttft["count"] == 4.0
    assert ttft["mean_s"] == pytest.approx(0.075)
    assert ttft["p50_s"] == pytest.approx(0.075)
    assert ttft["p95_s"] == pytest.approx(0.1)
    assert "dshinfer_ttft_seconds_count" in data["raw"]


def test_service_metrics_degrades_when_unreachable() -> None:
    data = service_metrics(None)
    assert data["available"] is False
    assert data["inflight"] == 0.0
    assert data["queued"] == 0.0
    assert data["requests_total"] == 0.0
    assert data["requests_by_status"] == {}
    assert data["histograms"] == {}


# ----------------------------------------------------------------- RateTracker


def _tracker() -> tuple[RateTracker, list[float]]:
    clock = [1000.0]
    return RateTracker(wall_clock=lambda: clock[0]), clock


def test_rate_tracker_instant_window_aggregate() -> None:
    tracker, clock = _tracker()
    first = tracker.update(
        {"tokens_predicted_total": 0.0, "tokens_predicted_seconds_total": 0.0}
    )
    assert first == {"instant": None, "window": None, "aggregate": None}

    clock[0] += 1.0
    second = tracker.update(
        {"tokens_predicted_total": 120.0, "tokens_predicted_seconds_total": 2.0}
    )
    # Δtokens/Δ生成秒 = 120/2 = 60；Δtokens/Δ墙钟 = 120/1 = 120
    assert second["instant"] == pytest.approx(60.0)
    assert second["window"] == pytest.approx(60.0)
    assert second["aggregate"] == pytest.approx(120.0)

    clock[0] += 1.0
    third = tracker.update(
        {"tokens_predicted_total": 240.0, "tokens_predicted_seconds_total": 4.0}
    )
    assert third["instant"] == pytest.approx(60.0)
    assert third["aggregate"] == pytest.approx(120.0)


def test_rate_tracker_rejects_too_short_gaps() -> None:
    tracker, clock = _tracker()
    tracker.update({"tokens_predicted_total": 100.0, "tokens_predicted_seconds_total": 1.0})
    clock[0] += 1.0
    good = tracker.update(
        {"tokens_predicted_total": 200.0, "tokens_predicted_seconds_total": 3.0}
    )
    assert good["instant"] == pytest.approx(50.0)

    # 0.05 s 内连打：令牌几乎不变，绝不能算出 0 或离谱的峰，而是沿用上一轮
    for _ in range(3):
        clock[0] += 0.05
        spammed = tracker.update(
            {"tokens_predicted_total": 201.0, "tokens_predicted_seconds_total": 3.001}
        )
        assert spammed["instant"] == pytest.approx(50.0)
        assert spammed["window"] == pytest.approx(50.0)
        assert spammed["aggregate"] == pytest.approx(100.0)


def test_rate_tracker_resets_on_counter_rollback() -> None:
    tracker, clock = _tracker()
    tracker.update({"tokens_predicted_total": 500.0, "tokens_predicted_seconds_total": 9.0})
    clock[0] += 1.0
    assert tracker.update(
        {"tokens_predicted_total": 600.0, "tokens_predicted_seconds_total": 11.0}
    )["instant"] == pytest.approx(50.0)

    # 引擎重启：计数器回退，基线清空，这一轮不报数
    clock[0] += 1.0
    reset = tracker.update({"tokens_predicted_total": 3.0, "tokens_predicted_seconds_total": 0.2})
    assert reset["instant"] is None
    # 下一个样本重新建立基线，也还不报数，再下一个才恢复
    clock[0] += 1.0
    assert tracker.update(
        {"tokens_predicted_total": 50.0, "tokens_predicted_seconds_total": 2.2}
    )["instant"] is None
    clock[0] += 1.0
    assert tracker.update(
        {"tokens_predicted_total": 150.0, "tokens_predicted_seconds_total": 4.2}
    )["instant"] == pytest.approx(50.0)


def test_rate_tracker_tolerates_missing_counters() -> None:
    tracker, _ = _tracker()
    empty = tracker.update(None)
    assert empty == {"instant": None, "window": None, "aggregate": None}
    assert tracker.update({})["instant"] is None
    # 只有 tokens、没有生成秒数：instant 无法算，但不报错
    assert tracker.update({"tokens_predicted_total": 5.0})["instant"] is None
    tracker.reset()
    assert tracker.update({"tokens_predicted_total": 1.0})["instant"] is None


def test_rate_tracker_aggregate_survives_without_seconds() -> None:
    tracker, clock = _tracker()
    tracker.update({"tokens_predicted_total": 0.0})
    clock[0] += 1.0
    result = tracker.update({"tokens_predicted_total": 100.0})
    assert result["instant"] is None
    assert result["aggregate"] == pytest.approx(100.0)


# ------------------------------------------------------------------ 硬件采集


def test_gpu_optional_fields_default_to_none() -> None:
    gpu = GpuInfo(index=0)
    assert gpu.total_mib == 0.0
    assert gpu.util_pct is None
    assert gpu.temp_c is None
    assert gpu.fan_pct is None
    assert gpu.clock_core_mhz is None


def test_dataclass_defaults() -> None:
    hardware = Hardware()
    assert hardware.gpus == []
    assert hardware.cpu == CpuInfo()
    assert hardware.mem == SysMem()
    assert hardware.backend == "none"
    assert hardware.error is None


def test_sample_hardware_never_raises_and_has_shape() -> None:
    hardware = sample_hardware()
    assert isinstance(hardware, Hardware)
    assert isinstance(hardware.backend, str) and hardware.backend
    assert hardware.error is None or isinstance(hardware.error, str)
    assert isinstance(hardware.gpus, list)
    assert isinstance(hardware.cpu, CpuInfo)
    assert isinstance(hardware.mem, SysMem)
    assert hardware.cpu.cores >= 0
    assert 0.0 <= hardware.cpu.usage_pct <= 100.0
    assert hardware.mem.total_mib >= 0.0
    assert hardware.mem.used_mib >= 0.0
    assert hardware.mem.available_mib >= 0.0
    if hardware.mem.total_mib > 0:
        assert hardware.mem.used_mib + hardware.mem.available_mib == pytest.approx(
            hardware.mem.total_mib, abs=1.0
        )
    for gpu in hardware.gpus:
        assert isinstance(gpu.index, int)
        assert gpu.total_mib >= 0.0
        # 百分比/温度/功耗是 float；PCIe 代宽、风扇、时钟是 NVML 的整数输出
        for value in (
            gpu.util_pct,
            gpu.mem_util_pct,
            gpu.temp_c,
            gpu.power_w,
            gpu.power_limit_w,
        ):
            assert value is None or isinstance(value, float)
        for value in (
            gpu.pcie_gen,
            gpu.pcie_width,
            gpu.fan_pct,
            gpu.clock_core_mhz,
            gpu.clock_mem_mhz,
        ):
            assert value is None or isinstance(value, int)
    # 没有 GPU 时必须给原因，UI 要能显示为什么这块是空的
    if not hardware.gpus:
        assert hardware.error
    else:
        assert hardware.backend.startswith("nvml:")


def test_sample_hardware_repeated_calls_are_cheap_and_stable() -> None:
    first = sample_hardware()
    time.sleep(1.1)  # 让 CPU 差分有真实推进
    second = sample_hardware()
    assert len(second.gpus) == len(first.gpus)
    assert second.backend == first.backend
    assert 0.0 <= second.cpu.usage_pct <= 100.0
    assert all(0.0 <= value <= 100.0 for value in second.cpu.per_core_pct)


def test_sample_cpu_and_mem_do_not_raise() -> None:
    cpu = sample_cpu()
    assert isinstance(cpu, CpuInfo)
    assert isinstance(cpu.per_core_pct, list)
    assert cpu.load1 is None or cpu.load1 >= 0.0
    mem = sample_mem()
    assert isinstance(mem, SysMem)
    assert mem.total_mib >= 0.0


def test_nvml_library_path_matches_backend() -> None:
    # backend 按 UI 约定只分 windows/linux；WSL 与普通 Linux 的区别看库路径
    path = probe.nvml_library_path()
    assert isinstance(path, str)
    backend = sample_hardware().backend
    if backend != "none":
        assert path
        assert backend in {"nvml:windows", "nvml:linux"}


def test_cpu_per_core_matches_core_count_when_available() -> None:
    # 第一次调用只建基线，不产数据；第二次才有差分值
    sample_cpu()
    time.sleep(1.1)
    cpu = sample_cpu()
    if cpu.per_core_pct:
        assert len(cpu.per_core_pct) == cpu.cores


# --------------------------------------------------------------- 依赖与副作用


def test_probe_imports_only_stdlib() -> None:
    source = Path(probe.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    allowed = set(sys.stdlib_module_names)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add((node.module or "").split(".")[0])
    assert imported <= allowed, f"非标准库依赖：{sorted(imported - allowed)}"
    assert "textual" not in imported


def test_probe_module_has_no_print_debugging() -> None:
    source = Path(probe.__file__).read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("print("), f"残留 print 调试：{line}"
