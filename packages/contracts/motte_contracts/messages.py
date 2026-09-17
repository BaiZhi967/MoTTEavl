from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Message(Contract):
    role: str
    content: str | list[dict[str, Any]]
    # canonical 工具回合（OpenAI 形状）：assistant 消息携带的工具调用、tool 消息的对应 id
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class ModelRequest(Contract):
    model: str
    messages: list[Message]
    system: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = Field(default=None, gt=0, strict=True)
    stop: str | list[str] | None = None
    seed: int | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_choice: str | dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(Contract):
    model: str
    content: str = ""
    finish_reason: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    # canonical 工具调用 [{id, name, arguments(JSON 字符串)}]
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    response_id: str | None = None
    # 协议特有计量扩展（canonical 键）：reasoning_tokens、cache_read_input_tokens 等
    usage_details: dict[str, int] = Field(default_factory=dict)


class StreamEvent(Contract):
    type: str
    delta: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
