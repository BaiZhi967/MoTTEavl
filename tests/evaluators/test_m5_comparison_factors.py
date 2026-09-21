"""M5-T08 比较因子反例：Skill/Workflow/Fixture/Judge/rubric/校准/预算政策。

证明的是"不该被归因成 Skill 增益"的条件真的阻断比较：预算政策不同、模型
不同、工具不同、scorer 版本不同、人工介入过、Workflow 或 Fixture 漂移，
都必须产生不可比原因，而不是静默比较。
"""
from __future__ import annotations

import pytest

from motte_contracts.comparison import ComparisonPolicy, RunReportRef
from motte_eval.comparison import compare_run_reports


def ref(run_id: str) -> RunReportRef:
    return RunReportRef(
        run_id=run_id, scoring_pass_id=f"pass-{run_id}", report_schema="run-report@2",
        evidence_hash="sha256:" + "a" * 64,
    )


def manifest(run_id: str, **overrides):
    payload = {
        "case_ids": ["case-1", "case-2"],
        "model": "gpt-x",
        "workflow": "order-cancel-confirmed@1",
        "workflow_snapshot": {
            "ref": "order-cancel-confirmed@1",
            "content_hash": "sha256:" + "1" * 64,
            "schema_version": 1,
            "step_ids": ["request", "confirm"],
            "limits": {"max_total_steps": 20, "max_turns": 4, "wall_time_sec": 30},
            "failure_policy": "stop_case",
        },
        "fixture_snapshot": {
            "order-state@1": {"fixture_id": "order-state", "content_hash": "sha256:" + "2" * 64},
        },
        "skill_snapshot": {"ref": "cancel-helper@1", "content_hash": "sha256:" + "3" * 64},
        "skills": ["cancel-helper@1"],
        "skill_arm": "skill-v1",
        "budget": {"policy": "same-total-budget"},
        "tools": ["orders.get"],
    }
    payload.update(overrides)
    return payload


def compare(base, cand, factors=("skill",)):
    return compare_run_reports(
        ref(base.get("_run", "base")), ref(cand.get("_run", "cand")),
        baseline_manifest=base, candidate_manifest=cand,
        policy=ComparisonPolicy(allowed_factors=factors),
    )


def test_skill_only_change_is_comparable_when_the_policy_allows_it():
    result = compare(manifest("a"), manifest("b", skill_arm="skill-v2", skills=["cancel-helper@2"],
                                            skill_snapshot={"ref": "cancel-helper@2",
                                                            "content_hash": "sha256:" + "4" * 64}))
    assert result.eligible is True
    assert any("ALLOWED_FACTOR:skill" in item for item in result.allowed_differences)


def test_skill_change_without_the_factor_is_blocked():
    result = compare(manifest("a"), manifest("b", skill_arm="skill-v2", skills=["cancel-helper@2"],
                                             skill_snapshot={"ref": "cancel-helper@2",
                                                             "content_hash": "sha256:" + "4" * 64}),
                     factors=())
    assert result.eligible is False
    assert any("SKILL_CHANGED" in reason for reason in result.reasons)


def test_no_skill_arm_versus_skill_arm_is_an_identity_change_not_a_missing_field():
    result = compare(
        manifest("a", skills=[], skill_arm="no-skill", skill_snapshot=None),
        manifest("b"),
    )
    assert result.eligible is True  # 政策允许 skill 因子
    one_sided = compare(manifest("a", skills=None, skill_arm=None, skill_snapshot=None), manifest("b"))
    assert one_sided.eligible is False
    assert any("IDENTITY_MISSING:skill" in reason for reason in one_sided.reasons)


