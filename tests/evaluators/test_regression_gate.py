"""M6 完整 Gate 引擎行为测试（T08：规则语义、优先级、决策映射）。

覆盖执行计划 T08 反例与验收 A05/A14-A17：质量失败+证据不足并存、
未校准 Judge、baseline=0、partial compare、安全标记、completed 但目标失败。
全部纯求值：零 Provider/Judge/Runner。
"""
from __future__ import annotations

import pytest

from motte_contracts.comparison import ComparabilityLevel, RunReportRef
from motte_contracts.gates import (
    GateDecision,
    GatePolicyVersion,
    GateRule,
    RuleResult,
)
from motte_eval.gates import (
    GateInputError,
    evaluate_gate_policy,
    gate_exit_code,
)


def _ref(run_id: str = "run-c1") -> RunReportRef:
    return RunReportRef(
        run_id=run_id, scoring_pass_id="pass-1",
        report_schema="report-v1", evidence_hash="sha256:c1",
    )


def _snapshot(**overrides) -> dict:
    base = {
        "metric_values": {"accuracy": 0.8, "cost.total_usd": 1.5},
        "coverage": 0.8,
        "counts": {
            "selected": 10, "attempted": 8, "judged": 8, "scored": 8,
            "call_failed": 0, "unknown": 2, "not_attempted": 2,
            "needs_review": 0, "no_expectation": 0,
        },
        "case_dispositions": [
            {"case_id": "hard-1", "disposition": "judged"},
            {"case_id": "hard-2", "disposition": "unknown"},
        ],
        "case_results": {"hard-1": True},
    }
    base.update(overrides)
    return base


def _policy(rules: tuple[GateRule, ...], **kwargs) -> GatePolicyVersion:
    return GatePolicyVersion(
        policy_id=kwargs.pop("policy_id", "pol"), version="1",
        rules=rules, created_by="test", reason="test", **kwargs,
    )


def _evaluate(policy, snapshot=None, **kwargs):
    return evaluate_gate_policy(
        policy,
        candidate_ref=_ref(),
        candidate_snapshot=snapshot if snapshot is not None else _snapshot(),
        candidate_run_status=kwargs.pop("candidate_run_status", "completed"),
        **kwargs,
    )


def test_completed_run_with_quality_failure_still_gates():
    """A17：Run completed 只代表执行终态；目标失败给 quality_fail（不放行）。"""
    policy = _policy((
        GateRule(
            rule_id="acc", kind="metric_threshold",
            metric_id="accuracy", operator="gte", threshold=0.9,
        ),
    ))
    result = _evaluate(policy)
    assert result.decision is GateDecision.QUALITY_FAIL
    assert gate_exit_code(result.decision) == 1
    assert all(rule.passed is not None for rule in result.rule_results)


def test_quality_fail_and_insufficient_evidence_coexist_priority():
    """A16：多失败并存 → JSON 全量规则、决策按冻结优先级（证据不足 > 质量失败）。"""
    policy = _policy((
        GateRule(
            rule_id="acc", kind="metric_threshold",
            metric_id="accuracy", operator="gte", threshold=0.9,
        ),
        GateRule(rule_id="cov", kind="coverage", min_coverage=1.0),
    ))
    result = _evaluate(policy)
    statuses = {rule.rule_id: rule.status for rule in result.rule_results}
    assert statuses["acc"] == "fail"
    assert statuses["cov"] == "insufficient"
    # insufficient_evidence 优先于 quality_fail（协议 §6）。
    assert result.decision is GateDecision.INSUFFICIENT_EVIDENCE
    assert gate_exit_code(result.decision) == 5


def test_safety_block_highest_priority():
    """安全阻断优先于一切（协议 §6）。"""
    policy = _policy((
        GateRule(
            rule_id="acc", kind="metric_threshold",
            metric_id="accuracy", operator="gte", threshold=0.9,
        ),
        GateRule(rule_id="safety", kind="safety_marker"),
        GateRule(rule_id="cov", kind="coverage", min_coverage=1.0),
    ))
    result = _evaluate(
        policy, candidate_evidence={"safety_markers": ["secret-leak"]},
    )
    assert result.decision is GateDecision.SAFETY_BLOCK
    assert gate_exit_code(result.decision) == 6
    # 全部规则结果都保留。
    assert len(result.rule_results) == 3


def test_execution_error_blocks_release():
    policy = _policy((
        GateRule(
            rule_id="acc", kind="metric_threshold",
            metric_id="accuracy", operator="gte", threshold=0.5,
            requires_successful_run=True,
        ),
    ))
    result = _evaluate(policy, candidate_run_status="needs_review")
    assert result.decision is GateDecision.EXECUTION_ERROR
    assert gate_exit_code(result.decision) == 3


