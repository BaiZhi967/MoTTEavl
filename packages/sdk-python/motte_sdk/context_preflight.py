"""Conservative, tokenizer-independent context-window preflight estimates."""
from __future__ import annotations

import json
from math import ceil
from typing import Any

METHOD_VERSION = "utf8-bytes-plus-structure-v1"
REQUEST_STRUCTURE_TOKENS = 32
MESSAGE_STRUCTURE_TOKENS = 16


def _utf8_bytes(value: Any) -> int:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return len(text.encode("utf-8"))


def _nearest_rank(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _distribution(values: list[int]) -> dict[str, int]:
    return {
        "max": max(values, default=0),
        "p50": _nearest_rank(values, 0.50),
        "p95": _nearest_rank(values, 0.95),
    }


def estimate_context_preflight(
    cases: dict[str, dict[str, Any]],
    *,
    context_window: int,
    output_tokens_reserved: int,
) -> dict[str, Any]:
    """Estimate an upper bound for each case without retaining request text.

    A byte-level tokenizer cannot produce more content tokens than the number of
    UTF-8 bytes it consumes. Canonical JSON is used for message arrays so roles
    and structured content are counted. Fixed request/message allowances cover
    provider chat-envelope structure without depending on one tokenizer.
    """
    if type(context_window) is not int or context_window <= 0:
        raise ValueError("context_window must be a positive integer")
    if type(output_tokens_reserved) is not int or output_tokens_reserved < 0:
        raise ValueError("output_tokens_reserved must be a non-negative integer")

    per_case: dict[str, dict[str, Any]] = {}
    input_estimates: list[int] = []
    total_estimates: list[int] = []

    for case_id, case in cases.items():
        messages = case.get("messages")
        if isinstance(messages, list) and messages:
            source = "messages"
            message_count = len(messages)
            content_bytes = _utf8_bytes(messages)
        else:
            source = "prompt"
            message_count = 1
            content_bytes = _utf8_bytes(case.get("prompt", ""))

        structure_tokens = REQUEST_STRUCTURE_TOKENS + MESSAGE_STRUCTURE_TOKENS * message_count
        input_upper_bound = content_bytes + structure_tokens
        total_upper_bound = input_upper_bound + output_tokens_reserved
        per_case[str(case_id)] = {
            "source": source,
            "utf8_bytes": content_bytes,
            "message_count": message_count,
            "structure_tokens": structure_tokens,
            "input_tokens_upper_bound": input_upper_bound,
            "total_tokens_upper_bound": total_upper_bound,
        }
        input_estimates.append(input_upper_bound)
        total_estimates.append(total_upper_bound)

    return {
        "method": METHOD_VERSION,
        "context_window": context_window,
        "output_tokens_reserved": output_tokens_reserved,
        "per_case": per_case,
        "stats": {
            "case_count": len(per_case),
            "input_tokens_upper_bound": _distribution(input_estimates),
            "total_tokens_upper_bound": _distribution(total_estimates),
        },
    }
