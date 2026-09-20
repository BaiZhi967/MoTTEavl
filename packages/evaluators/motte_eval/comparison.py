"""比较资格求值（M6-T01 Lite）：固定两份 manifest → 逐条件/逐指标结论。

比较按任务源对齐（dataset revision + selected case 集合 + 期望/提取器/
prompt 版本），不按渲染后的 prompt。模型是合法变量时可比；缺费用只影响
费用指标，不影响质量指标资格。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from motte_contracts.comparison import ComparisonPolicy, RunReportRef


@dataclass(frozen=True)
class ComparisonResult:
    eligible: bool
    reasons: tuple[str, ...]
    metric_eligibility: dict[str, bool] = field(default_factory=dict)
    case_diff: dict[str, list[str]] = field(
        default_factory=lambda: {"added": [], "removed": [], "changed": []},
    )
    #: 政策显式允许的差异（典型是 model）；记录以便"允许"本身可审计。
    allowed_differences: tuple[str, ...] = ()


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
        "config_hash": profile.get("config_hash") if isinstance(profile, dict) else None,
        "native_settings": native,
        "budgets": profile.get("budgets") if isinstance(profile, dict) else None,
        "workspace": profile.get("workspace") if isinstance(profile, dict) else None,
        "credential_refs": profile.get("credential_refs") if isinstance(profile, dict) else None,
    }


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

    # 逐指标资格：费用未知只影响 cost。
    baseline_cost_known = bool((baseline_cost or {}).get("known"))
    candidate_cost_known = bool((candidate_cost or {}).get("known"))
    structural_ok = not reasons
    metric_eligibility["quality"] = structural_ok
    cost_ok = structural_ok and baseline_cost_known and candidate_cost_known
    metric_eligibility["cost"] = cost_ok
    if structural_ok and not cost_ok:
        if not baseline_cost_known:
            reasons.append("COST_UNKNOWN:baseline")
        if not candidate_cost_known:
            reasons.append("COST_UNKNOWN:candidate")

    return ComparisonResult(
        eligible=structural_ok,
        reasons=tuple(reasons),
        metric_eligibility=metric_eligibility,
        case_diff={
            "added": added,
            "removed": removed,
            "changed": sorted(set(changed) | set(content_changed)),
        },
        allowed_differences=tuple(allowed_differences),
    )
