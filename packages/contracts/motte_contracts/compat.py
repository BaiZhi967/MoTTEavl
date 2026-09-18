"""Explicit read adapters for persisted schema-v1 records."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


_TRACE_FIELDS = {
    "protocol",
    "schema_version",
    "run_id",
    "seq",
    "type",
    "payload",
    "span_id",
    "parent_span_id",
    "recorded_at",
}


def adapt_legacy_run(record: dict[str, Any]) -> dict[str, Any]:
    """Project a pre-revision Run into the public v1-compatible shape."""
    result = deepcopy(record)
    if "scenario_version" not in result and "scenario_id" in result:
        result["scenario_version"] = result.pop("scenario_id")
    result.setdefault("revision", 0)
    result.setdefault("schema_version", 1 if result["revision"] == 0 else 2)
    result.setdefault("manifest", {})
    result.setdefault("case_ids", [])
    result.setdefault("cases", [])
    result.setdefault("scores", [])
    return result


def adapt_legacy_trace_event(record: dict[str, Any]) -> dict[str, Any]:
    """Move legacy flat event details into the stable TraceEvent payload."""
    result = deepcopy(record)
    result.setdefault("protocol", "motte.trace")
    result.setdefault("schema_version", 1)
    if "recorded_at" not in result and "timestamp" in result:
        result["recorded_at"] = result.pop("timestamp")
    payload = result.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("trace event payload must be an object")
    for key in list(result):
        if key not in _TRACE_FIELDS:
            payload[key] = result.pop(key)
    result["payload"] = payload
    return result


def adapt_legacy_report(record: dict[str, Any]) -> dict[str, Any]:
    """Mark reports without a selected scoring pass as schema v1 views."""
    result = deepcopy(record)
    if "scenario_version" not in result and "scenario_id" in result:
        result["scenario_version"] = result.pop("scenario_id")
    result.setdefault("schema_version", 1 if not result.get("scoring_pass_id") else 2)
    result.setdefault("scoring_pass_id", None)
    result.setdefault("scores", [])
    result.setdefault("cases", [])
    return result
