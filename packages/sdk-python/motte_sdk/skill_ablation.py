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
from motte_eval.comparison import BUDGET_DIMENSIONS, budget_dimensions

#: 允许的对照臂名字；顺序即报告顺序。
ARM_NO_SKILL = "no-skill"
ARM_SKILL_V1 = "skill-v1"
ARM_SKILL_V2 = "skill-v2"
ARM_IDS: tuple[str, ...] = (ARM_NO_SKILL, ARM_SKILL_V1, ARM_SKILL_V2)

#: 只有这两种预算政策才允许把差异归因给 Skill。
COMPARABLE_BUDGET_POLICIES: tuple[str, ...] = ("same-total-budget", "same-execution-budget")

#: 除 Skill 之外必须逐字段一致的条件；不一致即拒绝生成计划。实际预算额度单独
#: 冻结在 frozen_conditions["budget"] 里（政策名由计划决定，F11）。
FROZEN_FIELDS: tuple[str, ...] = (
    "workflow", "workflow_snapshot", "fixture_snapshot", "target_snapshot",
    "model", "provider", "runtime", "runtime_profile", "agent", "agent_config",
    "tools", "tool_policy", "environment", "retries", "timeouts", "limits",
    "evaluation", "case_expectations", "case_content_hashes",
)

#: 预算里的政策名键：政策名由计划统一写入，实际额度必须逐字段固定。
_BUDGET_POLICY_KEYS = ("policy", "comparison_policy")

#: 政策固定哪一维：另一维是政策显式允许按臂分配的变化（F11）。
_FIXED_BUDGET_DIMENSION: dict[str, str] = {
    "same-total-budget": "total_allowance",
    "same-execution-budget": "execution_allowance",
}

#: 按臂分配只允许这三个维度，名字与 motte_eval.comparison 的预算维度一致。
BUDGET_ALLOCATION_KEYS: tuple[str, ...] = BUDGET_DIMENSIONS


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
    #: 政策显式允许的按臂预算分配；进入 plan_hash，展开时逐臂应用（F11）。
    budget_allocation: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "arm_id": self.arm_id,
            "run_ref": self.run_ref,
            "skills": list(self.skills),
            "skill_snapshot": self.skill_snapshot,
            "scoring_pass_hint": self.scoring_pass_hint,
            "differences": list(self.differences),
            "budget_allocation": deepcopy(self.budget_allocation),
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
    """一个臂的显式已发布 Skill 引用，以及政策允许的按臂预算分配。"""

    arm_id: str
    skills: tuple[str, ...] = ()
    skill_snapshot: Mapping[str, Any] | None = None
    #: 只允许政策不固定的那一维 + 指令开销；政策固定的维度不得出现（F11）。
    budget_allocation: Mapping[str, Any] | None = None


