"""M5-T08：no-skill / skill-v1 / skill-v2 三臂受控对照的创建服务。

一次冻结请求生成三条**普通 Run**：除 Skill 之外的一切条件必须逐字段相同
（Dataset/Workflow/样本、Target/runtime/model、fixture 初态、工具政策、scorer）。
预算政策必须显式选择 same-total-budget 或 same-execution-budget，并写进比较
条件，否则 Skill 开销与预算变化会混在一起，差异不能归因于 Skill。

本模块只做**计划**（固定输入与臂身份），不新建调度器、不直接建 Run、也不
重新实现比较与 Gate：执行与比较仍走既有 RunDispatcher 与 M5 比较因子。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from motte_contracts.identity import canonical_sha256

#: 允许的对照臂名字；顺序即报告顺序。
ARM_NO_SKILL = "no-skill"
ARM_SKILL_V1 = "skill-v1"
ARM_SKILL_V2 = "skill-v2"
ARM_IDS: tuple[str, ...] = (ARM_NO_SKILL, ARM_SKILL_V1, ARM_SKILL_V2)

#: 只有这两种预算政策才允许把差异归因给 Skill。
COMPARABLE_BUDGET_POLICIES: tuple[str, ...] = ("same-total-budget", "same-execution-budget")

#: 除 Skill 之外必须逐字段一致的条件；不一致即拒绝生成计划。
FROZEN_FIELDS: tuple[str, ...] = (
    "workflow", "workflow_snapshot", "fixture_snapshot", "target_snapshot",
    "model", "provider", "runtime", "runtime_profile", "agent", "agent_config",
    "tools", "tool_policy", "environment", "retries", "timeouts", "limits",
    "evaluation", "case_expectations", "case_content_hashes",
)


class AblationPlanError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ArmPlan:
    arm_id: str
    run_ref: str
    skills: tuple[str, ...]
    skill_snapshot: dict[str, Any] | None
    scoring_pass_hint: str
    differences: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "arm_id": self.arm_id,
            "run_ref": self.run_ref,
            "skills": list(self.skills),
            "skill_snapshot": self.skill_snapshot,
            "scoring_pass_hint": self.scoring_pass_hint,
            "differences": list(self.differences),
        }


@dataclass(frozen=True)
class AblationPlan:
    plan_hash: str
    experiment_ref: str
    budget_policy: str
    paired_case_keys: tuple[str, ...]
    arms: tuple[ArmPlan, ...]
    frozen_conditions: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_hash": self.plan_hash,
            "experiment_ref": self.experiment_ref,
            "budget_policy": self.budget_policy,
            "paired_case_keys": list(self.paired_case_keys),
            "arms": [arm.as_dict() for arm in self.arms],
            "frozen_conditions": deepcopy(self.frozen_conditions),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ArmSpec:
    """一个臂的显式已发布 Skill 引用。"""

    arm_id: str
    skills: tuple[str, ...] = ()
    skill_snapshot: Mapping[str, Any] | None = None


def plan_skill_ablation(
    *,
    base_manifest: Mapping[str, Any],
    experiment_ref: str,
    arms: Sequence[ArmSpec],
    budget_policy: str,
    case_keys: Sequence[str],
) -> AblationPlan:
    """生成三臂计划；条件不一致或引用不完整时具名拒绝。"""
    if budget_policy not in COMPARABLE_BUDGET_POLICIES:
        raise AblationPlanError(
            "ABLATION_BUDGET_POLICY_INVALID",
            "budget_policy must be one of " + ", ".join(COMPARABLE_BUDGET_POLICIES),
        )
    if not experiment_ref:
        raise AblationPlanError("ABLATION_EXPERIMENT_REQUIRED", "experiment_ref is required")
    keys = [str(key) for key in case_keys]
    if not keys:
        raise AblationPlanError("ABLATION_CASES_REQUIRED", "paired case keys are required")
    seen = [arm.arm_id for arm in arms]
    if len(set(seen)) != len(seen):
        raise AblationPlanError("ABLATION_ARM_DUPLICATE", "arm ids must be distinct")
    missing = [arm for arm in (ARM_NO_SKILL, ARM_SKILL_V1, ARM_SKILL_V2) if arm not in seen]
    if missing:
        raise AblationPlanError(
            "ABLATION_ARMS_INCOMPLETE", "missing arms: " + ", ".join(missing)
        )
    # 每条臂必须显式引用**已发布**版本，不能靠名字或 draft 继承。
    for arm in arms:
        if arm.arm_id != ARM_NO_SKILL and not arm.skills:
            raise AblationPlanError(
                "ABLATION_SKILL_REF_REQUIRED",
                f"arm {arm.arm_id} must reference explicit published skill versions",
            )
        for ref in arm.skills:
            if "@" not in str(ref):
                raise AblationPlanError(
                    "ABLATION_SKILL_REF_REQUIRED",
                    f"skill reference must be name@version: {ref!r}",
                )
    frozen = {
        field_name: deepcopy(base_manifest.get(field_name))
        for field_name in FROZEN_FIELDS
        if field_name in base_manifest
    }
    budget = deepcopy(base_manifest.get("budget") or {})
    budget["policy"] = budget_policy
    plans: list[ArmPlan] = []
    warnings: list[str] = []
    for arm in arms:
        manifest = deepcopy(dict(base_manifest))
        manifest["skills"] = list(arm.skills)
        manifest["skill_arm"] = arm.arm_id
        manifest["skill_snapshot"] = deepcopy(dict(arm.skill_snapshot)) if arm.skill_snapshot else None
        manifest["budget"] = deepcopy(budget)
        # 冻结条件必须逐字段一致：这里逐一核对，发现漂移立刻拒绝。
        drifted = [
            field_name for field_name, value in frozen.items()
            if manifest.get(field_name) != value
        ]
        if drifted:
            raise AblationPlanError(
                "ABLATION_CONDITION_DRIFT",
                f"arm {arm.arm_id} changes non-skill conditions: " + ", ".join(sorted(drifted)),
            )
        if not manifest["skill_snapshot"] and arm.arm_id != ARM_NO_SKILL:
            warnings.append(f"ARM_WITHOUT_SKILL_SNAPSHOT:{arm.arm_id}")
        plans.append(ArmPlan(
            arm_id=arm.arm_id,
            run_ref=f"{experiment_ref}/{arm.arm_id}",
            skills=tuple(str(ref) for ref in arm.skills),
            skill_snapshot=deepcopy(dict(arm.skill_snapshot)) if arm.skill_snapshot else None,
            scoring_pass_hint=f"{experiment_ref}/{arm.arm_id}/scoring-pass",
            differences=tuple(
                sorted({"skill_arm", "skills", "skill_snapshot"}),
            ),
        ))
    plan_hash = canonical_sha256({
        "experiment_ref": experiment_ref,
        "budget_policy": budget_policy,
        "case_keys": keys,
        "arms": [plan.as_dict() for plan in plans],
        "frozen": frozen,
    })
    return AblationPlan(
        plan_hash=plan_hash,
        experiment_ref=experiment_ref,
        budget_policy=budget_policy,
        paired_case_keys=tuple(keys),
        arms=tuple(plans),
        frozen_conditions=frozen,
        warnings=tuple(warnings),
    )


def arm_manifests(
    base_manifest: Mapping[str, Any], plan: AblationPlan,
) -> dict[str, dict[str, Any]]:
    """把计划展开成三条待创建 Run 的 manifest（除 Skill 外逐字段相同）。"""
    manifests: dict[str, dict[str, Any]] = {}
    for arm in plan.arms:
        manifest = deepcopy(dict(base_manifest))
        manifest["skills"] = list(arm.skills)
        manifest["skill_arm"] = arm.arm_id
        manifest["skill_snapshot"] = deepcopy(arm.skill_snapshot)
        budget = deepcopy(manifest.get("budget") or {})
        budget["policy"] = plan.budget_policy
        manifest["budget"] = budget
        manifests[arm.arm_id] = manifest
    return manifests
