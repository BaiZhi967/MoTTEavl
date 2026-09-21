"""M5-T08 三臂计划反例：除 Skill 之外的条件必须逐字段一致。

证明的是"偷偷改预算/模型/工具/scorer 会被看见"，而不是字段存在。
"""
from __future__ import annotations

import pytest

from motte_sdk.skill_ablation import (
    ARM_IDS,
    AblationPlanError,
    ArmSpec,
    arm_manifests,
    plan_skill_ablation,
)


def base_manifest(**overrides):
    payload = {
        "workflow": "order-cancel-confirmed@1",
        "workflow_snapshot": {"ref": "order-cancel-confirmed@1",
                              "content_hash": "sha256:" + "1" * 64},
        "fixture_snapshot": {"order-state@1": {"content_hash": "sha256:" + "2" * 64}},
        "target_snapshot": {"multi_turn": True},
        "agent": "builtin-agent@1",
        "agent_config": {"mode": "legacy-json"},
        "tools": ["orders.get", "orders.cancel"],
        "evaluation": {"scorer_id": "workflow", "scorer_version": "1"},
        "budget": {"max_total_steps": 20, "max_turns": 4, "wall_time_sec": 60},
        "case_ids": ["case-1", "case-2"],
    }
    payload.update(overrides)
    return payload


def arms(skills_v1=("cancel-helper@1",), skills_v2=("cancel-helper@2",)):
    return [
        ArmSpec(arm_id="no-skill"),
        ArmSpec(arm_id="skill-v1", skills=skills_v1,
                skill_snapshot={"ref": skills_v1[0], "content_hash": "sha256:" + "3" * 64}),
        ArmSpec(arm_id="skill-v2", skills=skills_v2,
                skill_snapshot={"ref": skills_v2[0], "content_hash": "sha256:" + "4" * 64}),
    ]


def plan(**overrides):
    kwargs = {
        "base_manifest": base_manifest(**overrides.pop("base", {})),
        "experiment_ref": "exp-order-cancel",
        "arms": overrides.pop("arms", arms()),
        "budget_policy": overrides.pop("budget_policy", "same-total-budget"),
        "case_keys": overrides.pop("case_keys", ["case-1", "case-2"]),
    }
    kwargs.update(overrides)
    return plan_skill_ablation(**kwargs)


def test_plan_covers_three_arms_with_paired_cases_and_explicit_pass_hints():
    outcome = plan()
    assert [arm.arm_id for arm in outcome.arms] == list(ARM_IDS)
    assert outcome.paired_case_keys == ("case-1", "case-2")
    assert outcome.budget_policy == "same-total-budget"
    for arm in outcome.arms:
        assert arm.scoring_pass_hint.endswith("scoring-pass")
        assert outcome.plan_hash
    assert outcome.arms[0].skills == ()
    assert outcome.arms[1].skills == ("cancel-helper@1",)


def test_only_skill_fields_may_differ_across_arms():
    manifests = arm_manifests(base_manifest(), plan())
    reference = manifests["no-skill"]
    for arm_id, manifest in manifests.items():
        for key, value in reference.items():
            if key in {"skills", "skill_arm", "skill_snapshot"}:
                continue
            assert manifest[key] == value, f"{arm_id} changed {key}"


def test_budget_policy_is_written_into_every_arm():
    manifests = arm_manifests(base_manifest(), plan(budget_policy="same-execution-budget"))
    for manifest in manifests.values():
        assert manifest["budget"]["policy"] == "same-execution-budget"


@pytest.mark.parametrize("policy", ["unbounded", "", "same-total-cost"])
def test_unknown_budget_policies_are_refused(policy):
    with pytest.raises(AblationPlanError, match="budget_policy"):
        plan(budget_policy=policy)


def test_missing_arm_is_refused_instead_of_silently_compared():
    with pytest.raises(AblationPlanError, match="missing arms"):
        plan(arms=[ArmSpec(arm_id="no-skill"), ArmSpec(arm_id="skill-v1",
                                                      skills=("cancel-helper@1",))])


def test_skill_arms_require_explicit_published_version_references():
    with pytest.raises(AblationPlanError, match="name@version"):
        plan(arms=[
            ArmSpec(arm_id="no-skill"),
            ArmSpec(arm_id="skill-v1", skills=("cancel-helper",)),
            ArmSpec(arm_id="skill-v2", skills=("cancel-helper@2",)),
        ])
    with pytest.raises(AblationPlanError, match="explicit published skill versions"):
        plan(arms=[
            ArmSpec(arm_id="no-skill"),
            ArmSpec(arm_id="skill-v1"),
            ArmSpec(arm_id="skill-v2", skills=("cancel-helper@2",)),
        ])


def test_duplicate_arm_ids_are_refused():
    with pytest.raises(AblationPlanError, match="distinct"):
        plan(arms=[ArmSpec(arm_id="no-skill"), ArmSpec(arm_id="no-skill"),
                   ArmSpec(arm_id="skill-v2", skills=("cancel-helper@2",))])


def test_case_keys_are_required_for_pairing():
    with pytest.raises(AblationPlanError, match="paired case keys"):
        plan(case_keys=[])


def test_plan_hash_tracks_the_budget_policy_and_arm_identity():
    first = plan(budget_policy="same-total-budget")
    second = plan(budget_policy="same-execution-budget")
    assert first.plan_hash != second.plan_hash
    third = plan(arms=arms(skills_v1=("cancel-helper@9",)))
    assert third.plan_hash != first.plan_hash
