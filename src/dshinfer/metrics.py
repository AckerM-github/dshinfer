"""Prometheus 指标：请求数、令牌数、首字延迟、端到端延迟、在飞请求、排队数。"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry()

REQUESTS = Counter(
    "dshinfer_requests_total",
    "请求总数",
    ["endpoint", "status", "stream"],
    registry=REGISTRY,
)
TOKENS = Counter(
    "dshinfer_tokens_total", "令牌总数", ["direction"], registry=REGISTRY
)
TTFT = Histogram(
    "dshinfer_ttft_seconds",
    "首字延迟",
    buckets=(0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 30),
    registry=REGISTRY,
)
LATENCY = Histogram(
    "dshinfer_request_seconds",
    "端到端延迟",
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
    registry=REGISTRY,
)
INFLIGHT = Gauge("dshinfer_inflight_requests", "正在生成的请求数", registry=REGISTRY)
QUEUE_DEPTH = Gauge("dshinfer_queue_depth", "排队等待的请求数", registry=REGISTRY)
QUEUE_WAIT = Histogram(
    "dshinfer_queue_wait_seconds",
    "排队等了多久（从入队到拿到槽位）",
    buckets=(0.005, 0.01, 0.05, 0.1, 0.5, 1, 2, 5, 15, 30),
    registry=REGISTRY,
)
SCHED_EVENTS = Counter(
    "dshinfer_scheduler_events_total",
    "调度事件：enqueue/dispatch/reject_queue_full/reject_tenant_queue/queue_timeout",
    ["event"],
    registry=REGISTRY,
)


def render() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
