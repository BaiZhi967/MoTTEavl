"""M6-T07 回归分类（纯函数）：基线 vs 候选在**共同稳定样本集**上的逐 Case 差异。

依据 docs/protocols/experiments-and-comparison.md §1/§4：Case 按
(dataset source/revision, stable_case_key) 的内容对齐，不按名字；Baseline
固定 RunReportRef 引用，不受 current pass 漂移影响。本模块只消费已持久化的
Case 结果，零 Provider / Judge / Runner 调用，重复调用结论确定。

原则（A10 / G14，§2 真值表）：

- 回归 / 修复只在两侧**共同**样本上判定；只在候选 = added_case、只在基线 =
  removed_case，独立列出，绝不被误报成 fixed / new_failure。
- 同一 Case 多次 Trial 混合 pass/fail → instability，交统计政策处理，
  **不只保留最好的一次**（operator retry 不静默消失，A10/T07）。
- unknown 不是成功：任一侧 unknown → changed_unknown；缺失/未知/失败永不
  从分母静默删除。
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

#: 单次 Case 结果。unknown = 结果未知（服务端无结论），不是成功。
CaseOutcome = str  # "pass" | "fail" | "unknown"

#: 同一 Case 多 Trial 聚合后的状态；instability 表示混合了 pass 与 fail。
AggregatedOutcome = str  # "pass" | "fail" | "unknown" | "instability"

#: 八个互斥回归分类（M6-T07 冻结集合）。
REGRESSION_CLASSES: tuple[str, ...] = (
    "new_failure",
    "fixed",
    "persistent_failure",
    "persistent_pass",
    "changed_unknown",
    "added_case",
    "removed_case",
    "instability",
)

_DEFINITIVE_OUTCOMES = ("pass", "fail")

#: 视为"结果发生变化"的分类。instability 有专门的 ``unstable_cases`` 清单，
#: 不重复计入 ``changed``（G14：每类差异只在一处可查）。
_CHANGED_CLASSES = frozenset({"new_failure", "fixed", "changed_unknown"})


def _normalize_outcome(value: Any) -> CaseOutcome:
    """未知取值一律按 unknown 处理（fail-closed）：不认识的值绝不是 pass。"""
    return value if value in _DEFINITIVE_OUTCOMES else "unknown"


def _classify_pair(base: AggregatedOutcome, cand: AggregatedOutcome) -> str:
    """共同样本的 (基线, 候选) 聚合状态 → 互斥分类。

    instability 优先于 changed_unknown（不稳定本身就是需要显式处理的结论）；
    其余组合里任一侧非确定结果（unknown）→ changed_unknown。
    """
    if base == "instability" or cand == "instability":
        return "instability"
    if base not in _DEFINITIVE_OUTCOMES or cand not in _DEFINITIVE_OUTCOMES:
        return "changed_unknown"
    if base == cand:
        return "persistent_pass" if base == "pass" else "persistent_failure"
    return "new_failure" if base == "pass" else "fixed"


def _classify_aligned(
    baseline: Mapping[str, AggregatedOutcome],
    candidate: Mapping[str, AggregatedOutcome],
) -> dict[str, Any]:
    """已对齐（单结果或聚合后）的两份 case→outcome → 分类结果。"""
    common = sorted(set(baseline) & set(candidate))
    added = sorted(set(candidate) - set(baseline))
    removed = sorted(set(baseline) - set(candidate))
    classification: dict[str, str] = {
        case_id: _classify_pair(baseline[case_id], candidate[case_id])
        for case_id in common
    }
    for case_id in added:
        classification[case_id] = "added_case"
    for case_id in removed:
        classification[case_id] = "removed_case"
    counts = {name: 0 for name in REGRESSION_CLASSES}
    for outcome_class in classification.values():
        counts[outcome_class] += 1
    return {
        "classification": classification,
        "counts": counts,
        "common": common,
        "added": added,
        "removed": removed,
        "changed": sorted(
            case_id for case_id, outcome_class in classification.items()
            if outcome_class in _CHANGED_CLASSES
        ),
        "total_baseline": len(baseline),
        "total_candidate": len(candidate),
    }


def classify_cases(
    baseline: Mapping[str, CaseOutcome],
    candidate: Mapping[str, CaseOutcome],
) -> dict[str, Any]:
    """单结果形态：case→outcome 的两份固定报告 → 逐 Case 回归分类。

    返回 ``{"classification": {case_id: class}, "counts": {class: int}
    (8 类全键恒存在), "common" / "added" / "removed" / "changed": [case_id]
    (排序), "total_baseline", "total_candidate"}``。``changed`` =
    new_failure + fixed + changed_unknown。空输入安全。
    """
    return _classify_aligned(
        {case_id: _normalize_outcome(value) for case_id, value in baseline.items()},
        {case_id: _normalize_outcome(value) for case_id, value in candidate.items()},
    )


def _aggregate_trials(outcomes: Any) -> AggregatedOutcome:
    """同一 Case 的多次 Trial → 单一状态（A10：不只保留最好的一次）。

    - 全部 pass → pass；全部 fail → fail
    - 混合 pass/fail → instability（交统计政策处理，绝不挑最好的一次冒充稳定结论）
    - 含 unknown 且未混合 pass/fail → unknown（结果未知不是成功）
    - 空列表 → unknown；非列表取值按 unknown 处理（fail-closed）
    """
    if not isinstance(outcomes, (list, tuple)) or not outcomes:
        return "unknown"
    has_pass = any(value == "pass" for value in outcomes)
    has_fail = any(value == "fail" for value in outcomes)
    if has_pass and has_fail:
        return "instability"
    if not has_pass and not has_fail:
        return "unknown"
    definitive = "pass" if has_pass else "fail"
    if all(value == definitive for value in outcomes):
        return definitive
    return "unknown"


def classify_with_trials(
    baseline: Mapping[str, Sequence[CaseOutcome]],
    candidate: Mapping[str, Sequence[CaseOutcome]],
) -> dict[str, Any]:
    """多 Trial 形态：case→list[outcome] 先聚合再分类，语义同 :func:`classify_cases`。

    instability 在共同集分类里优先于 changed_unknown（任一侧聚合为
    instability 即分类 instability）；added/removed 仍独立分类。返回结构同
    :func:`classify_cases`，另加 ``"unstable_cases"``：任一侧聚合不稳定的
    case 全集（含 added/removed 里的不稳定样本——分类保持互斥，该清单是
    叠加的诊断视图）。
    """
    aggregated_baseline = {
        case_id: _aggregate_trials(value) for case_id, value in baseline.items()
    }
    aggregated_candidate = {
        case_id: _aggregate_trials(value) for case_id, value in candidate.items()
    }
    result = _classify_aligned(aggregated_baseline, aggregated_candidate)
    result["unstable_cases"] = sorted(
        case_id for case_id in set(baseline) | set(candidate)
        if aggregated_baseline.get(case_id) == "instability"
        or aggregated_candidate.get(case_id) == "instability"
    )
    return result


def regression_report(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """统一入口：自动检测输入形态（value 是 list → 多 Trial 聚合，否则单结果）。

    混合形态（一侧单结果、一侧列表）按多 Trial 路径处理，单结果取值视为
    一次 Trial。返回分类结果（见 :func:`classify_cases` /
    :func:`classify_with_trials`），另加：

    - ``has_regressions``：new_failure / removed_case / instability 任一非空
    - ``has_improvements``：fixed 非空
    """
    trials_mode = any(
        isinstance(value, (list, tuple))
        for value in [*baseline.values(), *candidate.values()]
    )
    if trials_mode:
        result = classify_with_trials(
            {
                case_id: value if isinstance(value, (list, tuple)) else [value]
                for case_id, value in baseline.items()
            },
            {
                case_id: value if isinstance(value, (list, tuple)) else [value]
                for case_id, value in candidate.items()
            },
        )
    else:
        result = classify_cases(baseline, candidate)
    counts = result["counts"]
    result["has_regressions"] = (
        counts["new_failure"] > 0
        or counts["removed_case"] > 0
        or counts["instability"] > 0
    )
    result["has_improvements"] = counts["fixed"] > 0
    return result


def _numeric(value: Any) -> float | None:
    """None / NaN / 非数值（bool 不算）→ None（不可比）；其余转 float。

    §2 真值表：NaN / Infinity / null 不产生结论，绝不折算成 0。
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = float(value)
    return None if math.isnan(number) else number


