"""单请求 trace：一条请求在服务层里到底把时间花在哪了。

指标（Prometheus histogram）回答「整体多快」，trace 回答「这一条慢在哪」——
排队 3 秒、还是预填充 3 秒，处理方式完全不同。所以两者都要有。

设计约束：
  · 只留最近 N 条在内存里（默认 512），不落盘、不引入分布式 collector；
  · 成本是十几次 `perf_counter()`，可以常开，不需要采样开关；
  · 对外 `GET /admin/traces` 与 `/admin/traces/{id}`，请求头 `X-Request-Id` 可自带，
    响应头会回显，方便和客户端日志对准。

时间线（服务层能观测到的）：

    received ──rendered──admitted────────engine_start──first_token──last_token──done
       │          │         │                    │           │          │
       │          │         └─ queue  ────────────┘           │          │
       │          └─ render ─┘                                │          │
       └──────────────── total ──────────────────────────────────────────┘
                                      └─ ttft ─┘  └─ decode ─┘
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Mark:
    name: str
    at: float  # time.perf_counter()


@dataclass(slots=True)
class RequestTrace:
    id: str
    model: str = ""
    tenant: str = "default"
    priority: int = 0
    t0: float = field(default_factory=time.perf_counter)
    marks: list[Mark] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)
    engine_ms: dict[str, float] = field(default_factory=dict)
    status: str = "running"
    finish_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.marks:
            self.marks.append(Mark("received", self.t0))

    # ---------------------------------------------------------------- 打点

    def mark(self, name: str) -> None:
        """同一个名字只记第一次：first_token 之类重复触发不该被后者覆盖。"""
        if any(m.name == name for m in self.marks):
            return
        self.marks.append(Mark(name, time.perf_counter()))

    def at(self, name: str) -> float | None:
        for m in self.marks:
            if m.name == name:
                return m.at
        return None

    def span_ms(self, start: str, end: str) -> float | None:
        a, b = self.at(start), self.at(end)
        if a is None or b is None:
            return None
        return round((b - a) * 1000, 1)

    # ---------------------------------------------------------------- 输出

    def summary(self) -> dict[str, Any]:
        spans: dict[str, float | None] = {
            "queue": self.span_ms("received", "admitted"),
            "render": self.span_ms("received", "rendered"),
            "ttft": self.span_ms("engine_start", "first_token"),
            "decode": self.span_ms("first_token", "last_token"),
            "total": self.span_ms("received", "done"),
        }
        return {
            "id": self.id,
            "model": self.model,
            "tenant": self.tenant,
            "priority": self.priority,
            "status": self.status,
            "finish_reason": self.finish_reason,
            "spans_ms": {k: v for k, v in spans.items() if v is not None},
            "tokens": dict(self.tokens),
            "engine_ms": dict(self.engine_ms),
        }

    def detail(self) -> dict[str, Any]:
        out = self.summary()
        out["marks_ms"] = {m.name: round((m.at - self.t0) * 1000, 1) for m in self.marks}
        return out


class TraceStore:
    """最近 N 条 trace 的环形仓库（单事件循环，不需要锁）。"""

    def __init__(self, capacity: int = 512) -> None:
        self._items: OrderedDict[str, RequestTrace] = OrderedDict()
        self._capacity = max(1, capacity)

    def add(self, trace: RequestTrace) -> None:
        self._items[trace.id] = trace
        self._items.move_to_end(trace.id)
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)

    def get(self, trace_id: str) -> RequestTrace | None:
        return self._items.get(trace_id)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        items = list(self._items.values())[-limit:]
        return [t.summary() for t in reversed(items)]

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)
