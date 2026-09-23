"""比较资格求值（M6）：固定两份 manifest → 逐条件/逐指标/三级结论。

比较按任务源对齐（dataset revision + selected case 集合 + 期望/提取器/
prompt 版本），不按渲染后的 prompt。模型是合法变量时可比；缺费用只影响
费用指标，不影响质量指标资格。

M6-Full（协议 §3）：结论分三级——comparable（无阻断）、partially_comparable
（结构可比但部分 metric 资格不足，典型仅 cost unknown）、not_comparable
（结构性阻断）。``eligible`` 保持 Lite 兼容语义 = 结构可比（质量指标资格）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from motte_contracts.comparison import ComparabilityLevel, ComparisonPolicy, RunReportRef


@dataclass(frozen=True)
class ComparisonResult:
    eligible: bool
    reasons: tuple[str, ...]
    baseline_ref: RunReportRef
    candidate_ref: RunReportRef
    metric_eligibility: dict[str, bool] = field(default_factory=dict)
    case_diff: dict[str, list[str]] = field(
        default_factory=lambda: {"added": [], "removed": [], "changed": []},
    )
    #: 政策显式允许的差异（典型是 model）；记录以便"允许"本身可审计。
    allowed_differences: tuple[str, ...] = ()
    #: 三级结论（协议 §3）；Lite 消费者继续读 eligible。
    level: ComparabilityLevel = ComparabilityLevel.NOT_COMPARABLE
    #: 结构性阻断原因（阻断一切指标）；与指标级原因（metric_reasons）分开。
    structural_reasons: tuple[str, ...] = ()
    #: 指标级原因（如 COST_UNKNOWN）；只影响对应 metric 资格。
    metric_reasons: tuple[str, ...] = ()


def _external(manifest: dict[str, Any]) -> dict[str, Any]:
    external = manifest.get("external_benchmark")
    return external if isinstance(external, dict) else {}


def _profile(manifest: dict[str, Any]) -> dict[str, Any]:
    profile = _external(manifest).get("profile")
    return profile if isinstance(profile, dict) else {}


# 冻结 Profile 里不允许悄悄变化的口径字段。第三项是取值来源：``profile``
# 是 M3 冻结的 Profile，``external`` 是 M2 的 external_benchmark 顶层（
# environment_digest / runner_version / dataset_revision 在 TB 里放这里）。
_PROFILE_INVARIANTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("benchmark_id", "BENCHMARK_IDENTITY_CHANGED", ("profile",)),
    ("benchmark_version", "BENCHMARK_IDENTITY_CHANGED", ("profile",)),
    ("dataset_revision", "DATASET_REVISION_CHANGED", ("profile", "external")),
    ("split", "SPLIT_CHANGED", ("profile",)),
    ("few_shot", "FEWSHOT_CHANGED", ("profile",)),
    ("prompt_template_version", "EXTRACTOR_OR_PROMPT_CHANGED", ("profile",)),
    ("answer_extractor", "EXTRACTOR_OR_PROMPT_CHANGED", ("profile",)),
    ("extractor_version", "EXTRACTOR_OR_PROMPT_CHANGED", ("profile",)),
    ("aggregation", "AGGREGATION_CHANGED", ("profile",)),
    ("aggregation_version", "AGGREGATION_CHANGED", ("profile",)),
    ("seed", "SEED_CHANGED", ("profile",)),
    ("runner_version", "RUNNER_CHANGED", ("profile", "external")),
    ("environment_digest", "ENVIRONMENT_CHANGED", ("profile", "external")),
    # M3 冻结实验指纹（review R09）：重复数、Agent、预算/资源、超时、工具与
    # 环境不同就是不同实验条件，不能被当作模型差异比较。
    ("agent_id", "AGENT_CHANGED", ("profile",)),
    ("agent_version", "AGENT_CHANGED", ("profile",)),
    ("n_trials", "REPEATS_CHANGED", ("profile",)),
    ("timeouts", "TIMEOUTS_CHANGED", ("profile",)),
    ("resources", "RESOURCES_CHANGED", ("profile",)),
    ("tools", "TOOLS_CHANGED", ("profile",)),
    ("retries", "RETRIES_CHANGED", ("profile",)),
    ("limits", "LIMITS_CHANGED", ("profile",)),
    ("environment", "ENVIRONMENT_CHANGED", ("profile",)),
    # 凭据只比较**引用**（env:NAME），不接触秘密本身。
    ("credentials", "CREDENTIALS_CHANGED", ("profile",)),
)


def _invariant_value(
    profile: dict[str, Any], external: dict[str, Any], field: str,
    sources: tuple[str, ...],
) -> Any:
    """按声明顺序取值：``profile`` 优先，缺失时回退 ``external`` 顶层。"""
    for source in sources:
        container = profile if source == "profile" else external
        value = container.get(field)
        if value is not None:
            return value
    return None


def _model_identity(manifest: dict[str, Any]) -> Any:
    """按**套件实际冻结的模型身份**取值（review R2-10）。

    旧套件（GSM8K 等）把模型放在 manifest 顶层；M3 的 Terminal-Bench 放在
    ``external_benchmark.profile.model``；M4 的 runtime 运行把原生模型放在
    ``runtime_profile.native_settings.model``。只看顶层会让"政策不允许换
    模型"的比较悄悄放行原生模型差异（M4 review R16）。
    """
    profile_model = _profile(manifest).get("model")
    if isinstance(profile_model, Mapping) and profile_model.get("model"):
        return {
            "provider": profile_model.get("provider"),
            "model": profile_model.get("model"),
        }
    runtime_profile = manifest.get("runtime_profile")
    if isinstance(runtime_profile, dict):
        native_model = (runtime_profile.get("native_settings") or {}).get("model")
        if native_model:
            return str(native_model)
    return manifest.get("model")


def _runtime_identity(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """runtime 实验条件身份：backend 引用 + profile 的原生配置/预算/工作区。

    ``native_settings`` 去掉 ``model``（模型差异由 model 分支处理），其余
    配置差异都是实验条件差异，不允许时阻断比较（M4 review R16）。
    """
    runtime = manifest.get("runtime")
    profile = manifest.get("runtime_profile")
    if not isinstance(runtime, str) and not isinstance(profile, dict):
        return None
    native = dict(profile.get("native_settings") or {}) if isinstance(profile, dict) else {}
    native.pop("model", None)
    return {
        "runtime": runtime if isinstance(runtime, str) else None,
        # The full profile hash also includes model, an independently allowed
        # factor. Compare its projected contents, not that unprojected hash.
        "native_settings": native,
        "budgets": profile.get("budgets") if isinstance(profile, dict) else None,
        "workspace": profile.get("workspace") if isinstance(profile, dict) else None,
        "credential_refs": profile.get("credential_refs") if isinstance(profile, dict) else None,
    }


def _agent_config_identity(manifest: dict[str, Any]) -> Any:
    """agent_config 身份：去掉 budget 子键（实际预算由预算维度单独比对，F11）。"""
    config = manifest.get("agent_config")
    if not isinstance(config, dict):
        return config
    stripped = {key: value for key, value in config.items() if key != "budget"}
    return stripped or None


#: 场景 Run 的实验条件（顶层字段）→ 阻断码。这些字段在 benchmark Run 里
#: 位于 external_benchmark.profile，由 _PROFILE_INVARIANTS 统一覆盖。
_SCENARIO_TOP_LEVEL_FIELDS: tuple[tuple[str, str], ...] = (
    ("tools", "TOOLS_CHANGED"),
    ("tool_policy", "TOOLS_CHANGED"),
    ("timeouts", "TIMEOUTS_CHANGED"),
    ("limits", "LIMITS_CHANGED"),
    ("retries", "RETRIES_CHANGED"),
    ("environment", "ENVIRONMENT_CHANGED"),
    ("credentials", "CREDENTIALS_CHANGED"),
    # M5（F11）：provider 与冻结 Target 同样是实验条件，不能被悄悄换掉。
    ("provider", "PROVIDER_CHANGED"),
    ("target_snapshot", "TARGET_CHANGED"),
)

#: M5：Skill 归因只有在预算政策可比时才成立。
COMPARABLE_BUDGET_POLICIES: tuple[str, ...] = ("same-total-budget", "same-execution-budget")

#: 实际预算的三个维度（F11）：政策固定其中一维，另一维是政策显式允许按臂分配的
#: 变化。名称同时是计划服务（motte_sdk.skill_ablation）的接口。
BUDGET_DIMENSIONS: tuple[str, ...] = (
    "total_allowance", "instruction_overhead", "execution_allowance",
)

_BUDGET_POLICY_KEYS = ("policy", "comparison_policy")
#: 冻结预算里表达同一维度的等价键名；只用于读取，不用于生成。
_TOTAL_ALLOWANCE_KEYS = ("total_allowance", "max_total_tokens", "total_tokens")
_EXECUTION_ALLOWANCE_KEYS = (
    "execution_allowance", "max_execution_tokens", "execution_tokens",
)
_INSTRUCTION_OVERHEAD_KEYS = (
    "instruction_overhead", "instruction_overhead_tokens", "skill_instruction_tokens",
)
_BUDGET_ALIAS_KEYS = frozenset(
    _BUDGET_POLICY_KEYS
    + _TOTAL_ALLOWANCE_KEYS
    + _EXECUTION_ALLOWANCE_KEYS
    + _INSTRUCTION_OVERHEAD_KEYS
)


def _workflow_identity(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Workflow 身份：固定引用 + 内容 hash + fixture 固定版本集合。"""
    snapshot = manifest.get("workflow_snapshot")
    reference = manifest.get("workflow")
    fixtures = manifest.get("fixture_snapshot")
    if isinstance(snapshot, dict) and snapshot:
        identity = {
            "ref": snapshot.get("ref") or reference,
            "content_hash": snapshot.get("content_hash"),
            "schema_version": snapshot.get("schema_version"),
            "step_ids": list(snapshot.get("step_ids") or []),
            "limits": snapshot.get("limits"),
            "failure_policy": snapshot.get("failure_policy"),
        }
    elif isinstance(reference, str):
        identity = {"ref": reference, "content_hash": None, "schema_version": None,
                    "step_ids": [], "limits": None, "failure_policy": None}
    else:
        return None
    if isinstance(fixtures, dict):
        identity["fixtures"] = sorted(
            f"{key}:{(record or {}).get('content_hash')}"
            for key, record in fixtures.items()
        )
    else:
        identity["fixtures"] = None
    return identity


