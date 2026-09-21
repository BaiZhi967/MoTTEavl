"""M6-T07 回归分类：共同稳定样本集上的逐 Case 差异（行为测试）。

反例与期望（A10/G14，docs/protocols/experiments-and-comparison.md §1/§4）：
- added/removed 独立列出，绝不冒充 fixed / new_failure。
- 同 Case 多 Trial 混合 pass/fail → instability，不只保留最好的一次。
- instability 优先于 changed_unknown；unknown 不是成功。
- 空输入不崩溃；counts 恒有全部 8 类键。
"""
import pytest

from motte_eval.regression import (
    REGRESSION_CLASSES,
    classify_cases,
    classify_with_trials,
    regression_report,
    summary_delta,
)


def test_classify_cases_covers_all_common_combinations():
    baseline = {
        "c_new_failure": "pass",
        "c_fixed": "fail",
        "c_persist_fail": "fail",
        "c_persist_pass": "pass",
        "c_base_unknown": "unknown",
        "c_cand_unknown": "pass",
        "c_both_unknown": "unknown",
    }
    candidate = {
        "c_new_failure": "fail",
        "c_fixed": "pass",
        "c_persist_fail": "fail",
        "c_persist_pass": "pass",
        "c_base_unknown": "pass",
        "c_cand_unknown": "unknown",
        "c_both_unknown": "unknown",
    }
    result = classify_cases(baseline, candidate)
    assert result["classification"] == {
        "c_new_failure": "new_failure",
        "c_fixed": "fixed",
        "c_persist_fail": "persistent_failure",
        "c_persist_pass": "persistent_pass",
        # 任一侧 unknown（含两侧都 unknown）→ changed_unknown；unknown 不是成功。
        "c_base_unknown": "changed_unknown",
        "c_cand_unknown": "changed_unknown",
        "c_both_unknown": "changed_unknown",
    }
    assert result["counts"] == {
        "new_failure": 1,
        "fixed": 1,
        "persistent_failure": 1,
        "persistent_pass": 1,
        "changed_unknown": 3,
        "added_case": 0,
        "removed_case": 0,
        "instability": 0,
    }
    assert set(result["common"]) == set(baseline)
    assert result["added"] == []
    assert result["removed"] == []
    # changed = new_failure + fixed + changed_unknown（结果发生变化的 case）。
    assert result["changed"] == [
        "c_base_unknown", "c_both_unknown", "c_cand_unknown", "c_fixed", "c_new_failure",
    ]
    assert result["total_baseline"] == 7
    assert result["total_candidate"] == 7


def test_added_and_removed_cases_are_independent_not_fake_regressions():
    # A10：共同集之外的样本独立列出，绝不被误报成 fixed / new_failure。
    baseline = {"c_kept": "pass", "c_removed": "pass"}
    candidate = {"c_kept": "pass", "c_added": "fail"}
    result = classify_cases(baseline, candidate)
    assert result["classification"]["c_added"] == "added_case"
    assert result["classification"]["c_removed"] == "removed_case"
    assert result["classification"]["c_kept"] == "persistent_pass"
    assert result["added"] == ["c_added"]
    assert result["removed"] == ["c_removed"]
    assert result["counts"]["new_failure"] == 0
    assert result["counts"]["fixed"] == 0
    assert result["counts"]["added_case"] == 1
    assert result["counts"]["removed_case"] == 1
    assert result["changed"] == []
    assert result["total_baseline"] == 2
    assert result["total_candidate"] == 2


def test_trials_aggregation_rules():
    # 聚合规则逐条验证（对侧固定为单一确定结果，让聚合直接决定分类）：
    # 混合 → instability；全 pass → pass；全 fail → fail；
    # 含 unknown 且未混合 → unknown；空列表 → unknown。
    result = classify_with_trials(
        baseline={
            "c_mixed": ["pass"],
            "c_all_pass": ["fail"],
            "c_all_fail": ["pass"],
            "c_with_unknown": ["pass"],
            "c_empty": ["pass"],
            "c_all_unknown": ["pass"],
        },
        candidate={
            "c_mixed": ["pass", "fail", "pass"],
            "c_all_pass": ["pass", "pass"],
            "c_all_fail": ["fail", "fail"],
            "c_with_unknown": ["fail", "unknown"],
            "c_empty": [],
            "c_all_unknown": ["unknown", "unknown"],
        },
    )
    assert result["classification"] == {
        "c_mixed": "instability",
        "c_all_pass": "fixed",
        "c_all_fail": "new_failure",
        "c_with_unknown": "changed_unknown",
        "c_empty": "changed_unknown",
        "c_all_unknown": "changed_unknown",
    }
    # 不稳定样本逐项可见，不只保留最好的一次（A10/T07）。
    assert result["unstable_cases"] == ["c_mixed"]