def test_uncalibrated_judge_experimental_evidence_fail_closed():
    """T08：未校准 Judge 的规则在正式门禁 fail-closed（insufficient）。"""
    rule = GateRule(
        rule_id="judge-quality", kind="metric_threshold",
        metric_id="accuracy", operator="gte", threshold=0.8,
        experimental_evidence=True,
    )
    formal = _evaluate(_policy((rule,)))
    assert formal.decision is GateDecision.INSUFFICIENT_EVIDENCE
    diagnostic = _policy((rule,), diagnostic=True)
    relaxed = _evaluate(diagnostic)
    # 诊断政策下允许继续求值（本例指标足够，通过）。
    assert relaxed.decision is GateDecision.PASS


def test_model_identity_missing_is_insufficient_not_fail():
    """M4 identity unknown：请求模型不替代实际回报（A15 语义）。"""
    policy = _policy((
        GateRule(rule_id="identity", kind="model_identity", expected="deepseek-v4.1-flash"),
    ))
    missing = _evaluate(policy, candidate_evidence={})
    assert missing.decision is GateDecision.INSUFFICIENT_EVIDENCE
    mismatch = _evaluate(
        policy, candidate_evidence={"model_identity": "gpt-x"},
    )
    assert mismatch.decision is GateDecision.QUALITY_FAIL
    matched = _evaluate(
        policy, candidate_evidence={"model_identity": "deepseek-v4.1-flash"},
    )
    assert matched.decision is GateDecision.PASS


def test_baseline_delta_and_zero_baseline():
    """T08：baseline=0 按绝对差处理并显式记录；退化超限 quality_fail。"""
    rule = GateRule(
        rule_id="no-reg", kind="baseline_delta",
        metric_id="accuracy", max_regression=0.05,
    )
    policy = _policy((rule,))
    improved = _evaluate(
        policy,
        baseline_ref=_ref("run-base"),
        baseline_snapshot=_snapshot(metric_values={"accuracy": 0.7}),
    )
    assert improved.decision is GateDecision.PASS
    regressed = _evaluate(
        policy,
        baseline_ref=_ref("run-base"),
        baseline_snapshot=_snapshot(metric_values={"accuracy": 0.9}),
    )
    assert regressed.decision is GateDecision.QUALITY_FAIL
    zero_base = _evaluate(
        policy,
        baseline_ref=_ref("run-base"),
        baseline_snapshot=_snapshot(metric_values={"accuracy": 0.0}),
    )
    assert zero_base.decision is GateDecision.PASS
    zero_note = next(
        rule for rule in zero_base.rule_results if rule.rule_id == "no-reg"
    )
    assert "baseline=0" in zero_note.reason


def test_baseline_delta_without_baseline_is_insufficient():
    rule = GateRule(
        rule_id="no-reg", kind="baseline_delta",
        metric_id="accuracy", max_regression=0.05,
    )
    result = _evaluate(_policy((rule,)))
    assert result.decision is GateDecision.INSUFFICIENT_EVIDENCE


def test_partial_comparability_blocks_comparable_rules():
    """T08：required_comparability=comparable + partial → not_comparable。"""
    rule = GateRule(
        rule_id="acc", kind="metric_threshold",
        metric_id="accuracy", operator="gte", threshold=0.5,
        required_comparability="comparable",
    )
    result = _evaluate(
        _policy((rule,)),
        comparison_level=ComparabilityLevel.PARTIALLY_COMPARABLE,
        metric_eligibility={"quality": True, "cost": False},
    )
    assert result.decision is GateDecision.NOT_COMPARABLE
    assert gate_exit_code(result.decision) == 5


def test_partial_comparability_allows_partial_rules():
    rule = GateRule(
        rule_id="acc", kind="metric_threshold",
        metric_id="accuracy", operator="gte", threshold=0.5,
        required_comparability="partial",
    )
    result = _evaluate(
        _policy((rule,)),
        comparison_level=ComparabilityLevel.PARTIALLY_COMPARABLE,
        metric_eligibility={"quality": True, "cost": False},
    )
    assert result.decision is GateDecision.PASS


def test_cost_rule_unknown_cost_never_passes():
    """A14/协议 §6.2：unknown cost 的正式 cost gate 不能通过。"""
    policy = _policy((
        GateRule(
            rule_id="budget", kind="cost", metric_id="cost.total_usd",
            operator="lte", threshold=5.0,
        ),
    ))
    result = _evaluate(
        policy, _snapshot(metric_values={"accuracy": 0.8, "cost.total_usd": None}),
    )
    assert result.decision is GateDecision.INSUFFICIENT_EVIDENCE
    assert gate_exit_code(result.decision) == 5


def test_critical_case_missing_or_unknown():
    policy = _policy((
        GateRule(rule_id="key", kind="critical_case", critical_case_ids=("hard-2",)),
    ))
    unknown = _evaluate(policy)
    assert unknown.decision is GateDecision.INSUFFICIENT_EVIDENCE
    absent = _evaluate(
        policy, _snapshot(case_dispositions=[
            {"case_id": "hard-1", "disposition": "judged"},
        ], case_results={"hard-1": True}),
    )
    assert absent.decision is GateDecision.INSUFFICIENT_EVIDENCE
    failed = _evaluate(
        policy, _snapshot(case_dispositions=[
            {"case_id": "hard-2", "disposition": "judged"},
        ], case_results={"hard-2": False}),
    )
    assert failed.decision is GateDecision.QUALITY_FAIL
    passed = _evaluate(
        policy, _snapshot(case_dispositions=[
            {"case_id": "hard-2", "disposition": "judged"},
        ], case_results={"hard-2": True}),
    )
    assert passed.decision is GateDecision.PASS


