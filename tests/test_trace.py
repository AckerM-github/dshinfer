"""trace 的单测 + 通过 API 的端到端验证。"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from dshinfer.api import create_app
from dshinfer.config import Settings
from dshinfer.trace import RequestTrace, TraceStore


def test_spans_from_marks() -> None:
    tr = RequestTrace(id="t1")
    time.sleep(0.01)
    tr.mark("rendered")
    time.sleep(0.02)
    tr.mark("admitted")
    tr.mark("engine_start")
    time.sleep(0.03)
    tr.mark("first_token")
    time.sleep(0.01)
    tr.mark("last_token")
    tr.mark("done")

    spans = tr.summary()["spans_ms"]
    # 队列 = received → admitted，约 30ms；ttft = engine_start → first_token，约 30ms
    assert 25 <= spans["queue"] <= 90
    assert 25 <= spans["ttft"] <= 90
    assert spans["decode"] >= 5
    assert spans["total"] >= spans["queue"] + spans["ttft"]


def test_mark_is_idempotent() -> None:
    tr = RequestTrace(id="t2")
    tr.mark("first_token")
    first = tr.at("first_token")
    time.sleep(0.01)
    tr.mark("first_token")
    assert tr.at("first_token") == first
    assert [m.name for m in tr.marks].count("first_token") == 1


def test_store_capacity_and_order() -> None:
    store = TraceStore(capacity=3)
    for i in range(5):
        store.add(RequestTrace(id=f"t{i}"))
    assert len(store) == 3
    assert store.get("t0") is None
    assert store.get("t4") is not None
    recent = store.recent(2)
    assert [r["id"] for r in recent] == ["t4", "t3"]


def test_detail_has_absolute_offsets() -> None:
    tr = RequestTrace(id="t3")
    time.sleep(0.005)
    tr.mark("done")
    detail = tr.detail()
    assert detail["marks_ms"]["received"] == 0.0
    assert detail["marks_ms"]["done"] >= 5


@pytest.fixture()
def app():
    return create_app(
        Settings(engine="mock", mock_ttft_ms=1, mock_tokens_per_second=2000, mock_response="好")
    )


async def _post(app, body: dict, headers: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        async with app.router.lifespan_context(app):
            return await client.post("/v1/chat/completions", json=body, headers=headers or {})


async def test_api_trace_roundtrip(app) -> None:
    body = {"model": "m", "messages": [{"role": "user", "content": "你好"}], "max_tokens": 4}
    r = await _post(app, body, {"X-Request-Id": "my-trace-1"})
    assert r.status_code == 200
    # 客户端自带 id 时回显同一个，方便和客户端日志对齐
    assert r.headers["x-request-id"] == "my-trace-1"
    assert "ttft" in r.headers["server-timing"] or "total" in r.headers["server-timing"]

    detail = app.state.service.traces.get("my-trace-1").detail()
    assert detail["id"] == "my-trace-1"
    assert {"received", "rendered", "admitted", "first_token", "done"} <= set(
        detail["marks_ms"]
    )
    assert detail["tokens"]["completion"] >= 1
    assert detail["status"] == "ok"


async def test_api_trace_generated_id(app) -> None:
    body = {"model": "m", "messages": [{"role": "user", "content": "你好"}], "max_tokens": 4}
    r = await _post(app, body)
    tid = r.headers["x-request-id"]
    assert tid.startswith("chatcmpl-")
    assert app.state.service.traces.get(tid) is not None


async def test_admin_traces_endpoints(app) -> None:
    body = {"model": "m", "messages": [{"role": "user", "content": "你好"}], "max_tokens": 4}
    r = await _post(app, body, {"X-Request-Id": "trace-x"})
    assert r.status_code == 200

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        listing = await client.get("/admin/traces?limit=5")
        assert listing.status_code == 200
        ids = [row["id"] for row in listing.json()["data"]]
        assert "trace-x" in ids

        one = await client.get("/admin/traces/trace-x")
        assert one.status_code == 200
        assert one.json()["status"] == "ok"

        missing = await client.get("/admin/traces/nope")
        assert missing.status_code == 404


async def test_trace_is_optional() -> None:
    """不给 trace 时服务照常工作并自动记账（内部调用不该被迫建 trace）。"""
    from dshinfer.engine.mock import MockEngine
    from dshinfer.schemas import ChatCompletionRequest, ChatMessage
    from dshinfer.service import ChatService

    settings = Settings(engine="mock", mock_ttft_ms=0, mock_tokens_per_second=10000)
    svc = ChatService(MockEngine(settings), settings)
    req = ChatCompletionRequest(
        model="m", messages=[ChatMessage(role="user", content="你好")], max_tokens=4
    )
    prep = svc.prepare(req)
    assert prep.trace is None
    assert len(svc.traces) == 0
