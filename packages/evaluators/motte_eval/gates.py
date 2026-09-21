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
# Terminal-Bench（review R18）：Trial 口径的质量与覆盖各有一个注册指标，
# 分母分别是有效 Trial 与计划 Trial——覆盖不足与质量不足分开判断。
METRIC_REGISTRY: dict[str, dict] = {
    "accuracy": {
        "direction": "gte", "unit": "ratio", "denominator": "selected_cases",
        "description": "selected-case accuracy (platform recomputation)",
    },
    "valid_trial_pass_rate": {
        "direction": "gte", "unit": "ratio", "denominator": "valid_trials",
        "description": "Terminal-Bench valid-Trial pass rate (Harbor rewards)",
    },
    "valid_trial_coverage": {
        "direction": "gte", "unit": "ratio", "denominator": "planned_trials",
        "description": "valid Trials over the frozen trial plan (Harbor)",
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


# --------------------------------------------------------------------------
# M6-T03/T08 Full：规则化 Regression Gate 引擎（协议 §6/§7/§8）。
# --------------------------------------------------------------------------

import math  # noqa: E402
from typing import Any, Mapping  # noqa: E402

from motte_contracts.comparison import (  # noqa: E402
    ComparabilityLevel,
    ReportSnapshot,
    RunReportRef,
)
from motte_contracts.gates import (  # noqa: E402
    DECISION_EXIT_CODES,
    DECISION_PRIORITY,
    GATE_ENGINE_VERSION,
    GATE_RULE_REGISTRY_VERSION,
    GateDecision,
    GatePolicyVersion,
    GateResult,
    GateRule,
    RuleResult,
)
from motte_contracts.hashing import canonical_hash  # noqa: E402
from motte_contracts.metrics import (  # noqa: E402
    METRIC_REGISTRY_VERSION,
    lookup_metric,
)

#: 决策优先级的运行时视图（协议 §6 冻结）。
DECISION_ORDER: tuple[GateDecision, ...] = DECISION_PRIORITY


class GateInputError(ValueError):
    """Gate 求值输入不满足前置（API 映射 422/退出码 2）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _metric_value(snapshot: Mapping[str, Any], metric_id: str) -> tuple[bool, float | None, str]:
    """按注册指标身份从 ReportSnapshot 取值；缺失/未知指标给明确原因。"""
    definition = lookup_metric(metric_id)
    if definition is None:
        return False, None, f"unknown metric: {metric_id!r} (registry {METRIC_REGISTRY_VERSION})"
    qualified = f"{metric_id}@{definition.version}"
    values = snapshot.get("metric_values") or {}
    if metric_id not in values:
        return (
            False, None,
            f"metric value missing: {qualified} (insufficient evidence; "
            "missing values never become 0 or pass)",
        )
    value = values[metric_id]
    if value is None:
        reason = (
            f"cost unknown: {qualified} cannot pass without observed cost"
            if "price_table" in definition.required_evidence
            else f"metric value missing: {qualified} (empty denominator or unscored)"
        )
        return False, None, reason
    if isinstance(value, float) and math.isnan(value):
        return False, None, f"metric value is NaN: {qualified}"
    if isinstance(value, float) and math.isinf(value):
        return False, None, f"metric value is infinite: {qualified}"
    return True, float(value), ""


def _rule_direction_reason(rule: GateRule, definition: Any) -> str | None:
    """规则 operator 必须与 metric direction 一致（协议 §6）。"""
    if rule.operator is None:
        return None
    if rule.operator == definition.direction:
        return None
    if rule.operator in ("gt", "lt") and rule.operator[:2] == definition.direction[:2]:
        return None
    return (
        f"rule operator {rule.operator!r} contradicts metric direction "
        f"{definition.direction!r} for {rule.metric_id}"
    )


def _comparability_gate(
    rule: GateRule,
    comparison_level: ComparabilityLevel | None,
    metric_eligibility: Mapping[str, bool] | None,
) -> tuple[str | None, GateDecision | None]:
    """required_comparability 检查（协议 §6：partial 且要求 comparable → 不可比）。"""
    if rule.required_comparability is None:
        return None, None
    if comparison_level is None:
        return "no comparison result available", GateDecision.NOT_COMPARABLE
    if (
        rule.required_comparability == "comparable"
        and comparison_level != ComparabilityLevel.COMPARABLE
    ):
        return (
            f"comparison level {comparison_level.value} does not satisfy "
            "required_comparability=comparable",
            GateDecision.NOT_COMPARABLE,
        )
    if (
        rule.required_comparability == "partial"
        and comparison_level == ComparabilityLevel.NOT_COMPARABLE
    ):
        return (
            "comparison level not_comparable does not satisfy "
            "required_comparability=partial",
            GateDecision.NOT_COMPARABLE,
        )
    # partial 可比时逐指标资格仍要满足（cost 规则在 cost 不可比时不足证据）。
    if comparison_level == ComparabilityLevel.PARTIALLY_COMPARABLE and metric_eligibility:
        scope = "cost" if (rule.metric_id or "").startswith("cost.") else "quality"
        if metric_eligibility.get(scope) is False:
            return (
                f"metric scope {scope!r} ineligible under partial comparability",
                GateDecision.INSUFFICIENT_EVIDENCE,
            )
    return None, None


def _case_outcome(snapshot: Mapping[str, Any], case_id: str) -> str | None:
    """固定报告里单个 case 的明确结果：pass/fail/unknown；缺失返回 None。

    ``no_expectation`` 的 case 不属于该质量判定的观察集，返回 None
    （critical_case 规则按 missing 处理——关键样本必须可判定）。
    """
    results = snapshot.get("case_results") or {}
    for record in snapshot.get("case_dispositions") or ():
        if record.get("case_id") != case_id:
            continue
        disposition = record.get("disposition")
        if disposition == "judged":
            value = results.get(case_id)
            if value is True:
                return "pass"
            if value is False:
                return "fail"
            return "unknown"
        if disposition in ("unknown", "needs_review"):
            return "unknown"
        if disposition == "no_expectation":
            return None
        # call_failed：调用失败是明确失败（不是 unknown）。
        if disposition == "call_failed":
            return "fail"
        return "unknown"
    return None


def evaluate_gate_policy(
    policy: GatePolicyVersion,
    *,
    candidate_ref: RunReportRef,
    candidate_snapshot: ReportSnapshot | Mapping[str, Any],
    candidate_run_status: str,
    baseline_ref: RunReportRef | None = None,
    baseline_snapshot: ReportSnapshot | Mapping[str, Any] | None = None,
    comparison_level: ComparabilityLevel | None = None,
    comparison_reasons: tuple[str, ...] | list[str] = (),
    metric_eligibility: Mapping[str, bool] | None = None,
    candidate_evidence: Mapping[str, Any] | None = None,
    statistical_policy_ref: str = "statistical_policy@1",
    evaluated_at: str | None = None,
) -> GateResult:
    """完整 Regression Gate 纯求值（协议 §6–§8）。

    只消费固定引用与冻结快照；不执行模型/Judge/Runner，不触发补跑。重复
    调用（同输入、同政策）产生等价结论：``evaluated_at`` 只进审计字段。
    ``candidate_evidence`` 是可选的额外证据视图（side effects / safety
    markers / 实际模型身份），全部由调用方从固定 pass/report 投影。
    """
    snapshot_view: Mapping[str, Any] = (
        candidate_snapshot.model_dump() if isinstance(candidate_snapshot, ReportSnapshot)
        else candidate_snapshot
    )
    baseline_view: Mapping[str, Any] | None = (
        baseline_snapshot.model_dump() if isinstance(baseline_snapshot, ReportSnapshot)
        else baseline_snapshot
    )
    evidence: Mapping[str, Any] = candidate_evidence or {}
    rule_results: list[RuleResult] = []

    def finish(
        rule: GateRule, status: str, reason: str,
        decision: GateDecision | None = None,
    ) -> None:
        skipped = status == "skipped_diagnostic"
        # 诊断跳过不能把整体变 pass（协议 §6）：跳过规则贡献 insufficient。
        effective_decision = (
            GateDecision.INSUFFICIENT_EVIDENCE if skipped and decision is None
            else decision
        )
        rule_results.append(RuleResult(
            rule_id=rule.rule_id, kind=rule.kind, status=status,
            severity=rule.severity, decision=effective_decision, reason=reason,
            diagnostic_skipped=skipped,
        ))

    for rule in policy.rules:
        # 1) 执行成功前置（协议 §6：执行错误不覆盖分数，但阻断放行）。
        if rule.requires_successful_run and candidate_run_status != "completed":
            finish(
                rule, "fail",
                f"run status {candidate_run_status!r} is not completed; "
                "execution error does not overwrite scores but blocks release",
                decision=GateDecision.EXECUTION_ERROR,
            )
            continue
        # 2) 实验性证据源：正式模式 fail-closed（协议 §6）。
        if rule.experimental_evidence and not policy.diagnostic:
            finish(
                rule, "insufficient",
                "experimental evidence source (uncalibrated judge / unknown runtime "
                "identity) is fail-closed in formal gates",
                decision=GateDecision.INSUFFICIENT_EVIDENCE,
            )
            continue
        # 3) 可比性前置。
        comparability_reason, comparability_decision = _comparability_gate(
            rule, comparison_level, metric_eligibility,
        )
        if comparability_reason is not None:
            if rule.missing_policy == "diagnostic_skip":
                finish(rule, "skipped_diagnostic", comparability_reason)
            else:
                finish(
                    rule, "insufficient", comparability_reason,
                    decision=comparability_decision,
                )
            continue

        # 4) 按 kind 求值。
        if rule.kind in ("metric_threshold", "baseline_delta", "cost", "latency"):
            metric_id = rule.metric_id or ""
            resolvable, value, resolve_reason = _metric_value(snapshot_view, metric_id)
            definition = lookup_metric(metric_id)
            if definition is None:
                finish(rule, "insufficient", resolve_reason,
                       decision=GateDecision.INSUFFICIENT_EVIDENCE)
                continue
            direction_reason = _rule_direction_reason(rule, definition)
            if direction_reason is not None:
                raise GateInputError("RULE_DIRECTION_INVALID", direction_reason)
            if not resolvable or value is None:
                if rule.missing_policy == "diagnostic_skip":
                    finish(rule, "skipped_diagnostic", resolve_reason)
                else:
                    finish(rule, "insufficient", resolve_reason,
                           decision=GateDecision.INSUFFICIENT_EVIDENCE)
                continue
            if rule.kind == "baseline_delta":
                # 相对基线最大允许退化：绝对差口径（协议 §6：baseline=0 按
                # 绝对阈值 0 处理并显式记录，不用比率）。
                if baseline_view is None or baseline_ref is None:
                    finish(rule, "insufficient",
                           "baseline_delta requires a baseline report",
                           decision=GateDecision.INSUFFICIENT_EVIDENCE)
                    continue
                base_resolvable, base_value, base_reason = _metric_value(
                    baseline_view, metric_id,
                )
                if not base_resolvable or base_value is None:
                    finish(
                        rule, "insufficient",
                        f"baseline metric unavailable: {base_reason}",
                        decision=GateDecision.INSUFFICIENT_EVIDENCE,
                    )
                    continue
                assert rule.max_regression is not None
                if definition.direction == "gte":
                    regression = float(base_value) - float(value)
                else:
                    regression = float(value) - float(base_value)
                baseline_note = (
                    " (baseline=0: relative regression treated as absolute delta)"
                    if float(base_value) == 0.0 else ""
                )
                passed = regression <= float(rule.max_regression)
                finish(
                    rule, "pass" if passed else "fail",
                    (
                        f"{metric_id}@{definition.version} regression "
                        f"{round(regression, 6)} <= {rule.max_regression}{baseline_note}"
                        if passed else
                        f"{metric_id}@{definition.version} regression "
                        f"{round(regression, 6)} > {rule.max_regression}{baseline_note}"
                    ),
                    decision=None if passed else GateDecision.QUALITY_FAIL,
                )
                continue
            # cost/latency 允许 threshold=None：此时规则语义是"指标必须已知"
            # （cost unknown 不能通过严格成本规则），已由 _metric_value 保证。
            if rule.threshold is None or rule.operator is None:
                finish(
                    rule, "pass",
                    f"{metric_id}@{definition.version}={value} observed and known",
                )
                continue
            passed = _GATE_OPS[rule.operator](value, float(rule.threshold))
            verdict = (
                f"{metric_id}@{definition.version}={value} ({definition.unit}) "
                f"{rule.operator} {rule.threshold}"
            )
            finish(
                rule, "pass" if passed else "fail",
                verdict if passed else verdict + " NOT satisfied",
                decision=None if passed else GateDecision.QUALITY_FAIL,
            )
            continue

        if rule.kind == "coverage":
            coverage = snapshot_view.get("coverage")
            counts = snapshot_view.get("counts") or {}
            denominator = counts.get("selected", counts.get("judged", 0))
            problems: list[str] = []
            if rule.min_coverage is not None:
                if coverage is None:
                    problems.append("coverage missing (empty denominator)")
                elif float(coverage) < float(rule.min_coverage):
                    problems.append(
                        f"coverage {coverage} < required {rule.min_coverage}"
                    )
            if rule.min_samples is not None and denominator is not None:
                if int(denominator) < int(rule.min_samples):
                    problems.append(
                        f"samples {denominator} < required {rule.min_samples}"
                    )
            if problems:
                finish(rule, "insufficient", "; ".join(problems),
                       decision=GateDecision.INSUFFICIENT_EVIDENCE)
            else:
                finish(rule, "pass",
                       f"coverage {coverage} and samples {denominator} satisfy minimums")
            continue

        if rule.kind == "critical_case":
            failures: list[str] = []
            insufficient: list[str] = []
            for case_id in rule.critical_case_ids:
                outcome = _case_outcome(snapshot_view, case_id)
                if outcome == "pass":
                    continue
                if outcome == "fail":
                    failures.append(case_id)
                elif outcome == "unknown":
                    insufficient.append(case_id)
                else:
                    insufficient.append(f"{case_id} (missing/no-expectation)")
            if failures or insufficient:
                parts = []
                if failures:
                    parts.append("failed: " + ",".join(failures))
                if insufficient:
                    parts.append("unknown/missing: " + ",".join(insufficient))
                finish(
                    rule, "fail" if failures else "insufficient",
                    "critical cases " + "; ".join(parts),
                    decision=GateDecision.QUALITY_FAIL if failures
                    else GateDecision.INSUFFICIENT_EVIDENCE,
                )
            else:
                finish(rule, "pass",
                       "all critical cases passed: " + ",".join(rule.critical_case_ids))
            continue

        if rule.kind == "model_identity":
            reported = evidence.get("model_identity")
            if reported is None:
                finish(rule, "insufficient",
                       "actual model identity not reported (requested model never "
                       "substitutes for the reported one)",
                       decision=GateDecision.INSUFFICIENT_EVIDENCE)
            elif str(reported) != str(rule.expected):
                finish(rule, "fail",
                       f"actual model identity {reported!r} != expected {rule.expected!r}",
                       decision=GateDecision.QUALITY_FAIL)
            else:
                finish(rule, "pass", f"actual model identity {reported!r} matches")
            continue

        if rule.kind in ("no_side_effect", "safety_marker"):
            key = (
                "side_effect_violations" if rule.kind == "no_side_effect"
                else "safety_markers"
            )
            marker = evidence.get(key)
            if marker is None:
                finish(rule, "insufficient",
                       f"{key} not observable from the fixed report (absence of "
                       "evidence is not evidence of absence)",
                       decision=GateDecision.INSUFFICIENT_EVIDENCE)
            elif rule.kind == "no_side_effect":
                count = marker if isinstance(marker, int) else len(marker)
                if count:
                    finish(rule, "fail", f"{count} side-effect violation(s) observed",
                           decision=GateDecision.SAFETY_BLOCK)
                else:
                    finish(rule, "pass", "no side-effect violations observed")
            else:
                markers = marker if isinstance(marker, (list, tuple)) else [marker]
                if markers:
                    finish(rule, "fail",
                           "safety markers present: "
                           + ",".join(str(item) for item in markers),
                           decision=GateDecision.SAFETY_BLOCK)
                else:
                    finish(rule, "pass", "no safety markers present")
            continue

        finish(rule, "not_applicable", f"rule kind {rule.kind!r} not wired")

    # 聚合决策（协议 §6 优先级）：warn 规则不贡献决策。
    contributions = [
        result.decision for result in rule_results
        if result.decision is not None and result.severity == "block"
    ]
    present = set(contributions)
    decision = GateDecision.PASS
    for candidate_decision in DECISION_ORDER:
        if candidate_decision in present:
            decision = candidate_decision
            break
    # 诊断政策：diagnostic_skip 参与过求值时整体不得显示为 pass（协议 §6）。
    if (
        decision == GateDecision.PASS
        and any(result.diagnostic_skipped for result in rule_results)
    ):
        decision = GateDecision.INSUFFICIENT_EVIDENCE

    evaluation_input_hash = canonical_hash({
        "policy": policy.content_hash(),
        "candidates": [candidate_ref.model_dump()],
        "baseline": baseline_ref.model_dump() if baseline_ref is not None else None,
        "statistical_policy": statistical_policy_ref,
    })
    result_semantics_hash = canonical_hash({
        "engine": GATE_ENGINE_VERSION,
        "rule_registry": GATE_RULE_REGISTRY_VERSION,
        "metric_registry": METRIC_REGISTRY_VERSION,
        "statistical_policy": statistical_policy_ref,
    })
    suggested: list[str] = []
    if decision in (GateDecision.INSUFFICIENT_EVIDENCE, GateDecision.NOT_COMPARABLE):
        suggested.append(
            "collect the missing evidence or re-score both sides from the same "
            "frozen evidence into a common scoring pass (explicit, possibly paid; "
            "never triggered automatically by the gate)"
        )
    if decision == GateDecision.EXECUTION_ERROR:
        suggested.append(
            "inspect the failing run; an explicit retry creates a superseding "
            "child run and preserves the original result"
        )
    result = GateResult(
        gate_result_id="pending",
        policy_id=policy.policy_id,
        policy_version=policy.version,
        policy_content_hash=policy.content_hash(),
        baseline=(
            {
                "run_id": baseline_ref.run_id,
                "scoring_pass_id": baseline_ref.scoring_pass_id,
            }
            if baseline_ref is not None else None
        ),
        candidates=(candidate_ref,),
        decision=decision,
        rule_results=tuple(rule_results),
        evaluation_input_hash=evaluation_input_hash,
        result_semantics_hash=result_semantics_hash,
        conclusion_hash="pending",
        evaluated_at=evaluated_at,
        suggested_actions=tuple(suggested),
    )
    return result.model_copy(update={
        "gate_result_id": result.compute_gate_result_id(),
        "conclusion_hash": result.compute_conclusion_hash(),
    })


def gate_exit_code(decision: GateDecision | str) -> int:
    """决策 → CLI 退出码（协议 §7）。"""
    resolved = (
        decision if isinstance(decision, GateDecision) else GateDecision(decision)
    )
    return DECISION_EXIT_CODES[resolved]


def legacy_policy_to_rules(policy: Mapping[str, Any]) -> GatePolicyVersion:
    """Lite dict 政策 → 规则化政策的兼容投影（M2-T09 旧输入）。"""
    metric_id = str(policy.get("metric") or "accuracy")
    operator = str(policy.get("op") or "gte")
    threshold = (
        float(policy["threshold"]) if policy.get("threshold") is not None else None
    )
    rules: list[GateRule] = [
        GateRule(
            rule_id="metric_threshold",
            kind="metric_threshold",
            metric_id=metric_id,
            operator=operator,
            threshold=threshold,
        ),
        GateRule(
            rule_id="coverage",
            kind="coverage",
            min_coverage=(
                float(policy["required_coverage"])
                if policy.get("required_coverage") is not None else 0.0
            ),
        ),
    ]
    if policy.get("require_cost_known"):
        rules.append(GateRule(
            rule_id="cost_known", kind="cost", metric_id="cost.total_usd",
            operator="lte", threshold=None,
        ))
    if policy.get("require_comparable"):
        rules.append(GateRule(
            rule_id="comparable", kind="metric_threshold", metric_id=metric_id,
            operator=operator, threshold=threshold,
            required_comparability="comparable",
        ))
    return GatePolicyVersion(
        policy_id=str(policy.get("policy_id") or "legacy-lite"),
        version=str(policy.get("version") or "1"),
        rules=tuple(rules),
        created_by="legacy-projection",
        reason="gate-lite@2 dict policy projected to rule form",
    )
