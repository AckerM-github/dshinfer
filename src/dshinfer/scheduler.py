"""调度器：把「信号量先到先服务」换成「优先级 + 老化 + 租户配额 + 前缀亲和」。

为什么服务层要自己调度？
  引擎（llama-server / vLLM）内部批的是 token 步，服务层批的是**请求**。
  请求这一层能做的策略比想象中多：

  1. 优先级：交互式对话不能让离线批量任务堵在后面。
  2. 老化：低优先级请求等久了要自动升权，否则永远轮不到（饿死）。
  3. 租户配额：一个用户开 50 个并发，不能把别人的槽位吃光。
  4. 前缀亲和：同一个系统提示词尽量落到同一个槽位，引擎的 prompt cache 才命中。

注意「连续批处理」不在这里——那是引擎的活（它得管 KV 页和 token 步）。
服务层的对应物是**准入 + 排队 + 亲和**，这三件事做得对不对，直接决定尾延迟。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .metrics import QUEUE_DEPTH, QUEUE_WAIT, SCHED_EVENTS


class QueueFull(RuntimeError):
    """排队满了或排队超时——调用方据此返回 429。"""


@dataclass(slots=True)
class Ticket:
    req_id: str
    priority: int
    tenant: str
    prefix_key: str
    seq: int
    enqueued_at: float
    future: asyncio.Future
    queued: bool = True


@dataclass(slots=True)
class Grant:
    """拿到槽位的凭证：里面有分到哪个 slot，以及排队等了多久。"""

    req_id: str
    slot: int
    priority: int
    tenant: str
    waited_s: float
    prefix_hit: bool


class Scheduler:
    def __init__(
        self,
        *,
        max_concurrency: int,
        queue_limit: int,
        queue_timeout_s: float,
        aging_s: float = 2.0,
        per_tenant_inflight: int = 0,
        per_tenant_queue: int = 0,
    ) -> None:
        self.max_concurrency = max_concurrency
        self.queue_limit = queue_limit
        self.queue_timeout_s = queue_timeout_s
        self.aging_s = aging_s
        self.per_tenant_inflight = per_tenant_inflight
        self.per_tenant_queue = per_tenant_queue

        self._free_slots: list[int] = list(range(max_concurrency))
        self._queue: list[Ticket] = []
        self._waiters: dict[str, Ticket] = {}
        self._inflight_by_tenant: dict[str, int] = {}
        self._queued_by_tenant: dict[str, int] = {}
        self._slot_hint: dict[str, int] = {}
        self._seq = 0
        self.dispatched = 0
        self.aging_wins = 0
        self.prefix_hits = 0

    # ---------------- 对外 ----------------

    async def acquire(
        self, req_id: str, *, priority: int = 0, tenant: str = "default", prefix_key: str = ""
    ) -> Grant:
        if len(self._queue) >= self.queue_limit:
            SCHED_EVENTS.labels(event="reject_queue_full").inc()
            raise QueueFull(f"队列已满（{self.queue_limit}）")
        if self.per_tenant_queue and self._queued_by_tenant.get(tenant, 0) >= self.per_tenant_queue:
            SCHED_EVENTS.labels(event="reject_tenant_queue").inc()
            raise QueueFull(f"租户 {tenant} 排队已达上限（{self.per_tenant_queue}）")

        loop = asyncio.get_running_loop()
        self._seq += 1
        ticket = Ticket(
            req_id=req_id,
            priority=priority,
            tenant=tenant,
            prefix_key=prefix_key,
            seq=self._seq,
            enqueued_at=time.perf_counter(),
            future=loop.create_future(),
        )
        self._queue.append(ticket)
        self._waiters[req_id] = ticket
        self._queued_by_tenant[tenant] = self._queued_by_tenant.get(tenant, 0) + 1
        QUEUE_DEPTH.inc()
        SCHED_EVENTS.labels(event="enqueue").inc()

        try:
            self._dispatch()
            try:
                return await asyncio.wait_for(ticket.future, timeout=self.queue_timeout_s)
            except TimeoutError:
                SCHED_EVENTS.labels(event="queue_timeout").inc()
                raise QueueFull(f"排队超过 {self.queue_timeout_s}s") from None
        finally:
            if ticket.queued:
                self._remove(ticket)

    def release(self, grant: Grant) -> None:
        if grant.slot not in self._free_slots:
            self._free_slots.append(grant.slot)
            self._free_slots.sort()
        n = self._inflight_by_tenant.get(grant.tenant, 0) - 1
        if n > 0:
            self._inflight_by_tenant[grant.tenant] = n
        else:
            self._inflight_by_tenant.pop(grant.tenant, None)
        self._dispatch()

    def cancel(self, req_id: str) -> bool:
        """请求还没轮到就撤销了（客户端断开 / 上层超时）。"""
        t = self._waiters.get(req_id)
        if t is None or not t.queued:
            return False
        self._remove(t)
        if not t.future.done():
            t.future.cancel()
        return True

    def stats(self) -> dict[str, object]:
        return {
            "queued": len(self._queue),
            "free_slots": len(self._free_slots),
            "inflight": sum(self._inflight_by_tenant.values()),
            "by_tenant": dict(self._inflight_by_tenant),
            "dispatched": self.dispatched,
            "aging_wins": self.aging_wins,
            "prefix_hits": self.prefix_hits,
        }

    # ---------------- 内部 ----------------

    def _remove(self, t: Ticket) -> None:
        if t in self._queue:
            self._queue.remove(t)
        t.queued = False
        QUEUE_DEPTH.dec()
        n = self._queued_by_tenant.get(t.tenant, 0) - 1
        if n > 0:
            self._queued_by_tenant[t.tenant] = n
        else:
            self._queued_by_tenant.pop(t.tenant, None)
        self._waiters.pop(t.req_id, None)

    def _pick_slot(self, prefix_key: str) -> int:
        hint = self._slot_hint.get(prefix_key) if prefix_key else None
        if hint is not None and hint in self._free_slots:
            self.prefix_hits += 1
            return hint
        return self._free_slots[0]

    def _dispatch(self) -> None:
        while self._free_slots and self._queue:
            now = time.perf_counter()
            best: Ticket | None = None
            best_key: tuple[float, int] | None = None
            for t in self._queue:
                if (
                    self.per_tenant_inflight
                    and self._inflight_by_tenant.get(t.tenant, 0) >= self.per_tenant_inflight
                ):
                    continue
                # 老化：等得越久，等效优先级越高（每 aging_s 秒涨 1 级）
                effective = t.priority + (now - t.enqueued_at) / self.aging_s
                key = (-effective, t.seq)
                if best_key is None or key < best_key:
                    best_key, best = key, t
            if best is None:  # 全被租户配额挡住了
                return

            slot = self._pick_slot(best.prefix_key)
            hit = bool(best.prefix_key) and self._slot_hint.get(best.prefix_key) == slot
            self._free_slots.remove(slot)
            self._remove(best)
            self._inflight_by_tenant[best.tenant] = self._inflight_by_tenant.get(best.tenant, 0) + 1
            if best.prefix_key:
                self._slot_hint[best.prefix_key] = slot
            self.dispatched += 1
            waited = now - best.enqueued_at
            if waited / self.aging_s >= 1:
                self.aging_wins += 1
            QUEUE_WAIT.observe(waited)
            SCHED_EVENTS.labels(event="dispatch").inc()
            if not best.future.done():
                best.future.set_result(
                    Grant(
                        req_id=best.req_id,
                        slot=slot,
                        priority=best.priority,
                        tenant=best.tenant,
                        waited_s=waited,
                        prefix_hit=hit,
                    )
                )
