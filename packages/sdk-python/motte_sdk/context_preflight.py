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


def external_benchmark_preflight(
    profile: dict[str, Any],
    model: dict[str, Any],
    *,
    dataset: dict[str, Any],
    runner_connected: bool,
    prompt_bytes: int | None = None,
) -> dict[str, Any]:
    """外部 Benchmark 静态预检：只验证本地/静态条件，不触发模型探针。

    数据/Runner 未就绪、学科覆盖不足、上下文预算不足都给出具体原因；
    真实模型探针是显式动作，与打开页面/预检分开（M2 需求第 9 节）。
    """
    reasons: list[str] = []
    dataset_state = str(dataset.get("state") or "unprepared")
    dataset_ready = dataset_state == "ready"
    if not dataset_ready:
        reasons.append("DATASET_UNPREPARED")
    if not runner_connected:
        reasons.append("RUNNER_NOT_CONNECTED")

    subjects = [str(item) for item in (dataset.get("subjects") or [])]
    for subject in (profile.get("selected_subjects") or ()):
        if str(subject) not in subjects:
            reasons.append(f"SUBJECT_NOT_IN_DATASET:{subject}")
    if str(dataset.get("split") or "") != str(profile.get("split") or ""):
        reasons.append("SPLIT_MISMATCH")

    for field in ("runner_version", "environment_digest"):
        if not str(profile.get(field) or "").strip():
            reasons.append(f"PROFILE_UNPINNED:{field}")

    if not str(model.get("model") or model.get("id") or "").strip():
        reasons.append("MODEL_IDENTITY_MISSING")

    context_window = model.get("context_window")
    estimate = max(
        (len(str(case.get("prompt") or "")) for case in (dataset.get("cases") or [])),
        default=prompt_bytes or 0,
    )
    # 保守的 token 上界：UTF-8 字节近似（每字节≤1 token 的上界估计）。
    context_sufficient: bool | None = None
    if isinstance(context_window, int):
        context_sufficient = estimate <= context_window
        if not context_sufficient:
            reasons.append(
                f"CONTEXT_WINDOW_INSUFFICIENT:prompt~{estimate} > window {context_window}",
            )
    return {
        "method": "external-benchmark-static-v1",
        "ok": not reasons,
        "reasons": reasons,
        "checks": {
            "dataset_ready": dataset_ready,
            "runner_connected": runner_connected,
            "subjects_covered": not any(
                reason.startswith("SUBJECT_NOT_IN_DATASET") for reason in reasons
            ),
            "context_window_sufficient": context_sufficient,
        },
    }
