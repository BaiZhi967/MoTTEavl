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
from dataclasses import dataclass, field, replace
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
        #: 注意：skill_snapshot 只存在于**计划**身份里。ResolvedManifest 不接受
        #: 它作为顶层键（extra=forbid），真实冻结快照由创建期写进
        #: resource_snapshots.skill_injection（motte_sdk.resolve）。
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
        if not arm.skill_snapshot and arm.arm_id != ARM_NO_SKILL:
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


# ------------------------------------------------------- 展开与配对报告

#: Run 状态里"这条臂已经有终态结果"的集合；其余状态都算覆盖不足。
TERMINAL_RUN_STATUSES: tuple[str, ...] = ("completed",)

#: 逐 Case 的配对状态；只有 `passed` / `failed` 才算"有明确结果"。
DEFINITE_CASE_STATUSES: tuple[str, ...] = ("passed", "failed")


@dataclass(frozen=True)
class ArmRun:
    """一条臂的**固定引用**：run_id + 冻结 pass + 报告引用。"""

    arm_id: str
    run_ref: str
    run_id: str
    scoring_pass_id: str | None = None
    report_ref: dict[str, Any] | None = None
    skills: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "arm_id": self.arm_id,
            "run_ref": self.run_ref,
            "run_id": self.run_id,
            "scoring_pass_id": self.scoring_pass_id,
            "report_ref": deepcopy(self.report_ref),
            "skills": list(self.skills),
        }


@dataclass(frozen=True)
class AblationExecution:
    """一次三臂展开的结果：三个普通 Run 的固定引用，没有第二套调度。"""

    experiment_ref: str
    plan_hash: str
    budget_policy: str
    arms: tuple[ArmRun, ...]

    def arm(self, arm_id: str) -> ArmRun:
        for item in self.arms:
            if item.arm_id == arm_id:
                return item
        raise AblationPlanError("ABLATION_ARM_UNKNOWN", f"unknown arm: {arm_id}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "experiment_ref": self.experiment_ref,
            "plan_hash": self.plan_hash,
            "budget_policy": self.budget_policy,
            "arms": [arm.as_dict() for arm in self.arms],
        }


def _reference_payload(reference: Any) -> dict[str, Any] | None:
    """报告引用规范化：Mapping 或带 model_dump 的契约对象都接受，其余为 None。"""
    if isinstance(reference, Mapping):
        return dict(reference)
    dump = getattr(reference, "model_dump", None)
    if callable(dump):
        payload = dump(mode="json")
        return dict(payload) if isinstance(payload, Mapping) else None
    return None


def run_skill_ablation(
    plan: AblationPlan,
    base_manifest: Mapping[str, Any],
    *,
    create_run: Any,
    pass_id_of: Any = None,
    report_ref: Any = None,
) -> AblationExecution:
    """把计划展开成三条**既有普通 Run**，并保存每臂的固定引用。

    `create_run(arm_id, manifest) -> run view` 是调用方的既有创建入口
    （API/CLI/RunService），本模块不新增调度器、不直接建表、也不执行 Run。
    返回的 Run 必须真的是这条臂的冻结身份，否则具名拒绝（不能把 A 臂的 Run
    当成 B 臂的证据）。
    """
    if plan.budget_policy not in COMPARABLE_BUDGET_POLICIES:
        raise AblationPlanError(
            "ABLATION_BUDGET_POLICY_INVALID",
            "budget_policy must be one of " + ", ".join(COMPARABLE_BUDGET_POLICIES),
        )
    manifests = arm_manifests(base_manifest, plan)
    engine = _arm_pass_id if pass_id_of is None else pass_id_of
    runs: list[ArmRun] = []
    for arm in plan.arms:
        manifest = manifests[arm.arm_id]
        try:
            view = create_run(arm.arm_id, manifest)
        except Exception as error:  # noqa: BLE001 - 具名解释"哪条臂没建起来"
            raise AblationPlanError(
                "ABLATION_ARM_CREATE_FAILED",
                f"arm {arm.arm_id} could not be created: {type(error).__name__}: {error}",
            ) from error
        if not isinstance(view, Mapping) or not view.get("id"):
            raise AblationPlanError(
                "ABLATION_ARM_CREATE_FAILED",
                f"arm {arm.arm_id} creation did not return a run view with an id",
            )
        frozen = view.get("manifest") or {}
        if frozen.get("skill_arm") != arm.arm_id:
            raise AblationPlanError(
                "ABLATION_ARM_IDENTITY_MISMATCH",
                f"arm {arm.arm_id} produced a run whose skill_arm is "
                f"{frozen.get('skill_arm')!r}",
            )
        if list(frozen.get("skills") or []) != list(arm.skills):
            raise AblationPlanError(
                "ABLATION_ARM_IDENTITY_MISMATCH",
                f"arm {arm.arm_id} produced a run whose skills differ from the plan",
            )
        pass_id = engine(view)
        reference = report_ref(view["id"], pass_id) if callable(report_ref) else None
        runs.append(ArmRun(
            arm_id=arm.arm_id,
            run_ref=arm.run_ref,
            run_id=str(view["id"]),
            scoring_pass_id=str(pass_id) if pass_id else None,
            report_ref=_reference_payload(reference),
            skills=tuple(arm.skills),
        ))
    return AblationExecution(
        experiment_ref=plan.experiment_ref,
        plan_hash=plan.plan_hash,
        budget_policy=plan.budget_policy,
        arms=tuple(runs),
    )


