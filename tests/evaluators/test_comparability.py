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


# ------------------------------------------------------------------ M4 runtime

def _runtime_manifest(runtime="claude-cli@1", native_model="model-a", **native_extra):
    manifest = _manifest()
    manifest.pop("model")
    manifest["runtime"] = runtime
    manifest["runtime_profile"] = {
        "runtime": runtime,
        "native_settings": {"model": native_model, **native_extra},
    }
    return manifest


def test_native_model_difference_is_visible_to_the_model_factor():
    # 原生模型在 runtime_profile.native_settings.model：模型因子可见（R16）。
    blocked = compare_run_reports(
        _ref("run-a"), _ref("run-b"),
        baseline_manifest=_runtime_manifest(native_model="model-a"),
        candidate_manifest=_runtime_manifest(native_model="model-b"),
        policy=ComparisonPolicy(allowed_factors=[]),
    )
    assert blocked.eligible is False
    assert any(reason.startswith("FACTOR_NOT_ALLOWED:model") for reason in blocked.reasons)


def test_model_factor_ignores_full_profile_hash_but_keeps_other_configuration():
    base = _runtime_manifest(native_model="model-a", max_turns=5)
    candidate = _runtime_manifest(native_model="model-b", max_turns=5)
    base["runtime_profile"]["config_hash"] = "sha256:" + "a" * 64
    candidate["runtime_profile"]["config_hash"] = "sha256:" + "b" * 64
    result = compare_run_reports(
        _ref("run-a"), _ref("run-b"),
        baseline_manifest=base, candidate_manifest=candidate,
        policy=ComparisonPolicy(allowed_factors=["model"]),
    )
    assert result.eligible is True
    candidate["runtime_profile"]["native_settings"]["max_turns"] = 6
    blocked = compare_run_reports(
        _ref("run-a"), _ref("run-b"),
        baseline_manifest=base, candidate_manifest=candidate,
        policy=ComparisonPolicy(allowed_factors=["model"]),
    )
    assert blocked.eligible is False


def test_runtime_change_blocks_comparison_unless_allowed():
    # 不同 runtime backend（Claude vs Codex）：即使 model 因子被允许，
    # runtime 条件不同仍阻断（M4 review R16）。
    result = compare_run_reports(
        _ref("run-a"), _ref("run-b"),
        baseline_manifest=_runtime_manifest(
            runtime="claude-cli@1", native_model="model-a",
        ),
        candidate_manifest=_runtime_manifest(
            runtime="codex-cli@1", native_model="model-b",
        ),
        policy=ComparisonPolicy(allowed_factors=["model"]),
    )
    assert result.eligible is False
    assert any(reason.startswith("RUNTIME_CHANGED:runtime") for reason in result.reasons)

    # 政策显式允许 runtime 因子：记录为可审计的允许差异。
    allowed = compare_run_reports(
        _ref("run-a"), _ref("run-b"),
        baseline_manifest=_runtime_manifest(
            runtime="claude-cli@1", native_model="model-a",
        ),
        candidate_manifest=_runtime_manifest(
            runtime="codex-cli@1", native_model="model-b",
        ),
        policy=ComparisonPolicy(allowed_factors=["model", "runtime"]),
    )
    assert allowed.eligible is True
    assert any(item.startswith("ALLOWED_FACTOR:runtime") for item in allowed.allowed_differences)


def test_runtime_config_difference_blocks_even_with_same_backend():
    # 同一 backend、不同原生配置/预算：runtime 条件不同 → 阻断（R16）。
    result = compare_run_reports(
        _ref("run-a"), _ref("run-b"),
        baseline_manifest=_runtime_manifest(max_turns=5),
        candidate_manifest=_runtime_manifest(max_turns=9),
        policy=ComparisonPolicy(allowed_factors=["model"]),
    )
    assert result.eligible is False
    assert any(reason.startswith("RUNTIME_CHANGED:runtime") for reason in result.reasons)


def test_runtime_vs_non_runtime_identity_missing_blocks():
    # 一侧 runtime 运行、一侧普通模型运行：身份缺失阻断，不默认相等。
    result = compare_run_reports(
        _ref("run-a"), _ref("run-b"),
        baseline_manifest=_runtime_manifest(),
        candidate_manifest=_manifest(),
        policy=ComparisonPolicy(allowed_factors=["model"]),
    )
    assert result.eligible is False
    assert any(reason.startswith("IDENTITY_MISSING:runtime") for reason in result.reasons)
