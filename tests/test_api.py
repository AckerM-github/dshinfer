"""服务层测试：用 ASGI 直连，不占端口、不落网络。"""

from __future__ import annotations

import json

import httpx
import pytest

from dshinfer.api import create_app
from dshinfer.config import Settings
from dshinfer.engine import MockEngine
from dshinfer.schemas import ChatCompletionRequest, ChatMessage
from dshinfer.service import ChatService, QueueFull


def _settings(**kw) -> Settings:
    base = dict(engine="mock", mock_ttft_ms=1, mock_tokens_per_second=2000, log_level="error")
    base.update(kw)
    return Settings(**base)


@pytest.fixture
async def client():
    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["engine"]["engine"] == "mock"


async def test_models(client):
    r = await client.get("/v1/models")
    assert r.json()["data"][0]["id"]


async def test_non_stream(client):
    r = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "你好"}], "max_tokens": 5},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"]
    assert body["usage"]["completion_tokens"] == 5
    assert body["choices"][0]["finish_reason"] == "length"


async def test_stream(client):
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "你好"}], "stream": True, "max_tokens": 8},
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        lines = [line async for line in r.aiter_lines() if line.startswith("data: ")]

    payloads = [line[6:] for line in lines]
    assert payloads[-1] == "[DONE]"

    text, finish = "", None
    for p in payloads[:-1]:
        chunk = json.loads(p)
        for ch in chunk["choices"]:
            text += (ch.get("delta") or {}).get("content") or ""
            finish = ch.get("finish_reason") or finish
    assert text
    assert finish == "length"


async def test_stop_string(client):
    r = await client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stop": ["引擎"],
            "max_tokens": 100,
        },
    )
    body = r.json()
    assert "引擎" not in body["choices"][0]["message"]["content"]
    assert body["choices"][0]["finish_reason"] == "stop"


async def test_queue_full_is_rejected():
    s = _settings(max_concurrency=1, queue_limit=0)
    svc = ChatService(MockEngine(s), s)
    prep = svc.prepare(
        ChatCompletionRequest(messages=[ChatMessage(role="user", content="hi")])
    )
    with pytest.raises(QueueFull):
        await svc.admit(prep)


async def test_metrics(client):
    await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 2},
    )
    r = await client.get("/metrics")
    assert "dshinfer_requests_total" in r.text
    assert "dshinfer_tokens_total" in r.text


TOOL_CALL_TEXT = (
    "好的，我查一下。\n<tool_call>\n<function=get_weather>\n<parameter=city>\n杭州\n"
    "</parameter>\n</function>\n</tool_call>"
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查天气",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }
]


@pytest.fixture
async def tool_client():
    app = create_app(_settings(mock_response=TOOL_CALL_TEXT))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_tool_call_non_stream(tool_client):
    r = await tool_client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "杭州天气"}],
            "tools": TOOLS,
            "max_tokens": 200,
        },
    )
    assert r.status_code == 200
    body = r.json()
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "好的，我查一下。"
    calls = choice["message"]["tool_calls"]
    assert len(calls) == 1
    assert calls[0]["type"] == "function"
    assert calls[0]["function"]["name"] == "get_weather"
    assert json.loads(calls[0]["function"]["arguments"]) == {"city": "杭州"}


async def test_tool_call_stream(tool_client):
    async with tool_client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "杭州天气"}],
            "tools": TOOLS,
            "stream": True,
            "max_tokens": 200,
        },
    ) as r:
        lines = [line async for line in r.aiter_lines() if line.startswith("data: ")]

    text, calls, finish = "", [], None
    for raw in lines:
        p = raw[6:]
        if p.strip() == "[DONE]":
            break
        chunk = json.loads(p)
        for ch in chunk["choices"]:
            delta = ch.get("delta") or {}
            text += delta.get("content") or ""
            calls.extend(delta.get("tool_calls") or [])
            finish = ch.get("finish_reason") or finish
    assert text.strip() == "好的，我查一下。"
    assert finish == "tool_calls"
    assert calls[0]["function"]["name"] == "get_weather"
    assert json.loads(calls[0]["function"]["arguments"]) == {"city": "杭州"}


THINKING_TOOL_TEXT = "先想一想</think>\n\n" + TOOL_CALL_TEXT


@pytest.fixture
async def thinking_tool_client():
    app = create_app(_settings(mock_response=THINKING_TOOL_TEXT, enable_thinking=True))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_thinking_with_tools_stream_keeps_reasoning_out_of_content(thinking_tool_client):
    """开思考 + 带工具：推理段只能走 reasoning_content，正文里不能混进 </think>。"""
    async with thinking_tool_client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "杭州天气"}],
            "tools": TOOLS,
            "stream": True,
            "max_tokens": 200,
        },
    ) as r:
        lines = [line async for line in r.aiter_lines() if line.startswith("data: ")]

    reasoning, text, calls, finish = "", "", [], None
    for raw in lines:
        p = raw[6:]
        if p.strip() == "[DONE]":
            break
        chunk = json.loads(p)
        for ch in chunk["choices"]:
            delta = ch.get("delta") or {}
            reasoning += delta.get("reasoning_content") or ""
            text += delta.get("content") or ""
            calls.extend(delta.get("tool_calls") or [])
            finish = ch.get("finish_reason") or finish
    assert reasoning.strip() == "先想一想"
    assert "</think>" not in text
    assert "先想一想" not in text
    assert text.strip() == "好的，我查一下。"
    assert finish == "tool_calls"
    assert calls[0]["function"]["name"] == "get_weather"


async def test_thinking_with_tools_non_stream(thinking_tool_client):
    r = await thinking_tool_client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "杭州天气"}],
            "tools": TOOLS,
            "max_tokens": 200,
        },
    )
    choice = r.json()["choices"][0]
    message = choice["message"]
    assert message["reasoning_content"] == "先想一想"
    assert message["content"] == "好的，我查一下。"
    assert choice["finish_reason"] == "tool_calls"


async def test_tool_choice_none_disables_tools(tool_client):
    """tool_choice="none" 时不该把工具段拼进提示词。"""
    svc = ChatService(MockEngine(_settings()), _settings())
    req = ChatCompletionRequest(
        messages=[ChatMessage(role="user", content="hi")],
        tools=TOOLS,
        tool_choice="none",
    )
    prep = svc.prepare(req)
    assert prep.tools is None
    assert "get_weather" not in prep.prompt