def bind_ablation_references(
    execution: AblationExecution, *, run_view: Any, report_ref: Any = None,
) -> AblationExecution:
    """执行后绑定每臂**固定的** pass / report 引用（run_id 与计划身份不变）。

    pass 只有在 Run 执行并发布评分后才存在，因此不能在创建时伪造；这里在
    展开完成后读取一次并固定下来，后续报告只读这些引用，不追随 current。
    """
    arms: list[ArmRun] = []
    for arm in execution.arms:
        view = run_view(arm.run_id) or {}
        pass_id = view.get("current_scoring_pass_id") or arm.scoring_pass_id
        reference = (
            report_ref(arm.run_id, pass_id) if callable(report_ref) else arm.report_ref
        )
        arms.append(replace(
            arm,
            scoring_pass_id=str(pass_id) if pass_id else None,
            report_ref=_reference_payload(reference),
        ))
    return AblationExecution(
        experiment_ref=execution.experiment_ref,
        plan_hash=execution.plan_hash,
        budget_policy=execution.budget_policy,
        arms=tuple(arms),
    )


def _arm_pass_id(view: Mapping[str, Any]) -> Any:
    return view.get("current_scoring_pass_id") or view.get("scoring_pass_id")


def _case_rows(evidence: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows: dict[str, Mapping[str, Any]] = {}
    for row in evidence.get("cases") or ():
        if isinstance(row, Mapping) and isinstance(row.get("case_id"), str):
            rows[str(row["case_id"])] = row
    return rows


def _scores_by_case(run: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in run.get("scores") or ():
        if not isinstance(row, Mapping):
            continue
        case_id = row.get("case_id")
        if isinstance(case_id, str):
            grouped.setdefault(case_id, []).append(row)
    return grouped


def _tool_calls_of(case: Mapping[str, Any] | None) -> int | None:
    """逐 Case 工具调用次数：冻结的 ToolCallRecord 列表优先，其次观察计数。"""
    if not isinstance(case, Mapping):
        return None
    result = case.get("result")
    if not isinstance(result, Mapping):
        return None
    frozen = result.get("frozen_observation")
    if isinstance(frozen, Mapping) and isinstance(frozen.get("tool_calls"), list):
        return len(frozen["tool_calls"])
    observation = result.get("observation")
    if isinstance(observation, Mapping) and isinstance(observation.get("tool_calls"), int):
        return int(observation["tool_calls"])
    return None


def _case_status(
    case: Mapping[str, Any] | None,
    score_rows: Sequence[Mapping[str, Any]],
    run_status: Any,
) -> dict[str, Any]:
    """一个 Case 在一条臂上的实际结局与失败分类（事实优先，不猜）。"""
    if str(run_status or "") == "cancelled":
        return {"status": "cancelled", "passed": None, "failure_class": "run_cancelled",
                "detail": "the run was cancelled"}
    if not isinstance(case, Mapping):
        return {"status": "no_result", "passed": None, "failure_class": "no_result",
                "detail": "the arm persisted no result for this case"}
    result = case.get("result")
    if isinstance(result, Mapping) and isinstance(result.get("error"), Mapping):
        error = result["error"]
        return {
            "status": "execution_error", "passed": False,
            "failure_class": "execution_error:" + str(error.get("class") or "unknown"),
            "detail": str(error.get("message") or "")[:500],
        }
    scenario = result.get("scenario") if isinstance(result, Mapping) else None
    if not isinstance(scenario, Mapping):
        return {"status": "no_result", "passed": None, "failure_class": "no_result",
                "detail": "case result carries no scenario envelope"}
    decisive = [row for row in score_rows if row.get("passed") is not None]
    if not decisive:
        reason = next(
            (str(row.get("reason") or row.get("status") or "") for row in score_rows),
            "no score rows",
        )
        return {
            "status": "insufficient_evidence", "passed": None,
            "failure_class": "insufficient_evidence", "detail": reason[:300],
            "scenario_status": scenario.get("status"),
        }
    failed = sorted({
        str(row.get("metric_id")) for row in decisive if row.get("passed") is not True
    })
    if failed:
        return {
            "status": "failed", "passed": False,
            "failure_class": "metric_failed:" + ",".join(failed[:3]),
            "detail": "failed metrics: " + ", ".join(failed),
            "scenario_status": scenario.get("status"),
            "needs_review": bool(scenario.get("needs_review")),
        }
    workflow_status = str(scenario.get("status") or "")
    if workflow_status and workflow_status != "completed":
        # 指标全过但 Workflow 自己没走完（例如过程断言失败后 stop_case）：
        # 这不是"通过"，不能只在成功 Case 上声称收益。
        return {
            "status": "failed", "passed": False,
            "failure_class": "workflow_" + workflow_status,
            "detail": "scenario ended as " + workflow_status + ": "
                      + str(scenario.get("reason") or ""),
            "scenario_status": workflow_status,
            "needs_review": bool(scenario.get("needs_review")),
        }
    return {
        "status": "passed", "passed": True, "failure_class": None,
        "detail": None, "scenario_status": workflow_status or None,
        "needs_review": bool(scenario.get("needs_review")),
    }


def _instruction_overhead(run: Mapping[str, Any]) -> dict[str, Any]:
    """该臂的指令 token overhead（estimated，单列，不重复计费）。"""
    manifest = run.get("manifest") or {}
    snapshots = manifest.get("resource_snapshots")
    declaration = (
        snapshots.get("skill_injection") if isinstance(snapshots, Mapping) else None
    )
    if not declaration:
        return {
            "method": "chars-over-4", "estimate": 0, "measured": False,
            "source": "skill-instruction", "entries": 0, "billed": False,
        }
    from motte_skill.injection import (
        InjectionError,
        instruction_overhead_totals,
        plan_from_declaration,
    )

    try:
        plan = plan_from_declaration(declaration)
    except InjectionError as error:
        # 声明不可信时如实标未知，不猜一个数字。
        return {
            "method": None, "estimate": None, "measured": False,
            "source": "skill-instruction", "entries": None, "billed": False,
            "unknown_reason": str(error),
        }
    return instruction_overhead_totals(plan)


def _total_cost(run: Mapping[str, Any]) -> dict[str, Any]:
    """总成本：只有在 manifest 明确声明 known 时才给数字，否则保持 unknown。"""
    manifest = run.get("manifest") or {}
    cost = manifest.get("cost") if isinstance(manifest, Mapping) else None
    if isinstance(cost, Mapping) and cost.get("known") is True:
        return {
            "known": True,
            "total_usd": cost.get("total_usd"),
            "currency": cost.get("currency") or "USD",
        }
    return {"known": False, "total_usd": None, "currency": None}


def build_ablation_report(
    plan: AblationPlan,
    execution: AblationExecution,
    evidence: Mapping[str, Mapping[str, Any]],
    *,
    baseline_arm: str = ARM_NO_SKILL,
    comparison: Any = None,
) -> dict[str, Any]:
    """把三条臂的真实 Run 事实组装成配对报告（含覆盖与阻断原因）。

    * 逐 Case 配对：每条臂的 status / passed / failure_class / 工具次数；
    * 缺少结果、取消、执行错误一律**显式**记录，并按覆盖不足阻断归因；
    * 成本未知保持 unknown；指令 token overhead 单列且不计入模型费用；
    * `comparison(arm_id) -> {"reasons": [...], "allowed": [...]}` 由既有比较
      服务提供（R4）：预算/Agent 等条件不同时，纯 Skill 归因必须被阻断。
    """
    if baseline_arm not in {arm.arm_id for arm in execution.arms}:
        raise AblationPlanError(
            "ABLATION_BASELINE_UNKNOWN", f"baseline arm {baseline_arm!r} is not part of the plan"
        )
    expected = [str(key) for key in plan.paired_case_keys]
    arm_facts: dict[str, dict[str, Any]] = {}
    for arm in execution.arms:
        record = evidence.get(arm.arm_id) or {}
        run = record.get("run") or {}
        rows = _case_rows(record)
        grouped = _scores_by_case(run)
        selected = [str(case_id) for case_id in (run.get("case_ids") or expected)]
        statuses: dict[str, dict[str, Any]] = {}
        for case_id in expected or selected:
            statuses[case_id] = _case_status(
                rows.get(case_id), grouped.get(case_id, []), run.get("status"),
            )
        tool_calls = [_tool_calls_of(rows.get(case_id)) for case_id in statuses]
        arm_facts[arm.arm_id] = {
            "arm_id": arm.arm_id,
            "run_id": arm.run_id,
            "scoring_pass_id": arm.scoring_pass_id,
            "report_ref": deepcopy(arm.report_ref),
            "run_ref": arm.run_ref,
            "skills": list(arm.skills),
            "status": run.get("status"),
            "selected_cases": selected,
            "terminal": str(run.get("status") or "") in TERMINAL_RUN_STATUSES,
            "cases": statuses,
            "passed": sum(1 for item in statuses.values() if item["passed"] is True),
            "failed": sum(1 for item in statuses.values() if item["passed"] is False),
            "cancelled": sum(1 for item in statuses.values() if item["status"] == "cancelled"),
            "no_result": sum(
                1 for item in statuses.values()
                if item["status"] in {"no_result", "execution_error", "insufficient_evidence"}
            ),
            "tool_calls": None if any(item is None for item in tool_calls)
            else sum(int(item) for item in tool_calls),
            "cost": _total_cost(run),
            "instruction_tokens": _instruction_overhead(run),
        }
    cases: list[dict[str, Any]] = []
    for case_id in expected:
        entry: dict[str, Any] = {
            "case_key": case_id,
            "arms": {},
        }
        for arm in execution.arms:
            entry["arms"][arm.arm_id] = dict(
                arm_facts[arm.arm_id]["cases"].get(case_id) or {
                    "status": "no_result", "passed": None,
                    "failure_class": "no_result", "detail": None,
                }
            )
        entry["paired"] = all(
            entry["arms"][arm.arm_id]["status"] in DEFINITE_CASE_STATUSES
            for arm in execution.arms
        )
        entry["coverage_complete"] = all(
            entry["arms"][arm.arm_id]["status"] != "no_result"
            for arm in execution.arms
        )
        cases.append(entry)
    paired = [item for item in cases if item["paired"]]
    unpaired = [item["case_key"] for item in cases if not item["paired"]]

    # 归因：先看覆盖，再看条件可比性（R4 的比较结果），两者都过才允许声称收益。
    reasons: list[str] = []
    blocking: list[str] = []
    allowed: list[str] = []
    cost_reasons: list[str] = []
    for arm in execution.arms:
        facts = arm_facts[arm.arm_id]
        if not facts["terminal"]:
            reasons.append(
                f"arm {arm.arm_id} is not terminal: status={facts['status']!r} "
                "(no result yet / cancelled / failed)"
            )
        if arm.scoring_pass_id is None or arm.report_ref is None:
            reasons.append(
                f"arm {arm.arm_id} has no fixed scoring pass / report reference"
            )
    for case_id in unpaired:
        reasons.append("case " + case_id + " has no definite result in every arm")
    if comparison is None:
        reasons.append(
            "condition comparability was not evaluated: a pure skill attribution is unproven"
        )
    else:
        for arm in execution.arms:
            if arm.arm_id == baseline_arm:
                continue
            outcome = comparison(arm.arm_id) or {}
            arm_reasons = [str(item) for item in (outcome.get("reasons") or ())]
            arm_allowed = [str(item) for item in (outcome.get("allowed") or ())]
            for item in arm_reasons:
                if item.startswith("COST_UNKNOWN"):
                    # 费用未知不是"条件不可比"，而是该维度不可归因：单列记录，
                    # 绝不补 0，也不冒充条件相同（M5-T08 第 5 条）。
                    cost_reasons.append(f"{arm.arm_id}: {item}")
                    continue
                blocking.append(f"{arm.arm_id}: {item}")
            allowed.extend(f"{arm.arm_id}: {item}" for item in arm_allowed)
        if blocking:
            reasons.append(
                "non-skill conditions differ between arms; a pure skill attribution is blocked"
            )
    baseline = arm_facts[baseline_arm]
    gains: dict[str, Any] = {}
    for arm in execution.arms:
        if arm.arm_id == baseline_arm:
            continue
        delta = 0
        counted = 0
        for item in paired:
            arm_result = item["arms"][arm.arm_id]["passed"]
            base_result = item["arms"][baseline_arm]["passed"]
            if arm_result is None or base_result is None:
                continue
            counted += 1
            delta += int(arm_result) - int(base_result)
        gains[f"{baseline_arm}->{arm.arm_id}"] = {
            "paired_cases": counted,
            "passed_delta": delta,
            "expected_cases": len(expected),
            "complete": counted == len(expected) and not unpaired,
        }
    costs = {arm_id: facts["cost"] for arm_id, facts in arm_facts.items()}
    known = all(item["known"] for item in costs.values())
    deltas: dict[str, Any] = {}
    for arm_id, item in costs.items():
        if arm_id == baseline_arm:
            continue
        if item["known"] and baseline["cost"]["known"]:
            deltas[arm_id] = (item["total_usd"] or 0) - (baseline["cost"]["total_usd"] or 0)
        else:
            deltas[arm_id] = None
    return {
        "experiment_ref": plan.experiment_ref,
        "plan_hash": plan.plan_hash,
        "execution": execution.as_dict(),
        "budget_policy": plan.budget_policy,
        "baseline_arm": baseline_arm,
        "arms": arm_facts,
        "cases": cases,
        "coverage": {
            "expected_cases": len(expected),
            "paired_cases": len(paired),
            "unpaired_cases": unpaired,
            "complete": bool(expected) and not unpaired,
        },
        "attribution": {
            "eligible": not reasons,
            "reasons": reasons,
            "blocking": blocking,
            "allowed": allowed,
            "cost_reasons": cost_reasons,
            "cost_comparable": not cost_reasons and known,
        },
        "gains": gains,
        "cost": {
            "per_arm": costs,
            "known": known,
            "delta_vs_baseline_usd": deltas,
            "instruction_tokens": {
                arm_id: facts["instruction_tokens"].get("estimate")
                for arm_id, facts in arm_facts.items()
            },
            "instruction_tokens_billed": False,
            "note": (
                "unknown usage/cost stays unknown; instruction tokens are estimated "
                "separately and are never added to billed model usage"
            ),
        },
    }

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
        # skill_snapshot 不是 ResolvedManifest 契约字段：真实冻结身份由
        # resource_snapshots.skill_injection 提供，避免未校验的顶层键被打回。
        manifest["budget"] = _arm_budget(
            base_budget, policy=plan.budget_policy, arm_id=arm.arm_id,
            allocation=arm.budget_allocation,
        )
        manifests[arm.arm_id] = manifest
    return manifests
