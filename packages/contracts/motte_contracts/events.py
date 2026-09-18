"""Stable trace envelope; legacy flat events need an application-layer projection."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from .messages import Contract


class TraceEvent(Contract):
    protocol: Literal["motte.trace"] = "motte.trace"
    schema_version: int = Field(default=2, ge=1, strict=True)
    run_id: str
    seq: int = Field(ge=1, strict=True)
    type: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    span_id: str | None = None
    parent_span_id: str | None = None
    recorded_at: datetime | None = None
