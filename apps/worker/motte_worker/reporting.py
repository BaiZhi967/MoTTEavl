from __future__ import annotations

import json
import sys
from typing import Any, TextIO


class WorkerReporter:
    """Emit operation-safe Worker progress as one JSON object per stderr line."""

    def __init__(self, stream: TextIO | None = None, *, enabled: bool = True) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = enabled

    def emit(self, event: str, **fields: Any) -> None:
        if not self.enabled:
            return
        payload = {"component": "worker", "event": event, **fields}
        try:
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), file=self.stream, flush=True)
        except Exception:
            pass

    def observe_event(self, trace_event: dict[str, Any]) -> None:
        fields: dict[str, Any] = {}
        self._copy(trace_event, fields, "run_id", str)
        self._copy(trace_event, fields, "seq", int)
        self._copy(trace_event, fields, "status", str)
        self._copy(trace_event, fields, "case_id", str)
        self._copy(trace_event, fields, "passed", bool)
        self._copy(trace_event, fields, "outcome", str)
        self._copy(trace_event, fields, "attempted", bool)
        self._copy(trace_event, fields, "responded", bool)
        error = trace_event.get("error")
        if isinstance(error, dict):
            self._copy(error, fields, "code", str, target="error_code")
            self._copy(error, fields, "class", str, target="error_class")
        self.emit(str(trace_event.get("type") or "trace_event"), **fields)

    def observe_progress(self, progress: dict[str, Any]) -> None:
        fields: dict[str, Any] = {}
        for key, expected in (
            ("run_id", str),
            ("case_id", str),
            ("ordinal", int),
            ("total", int),
            ("duration_ms", (int, float)),
            ("outcome", str),
            ("reason", str),
            ("latency_ms", (int, float)),
            ("attempts", int),
            ("retry_count", int),
            ("error_class", str),
            ("prompt_tokens", int),
            ("completion_tokens", int),
            ("total_tokens", int),
            ("cost_total", (int, float)),
            ("cost_currency", str),
            ("price_table_version", str),
        ):
            self._copy(progress, fields, key, expected)
        self.emit(str(progress.get("event") or "case_progress"), **fields)

    @staticmethod
    def _copy(
        source: dict[str, Any],
        output: dict[str, Any],
        key: str,
        expected: type | tuple[type, ...],
        *,
        target: str | None = None,
    ) -> None:
        value = source.get(key)
        if isinstance(value, expected):
            output[target or key] = value
