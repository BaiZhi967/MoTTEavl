"""M6 三级比较结论行为测试（T01：comparable/partially_comparable/not_comparable）。

覆盖 A01（仅模型因素可比）、A02（同名内容改变不合并）、A06（scorer/Judge
变化不可比）、A10（added/removed 独立）、缺费用只降级 partially。
"""
from __future__ import annotations

from motte_contracts.comparison import ComparabilityLevel, ComparisonPolicy
from motte_eval.comparison import compare_run_reports


def _ref(run_id: str):
    from motte_contracts.comparison import RunReportRef

    return RunReportRef(
        run_id=run_id, scoring_pass_id="pass-" + run_id,
        report_schema="report-v1", evidence_hash="sha256:" + run_id,
    )


def _manifest(**overrides) -> dict:
    base = {
        "model": "deepseek-v4.1-flash",
        "case_ids": ["c1", "c2"],
        "case_expectations": {"c1": "A", "c2": "B"},
        "case_content_hashes": {"c1": "h1", "c2": "h2"},
        "evaluation": {"scorer_id": "exact", "scorer_version": "1"},
    }
    base.update(overrides)
    return base


def _compare(base, cand, policy=None, base_cost=None, cand_cost=None):
    return compare_run_reports(
        _ref("run-base"), _ref("run-cand"),
        baseline_manifest=_manifest(**(base or {})),
        candidate_manifest=_manifest(**(cand or {})),
        policy=policy or ComparisonPolicy(allowed_factors=("model",)),
        baseline_cost=base_cost,
        candidate_cost=cand_cost,
    )


def test_model_only_change_is_comparable():
    """A01：仅声明模型因素不同 → comparable，固定条件完整列出。"""
    result = _compare(
        {}, {"model": "gpt-other"},
        base_cost={"known": True}, cand_cost={"known": True},
    )
    assert result.level is ComparabilityLevel.COMPARABLE
    assert result.eligible is True
    assert any("model" in item for item in result.allowed_differences)
    assert not result.structural_reasons


def test_unknown_cost_downgrades_to_partially_comparable():
    """缺费用：质量可比、cost 指标不可比（G12/协议 §3）。"""
    result = _compare(
        {}, {},
        base_cost={"known": False}, cand_cost={"known": False},
    )
    assert result.level is ComparabilityLevel.PARTIALLY_COMPARABLE
    assert result.eligible is True
    assert result.metric_eligibility["quality"] is True
    assert result.metric_eligibility["cost"] is False
    assert result.metric_reasons
    assert not result.structural_reasons


def test_same_name_changed_gold_is_not_comparable():
    """A02：同 case 名 gold/内容改变 → not_comparable，不按名字合并。"""
    result = _compare(
        {}, {"case_expectations": {"c1": "CHANGED", "c2": "B"}},
    )
    assert result.level is ComparabilityLevel.NOT_COMPARABLE
    assert result.eligible is False
    assert "c1" in result.case_diff["changed"]


def test_case_content_hash_change_is_not_comparable():
    result = _compare(
        {}, {"case_content_hashes": {"c1": "h1", "c2": "h2-changed"}},
    )
    assert result.level is ComparabilityLevel.NOT_COMPARABLE
    assert "c2" in result.case_diff["changed"]


def test_scorer_change_is_not_comparable():
    """A06：scorer 版本变化不直接比较质量分数。"""
    result = _compare(
        {},
        {"evaluation": {"scorer_id": "exact", "scorer_version": "2"}},
    )
    assert result.level is ComparabilityLevel.NOT_COMPARABLE
    assert any("SCORER_CHANGED" in reason for reason in result.reasons)


def test_added_removed_cases_are_not_regressions():
    """A10：added/removed 独立列出，不混进 changed。"""
    result = _compare(
        {"case_ids": ["c1", "c2", "c3"]},
        {"case_ids": ["c1", "c2", "c4"],
         "case_expectations": {"c1": "A", "c2": "B", "c4": "D"},
         "case_content_hashes": {"c1": "h1", "c2": "h2", "c4": "h4"}},
    )
    assert result.level is ComparabilityLevel.NOT_COMPARABLE
    assert result.case_diff["added"] == ["c4"]
    assert result.case_diff["removed"] == ["c3"]
    assert result.case_diff["changed"] == []


def test_intervention_blocks_comparability():
    """A15：人工干预未知 → not_comparable。"""
    result = _compare(
        {}, {"interventions": {"possible": True}},
    )
    assert result.level is ComparabilityLevel.NOT_COMPARABLE
    assert any("INTERVENTION" in reason for reason in result.structural_reasons)


def test_model_change_without_allowance_blocks():
    strict = ComparisonPolicy(allowed_factors=())
    result = _compare({"model": "a"}, {"model": "b"}, strict)
    assert result.level is ComparabilityLevel.NOT_COMPARABLE
    assert any("FACTOR_NOT_ALLOWED" in reason for reason in result.structural_reasons)


def test_structural_and_metric_reasons_are_separated():
    """结构性原因与指标级原因分开（协议 §3）。"""
    both = _compare(
        {"case_expectations": {"c1": "X", "c2": "B"}},
        {},
        base_cost={"known": False}, cand_cost={"known": False},
    )
    # 结构性阻断存在时，指标级原因不重复报告（整体已不可比）。
    assert both.level is ComparabilityLevel.NOT_COMPARABLE
    assert both.structural_reasons
    assert both.metric_eligibility["quality"] is False
