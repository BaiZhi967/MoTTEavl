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


def test_plan_hash_includes_the_actual_budget_values():
    """只改实际额度（政策名不变）也必须是另一份计划（F11）。"""
    small = {"policy": "same-total-budget", "max_total_tokens": 100}
    large = {"policy": "same-total-budget", "max_total_tokens": 100000}
    first = plan(base={"budget": small})
    second = plan(base={"budget": large})
    assert first.plan_hash != second.plan_hash
    # 冻结条件里保存的是政策名之外的实际额度值。
    assert first.frozen_conditions["budget"]["max_total_tokens"] == 100


def test_expanding_a_stale_plan_with_drifted_conditions_is_refused():
    budget = {"policy": "same-total-budget", "max_total_tokens": 100}
    planned = plan(base={"budget": budget})
    # 旧计划换 model：不能静默展开。
    with pytest.raises(AblationPlanError, match="non-skill conditions"):
        arm_manifests(base_manifest(model="different-model", budget=budget), planned)
    # 只改实际预算额度：同样拒绝。
    with pytest.raises(AblationPlanError, match="non-skill conditions"):
        arm_manifests(base_manifest(
            budget={"policy": "same-total-budget", "max_total_tokens": 100000},
        ), planned)
    # 只改 agent_config 的执行配置：同样拒绝。
    with pytest.raises(AblationPlanError, match="non-skill conditions"):
        arm_manifests(base_manifest(
            budget=budget, agent_config={"mode": "legacy-json", "max_steps": 200},
        ), planned)
    # 原基底仍然可以展开。
    assert set(arm_manifests(base_manifest(budget=budget), planned)) == set(ARM_IDS)


def test_arm_allocation_must_stay_inside_the_policy_permitted_dimension():
    """same-total-budget 固定总额度：臂分配不得改它（F11）。"""
    budget = {"policy": "same-total-budget", "total_allowance": 1000}
    with pytest.raises(AblationPlanError, match="cannot allocate") as raised:
        plan(base={"budget": budget}, arms=[
            ArmSpec("no-skill"),
            ArmSpec("skill-v1", ("cancel-helper@1",),
                    skill_snapshot={"ref": "cancel-helper@1", "content_hash": "sha256:" + "3" * 64},
                    budget_allocation={"total_allowance": 5000}),
            ArmSpec("skill-v2", ("cancel-helper@2",),
                    skill_snapshot={"ref": "cancel-helper@2", "content_hash": "sha256:" + "4" * 64}),
        ])
    assert raised.value.code == "ABLATION_BUDGET_ALLOCATION_INVALID"
    # same-execution-budget 固定执行额度：改它同样拒绝。
    execution = {"policy": "same-execution-budget", "total_allowance": 1000,
                 "execution_allowance": 900}
    with pytest.raises(AblationPlanError, match="cannot allocate") as raised:
        plan(base={"budget": execution}, budget_policy="same-execution-budget", arms=[
            ArmSpec("no-skill"),
            ArmSpec("skill-v1", ("cancel-helper@1",),
                    skill_snapshot={"ref": "cancel-helper@1", "content_hash": "sha256:" + "3" * 64},
                    budget_allocation={"execution_allowance": 1200}),
            ArmSpec("skill-v2", ("cancel-helper@2",),
                    skill_snapshot={"ref": "cancel-helper@2", "content_hash": "sha256:" + "4" * 64}),
        ])
    assert raised.value.code == "ABLATION_BUDGET_ALLOCATION_INVALID"
    # 允许的分配也不能超过政策固定的额度。
    with pytest.raises(AblationPlanError, match="exceeds the fixed") as raised:
        plan(base={"budget": budget}, arms=[
            ArmSpec("no-skill"),
            ArmSpec("skill-v1", ("cancel-helper@1",),
                    skill_snapshot={"ref": "cancel-helper@1", "content_hash": "sha256:" + "3" * 64},
                    budget_allocation={"execution_allowance": 2000}),
            ArmSpec("skill-v2", ("cancel-helper@2",),
                    skill_snapshot={"ref": "cancel-helper@2", "content_hash": "sha256:" + "4" * 64}),
        ])
    assert raised.value.code == "ABLATION_BUDGET_ALLOCATION_INVALID"


def test_allocation_is_refused_when_the_policy_fixed_dimension_is_unknown():
    """固定维度未知时不能补 0 也不能默认一致：臂分配直接拒绝（F11）。"""
    with pytest.raises(AblationPlanError, match="unknown") as raised:
        plan(base={"budget": {"policy": "same-total-budget"}}, arms=[
            ArmSpec("no-skill"),
            ArmSpec("skill-v1", ("cancel-helper@1",),
                    skill_snapshot={"ref": "cancel-helper@1", "content_hash": "sha256:" + "3" * 64},
                    budget_allocation={"execution_allowance": 800}),
            ArmSpec("skill-v2", ("cancel-helper@2",),
                    skill_snapshot={"ref": "cancel-helper@2", "content_hash": "sha256:" + "4" * 64}),
        ])
    assert raised.value.code == "ABLATION_BUDGET_ALLOCATION_UNVERIFIABLE"


def test_same_execution_budget_records_the_per_arm_allocation():
    """same-execution-budget 允许按臂分配总额度与指令开销，执行额度保持固定。"""
    budget = {"policy": "same-execution-budget", "total_allowance": 1000,
              "execution_allowance": 900}
    planned = plan(
        base={"budget": budget}, budget_policy="same-execution-budget",
        arms=[
            ArmSpec("no-skill", budget_allocation={"instruction_overhead": 0}),
            ArmSpec("skill-v1", ("cancel-helper@1",),
                    skill_snapshot={"ref": "cancel-helper@1", "content_hash": "sha256:" + "3" * 64},
                    budget_allocation={"instruction_overhead": 50, "total_allowance": 950}),
            ArmSpec("skill-v2", ("cancel-helper@2",),
                    skill_snapshot={"ref": "cancel-helper@2", "content_hash": "sha256:" + "4" * 64},
                    budget_allocation={"instruction_overhead": 120, "total_allowance": 1020}),
        ],
    )
    manifests = arm_manifests(base_manifest(budget=budget), planned)
    assert manifests["skill-v2"]["budget"]["execution_allowance"] == 900
    assert manifests["skill-v2"]["budget"]["total_allowance"] == 1020
    assert manifests["skill-v2"]["budget"]["instruction_overhead"] == 120
    assert manifests["no-skill"]["budget"]["instruction_overhead"] == 0
    # 除 Skill 与政策允许的预算分配之外，逐字段一致。
    reference = manifests["no-skill"]
    for arm_id, manifest in manifests.items():
        for key, value in reference.items():
            if key in {"skills", "skill_arm", "skill_snapshot", "budget"}:
                continue
            assert manifest[key] == value, f"{arm_id} changed {key}"
