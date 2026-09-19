class RegressionGate:
    def __init__(self, threshold, direction="gte"):
        self.threshold = threshold
        self.direction = direction

    def check(self, value):
        return value >= self.threshold if self.direction == "gte" else value <= self.threshold


# --------------------------------------------------------------------------
# M6-T03 Lite：版本化纯求值 Gate（固定 baseline/候选/政策 → 结构化结论）。
# --------------------------------------------------------------------------

GATE_SCHEMA_VERSION = "gate-lite@2"

_GATE_OPS = {
    "gte": lambda value, threshold: value >= threshold,
    "lte": lambda value, threshold: value <= threshold,
    "gt": lambda value, threshold: value > threshold,
    "lt": lambda value, threshold: value < threshold,
}

# 注册指标的口径（review R07）：Gate 按 metric 身份选择数值，不再"指哪
# 个指标都拿 accuracy 比较"。方向/单位/分母进入规则文本；费用指标在
# 费用未知时自动不足证据，不依赖调用方记得设置布尔开关。
METRIC_REGISTRY: dict[str, dict] = {
    "accuracy": {
        "direction": "gte", "unit": "ratio", "denominator": "selected_cases",
        "description": "selected-case accuracy (platform recomputation)",
    },
    "cost.total_usd": {
        "direction": "lte", "unit": "USD", "denominator": "run",
        "description": "total observed run cost",
        "requires_cost_known": True,
    },
}


def _conclusion_hash(payload: dict) -> str:
    import hashlib
    import json

    return "sha256:" + hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _resolve_metric_value(policy: dict, candidate: dict) -> tuple[bool, object, str]:
    """按注册指标身份取值；未知指标或费用未知都返回明确失败原因。"""
    metric_id = str(policy.get("metric") or "accuracy")
    spec = METRIC_REGISTRY.get(metric_id)
    if spec is None:
        known = ", ".join(sorted(METRIC_REGISTRY))
        return (False, None, f"unsupported metric: {metric_id!r} (known: {known})")
    values = candidate.get("metric_values")
    if not isinstance(values, dict) or metric_id not in values:
        # 兼容只带 metric_value（=accuracy）的旧输入。
        if metric_id == "accuracy" and candidate.get("metric_value") is not None:
            return (True, candidate.get("metric_value"), "")
        return (False, None, f"metric value missing: {metric_id} (insufficient evidence)")
    value = values.get(metric_id)
    if value is None:
        if spec.get("requires_cost_known"):
            return (False, None, f"cost unknown: {metric_id} cannot pass without observed cost")
        return (False, None, f"metric value missing: {metric_id} (empty denominator or unscored)")
    import math

    if isinstance(value, float) and math.isnan(value):
        return (False, None, f"metric value is NaN: {metric_id}")
    return (True, value, "")


def evaluate_gate(
    policy: dict,
    candidate: dict,
    *,
    comparison: dict | None = None,
    evaluated_at: str | None = None,
) -> dict:
    """纯求值：不执行 Runner/Judge，不读取网络；重复调用结论确定。

    ``candidate`` 是 ``coverage_summary`` 的输出（含 metric_values/
    coverage/cost）。按 ``policy.metric`` 在注册表中解析指标身份与数值
    （review R07）；缺覆盖、NaN/空分母、unknown cost、不可比分别对应硬
    规则失败；``evaluated_at`` 只进审计元数据，不进结论 hash。
    """
    threshold = policy.get("threshold")
    operation = str(policy.get("op") or "gte")
    metric_id = str(policy.get("metric") or "accuracy")
    metric_spec = METRIC_REGISTRY.get(metric_id) or {}
    resolvable, metric_value, resolve_reason = _resolve_metric_value(policy, candidate)
    rules: list[dict] = []

    metric_ok = False
    if not resolvable:
        reason = resolve_reason
    elif operation not in _GATE_OPS or not isinstance(threshold, (int, float)):
        reason = f"policy invalid: op={operation!r} threshold={threshold!r}"
    else:
        metric_ok = _GATE_OPS[operation](float(metric_value), float(threshold))
        unit = metric_spec.get("unit")
        reason = (
            f"{metric_id}={metric_value} ({unit}) {operation} {threshold}"
            if metric_ok else (
                f"{metric_id}={metric_value} ({unit}) not {operation} {threshold}"
            )
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
        "metric_id": metric_id,
        "passed": passed,
        "rules": rules,
    }
    conclusion["conclusion_hash"] = _conclusion_hash(conclusion)
    conclusion["evaluated_at"] = evaluated_at
    return conclusion