def _skill_identity(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Skill 臂身份：声明的技能引用、版本化快照以及对照臂 ID。

    只记录"选中了 Skill"不够：版本与内容 hash 也是身份的一部分，顺序改变
    同样改变 hash（M5-G11/G13）。
    """
    skills = manifest.get("skills")
    snapshot = manifest.get("skill_snapshot")
    arm = manifest.get("skill_arm")
    if not skills and not snapshot and not arm:
        return None
    return {
        "arm": arm,
        "refs": list(skills or []),
        "snapshot": snapshot if isinstance(snapshot, dict) else None,
    }


def budget_source(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """实际预算的来源字典（F11）：manifest.budget 优先，缺省回退 agent_config.budget。

    scenario 运行会把预算的一部分放在 agent_config 里（例如 max_steps），因此
    只看顶层 budget 会漏掉真实执行预算。
    """
    config = manifest.get("agent_config")
    nested = config.get("budget") if isinstance(config, dict) else None
    budget = manifest.get("budget")
    if not isinstance(nested, dict) and not isinstance(budget, dict):
        return None
    merged: dict[str, Any] = {}
    if isinstance(nested, dict):
        merged.update(nested)
    if isinstance(budget, dict):
        merged.update(budget)
    return merged


def _pick_allowance(budget: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = budget.get(key)
        if value is not None:
            return value
    return None


def budget_dimensions(budget: dict[str, Any]) -> dict[str, Any]:
    """预算 → 三个显式维度 + 其余配额；缺数据保持 None，绝不补 0。

    - total_allowance：总额度（例如 max_total_tokens）
    - instruction_overhead：Skill 指令开销
    - execution_allowance：留给执行的额度
    - other_allowances：其余配额（步数/轮数/墙钟等），逐字段比对
    """
    return {
        "policy": (
            budget.get("policy")
            if budget.get("policy") is not None
            else budget.get("comparison_policy")
        ),
        "total_allowance": _pick_allowance(budget, _TOTAL_ALLOWANCE_KEYS),
        "execution_allowance": _pick_allowance(budget, _EXECUTION_ALLOWANCE_KEYS),
        "instruction_overhead": _pick_allowance(budget, _INSTRUCTION_OVERHEAD_KEYS),
        "other_allowances": {
            key: value for key, value in budget.items() if key not in _BUDGET_ALIAS_KEYS
        },
    }


def _budget_dimensions(manifest: dict[str, Any]) -> dict[str, Any] | None:
    budget = budget_source(manifest)
    return budget_dimensions(budget) if budget is not None else None


def _budget_policy(manifest: dict[str, Any]) -> Any:
    budget = budget_source(manifest)
    if budget is None:
        return None
    policy = budget.get("policy")
    return policy if policy is not None else budget.get("comparison_policy")


def _compare_budget_dimensions(
    base: dict[str, Any], cand: dict[str, Any], policy_value: Any,
) -> tuple[list[str], list[str]]:
    """实际预算的逐维度判定（F11）。

    - 政策固定的那一维（same-total-budget → 总额度；same-execution-budget →
      执行额度）必须两侧已知且相等；一侧缺失 → 身份缺失阻断，绝不默认相等。
    - 政策允许变化的那几维显式计算并记录；一侧缺失时记录 unknown，不补 0。
    - 其余配额（步数/轮数/墙钟等）逐字段比对，差异阻断。
    """
    reasons: list[str] = []
    allowed: list[str] = []
    base_other = base["other_allowances"]
    cand_other = cand["other_allowances"]
    for key in sorted(set(base_other) | set(cand_other)):
        base_value = base_other.get(key)
        cand_value = cand_other.get(key)
        if base_value == cand_value:
            continue
        if (base_value is None) != (cand_value is None):
            reasons.append(
                f"IDENTITY_MISSING:budget.{key}: {base_value!r} vs {cand_value!r}"
            )
        else:
            reasons.append(
                f"BUDGET_ALLOWANCE_CHANGED:budget.{key}: "
                f"{base_value!r} -> {cand_value!r}"
            )
    if policy_value not in COMPARABLE_BUDGET_POLICIES:
        return reasons, allowed
    if policy_value == "same-total-budget":
        fixed, movable = "total_allowance", ("execution_allowance", "instruction_overhead")
    else:
        fixed, movable = "execution_allowance", ("total_allowance", "instruction_overhead")
    base_fixed = base[fixed]
    cand_fixed = cand[fixed]
    if base_fixed is None and cand_fixed is None:
        allowed.append(
            f"BUDGET_DIMENSION_UNKNOWN:{fixed}: neither arm declares it; "
            f"{policy_value} is recorded as unknown, never defaulted to equal"
        )
    elif base_fixed is None or cand_fixed is None:
        reasons.append(
            f"IDENTITY_MISSING:budget.{fixed}: {base_fixed!r} vs {cand_fixed!r}"
        )
    elif base_fixed != cand_fixed:
        reasons.append(
            f"BUDGET_ALLOWANCE_CHANGED:budget.{fixed}: {base_fixed!r} -> {cand_fixed!r}"
        )
    else:
        allowed.append(
            f"BUDGET_ALLOWANCE_EQUAL:budget.{fixed}: {base_fixed!r} "
            f"(held equal by {policy_value})"
        )
    for key in movable:
        base_value = base[key]
        cand_value = cand[key]
        if base_value == cand_value:
            continue
        if (base_value is None) != (cand_value is None):
            allowed.append(
                f"BUDGET_ALLOCATION_UNKNOWN:{key}: {base_value!r} -> {cand_value!r} "
                f"(permitted by {policy_value}; unknown is never filled with 0)"
            )
        else:
            allowed.append(
                f"BUDGET_ALLOCATION:{key}: {base_value!r} -> {cand_value!r} "
                f"(permitted by {policy_value})"
            )
    return reasons, allowed


def _scoring_provenance(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """所选 pass 的评分来源（Judge/rubric/校准/输入选择），不是当前 Judge 设置。

    历史缺字段保持 unknown；绝不从当前配置补齐（M5-T09c 第 12 条）。
    """
    provenance = manifest.get("scoring_provenance")
    return provenance if isinstance(provenance, dict) else None


def _compare_m5_invariants(
    baseline_manifest: dict[str, Any],
    candidate_manifest: dict[str, Any],
    policy: ComparisonPolicy,
) -> tuple[list[str], list[str]]:
    """M5 实验条件判定：Workflow / Fixture / Skill / 评分来源 / 预算政策。"""
    reasons: list[str] = []
    allowed: list[str] = []
    allowed_factors = set(policy.allowed_factors)

    base_workflow = _workflow_identity(baseline_manifest)
    cand_workflow = _workflow_identity(candidate_manifest)
    if (base_workflow is None) != (cand_workflow is None):
        reasons.append(
            f"IDENTITY_MISSING:workflow: {base_workflow!r} vs {cand_workflow!r}"
        )
    elif base_workflow is not None and base_workflow != cand_workflow:
        if "workflow" in allowed_factors:
            allowed.append(
                f"ALLOWED_FACTOR:workflow: {base_workflow.get('ref')!r} "
                f"-> {cand_workflow.get('ref')!r}"
            )
        else:
            reasons.append(
                f"WORKFLOW_CHANGED:workflow: {base_workflow!r} -> {cand_workflow!r}"
            )
    if base_workflow is not None and cand_workflow is not None:
        if base_workflow.get("fixtures") != cand_workflow.get("fixtures"):
            if "fixture" in allowed_factors:
                allowed.append("ALLOWED_FACTOR:fixture: fixture content hashes differ")
            else:
                reasons.append(
                    "FIXTURE_CHANGED:fixture: "
                    f"{base_workflow.get('fixtures')!r} -> {cand_workflow.get('fixtures')!r}"
                )

    base_skill = _skill_identity(baseline_manifest)
    cand_skill = _skill_identity(candidate_manifest)
    skill_differs = base_skill != cand_skill
    if skill_differs and (base_skill is None or cand_skill is None):
        reasons.append(
            f"IDENTITY_MISSING:skill: {base_skill!r} vs {cand_skill!r}"
        )
    elif skill_differs:
        if "skill" in allowed_factors:
            allowed.append(
                f"ALLOWED_FACTOR:skill: {(base_skill or {}).get('arm')!r} "
                f"-> {(cand_skill or {}).get('arm')!r}"
            )
        else:
            reasons.append(
                f"SKILL_CHANGED:skill: {(base_skill or {}).get('arm')!r} "
                f"-> {(cand_skill or {}).get('arm')!r}"
            )

    base_policy_value = _budget_policy(baseline_manifest)
    cand_policy_value = _budget_policy(candidate_manifest)
    if base_policy_value != cand_policy_value:
        if "budget_policy" in allowed_factors:
            allowed.append(
                f"ALLOWED_FACTOR:budget_policy: {base_policy_value!r} -> {cand_policy_value!r}"
            )
        else:
            reasons.append(
                f"BUDGET_POLICY_CHANGED:budget_policy: {base_policy_value!r} "
                f"-> {cand_policy_value!r}"
            )
    else:
        # 政策一致还不够：**实际额度**也是实验条件（F11）。政策固定的那一维必须
        # 相等，政策允许的分配差异显式计算并记录，未知维度绝不补 0 或默认一致。
        base_dimensions = _budget_dimensions(baseline_manifest)
        cand_dimensions = _budget_dimensions(candidate_manifest)
        if (base_dimensions is None) != (cand_dimensions is None):
            reasons.append(
                f"IDENTITY_MISSING:budget: {base_dimensions!r} vs {cand_dimensions!r}"
            )
        elif base_dimensions is not None:
            dimension_reasons, dimension_allowed = _compare_budget_dimensions(
                base_dimensions, cand_dimensions, base_policy_value,
            )
            reasons.extend(dimension_reasons)
            allowed.extend(dimension_allowed)
    if skill_differs and not reasons:
        # Skill 归因的前提：预算政策可比，且没有人工介入。否则差异不能只归因
        # 于 Skill（M5-A12）。
        if base_policy_value not in COMPARABLE_BUDGET_POLICIES:
            reasons.append(
                "SKILL_ATTRIBUTION_UNSAFE:budget_policy: skill arms must declare one of "
                + ", ".join(COMPARABLE_BUDGET_POLICIES)
            )
        interventions = (
            baseline_manifest.get("interventions") or candidate_manifest.get("interventions") or {}
        )
        if interventions.get("possible") or interventions.get("condition_hash"):
            reasons.append(
                "SKILL_ATTRIBUTION_UNSAFE:intervention: manual intervention makes a pure "
                "skill attribution invalid"
            )

    # 场景 Run 的实验条件放在 manifest 顶层（benchmark Run 放在
    # external_benchmark.profile，由 _PROFILE_INVARIANTS 覆盖）。只在任一侧确实
    # 声明了场景条件时比对顶层字段，避免对 benchmark Run 重复报告；没有
    # workflow 身份的 Target/直接 LLM Run 也必须有这一层。
    declared_scenario_fields = any(
        baseline_manifest.get(field) is not None
        or candidate_manifest.get(field) is not None
        for field, _code in _SCENARIO_TOP_LEVEL_FIELDS
    )
    if (
        base_workflow is not None or cand_workflow is not None
        or declared_scenario_fields
    ):
        for field, code in _SCENARIO_TOP_LEVEL_FIELDS:
            base_value = baseline_manifest.get(field)
            cand_value = candidate_manifest.get(field)
            if base_value == cand_value:
                continue
            if (base_value is None) != (cand_value is None):
                reasons.append(
                    f"IDENTITY_MISSING:{field}: {base_value!r} vs {cand_value!r}"
                )
                continue
            detail = f"{code}:{field}: {base_value!r} -> {cand_value!r}"
            if field in allowed_factors:
                allowed.append("ALLOWED_FACTOR:" + detail)
            else:
                reasons.append(detail)

    base_provenance = _scoring_provenance(baseline_manifest)
    cand_provenance = _scoring_provenance(candidate_manifest)
    if (base_provenance is None) != (cand_provenance is None):
        reasons.append(
            f"IDENTITY_MISSING:scoring_provenance: {base_provenance!r} vs {cand_provenance!r}"
        )
    elif base_provenance is not None and base_provenance != cand_provenance:
        # 逐字段给出可解释原因：identity/spec/prompt/rubric/输入选择/校准/人工修订。
        # 一侧有值一侧缺失 → 保持 unknown 并阻断，绝不从当前配置补齐（F09）。
        for field, factor, code in (
            ("scorer_id", "judge", "SCORER_CHANGED"),
            ("scorer_version", "judge", "SCORER_CHANGED"),
            ("judge_profile_id", "judge", "JUDGE_CHANGED"),
            ("mode", "judge", "JUDGE_MODE_CHANGED"),
            ("profile_sha256", "judge", "JUDGE_PROFILE_CHANGED"),
            ("spec_sha256", "judge", "JUDGE_SPEC_CHANGED"),
            ("model", "judge", "JUDGE_MODEL_CHANGED"),
            ("prompt", "judge", "JUDGE_PROMPT_CHANGED"),
            ("prompt_sha256", "judge", "JUDGE_PROMPT_CHANGED"),
            ("rubric_id", "rubric", "RUBRIC_CHANGED"),
            ("rubric_version", "rubric", "RUBRIC_CHANGED"),
            ("rubric_sha256", "rubric", "RUBRIC_CONTENT_CHANGED"),
            ("calibration_version", "calibration", "CALIBRATION_CHANGED"),
            ("input_selector", "judge", "JUDGE_INPUT_CHANGED"),
            ("missing_evidence_policy", "judge", "JUDGE_POLICY_CHANGED"),
            ("manual_revision", "intervention", "MANUAL_REVISION_CHANGED"),
        ):
            base_value = base_provenance.get(field)
            cand_value = cand_provenance.get(field)
            if base_value == cand_value:
                continue
            # 人工修订的"有/无"本身是已知事实（人改过评分），不是未知身份：
            # 一律按 MANUAL_REVISION_CHANGED 记录。
            if field != "manual_revision" and (base_value is None) != (cand_value is None):
                reasons.append(
                    f"IDENTITY_MISSING:scoring_provenance.{field}: "
                    f"{base_value!r} vs {cand_value!r}"
                )
                continue
            detail = f"{code}:{field}: {base_value!r} -> {cand_value!r}"
            if factor in allowed_factors:
                allowed.append("ALLOWED_FACTOR:" + detail)
            else:
                reasons.append(detail)
    return reasons, allowed


def _compare_invariants(
    baseline_manifest: dict[str, Any],
    candidate_manifest: dict[str, Any],
    policy: ComparisonPolicy,
) -> tuple[list[str], list[str]]:
    """逐字段判定评测口径；缺失身份不默认相等（review R08）。

    返回 ``(阻断原因, 政策显式允许的差异)``。政策允许的变量（缺省只有
    model 由下面的专门分支处理）不阻断，但必须留下可审计记录。
    """
    reasons: list[str] = []
    allowed: list[str] = []
    allowed_factors = set(policy.allowed_factors)
    base_interventions = baseline_manifest.get('interventions') or {}
    cand_interventions = candidate_manifest.get('interventions') or {}
    unknown_intervention = base_interventions.get('possible') or cand_interventions.get('possible')
    changed_intervention = base_interventions.get('condition_hash') != cand_interventions.get('condition_hash')
    if unknown_intervention or changed_intervention:
        if 'intervention' in allowed_factors:
            allowed.append('ALLOWED_FACTOR:intervention: human input/decision conditions differ or are unknown')
        else:
            reasons.append('INTERVENTION_UNKNOWN' if unknown_intervention else 'INTERVENTION_CHANGED')
    base_model = _model_identity(baseline_manifest)
    cand_model = _model_identity(candidate_manifest)
    if base_model != cand_model:
        if "model" not in allowed_factors:
            reasons.append(
                f"FACTOR_NOT_ALLOWED:model: {base_model!r} -> {cand_model!r}",
            )
        else:
            allowed.append(f"ALLOWED_FACTOR:model: {base_model!r} -> {cand_model!r}")
    # M4：runtime 实验条件（backend/transport/原生配置）进入可比性判定
    # （M4 review R16）：一侧声明 runtime、一侧没有 → 身份缺失阻断；两侧
    # runtime 条件不同且政策未允许 → 阻断并给出具体差异。
    base_runtime = _runtime_identity(baseline_manifest)
    cand_runtime = _runtime_identity(candidate_manifest)
    if (base_runtime is None) != (cand_runtime is None):
        reasons.append(
            "IDENTITY_MISSING:runtime: "
            f"{base_runtime!r} vs {cand_runtime!r}"
        )
    elif base_runtime is not None and base_runtime != cand_runtime:
        if "runtime" in allowed_factors:
            allowed.append(
                f"ALLOWED_FACTOR:runtime: {base_runtime.get('runtime')!r} "
                f"-> {cand_runtime.get('runtime')!r}"
            )
        else:
            reasons.append(
                f"RUNTIME_CHANGED:runtime: {base_runtime!r} -> {cand_runtime!r}"
            )
    base_external = _external(baseline_manifest)
    cand_external = _external(candidate_manifest)
    base_profile = _profile(baseline_manifest)
    cand_profile = _profile(candidate_manifest)
    for invariant_field, code, sources in _PROFILE_INVARIANTS:
        base_value = _invariant_value(
            base_profile, base_external, invariant_field, sources,
        )
        cand_value = _invariant_value(
            cand_profile, cand_external, invariant_field, sources,
        )
        if (base_value is None) != (cand_value is None):
            # 缺失身份不默认相等（review R08）：一侧冻结、一侧没有 → 阻断。
            reasons.append(
                f"IDENTITY_MISSING:{invariant_field}: {base_value!r} vs {cand_value!r}"
            )
        elif base_value is not None and base_value != cand_value:
            if invariant_field in allowed_factors:
                allowed.append(
                    f"ALLOWED_FACTOR:{invariant_field}: {base_value!r} -> {cand_value!r}"
                )
                continue
            reasons.append(
                f"{code}:{invariant_field}: {base_value!r} -> {cand_value!r}"
            )

    # M5（F11）：Agent 与 agent_config 是冻结执行条件。只改 max_steps 也是另一个
    # 实验，不能被当成模型/Skill 差异比较；agent_config.budget 归预算维度。
    base_agent = baseline_manifest.get("agent")
    cand_agent = candidate_manifest.get("agent")
    if (base_agent is None) != (cand_agent is None):
        reasons.append(
            f"IDENTITY_MISSING:agent: {base_agent!r} vs {cand_agent!r}"
        )
    elif base_agent is not None and base_agent != cand_agent:
        if {"agent_id", "agent_version"} & allowed_factors:
            allowed.append(f"ALLOWED_FACTOR:agent: {base_agent!r} -> {cand_agent!r}")
        else:
            reasons.append(f"AGENT_CHANGED:agent: {base_agent!r} -> {cand_agent!r}")
    base_config = _agent_config_identity(baseline_manifest)
    cand_config = _agent_config_identity(candidate_manifest)
    if (base_config is None) != (cand_config is None):
        reasons.append(
            f"IDENTITY_MISSING:agent_config: {base_config!r} vs {cand_config!r}"
        )
    elif base_config is not None and base_config != cand_config:
        reasons.append(
            f"AGENT_CONFIG_CHANGED:agent_config: {base_config!r} -> {cand_config!r}"
        )

    base_eval = baseline_manifest.get("evaluation")
    cand_eval = candidate_manifest.get("evaluation")
    if isinstance(base_eval, dict) and isinstance(cand_eval, dict):
        for field in ("scorer_id", "scorer_version"):
            if base_eval.get(field) != cand_eval.get(field):
                reasons.append(
                    f"SCORER_CHANGED:{field}: {base_eval.get(field)!r} -> "
                    f"{cand_eval.get(field)!r}",
                )
    return reasons, allowed


def _case_ids(manifest: dict[str, Any]) -> list[str]:
    """比较用的 case 集合：Run 视图的 ``case_ids`` 优先，缺失时用冻结任务集。

    冻结 manifest 自带 ``task_manifest.task_keys``/``selected_tasks``，因此
    "case 集合"在只有冻结快照（没有活 Run 视图）时也能比对——同 revision
    但选中任务不同必须表现为 ``CASE_SET_CHANGED``，不能因为缺字段跳过。
    """
    ids = [str(item) for item in manifest.get("case_ids") or []]
    if ids:
        return ids
    task_manifest = manifest.get("task_manifest")
    if isinstance(task_manifest, dict):
        keys = [str(item) for item in task_manifest.get("task_keys") or []]
        if keys:
            return keys
    return [str(item) for item in manifest.get("selected_tasks") or []]


def compare_run_reports(
    baseline_ref: RunReportRef,
    candidate_ref: RunReportRef,
    *,
    baseline_manifest: dict[str, Any],
    candidate_manifest: dict[str, Any],
    policy: ComparisonPolicy,
    baseline_cost: dict[str, Any] | None = None,
    candidate_cost: dict[str, Any] | None = None,
) -> ComparisonResult:
    reasons: list[str] = []
    metric_eligibility: dict[str, bool] = {}

    # 逐条件：允许因子之外的差异都阻断整体资格（R08 的完整字段清单）。
    invariance_reasons, allowed_differences = _compare_invariants(
        baseline_manifest, candidate_manifest, policy,
    )
    reasons.extend(invariance_reasons)
    # M5：Workflow / Fixture / Skill / 评分来源 / 预算政策也是实验条件。
    m5_reasons, m5_allowed = _compare_m5_invariants(
        baseline_manifest, candidate_manifest, policy,
    )
    reasons.extend(m5_reasons)
    allowed_differences = [*allowed_differences, *m5_allowed]

    base_cases = _case_ids(baseline_manifest)
    cand_cases = _case_ids(candidate_manifest)
    added = sorted(set(cand_cases) - set(base_cases))
    removed = sorted(set(base_cases) - set(cand_cases))
    changed: list[str] = []
    common = set(base_cases) & set(cand_cases)
    base_expectations = baseline_manifest.get("case_expectations") or {}
    cand_expectations = candidate_manifest.get("case_expectations") or {}
    for case_id in sorted(common):
        if base_expectations.get(case_id) != cand_expectations.get(case_id):
            changed.append(case_id)
    if added or removed or changed:
        reasons.append(
            f"CASE_SET_CHANGED: added={added} removed={removed} changed={changed}",
        )

    # 任务内容身份（review R2-05）：冻结行 hash 逐题比对；同 revision 字符串
    # 相同不证明内容相同。单侧缺失身份 → 阻断，不默认相等。
    base_content = baseline_manifest.get("case_content_hashes")
    cand_content = candidate_manifest.get("case_content_hashes")
    content_changed: list[str] = []
    if (base_content is None) != (cand_content is None):
        reasons.append(
            "IDENTITY_MISSING:case_content_hashes: "
            f"{type(base_content).__name__} vs {type(cand_content).__name__}"
        )
    elif isinstance(base_content, dict) and isinstance(cand_content, dict):
        for case_id in sorted(common):
            if base_content.get(case_id) != cand_content.get(case_id):
                content_changed.append(case_id)
        if content_changed:
            reasons.append(f"CASE_CONTENT_CHANGED: {content_changed}")
    base_few_shot = baseline_manifest.get("few_shot_hashes")
    cand_few_shot = candidate_manifest.get("few_shot_hashes")
    if (base_few_shot is None) != (cand_few_shot is None):
        reasons.append("IDENTITY_MISSING:few_shot_hashes")
    elif base_few_shot is not None and base_few_shot != cand_few_shot:
        reasons.append("FEWSHOT_CONTENT_CHANGED: few-shot example content differs")

    # 逐指标资格：费用未知只影响 cost（指标级原因，不阻断质量可比）。
    baseline_cost_known = bool((baseline_cost or {}).get("known"))
    candidate_cost_known = bool((candidate_cost or {}).get("known"))
    structural_reasons = tuple(reasons)
    structural_ok = not structural_reasons
    metric_eligibility["quality"] = structural_ok
    base_currencies = set((baseline_cost or {}).get("currencies") or
                          [(baseline_cost or {}).get("currency", "USD")])
    cand_currencies = set((candidate_cost or {}).get("currencies") or
                          [(candidate_cost or {}).get("currency", "USD")])
    currency_match = base_currencies == cand_currencies
    cost_ok = structural_ok and baseline_cost_known and candidate_cost_known and currency_match
    metric_eligibility["cost"] = cost_ok
    metric_reasons: list[str] = []
    if structural_ok and not cost_ok:
        if not baseline_cost_known:
            metric_reasons.append("COST_UNKNOWN:baseline")
        if not candidate_cost_known:
            metric_reasons.append("COST_UNKNOWN:candidate")
        if baseline_cost_known and candidate_cost_known and not currency_match:
            metric_reasons.append("COST_CURRENCY_MISMATCH")
    reasons.extend(metric_reasons)

    # 三级结论（协议 §3）：结构性阻断 → not_comparable；仅指标级资格不足 →
    # partially_comparable；全部资格完整 → comparable。
    if not structural_ok:
        level = ComparabilityLevel.NOT_COMPARABLE
    elif metric_reasons:
        level = ComparabilityLevel.PARTIALLY_COMPARABLE
    else:
        level = ComparabilityLevel.COMPARABLE

    return ComparisonResult(
        eligible=structural_ok,
        reasons=tuple(reasons),
        baseline_ref=baseline_ref,
        candidate_ref=candidate_ref,
        metric_eligibility=metric_eligibility,
        case_diff={
            "added": added,
            "removed": removed,
            "changed": sorted(set(changed) | set(content_changed)),
        },
        allowed_differences=tuple(allowed_differences),
        level=level,
        structural_reasons=structural_reasons,
        metric_reasons=tuple(metric_reasons),
    )
