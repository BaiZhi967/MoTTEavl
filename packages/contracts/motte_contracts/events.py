from typing import Any
from .messages import Contract

class TraceEvent(Contract):
    protocol: str = "motte.trace"
    schema_version: int = 1
    run_id: str
    seq: int
    span_id: str
    parent_span_id: str | None = None
    type: str
    payload: dict[str, Any] = {}
