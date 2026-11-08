"""调度器单测：不碰引擎、不起服务，纯 asyncio。

这些测试守住的是「策略」而不是「实现」：
  · 高优先级能不能插队
  · 低优先级等久了会不会被饿死
  · 一个大租户能不能把别人挤光
  · 队列满了 / 等太久，是不是都变成 429
"""

from __future__ import annotations

import asyncio

import pytest

from dshinfer.scheduler import QueueFull, Scheduler


async def _hold(s: Scheduler, rid: str = "holder", **kw):
    return await s.acquire(rid, **kw)


async def test_high_priority_jumps_queue():
    s = Scheduler(max_concurrency=1, queue_limit=10, queue_timeout_s=5, aging_s=1000)
    held = await _hold(s)
    order: list[str] = []

    async def waiter(rid: str, pr: int):
        g = await s.acquire(rid, priority=pr, tenant=rid)
        order.append(rid)
        s.release(g)

    low = asyncio.create_task(waiter("low", 0))
    await asyncio.sleep(0.02)
    high = asyncio.create_task(waiter("high", 5))
    await asyncio.sleep(0.02)
    s.release(held)
    await asyncio.gather(low, high)

    assert order == ["high", "low"], f"优先级没生效：{order}"


async def test_aging_prevents_starvation():
    # 老化窗口 0.05s：等 0.2s 相当于 +4 级
    s = Scheduler(max_concurrency=1, queue_limit=10, queue_timeout_s=5, aging_s=0.05)
    held = await _hold(s)
    order: list[str] = []

    async def waiter(rid: str, pr: int, delay: float = 0.0):
        await asyncio.sleep(delay)
        g = await s.acquire(rid, priority=pr, tenant=rid)
        order.append(rid)
        s.release(g)

    old = asyncio.create_task(waiter("old", 0))
    await asyncio.sleep(0.2)  # 让 old 等够久，等效优先级涨到 4
    new = asyncio.create_task(waiter("new", 3))
    await asyncio.sleep(0.02)
    s.release(held)
    await asyncio.gather(old, new)

    assert order == ["old", "new"], f"老化没生效：{order}"


async def test_tenant_quota_is_fair():
    s = Scheduler(
        max_concurrency=2, queue_limit=10, queue_timeout_s=5, aging_s=1000, per_tenant_inflight=1
    )
    a1 = await s.acquire("a1", priority=0, tenant="A")
    a2 = asyncio.create_task(s.acquire("a2", priority=0, tenant="A"))
    await asyncio.sleep(0.02)

    # A 已经占满配额，B 的请求应该立刻拿到槽位，而不是排在 a2 后面
    b1 = await s.acquire("b1", priority=0, tenant="B")
    assert b1.slot != a1.slot
    assert not a2.done(), "A 的第二个请求不该在配额满时被放行"

    s.release(a1)
    await asyncio.sleep(0.02)
    assert a2.done(), "A 释放后，排队的 a2 应该被放行"
    g = await a2
    s.release(g)
    s.release(b1)


async def test_queue_full_raises():
    s = Scheduler(max_concurrency=1, queue_limit=1, queue_timeout_s=5)
    held = await _hold(s)
    waiting = asyncio.create_task(s.acquire("w1", tenant="w1"))
    await asyncio.sleep(0.02)
    with pytest.raises(QueueFull):
        await s.acquire("w2", tenant="w2")
    s.release(held)
    g = await waiting
    s.release(g)


async def test_queue_timeout_raises():
    s = Scheduler(max_concurrency=1, queue_limit=10, queue_timeout_s=0.05)
    held = await _hold(s)
    with pytest.raises(QueueFull):
        await s.acquire("late", tenant="late")
    assert s.stats()["queued"] == 0, "超时后队列必须清干净"
    s.release(held)


async def test_prefix_affinity_reuses_slot():
    s = Scheduler(max_concurrency=2, queue_limit=10, queue_timeout_s=5)
    g1 = await s.acquire("r1", prefix_key="shared")
    s.release(g1)
    g2 = await s.acquire("r2", prefix_key="shared")
    assert g2.slot == g1.slot, "同前缀应该回到同一个槽位"
    assert g2.prefix_hit is True
    s.release(g2)


async def test_release_returns_slot():
    s = Scheduler(max_concurrency=2, queue_limit=10, queue_timeout_s=5)
    g1 = await s.acquire("r1")
    g2 = await s.acquire("r2")
    assert {g1.slot, g2.slot} == {0, 1}
    s.release(g1)
    assert s.stats()["free_slots"] == 1
    s.release(g2)
    assert s.stats()["free_slots"] == 2