@pytest.mark.parametrize("overrides, code", [
    ({"budget": {"policy": "same-execution-budget"}}, "BUDGET_POLICY_CHANGED"),
    ({"model": "gpt-y"}, "FACTOR_NOT_ALLOWED:model"),
    ({"tools": ["orders.cancel"]}, "TOOLS_CHANGED"),
    ({"workflow": "other@1", "workflow_snapshot": {
        "ref": "other@1", "content_hash": "sha256:" + "9" * 64, "schema_version": 1,
        "step_ids": ["x"], "limits": {}, "failure_policy": "stop_case",
    }}, "WORKFLOW_CHANGED"),
    ({"fixture_snapshot": {"order-state@1": {
        "fixture_id": "order-state", "content_hash": "sha256:" + "8" * 64,
    }}}, "FIXTURE_CHANGED"),
    ({"interventions": {"possible": True, "condition_hash": "sha256:abc"}},
     "INTERVENTION_UNKNOWN"),
])
def test_non_skill_condition_changes_block_pure_skill_attribution(overrides, code):
    result = compare(manifest("a"), manifest("b", skill_arm="skill-v2", skills=["cancel-helper@2"],
                                             skill_snapshot={"ref": "cancel-helper@2",
                                                             "content_hash": "sha256:" + "4" * 64},
                                             **overrides))
    assert result.eligible is False
    assert any(code in reason for reason in result.reasons), result.reasons


def test_skill_attribution_requires_a_comparable_budget_policy():
    body = {"skill_arm": "skill-v2", "skills": ["cancel-helper@2"],
            "skill_snapshot": {"ref": "cancel-helper@2", "content_hash": "sha256:" + "4" * 64}}
    unsafe = compare(manifest("a", budget={"policy": "unbounded"}),
                     manifest("b", budget={"policy": "unbounded"}, **body))
    assert unsafe.eligible is False
    assert any("SKILL_ATTRIBUTION_UNSAFE" in reason for reason in unsafe.reasons)

    missing = compare(manifest("a", budget={}), manifest("b", budget={}, **body))
    assert missing.eligible is False
    assert any("SKILL_ATTRIBUTION_UNSAFE" in reason for reason in missing.reasons)


def test_manual_intervention_blocks_pure_skill_attribution():
    outcome = compare(
        manifest("a"),
        manifest("b", skill_arm="skill-v2", skills=["cancel-helper@2"],
                 skill_snapshot={"ref": "cancel-helper@2", "content_hash": "sha256:" + "4" * 64},
                 interventions={"possible": True, "condition_hash": "sha256:abc"}),
        factors=("skill", "intervention"),
    )
    assert outcome.eligible is False
    assert any("SKILL_ATTRIBUTION_UNSAFE" in reason for reason in outcome.reasons)


def test_selected_pass_provenance_is_compared_not_the_current_judge_settings():
    base = manifest("a", scoring_provenance={
        "judge_profile_id": "judge@1", "rubric_version": "rubric@1",
        "calibration_version": "cal@1", "input_selector": "observation.whitelist@1",
    })
    same = manifest("b", scoring_provenance=dict(base["scoring_provenance"]))
    assert compare(base, same, factors=("skill",)).eligible is True

    changed = manifest("b", scoring_provenance={
        "judge_profile_id": "judge@2", "rubric_version": "rubric@2",
        "calibration_version": "cal@2", "input_selector": "observation.whitelist@2",
    })
    result = compare(base, changed, factors=("skill",))
    assert result.eligible is False
    reason_codes = " ".join(result.reasons)
    for code in ("JUDGE_CHANGED", "RUBRIC_CHANGED", "CALIBRATION_CHANGED", "JUDGE_INPUT_CHANGED"):
        assert code in reason_codes

    allowed = compare(base, changed, factors=("skill", "judge", "rubric", "calibration"))
    assert allowed.eligible is True


def test_history_without_provenance_stays_unknown_and_blocks():
    base = manifest("a", scoring_provenance={
        "judge_profile_id": "judge@1", "rubric_version": "rubric@1",
        "calibration_version": "cal@1", "input_selector": "observation.whitelist@1",
    })
    historical = manifest("b", scoring_provenance=None)
    result = compare(base, historical, factors=("skill",))
    assert result.eligible is False
    assert any("IDENTITY_MISSING:scoring_provenance" in reason for reason in result.reasons)


def test_policy_rejects_unknown_factors():
    with pytest.raises(ValueError, match="unknown comparison factors"):
        ComparisonPolicy(allowed_factors=("skill", "nonsense"))
