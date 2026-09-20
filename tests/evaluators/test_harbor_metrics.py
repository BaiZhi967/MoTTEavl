"""M3-T08：Trial/Task 两层评分、覆盖门禁与成本（M3-A12/A15，需求第 6/7 节）。"""
from __future__ import annotations

import pytest

from motte_eval.harbor import (
    TRIAL_EVALUATOR_ID,
    TRIAL_EVALUATOR_VERSION,
    TRIAL_METRIC_ID,
    cost_summary,
    coverage_gate,
    duration_summary,
    rescore_identity,
    task_aggregate,
    trial_scores,
)


def _trial(
    index: int, reward: float | None, *, status: str = "scored",
    disposition: str = "succeeded", task_key: str = "task-a", cost: float | None = None,
) -> dict[str, object]:
    return {
        "trial_id": f"trial-{task_key}-{index}",
        "task_key": task_key,
        "repeat_index": index,
        "disposition": disposition,
        "verifier_observation": {
            "status": status,
            "rewards": {} if reward is None else {"reward": reward},
        },
        "usage": {"cost_usd": cost, "coverage": "observed" if cost is not None else "unavailable"},
        "termination": {
            "timings": {
                "environment_setup_sec": 1.5 if index == 0 else None,
                "agent_execution_sec": 2.0 + index,
                "verifier_sec": 0.5,
                "total_sec": 4.0 + index,
            },
        },
        "parser_version": "harbor-terminal-bench-parser@1",
    }


def test_trial_quality_and_coverage_are_separate() -> None:
    """三 Trial 1/0/verifier_timeout：通过率 1/2、覆盖 2/3，完整覆盖 Gate 不通过。"""
    trials = [
        _trial(0, 1.0),
        _trial(1, 0.0, disposition="failed"),
        _trial(2, None, status="verifier_error", disposition="indeterminate"),
    ]
    aggregate = task_aggregate(trials=trials)

    assert aggregate["valid_trial_pass_rate"] == 0.5
    assert aggregate["valid_trial_coverage"] == pytest.approx(2 / 3, abs=1e-6)
    assert aggregate["selected_trials"] == 3
    assert aggregate["valid_trials"] == 2
    assert aggregate["invalid_trials"] == 1
    assert aggregate["unit"] == "trial"
    assert aggregate["denominator"] == "planned_trials"

    gate = coverage_gate(aggregate)
    assert gate["passed"] is False
    assert any("INSUFFICIENT_EVIDENCE" in reason for reason in gate["reasons"])
    # 覆盖率下降这件事必须可见，不能只显示 50%。
    assert aggregate["per_task"]["task-a"]["invalid_trials"] == 1
    assert aggregate["per_task"]["task-a"]["task_pass"] is True  # first-trial 规则


def test_reward_zero_is_a_valid_failure() -> None:
    """reward=0 是有效失败：进分母、判负，不当缺失。"""
    rows = trial_scores([_trial(0, 0.0, disposition="failed")])
    assert rows[0]["value"] == 0.0
    assert rows[0]["passed"] is False
    assert rows[0]["judged"] is True
    assert rows[0]["reason"] == "scored"
    aggregate = task_aggregate(trials=[_trial(0, 0.0, disposition="failed")])
    assert aggregate["valid_trial_pass_rate"] == 0.0
    assert aggregate["valid_trial_coverage"] == 1.0
    assert coverage_gate(aggregate)["passed"] is True


def test_missing_evidence_never_becomes_a_pass_or_zero() -> None:
    """缺证据既不通过也不当 0：覆盖率下降，Gate 拒绝。"""
    trials = [
        _trial(0, 1.0),
        _trial(1, None, status="missing_verifier_evidence", disposition="not_attempted"),
    ]
    aggregate = task_aggregate(trials=trials)
    assert aggregate["valid_trials"] == 1
    assert aggregate["valid_trial_coverage"] == 0.5
    assert aggregate["valid_trial_pass_rate"] == 1.0
    assert coverage_gate(aggregate)["passed"] is False
    rows = trial_scores(trials)
    assert rows[1]["value"] is None and rows[1]["passed"] is None


