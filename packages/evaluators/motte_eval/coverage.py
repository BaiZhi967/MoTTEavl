"""覆盖与合法分母（M6-T02 Lite）。

按注册指标的分母口径聚合（selected_cases / judged_cases ...），不统一
强改旧套件公式；互斥 disposition 合计必须等于 selected（缺样本不删除）。
空分母、NaN 指标值不能通过；费用 known/null 分开，零成功的单位成本
不适用（不当 0）。
"""
from __future__ import annotations

import math
from typing import Any


def coverage_summary(
    *,
    denominator: str,
    selected: int,
    judged: int,
    scored: int,
    attempted: int | None = None,
    unknown: int = 0,
    not_attempted: int = 0,
    failed_after_attempt: int = 0,
    metric_value: float | None = None,
    cost: dict[str, Any] | None = None,
    metric_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if attempted is None:
        attempted = judged + failed_after_attempt
    derived_not_attempted = selected - attempted
    if derived_not_attempted < 0:
        raise ValueError(
            f"attempted {attempted} exceeds selected {selected}",
        )
    if not_attempted:
        if not_attempted != derived_not_attempted:
            raise ValueError(
                f"disposition inconsistent: explicit not_attempted {not_attempted} != "
                f"derived {derived_not_attempted}; missing samples must stay visible",
            )
    else:
        not_attempted = derived_not_attempted
    total = judged + unknown + not_attempted + failed_after_attempt
    denominator_value = {
        "selected_cases": selected,
        "judged_cases": judged,
    }.get(denominator)
    coverage = (
        round(judged / selected, 6)
        if denominator == "selected_cases" and selected > 0
        else round(scored / judged, 6)
        if denominator == "judged_cases" and judged > 0
        else None
    )
    cost_view = {
        "known": bool((cost or {}).get("known")),
        "total_usd": (cost or {}).get("total_usd") if (cost or {}).get("known") else None,
        "currency": (cost or {}).get("currency") if (cost or {}).get("known") else None,
    }
    success_count = scored if metric_value is None else None
    per_success = None
    if (
        cost_view["known"] and isinstance(cost_view["total_usd"], (int, float))
        and isinstance(success_count, int) and success_count > 0
    ):
        per_success = round(float(cost_view["total_usd"]) / success_count, 6)
    return {
        "denominator": denominator,
        "denominator_value": denominator_value,
        "selected": selected,
        "attempted": attempted,
        "judged": judged,
        "scored": scored,
        "unknown": unknown,
        "not_attempted": not_attempted,
        "failed_after_attempt": failed_after_attempt,
        "disposition_total": total,
        "coverage": coverage,
        "passable": coverage is not None and coverage > 0,
        "metric_value": metric_value,
        "metric_passable": metric_value is None or not math.isnan(float(metric_value)),
        # 注册指标 → 数值（review R07）：Gate 按指标身份取值，不重算。
        **({"metric_values": dict(metric_values)} if metric_values else {}),
        "cost": {**cost_view, "per_success_usd": per_success},
        "cost_passable": cost_view["known"],
    }
