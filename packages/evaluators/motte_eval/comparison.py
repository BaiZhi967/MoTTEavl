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

    # 逐条件：允许因子之外的差异都阻断整体资格。
    if baseline_manifest.get("model") != candidate_manifest.get("model"):
        if "model" not in policy.allowed_factors:
            reasons.append(
                f"FACTOR_NOT_ALLOWED:model: {baseline_manifest.get('model')!r} -> "
                f"{candidate_manifest.get('model')!r}",
            )
    base_external = _external(baseline_manifest)
    cand_external = _external(candidate_manifest)
    if base_external.get("dataset_revision") != cand_external.get("dataset_revision"):
        reasons.append(
            "DATASET_REVISION_CHANGED: "
            f"{base_external.get('dataset_revision')!r} -> "
            f"{cand_external.get('dataset_revision')!r}",
        )
    base_profile = _profile(baseline_manifest)
    cand_profile = _profile(candidate_manifest)
    for key in ("answer_extractor", "extractor_version", "prompt_template_version"):
        if base_profile.get(key) != cand_profile.get(key):
            reasons.append(
                f"EXTRACTOR_OR_PROMPT_CHANGED:{key}: "
                f"{base_profile.get(key)!r} -> {cand_profile.get(key)!r}",
            )

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
        case_diff={"added": added, "removed": removed, "changed": changed},
    )