def summary_delta(
    baseline_summary: Mapping[str, float | None],
    candidate_summary: Mapping[str, float | None],
    metrics: tuple[str, ...],
) -> dict[str, Any]:
    """逐指标差值（candidate - baseline）。

    返回 ``{"deltas": {metric: delta | None}, "applicable": {metric: bool},
    "improved": [...], "regressed": [...]}``：

    - improved = delta > 0、regressed = delta < 0，**只按符号记录**；方向
      语义（哪个符号是"变好"）由 metric registry 的 direction（gte/lte）
      决定，本函数不做好坏判断——cost 上升与 accuracy 上升同记 improved。
    - 任一侧缺失 / None / NaN → 该指标即 ``{"delta": None,
      "applicable": False}`` 语义：deltas 记 None、applicable 记 False、
      不进 improved / regressed。
    - delta == 0 视为未变化，两侧皆不进。
    """
    deltas: dict[str, float | None] = {}
    applicable: dict[str, bool] = {}
    improved: list[str] = []
    regressed: list[str] = []
    for metric in metrics:
        base = _numeric(baseline_summary.get(metric))
        cand = _numeric(candidate_summary.get(metric))
        if base is None or cand is None:
            deltas[metric] = None
            applicable[metric] = False
            continue
        delta = cand - base
        deltas[metric] = delta
        applicable[metric] = True
        if delta > 0:
            improved.append(metric)
        elif delta < 0:
            regressed.append(metric)
    return {
        "deltas": deltas,
        "applicable": applicable,
        "improved": improved,
        "regressed": regressed,
    }
