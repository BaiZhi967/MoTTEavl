class RegressionGate:
    def __init__(self, threshold, direction="gte"):
        self.threshold = threshold
        self.direction = direction

    def check(self, value):
        return value >= self.threshold if self.direction == "gte" else value <= self.threshold


# --------------------------------------------------------------------------
# M6-T03 Lite：版本化纯求值 Gate（固定 baseline/候选/政策 → 结构化结论）。
# --------------------------------------------------------------------------

GATE_SCHEMA_VERSION = "gate-lite@1"

_GATE_OPS = {
    "gte": lambda value, threshold: value >= threshold,
    "lte": lambda value, threshold: value <= threshold,
    "gt": lambda value, threshold: value > threshold,
    "lt": lambda value, threshold: value < threshold,
}


def _conclusion_hash(payload: dict) -> str:
    import hashlib
    import json

    return "sha256:" + hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def evaluate_gate(
    policy: dict,
    candidate: dict,
    *,
    comparison: dict | None = None,
    evaluated_at: str | None = None,
) -> dict:
    """纯求值：不执行 Runner/Judge，不读取网络；重复调用结论确定。

    ``candidate`` 是 ``coverage_summary`` 的输出（含 metric_value/coverage/
    cost/metric_passable）。缺覆盖、NaN/空分母、unknown cost、不可比分别
    对应硬规则失败；``evaluated_at`` 只进审计元数据，不进结论 hash。
    """
    import math

    threshold = policy.get("threshold")
    operation = str(policy.get("op") or "gte")
    metric_value = candidate.get("metric_value")
    rules: list[dict] = []

    metric_ok = False
    if metric_value is None:
        reason = "metric value missing (empty denominator or unscored)"
    elif isinstance(metric_value, float) and math.isnan(metric_value):
        reason = "metric value is NaN"
    elif operation not in _GATE_OPS or not isinstance(threshold, (int, float)):
        reason = f"policy invalid: op={operation!r} threshold={threshold!r}"
    else:
        metric_ok = _GATE_OPS[operation](float(metric_value), float(threshold))
        reason = (
            f"{metric_value} {operation} {threshold}"
            if metric_ok else f"{metric_value} not {operation} {threshold}"
        )
    rules.append({"id": "metric_threshold", "passed": metric_ok, "reason": reason})

    required_coverage = float(policy.get("required_coverage", 0.0))
    coverage = candidate.get("coverage")
    coverage_ok = coverage is not None and float(coverage) >= required_coverage
    rules.append({
        "id": "coverage",
        "passed": coverage_ok,
        "reason": (
            f"coverage {coverage} >= {required_coverage}" if coverage_ok
            else f"coverage {coverage} < required {required_coverage} (insufficient evidence)"
        ),
    })

    if policy.get("require_cost_known"):
        cost_ok = bool(candidate.get("cost_passable"))
        rules.append({
            "id": "cost_known",
            "passed": cost_ok,
            "reason": "cost known" if cost_ok else "cost unknown; hard cost gate cannot pass",
        })

    if policy.get("require_comparable"):
        comparable = bool((comparison or {}).get("eligible"))
        rules.append({
            "id": "comparable",
            "passed": comparable,
            "reason": (
                "reports comparable" if comparable
                else "reports not comparable: " + "; ".join(
                    (comparison or {}).get("reasons") or ["no comparison result"],
                )
            ),
        })

    passed = all(rule["passed"] for rule in rules)
    conclusion = {
        "schema": GATE_SCHEMA_VERSION,
        "policy": dict(policy),
        "passed": passed,
        "rules": rules,
    }
    conclusion["conclusion_hash"] = _conclusion_hash(conclusion)
    conclusion["evaluated_at"] = evaluated_at
    return conclusion
