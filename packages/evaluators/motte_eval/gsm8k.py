"""Versioned Decimal scoring and strict selected-case aggregation (no calls)."""
from typing import Any

from motte_contracts.gsm8k import SCORER_VERSION, final_number

from .execution import CONTINUE_ERROR_CLASSES  # noqa: F401  （套件无关，调用点仍从这里导入）


def score_benchmark_case(result: Any, expected: Any) -> dict[str, Any]:
    error = result.get("error") if isinstance(result, dict) else None
    score = {"passed": False, "attempted": True, "responded": not bool(error),
             "scorer_version": SCORER_VERSION}
    if error:
        return {**score, "outcome": "call_failed", "error_class": error.get("class")}
    content = result.get("content") if isinstance(result, dict) else result
    parsed = final_number(content)
    gold = final_number(f"#### {expected}")
    if gold is None:
        raise ValueError("invalid benchmark expected number")
    if parsed is None:
        return {**score, "outcome": "parse_failure"}
    correct = parsed == gold
    return {**score, "outcome": "correct" if correct else "wrong_answer", "passed": correct,
            "parsed": str(parsed)}


def aggregate_benchmark(scores: list[dict[str, Any]], selected_count: int) -> dict[str, Any]:
    counts = {name: sum(s["outcome"] == name for s in scores)
              for name in ("correct", "wrong_answer", "parse_failure", "call_failed", "not_attempted")}
    attempted = sum(s.get("attempted", False) for s in scores)
    responded = sum(s.get("responded", False) for s in scores)
    return {**counts, "selected": selected_count, "attempted": attempted, "responded": responded,
            "completion": responded / selected_count, "attempt_rate": attempted / selected_count,
            "accuracy": counts["correct"] / selected_count, "scorer_version": SCORER_VERSION}
