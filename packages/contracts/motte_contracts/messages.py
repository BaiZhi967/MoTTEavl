from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Message(Contract):
    role: str
    content: str | list[dict[str, Any]]


class ModelRequest(Contract):
    model: str
    messages: list[Message]
    system: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    stop: str | list[str] | None = None
    seed: int | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(Contract):
    model: str
    content: str = ""
    finish_reason: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)


class StreamEvent(Contract):
    type: str
    delta: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