def test_nan_and_empty_metric_values_rejected():
    policy = _policy((
        GateRule(
            rule_id="acc", kind="metric_threshold",
            metric_id="accuracy", operator="gte", threshold=0.5,
        ),
    ))
    nan_result = _evaluate(
        policy, _snapshot(metric_values={"accuracy": float("nan")}),
    )
    assert nan_result.decision is GateDecision.INSUFFICIENT_EVIDENCE
    missing = _evaluate(policy, _snapshot(metric_values={}))
    assert missing.decision is GateDecision.INSUFFICIENT_EVIDENCE


def test_unknown_metric_insufficient_and_direction_guard():
    bad_metric = _policy((
        GateRule(
            rule_id="x", kind="metric_threshold",
            metric_id="no.such.metric", operator="gte", threshold=0.5,
        ),
    ))
    result = _evaluate(bad_metric)
    assert result.decision is GateDecision.INSUFFICIENT_EVIDENCE
    wrong_direction = GateRule(
        rule_id="y", kind="metric_threshold",
        metric_id="accuracy", operator="lte", threshold=0.5,
    )
    with pytest.raises(GateInputError) as excinfo:
        _evaluate(_policy((wrong_direction,)))
    assert excinfo.value.code == "RULE_DIRECTION_INVALID"


def test_warn_severity_does_not_change_decision():
    policy = _policy((
        GateRule(
            rule_id="soft", kind="metric_threshold",
            metric_id="accuracy", operator="gte", threshold=0.99,
            severity="warn",
        ),
    ))
    result = _evaluate(policy)
    assert result.decision is GateDecision.PASS
    assert result.rule_results[0].status == "fail"


def test_repeat_evaluation_is_equivalent():
    """G17：同输入同政策 → 同 gate_result_id/conclusion_hash（evaluated_at 无关）。"""
    policy = _policy((
        GateRule(
            rule_id="acc", kind="metric_threshold",
            metric_id="accuracy", operator="gte", threshold=0.7,
        ),
    ))
    first = _evaluate(policy, evaluated_at="2026-09-21T00:00:00")
    second = _evaluate(policy, evaluated_at="2099-01-01T00:00:00")
    assert first.gate_result_id == second.gate_result_id
    assert first.conclusion_hash == second.conclusion_hash
    assert first.decision == second.decision == GateDecision.PASS


def test_diagnostic_skip_never_yields_pass():
    """协议 §6：diagnostic_skip 参与过求值 → 整体不得显示为通过。"""
    policy = GatePolicyVersion(
        policy_id="diag", version="1", diagnostic=True,
        rules=(
            GateRule(
                rule_id="acc", kind="metric_threshold",
                metric_id="accuracy", operator="gte", threshold=0.5,
            ),
            GateRule(
                rule_id="cov", kind="coverage", min_coverage=1.0,
                missing_policy="diagnostic_skip",
            ),
        ),
        created_by="test", reason="test",
    )
    result = _evaluate(policy)
    skipped = next(
        rule for rule in result.rule_results if rule.rule_id == "cov"
    )
    assert skipped.diagnostic_skipped is True
    assert result.decision is GateDecision.INSUFFICIENT_EVIDENCE


def test_formal_policy_rejects_diagnostic_skip_at_validation():
    with pytest.raises(ValueError):
        GatePolicyVersion(
            policy_id="formal", version="1",
            rules=(
                GateRule(
                    rule_id="cov", kind="coverage", min_coverage=1.0,
                    missing_policy="diagnostic_skip",
                ),
            ),
            created_by="test", reason="test",
        )


def test_side_effect_violation_blocks():
    policy = _policy((
        GateRule(rule_id="side", kind="no_side_effect"),
    ))
    unobservable = _evaluate(policy, candidate_evidence={})
    assert unobservable.decision is GateDecision.INSUFFICIENT_EVIDENCE
    violated = _evaluate(
        policy, candidate_evidence={"side_effect_violations": 2},
    )
    assert violated.decision is GateDecision.SAFETY_BLOCK
    clean = _evaluate(policy, candidate_evidence={"side_effect_violations": 0})
    assert clean.decision is GateDecision.PASS


def test_min_samples_coverage():
    policy = _policy((
        GateRule(rule_id="cov", kind="coverage", min_coverage=0.5, min_samples=20),
    ))
    result = _evaluate(policy)
    assert result.decision is GateDecision.INSUFFICIENT_EVIDENCE
    assert "samples" in result.rule_results[0].reason


def test_rule_result_contract_status_domain():
    ok = RuleResult(
        rule_id="r", kind="coverage", status="pass", reason="ok",
    )
    assert ok.passed is True
    with pytest.raises(ValueError):
        RuleResult(rule_id="r", kind="coverage", status="bogus", reason="x")
