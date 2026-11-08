"""API 契约：OpenAI /v1/chat/completions 的请求体（子集）。

只实现我们真正支持的字段，其余字段接受但忽略——这样 OpenAI SDK 可以直接指过来。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Role = Literal["system", "user", "assistant", "tool", "developer"]


class ChatMessage(BaseModel):
    role: Role
    content: str | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None

    @field_validator("content", mode="before")
    @classmethod
    def _flatten_parts(cls, v: Any) -> Any:
        """content 允许是 [{"type":"text","text":"..."}] 这种分段格式。"""
        if isinstance(v, list):
            texts = [
                p.get("text", "") for p in v if isinstance(p, dict) and p.get("type") == "text"
            ]
            return "".join(texts)
        return v


class StreamOptions(BaseModel):
    include_usage: bool = False


class ToolFunction(BaseModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


class Tool(BaseModel):
    type: Literal["function"] = "function"
    function: ToolFunction


class ChatCompletionRequest(BaseModel):
    model_config = {"extra": "allow"}

    model: str | None = None
    messages: list[ChatMessage] = Field(min_length=1)

    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    stop: str | list[str] | None = None
    stream: bool = False
    stream_options: StreamOptions | None = None
    seed: int | None = None
    n: int = 1

    # 工具调用：tools 原样透传给 chat 模板（模板负责把 schema 拼进 system 段）
    tools: list[Tool] | None = None
    # "auto" | "none" | {"type":"function","function":{"name":...}}
    tool_choice: str | dict[str, Any] | None = None

    @property
    def stop_list(self) -> list[str]:
        if self.stop is None:
            return []
        return [self.stop] if isinstance(self.stop, str) else list(self.stop)

    @property
    def token_budget(self) -> int | None:
        return self.max_completion_tokens or self.max_tokens