def _budget_allowance(budget: Mapping[str, Any]) -> dict[str, Any]:
    """冻结的预算额度：去掉政策名，保留全部实际额度值（F11）。"""
    return {
        key: deepcopy(value)
        for key, value in budget.items()
        if key not in _BUDGET_POLICY_KEYS
    }


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _arm_budget(
    base_budget: Mapping[str, Any],
    *,
    policy: str,
    arm_id: str,
    allocation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """按臂预算 = 冻结额度 + 政策名 + 政策显式允许的分配。

    政策固定的那一维不可分配；固定维度未知时无法核验分配，直接具名拒绝，
    既不补 0，也不默认一致（F11）。
    """
    budget = {**_budget_allowance(base_budget), "policy": policy}
    values = dict(allocation or {})
    if not values:
        return budget
    unknown = sorted(set(values) - set(BUDGET_ALLOCATION_KEYS))
    if unknown:
        raise AblationPlanError(
            "ABLATION_BUDGET_ALLOCATION_INVALID",
            f"arm {arm_id} allocates unknown budget dimensions: " + ", ".join(unknown),
        )
    fixed = _FIXED_BUDGET_DIMENSION[policy]
    if fixed in values:
        raise AblationPlanError(
            "ABLATION_BUDGET_ALLOCATION_INVALID",
            f"arm {arm_id} cannot allocate the {policy}-fixed dimension {fixed}",
        )
    fixed_value = budget_dimensions(budget).get(fixed)
    if fixed_value is None:
        raise AblationPlanError(
            "ABLATION_BUDGET_ALLOCATION_UNVERIFIABLE",
            f"arm {arm_id} declares a budget allocation but the {policy}-fixed "
            f"dimension {fixed} is unknown; it is never assumed or filled with 0",
        )
    overhead = _numeric(values.get("instruction_overhead"))
    if "instruction_overhead" in values and (overhead is None or overhead < 0):
        raise AblationPlanError(
            "ABLATION_BUDGET_ALLOCATION_INVALID",
            f"arm {arm_id} instruction_overhead must be a non-negative number",
        )
    fixed_number = _numeric(fixed_value)
    for key in ("execution_allowance", "total_allowance"):
        if key not in values:
            continue
        allocated = _numeric(values[key])
        if allocated is None or allocated < 0:
            raise AblationPlanError(
                "ABLATION_BUDGET_ALLOCATION_INVALID",
                f"arm {arm_id} {key} must be a non-negative number",
            )
        if fixed_number is None:
            continue
        if key == "execution_allowance" and allocated > fixed_number:
            raise AblationPlanError(
                "ABLATION_BUDGET_ALLOCATION_INVALID",
                f"arm {arm_id} execution_allowance {allocated} exceeds the fixed "
                f"total_allowance {fixed_number}",
            )
        if key == "total_allowance" and allocated < fixed_number:
            raise AblationPlanError(
                "ABLATION_BUDGET_ALLOCATION_INVALID",
                f"arm {arm_id} total_allowance {allocated} is below the fixed "
                f"execution_allowance {fixed_number}",
            )
    budget.update(values)
    return budget


def _frozen_drift(
    manifest: Mapping[str, Any], frozen: Mapping[str, Any],
) -> list[str]:
    """输入基底与冻结条件的差异（F11）：额度值、执行配置与冻结字段集合。"""
    drifted: list[str] = []
    if "budget" not in frozen:
        # 计划没有冻结预算额度（旧计划或手工构造）：非空额度不能当作已冻结。
        budget = manifest.get("budget")
        if isinstance(budget, Mapping) and _budget_allowance(budget):
            drifted.append("budget")
    for field_name, value in frozen.items():
        if field_name == "budget":
            budget = manifest.get("budget")
            candidate = (
                _budget_allowance(budget) if isinstance(budget, Mapping) else {}
            )
            if candidate != value:
                drifted.append("budget")
            continue
        if manifest.get(field_name) != value:
            drifted.append(field_name)
    for field_name in FROZEN_FIELDS:
        if field_name in frozen or field_name == "budget":
            continue
        if field_name in manifest:
            # 计划没有冻结、输入却有：冻结身份集合不同，旧计划不能展开。
            drifted.append(field_name)
    return sorted(set(drifted))


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
    # 实际额度进冻结身份：政策名由计划决定，额度值必须逐字段固定（F11）。
    base_budget = base_manifest.get("budget")
    base_budget = base_budget if isinstance(base_budget, Mapping) else {}
    frozen["budget"] = _budget_allowance(base_budget)
    plans: list[ArmPlan] = []
    warnings: list[str] = []
    for arm in arms:
        allocation = dict(arm.budget_allocation or {})
        manifest = deepcopy(dict(base_manifest))
        manifest["skills"] = list(arm.skills)
        manifest["skill_arm"] = arm.arm_id
        manifest["skill_snapshot"] = deepcopy(dict(arm.skill_snapshot)) if arm.skill_snapshot else None
        # 冻结条件必须逐字段一致：先按冻结额度核对，再应用政策允许的分配。
        manifest["budget"] = {**deepcopy(frozen["budget"]), "policy": budget_policy}
        drifted = _frozen_drift(manifest, frozen)
        if drifted:
            raise AblationPlanError(
                "ABLATION_CONDITION_DRIFT",
                f"arm {arm.arm_id} changes non-skill conditions: " + ", ".join(drifted),
            )
        manifest["budget"] = _arm_budget(
            base_budget, policy=budget_policy, arm_id=arm.arm_id,
            allocation=allocation,
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
            budget_allocation=deepcopy(allocation),
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
    """把计划展开成三条待创建 Run 的 manifest（除 Skill 外逐字段相同）。

    展开前先核对输入基底与计划的冻结条件一致（F11）：旧计划不能换 model、换
    执行配置或改实际额度后静默展开。三条臂之间的允许变化只有已发布 Skill 与
    政策显式允许的预算分配；其余条件逐字段相同。
    """
    if plan.budget_policy not in COMPARABLE_BUDGET_POLICIES:
        raise AblationPlanError(
            "ABLATION_BUDGET_POLICY_INVALID",
            "budget_policy must be one of " + ", ".join(COMPARABLE_BUDGET_POLICIES),
        )
    drifted = _frozen_drift(base_manifest, plan.frozen_conditions)
    if drifted:
        raise AblationPlanError(
            "ABLATION_CONDITION_DRIFT",
            "base manifest changes non-skill conditions: " + ", ".join(drifted),
        )
    base_budget = base_manifest.get("budget")
    base_budget = base_budget if isinstance(base_budget, Mapping) else {}
    manifests: dict[str, dict[str, Any]] = {}
    for arm in plan.arms:
        manifest = deepcopy(dict(base_manifest))
        manifest["skills"] = list(arm.skills)
        manifest["skill_arm"] = arm.arm_id
        manifest["skill_snapshot"] = deepcopy(arm.skill_snapshot)
        manifest["budget"] = _arm_budget(
            base_budget, policy=plan.budget_policy, arm_id=arm.arm_id,
            allocation=arm.budget_allocation,
        )
        manifests[arm.arm_id] = manifest
    return manifests
