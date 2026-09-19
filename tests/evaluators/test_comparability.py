"""M6-T01 Lite：比较资格（固定 RunReportRef → ComparisonPolicy → 结果）。

反例与期望：
- 模型是允许变量时可比；同 case ID 改 gold/scorer 不可比。
- 只缺费用时质量可比、费用不可比（逐指标资格）。
- added/removed/changed case 逐项列出。
"""
import pytest

from motte_contracts.comparison import ComparisonPolicy, RunReportRef
from motte_eval.comparison import compare_run_reports


def _ref(run_id, **extra):
    return RunReportRef(
        run_id=run_id, scoring_pass_id=f"pass-{run_id}", report_schema="report-v1",
        evidence_hash="sha256:" + run_id, **extra,
    )


def _manifest(model="m1", dataset_rev="rev-1", case_ids=("c1", "c2"), extractor="e1"):
    return {
        "model": model,
        "external_benchmark": {
            "dataset_revision": dataset_rev,
            "profile": {
                "benchmark_id": "ceval", "benchmark_version": "1",
                "answer_extractor": extractor, "extractor_version": "1",
                "prompt_template_version": "p1",
            },
        },
        "case_ids": list(case_ids),
    }


def test_model_is_an_allowed_factor_for_comparison():
    result = compare_run_reports(
        _ref("run-a"), _ref("run-b"),
        baseline_manifest=_manifest(model="m1"),
        candidate_manifest=_manifest(model="m2"),
        policy=ComparisonPolicy(allowed_factors=["model"]),
        baseline_cost={"known": True, "total_usd": 1.0},
        candidate_cost={"known": True, "total_usd": 2.0},
    )
    assert result.eligible is True
    assert list(result.reasons) == []
    # 不把模型列入允许变量 → 不可比，原因具体。
    strict = compare_run_reports(
        _ref("run-a"), _ref("run-b"),
        baseline_manifest=_manifest(model="m1"),
        candidate_manifest=_manifest(model="m2"),
        policy=ComparisonPolicy(allowed_factors=[]),
    )
    assert strict.eligible is False
    assert any(reason.startswith("FACTOR_NOT_ALLOWED:model") for reason in strict.reasons)


def test_gold_or_scorer_change_breaks_comparability():
    base = _manifest()
    changed_gold = _manifest()
    changed_gold["external_benchmark"]["dataset_revision"] = "rev-2"
    result = compare_run_reports(
        _ref("run-a"), _ref("run-b"),
        baseline_manifest=base, candidate_manifest=changed_gold,
        policy=ComparisonPolicy(allowed_factors=["model"]),
    )
    assert result.eligible is False
    assert any("DATASET" in reason for reason in result.reasons)

    changed_extractor = _manifest(extractor="e2")
    quality = compare_run_reports(
        _ref("run-a"), _ref("run-b"),
        baseline_manifest=base, candidate_manifest=changed_extractor,
        policy=ComparisonPolicy(allowed_factors=["model"]),
    )
    assert quality.eligible is False
    assert any("extractor" in reason for reason in quality.reasons)


def test_added_removed_changed_cases_are_listed():
    result = compare_run_reports(
        _ref("run-a"), _ref("run-b"),
        baseline_manifest=_manifest(case_ids=("c1", "c2", "c3")),
        candidate_manifest=_manifest(case_ids=("c2", "c3", "c4")),
        policy=ComparisonPolicy(allowed_factors=["model"]),
    )
    assert result.eligible is False
    assert result.case_diff == {
        "added": ["c4"], "removed": ["c1"], "changed": [],
    }


def test_cost_comparability_is_per_metric():
    result = compare_run_reports(
        _ref("run-a"), _ref("run-b"),
        baseline_manifest=_manifest(), candidate_manifest=_manifest(model="m2"),
        policy=ComparisonPolicy(allowed_factors=["model"]),
        baseline_cost={"known": True, "total_usd": 1.0},
        candidate_cost={"known": False},
    )
    # 质量可比，费用不可比。
    assert result.eligible is True
    assert result.metric_eligibility["quality"] is True
    assert result.metric_eligibility["cost"] is False
    assert any(reason.startswith("COST_UNKNOWN") for reason in result.reasons)


def test_policy_rejects_unknown_factor_names():
    with pytest.raises(ValueError):
        ComparisonPolicy(allowed_factors=["model", "not-a-factor"])
