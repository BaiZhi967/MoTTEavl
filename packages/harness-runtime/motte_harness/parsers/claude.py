"""Claude batch（`-p --output-format json`）原生结果 parser。

单对象结果按 pinned 2.1.278 的文档形态解析；未知 schema/缺终态不变成
假成功：success→final；error_*→error；结构不符→insufficient（coverage
partial）。usage/cost 只取原生回报值，缺失保持 unknown（M4-A10）。
单对象结果**不包含工具轨迹**：tool_trajectory 如实标记 absent，
消费方不得据此证明"未调用工具"（M4 review R13）。
"""
from __future__ import annotations

import json
import math
from typing import Any

PARSER_VERSION = "claude-json-v1"

_KNOWN_TOP_FIELDS = frozenset({
    "type", "subtype", "is_error", "duration_ms", "duration_api_ms",
    "num_turns", "result", "session_id", "total_cost_usd", "usage",
    "modelUsage", "permission_denials",
})
_SUCCESS_SUBTYPES = frozenset({"success"})
_ERROR_SUBTYPES = frozenset({
    "error_max_turns", "error_during_execution",
})


def parse_claude_batch(stdout: str, *, raw_ref: str | None = None) -> dict[str, Any]:
    """解析 claude -p --output-format json 的单个结果对象。"""
    stripped = stdout.strip()
    if not stripped:
        return _insufficient("empty_output", raw_ref)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return _insufficient("malformed_json", raw_ref)
    if not isinstance(payload, dict):
        return _insufficient("non_object_result", raw_ref)
    unknown_fields = sorted(set(payload) - _KNOWN_TOP_FIELDS)
    if payload.get("type") != "result":
        return _insufficient("unknown_schema", raw_ref, unknown=unknown_fields)
    subtype = payload.get("subtype")
    if not isinstance(subtype, str):
        return _insufficient("invalid_subtype", raw_ref, unknown=unknown_fields)
    if subtype in _ERROR_SUBTYPES or payload.get("is_error") is True:
        return {
            "parser_version": PARSER_VERSION,
            "status": "error",
            "subtype": subtype,
            "final_output": payload.get("result"),
            "session_id": payload.get("session_id"),
            "usage": _usage(payload),
            "cost_usd": _cost(payload),
            "model": _observed_model(payload),
            "num_turns": payload.get("num_turns"),
            "coverage": "partial" if unknown_fields else "complete",
            "tool_trajectory": "absent",
            "unknown_fields": unknown_fields,
        }
    if subtype not in _SUCCESS_SUBTYPES:
        return _insufficient("unknown_subtype", raw_ref, unknown=unknown_fields)
    if not isinstance(payload.get("result"), str):
        return _insufficient("missing_result_text", raw_ref)
    return {
        "parser_version": PARSER_VERSION,
        "status": "final",
        "subtype": subtype,
        "final_output": payload["result"],
        "session_id": payload.get("session_id"),
        "usage": _usage(payload),
        "cost_usd": _cost(payload),
        "model": _observed_model(payload),
        "num_turns": payload.get("num_turns"),
        "coverage": "partial" if unknown_fields else "complete",
        "tool_trajectory": "absent",
        "unknown_fields": unknown_fields,
    }


def _usage(payload: dict[str, Any]) -> dict[str, Any]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {"reported": False, "input_tokens": None, "output_tokens": None}
    input_tokens = _int_or_none(usage.get("input_tokens"))
    output_tokens = _int_or_none(usage.get("output_tokens"))
    if input_tokens is None and output_tokens is None:
        return {"reported": False, "input_tokens": None, "output_tokens": None}
    return {
        "reported": True,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": _int_or_none(usage.get("cache_read_input_tokens")),
        "cache_creation_input_tokens": _int_or_none(
            usage.get("cache_creation_input_tokens")
        ),
    }


def _cost(payload: dict[str, Any]) -> float | None:
    cost = payload.get("total_cost_usd")
    if type(cost) not in (int, float) or cost < 0:
        return None
    try:
        return cost if math.isfinite(cost) else None
    except OverflowError:
        return None


def _observed_model(payload: dict[str, Any]) -> str | None:
    model_usage = payload.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        keys = [str(key) for key in model_usage if str(key).strip()]
        if len(keys) == 1:
            return keys[0]
        if keys:
            return ",".join(sorted(keys))
    return None


def _int_or_none(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _insufficient(
    reason: str, raw_ref: str | None, *, unknown: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "parser_version": PARSER_VERSION,
        "status": "insufficient",
        "reason": reason,
        "final_output": None,
        "session_id": None,
        "usage": {"reported": False, "input_tokens": None, "output_tokens": None},
        "cost_usd": None,
        "model": None,
        "num_turns": None,
        "coverage": "partial",
        "tool_trajectory": "absent",
        "unknown_fields": unknown or [],
        "raw_ref": raw_ref,
    }
