"""比较资格求值（M6-T01 Lite）：固定两份 manifest → 逐条件/逐指标结论。

比较按任务源对齐（dataset revision + selected case 集合 + 期望/提取器/
prompt 版本），不按渲染后的 prompt。模型是合法变量时可比；缺费用只影响
费用指标，不影响质量指标资格。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from motte_contracts.comparison import ComparisonPolicy, RunReportRef


@dataclass(frozen=True)
class ComparisonResult:
    eligible: bool
    reasons: tuple[str, ...]
    metric_eligibility: dict[str, bool] = field(default_factory=dict)
    case_diff: dict[str, list[str]] = field(
        default_factory=lambda: {"added": [], "removed": [], "changed": []},
    )


def _external(manifest: dict[str, Any]) -> dict[str, Any]:
    external = manifest.get("external_benchmark")
    return external if isinstance(external, dict) else {}


def _profile(manifest: dict[str, Any]) -> dict[str, Any]:
    profile = _external(manifest).get("profile")
    return profile if isinstance(profile, dict) else {}


# 冻结 Profile 里除 model（合法变量）外不允许变化的口径字段（review R08）。
_PROFILE_INVARIANTS: tuple[tuple[str, str], ...] = (
    ("benchmark_id", "BENCHMARK_IDENTITY_CHANGED"),
    ("benchmark_version", "BENCHMARK_IDENTITY_CHANGED"),
    ("dataset_revision", "DATASET_REVISION_CHANGED"),
    ("split", "SPLIT_CHANGED"),
    ("few_shot", "FEWSHOT_CHANGED"),
    ("prompt_template_version", "EXTRACTOR_OR_PROMPT_CHANGED"),
    ("answer_extractor", "EXTRACTOR_OR_PROMPT_CHANGED"),
    ("extractor_version", "EXTRACTOR_OR_PROMPT_CHANGED"),
    ("aggregation", "AGGREGATION_CHANGED"),
    ("aggregation_version", "AGGREGATION_CHANGED"),
    ("seed", "SEED_CHANGED"),
    ("runner_version", "RUNNER_CHANGED"),
    ("environment_digest", "ENVIRONMENT_CHANGED"),
)


def _compare_invariants(
    baseline_manifest: dict[str, Any],
    candidate_manifest: dict[str, Any],
    policy: ComparisonPolicy,
) -> list[str]:
    """逐字段判定评测口径；缺失身份不默认相等（review R08）。"""
    reasons: list[str] = []
    if baseline_manifest.get("model") != candidate_manifest.get("model"):
        if "model" not in policy.allowed_factors:
            reasons.append(
                f"FACTOR_NOT_ALLOWED:model: {baseline_manifest.get('model')!r} -> "
                f"{candidate_manifest.get('model')!r}",
            )
    base_external = _external(baseline_manifest)
    cand_external = _external(candidate_manifest)
    base_profile = _profile(baseline_manifest)
    cand_profile = _profile(candidate_manifest)
    for invariant_field, code in _PROFILE_INVARIANTS:
        base_value = (
            base_profile.get(invariant_field, base_external.get(invariant_field))
            if invariant_field == "dataset_revision" else base_profile.get(invariant_field)
        )
        cand_value = (
            cand_profile.get(invariant_field, cand_external.get(invariant_field))
            if invariant_field == "dataset_revision" else cand_profile.get(invariant_field)
        )
        if (base_value is None) != (cand_value is None):
            # 缺失身份不默认相等（review R08）：一侧冻结、一侧没有 → 阻断。
            reasons.append(
                f"IDENTITY_MISSING:{invariant_field}: {base_value!r} vs {cand_value!r}"
            )
        elif base_value is not None and base_value != cand_value:
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
    return reasons


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
    reasons.extend(_compare_invariants(baseline_manifest, candidate_manifest, policy))

    base_cases = list(baseline_manifest.get("case_ids") or [])
    cand_cases = list(candidate_manifest.get("case_ids") or [])
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
    )