def test_aggregation_policy_is_fixed_upfront() -> None:
    """first-trial 与 mean-success 事前固定；无有效 Trial 时返回不适用。"""
    trials = [_trial(0, 0.0, disposition="failed"), _trial(1, 1.0)]
    first = task_aggregate(trials=trials, aggregation="first-trial")
    mean = task_aggregate(trials=trials, aggregation="mean-success")
    assert first["per_task"]["task-a"]["task_pass"] is False
    assert mean["per_task"]["task-a"]["task_pass"] is True
    assert "first-trial" in first["per_task"]["task-a"]["task_pass_reason"]

    with pytest.raises(ValueError, match="unknown aggregation policy"):
        task_aggregate(trials=trials, aggregation="best-of")

    empty = task_aggregate(
        trials=[_trial(0, None, status="verifier_error", disposition="indeterminate")],
    )
    assert empty["valid_trial_pass_rate"] is None
    assert empty["per_task"]["task-a"]["task_pass"] is None
    assert empty["per_task"]["task-a"]["task_pass_reason"] == "no_valid_trial"


def test_planned_denominator_covers_units_that_never_reported() -> None:
    """计划里存在但完全没产出的 Trial 仍在分母里（覆盖可见）。"""
    trials = [_trial(0, 1.0)]
    aggregate = task_aggregate(
        trials=trials, planned_per_task={"task-a": 3},
    )
    assert aggregate["selected_trials"] == 3
    assert aggregate["valid_trials"] == 1
    assert aggregate["valid_trial_coverage"] == pytest.approx(1 / 3, abs=1e-6)
    assert aggregate["per_task"]["task-a"]["observed_trials"] == 1
    assert aggregate["per_task"]["task-a"]["planned_trials"] == 3


def test_cost_keeps_unknown_null_and_zero_success_na() -> None:
    """成本：未知保持 null 并计数；零成功时单位成本不适用（M3-A12）。"""
    trials = [
        _trial(0, 1.0, cost=0.5),
        _trial(1, 0.0, disposition="failed", cost=0.25),
        _trial(2, None, status="missing_verifier_evidence", disposition="not_attempted"),
    ]
    summary = cost_summary(trials, price_table_version="harbor-native@1")
    assert summary["known_cost_usd"] == 0.75
    assert summary["known_trials"] == 2
    assert summary["unknown_trials"] == 1
    assert summary["unknown_cost"] is True
    assert summary["per_success_usd"] == 0.75, "失败 Trial 的费用计入分子"
    assert summary["includes_failed_trials_in_numerator"] is True
    assert summary["price_table_version"] == "harbor-native@1"

    failing = cost_summary([_trial(0, 0.0, disposition="failed", cost=0.5)])
    assert failing["known_cost_usd"] == 0.5
    assert failing["per_success_usd"] is None, "零成功时单位成本不适用，不是 0"

    unknown_only = cost_summary([_trial(0, 1.0)])
    assert unknown_only["known_cost_usd"] is None
    assert unknown_only["unknown_trials"] == 1


def test_durations_keep_phases_separate() -> None:
    """时长分层：Agent/Verifier/环境分开，容器启动时间不冒充模型延迟。"""
    summary = duration_summary([_trial(0, 1.0), _trial(1, 0.0, disposition="failed")])
    assert summary["agent_execution_sec"]["observed_trials"] == 2
    assert summary["agent_execution_sec"]["total_sec"] == 5.0
    assert summary["environment_setup_sec"]["observed_trials"] == 1
    assert summary["environment_setup_sec"]["total_sec"] == 1.5
    assert summary["verifier_sec"]["observed_trials"] == 2


def test_rescore_identity_does_not_include_task_bytes() -> None:
    """重评分条件包含任务集合与聚合规则，供比较时解释差异（M3-A15）。"""
    trials = [_trial(0, 1.0, task_key="task-a"), _trial(0, 0.0, task_key="task-b")]
    aggregate = task_aggregate(trials=trials)
    identity = rescore_identity(
        aggregate=aggregate, trials=trials, parser_version="harbor-terminal-bench-parser@1",
    )
    assert identity["task_keys"] == ["task-a", "task-b"]
    assert identity["task_set_hash"].startswith("sha256:")
    assert identity["aggregation"] == "first-trial"
    assert identity["evaluator_version"] == TRIAL_EVALUATOR_VERSION
    assert identity["planned_trials"] == 2


def test_trial_rows_carry_the_trial_identity() -> None:
    """Trial 分数行带 trial_id 维度，复用 ScoreSet 的复合键。"""
    rows = trial_scores([
        _trial(0, 1.0, task_key="shared"), _trial(1, 0.0, task_key="shared",
                                                  disposition="failed"),
    ])
    assert {row["metric_id"] for row in rows} == {TRIAL_METRIC_ID}
    assert {row["evaluator_id"] for row in rows} == {TRIAL_EVALUATOR_ID}
    assert len({row["trial_id"] for row in rows}) == 2
    assert {row["case_id"] for row in rows} == {"shared"}
