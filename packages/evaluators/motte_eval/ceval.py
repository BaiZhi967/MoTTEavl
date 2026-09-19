"""C-Eval 诊断指标（M2-T06）。

只做平台侧的确定性重算与覆盖统计；Runner 原始聚合（native.*）原样
透传，缺输入不编造。诊断口径：selected-case accuracy = correct /
selected（分母是选择集，不是作答集）；observed-call coverage =
attempted / selected。无 gold 只产出 unscored 与预测工件语义，不伪造
正确率。原始聚合与重算不一致时记录 discrepancy 与 extractor 版本，
不相互覆盖（M2-A11）。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

SCORER_VERSION = "ceval-diagnostic@1"
EXTRACTOR_NOTE = "extractor=conclusion-first@b661bcdf-migrated"


def diagnostic_metrics(
    *,
    selected_case_ids: Sequence[str],
    records: Sequence[Mapping[str, Any]],
    native_aggregate: Mapping[str, float] | None = None,
    runner_exit_code: int | None = None,
) -> dict[str, Any]:
    """由 parser 的样本记录 + 选择集计算诊断指标与覆盖。

    records 元素形如 ``{"case_id", "prediction", "gold"|None}``；selected
    中没有记录出现的 case 记 not_attempted（不消失）。
    """
    selected = list(selected_case_ids)
    by_case: dict[str, Mapping[str, Any]] = {}
    for record in records:
        case_id = str(record.get("case_id") or record.get("source_case_id") or "")
        if case_id:
            by_case[case_id] = record
    attempted_ids = [case_id for case_id in selected if case_id in by_case]
    not_attempted = [case_id for case_id in selected if case_id not in by_case]

    gold_seen = False
    gold_missing = False
    correct = 0
    wrong = 0
    empty_predictions = 0
    for case_id in attempted_ids:
        record = by_case[case_id]
        prediction = str(record.get("prediction") or "").strip()
        if not prediction:
            empty_predictions += 1
        gold = record.get("gold")
        if gold is None or str(gold).strip() == "":
            gold_missing = True
            continue
        gold_seen = True
        if prediction and prediction.upper() == str(gold).strip().upper():
            correct += 1
        else:
            wrong += 1
    attempted = len(attempted_ids)
    unscored = selected and not gold_seen and (gold_missing or attempted == 0)

    metrics: dict[str, Any] = {
        "scorer_version": SCORER_VERSION,
        "extractor": EXTRACTOR_NOTE,
        "selected": len(selected),
        "attempted": attempted,
        "correct": correct,
        "wrong": wrong,
        "not_attempted": len(not_attempted),
        "not_attempted_case_ids": not_attempted,
        "empty_prediction_count": empty_predictions,
        "empty_prediction_ratio": (
            round(empty_predictions / attempted, 4) if attempted else 0.0
        ),
        "unscored": bool(unscored),
        "execution": {
            "failed": runner_exit_code not in (0, None),
            "runner_exit_code": runner_exit_code,
        },
        "diagnostic.selected_case_accuracy": (
            round(correct / len(selected), 6) if selected and gold_seen and not unscored else None
        ),
        "diagnostic.observed_call_coverage": (
            round(attempted / len(selected), 6) if selected else None
        ),
    }

    for key, value in (native_aggregate or {}).items():
        metrics[f"native.{key}"] = value

    if native_aggregate and metrics["diagnostic.selected_case_accuracy"] is not None:
        native_accuracy = native_aggregate.get("accuracy")
        if native_accuracy is not None and native_accuracy != metrics[
            "diagnostic.selected_case_accuracy"
        ]:
            metrics["discrepancy"] = {
                "accuracy": {
                    "native": native_accuracy,
                    "diagnostic": metrics["diagnostic.selected_case_accuracy"],
                    "reason": (
                        f"{EXTRACTOR_NOTE}; "
                        "native aggregate and sample-level recomputation differ"
                    ),
                },
            }
    return metrics