def test_instability_precedes_changed_unknown_and_keeps_added_removed_independent():
    result = classify_with_trials(
        baseline={
            "c_unstable_vs_unknown": ["pass", "fail"],  # instability 侧
            "c_removed": ["pass", "fail"],  # 只在 baseline 且不稳定
        },
        candidate={
            "c_unstable_vs_unknown": ["unknown"],  # 聚合为 unknown
            "c_added": ["pass", "fail"],  # 只在 candidate 且不稳定
        },
    )
    # 任一侧 instability → instability，优先于 changed_unknown。
    assert result["classification"]["c_unstable_vs_unknown"] == "instability"
    assert result["counts"]["changed_unknown"] == 0
    # added/removed 仍独立分类，不被 instability 吞并。
    assert result["classification"]["c_added"] == "added_case"
    assert result["classification"]["c_removed"] == "removed_case"
    assert result["added"] == ["c_added"]
    assert result["removed"] == ["c_removed"]
    # unstable_cases 是诊断清单：任一侧聚合不稳定的 case 都列出（含 added/removed）。
    assert result["unstable_cases"] == ["c_added", "c_removed", "c_unstable_vs_unknown"]
    # instability 不计入 changed（有自己的 unstable_cases 清单）。
    assert result["changed"] == []


def test_both_sides_instability_is_instability():
    result = classify_with_trials(
        baseline={"c": ["pass", "fail"]},
        candidate={"c": ["pass", "fail"]},
    )
    assert result["classification"]["c"] == "instability"
    assert result["counts"]["instability"] == 1


def test_single_trial_lists_behave_like_plain_outcomes():
    plain = classify_cases({"c": "pass"}, {"c": "fail"})
    trials = classify_with_trials({"c": ["pass"]}, {"c": ["fail"]})
    assert trials["classification"] == plain["classification"] == {"c": "new_failure"}
    assert trials["counts"] == plain["counts"]
    assert trials["unstable_cases"] == []


@pytest.mark.parametrize(
    "baseline,candidate,regressions,improvements",
    [
        # 只有 fixed → 有改善、无回归。
        ({"c": "fail"}, {"c": "pass"}, False, True),
        # new_failure / removed_case / instability 各自独立触发回归。
        ({"c": "pass"}, {"c": "fail"}, True, False),
        ({"c": "pass"}, {}, True, False),
        ({"c": ["pass"]}, {"c": ["pass", "fail"]}, True, False),
        # 纯 persistent / unknown / added：两者皆否。
        ({"c": "pass"}, {"c": "pass"}, False, False),
        ({"c": "fail"}, {"c": "fail"}, False, False),
        ({"c": "pass"}, {"c": "unknown"}, False, False),
        ({}, {"c": "pass"}, False, False),
    ],
)
def test_regression_report_truth_table(baseline, candidate, regressions, improvements):
    report = regression_report(baseline, candidate)
    assert report["has_regressions"] is regressions
    assert report["has_improvements"] is improvements


def test_regression_report_autodetects_trials_and_plain_shapes():
    plain = regression_report({"c": "pass"}, {"c": "fail"})
    assert plain["classification"]["c"] == "new_failure"
    assert "unstable_cases" not in plain
    assert plain["has_regressions"] is True

    trials = regression_report({"c": ["pass"]}, {"c": ["pass", "fail"]})
    assert trials["classification"]["c"] == "instability"
    assert trials["unstable_cases"] == ["c"]
    assert trials["has_regressions"] is True


def test_empty_inputs_do_not_crash_and_counts_cover_all_classes():
    assert len(REGRESSION_CLASSES) == 8
    for result in (
        classify_cases({}, {}),
        classify_with_trials({}, {}),
        regression_report({}, {}),
    ):
        assert result["classification"] == {}
        assert set(result["counts"]) == set(REGRESSION_CLASSES)
        assert result["counts"] == {name: 0 for name in REGRESSION_CLASSES}
        assert result["common"] == []
        assert result["added"] == []
        assert result["removed"] == []
        assert result["changed"] == []
        assert result["total_baseline"] == 0
        assert result["total_candidate"] == 0
    assert classify_with_trials({}, {})["unstable_cases"] == []
    assert regression_report({}, {})["has_regressions"] is False
    assert regression_report({}, {})["has_improvements"] is False


def test_summary_delta_computes_numeric_differences_by_sign():
    result = summary_delta(
        {"accuracy": 0.80, "cost.total_usd": 1.5, "latency_p50": 10.0},
        {"accuracy": 0.85, "cost.total_usd": 2.0, "latency_p50": 10.0},
        metrics=("accuracy", "cost.total_usd", "latency_p50"),
    )
    assert result["deltas"] == {
        "accuracy": pytest.approx(0.05),
        "cost.total_usd": pytest.approx(0.5),
        "latency_p50": 0.0,
    }
    # 只按 delta 符号记录；好坏方向由 metric registry 决定（cost 上升同样记 improved）。
    assert result["improved"] == ["accuracy", "cost.total_usd"]
    assert result["regressed"] == []
    assert result["applicable"] == {
        "accuracy": True, "cost.total_usd": True, "latency_p50": True,
    }


def test_summary_delta_missing_side_is_not_applicable():
    result = summary_delta(
        {"accuracy": 0.8, "cost.total_usd": 1.0},
        {"accuracy": None, "cost.total_usd": 1.2},
        metrics=("accuracy", "cost.total_usd", "absent_metric"),
    )
    # 任一侧 None（或键缺失）→ delta None、不可比、不进 improved/regressed。
    assert result["deltas"]["accuracy"] is None
    assert result["deltas"]["absent_metric"] is None
    assert result["deltas"]["cost.total_usd"] == pytest.approx(0.2)
    assert result["applicable"]["accuracy"] is False
    assert result["applicable"]["absent_metric"] is False
    assert result["applicable"]["cost.total_usd"] is True
    assert result["improved"] == ["cost.total_usd"]
    assert result["regressed"] == []
