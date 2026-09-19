"""M6-T03 Lite：固定 baseline/候选/政策 → Gate 纯求值。

反例与期望：
- 10 选 8 判全对：阈值达标但覆盖不足 → 仍 insufficient。
- unknown cost 不通过硬成本门。
- 换 current pass 不改变 baseline 快照结论。
- 重复 evaluate 结论确定且 0 模型/Judge 调用；evaluated_at 是审计元数据。
"""
import pytest

from motte_eval.coverage import coverage_summary
from motte_eval.gates import evaluate_gate
from motte_storage.baselines import MemoryBaselines


def _candidate(selected=10, judged=8, scored=8, metric_value=1.0, cost=None):
    return coverage_summary(
        denominator="selected_cases", selected=selected, judged=judged,
        scored=scored, metric_value=metric_value, cost=cost,
    )


POLICY = {
    "metric": "accuracy", "op": "gte", "threshold": 0.8,
    "required_coverage": 1.0, "require_cost_known": True, "require_comparable": True,
}


def test_gate_lite_pinned_pass_and_insufficient_evidence():
    result = evaluate_gate(POLICY, _candidate(metric_value=1.0, cost={"known": True}))
    # 阈值达标 + 费用已知，但 10 选 8 判 → 覆盖不足，整体不通过。
    assert result["passed"] is False
    by_id = {rule["id"]: rule for rule in result["rules"]}
    assert by_id["metric_threshold"]["passed"] is True
    assert by_id["coverage"]["passed"] is False
    assert "coverage" in by_id["coverage"]["reason"]

    full = evaluate_gate(
        POLICY, _candidate(
            judged=10, scored=10, metric_value=0.9,
            cost={"known": True, "total_usd": 1.0},
        ),
        comparison={"eligible": True, "reasons": []},
    )
    assert full["passed"] is True

    unknown_cost = evaluate_gate(POLICY, _candidate(
        judged=10, scored=10, metric_value=0.9, cost={"known": False},
    ))
    assert unknown_cost["passed"] is False
    cost_rules = {rule["id"]: rule for rule in unknown_cost["rules"]}
    assert cost_rules["cost_known"]["passed"] is False


def test_baseline_snapshot_is_pinned_across_pass_changes():
    baselines = MemoryBaselines()
    baselines.put({
        "id": "base-1", "run_id": "run-a", "scoring_pass_id": "pass-1",
        "metrics": {"accuracy": 0.75}, "manifest_digest": "sha256:x",
    })
    fetched = baselines.get("base-1")
    assert fetched["metrics"] == {"accuracy": 0.75}
    # current pass 漂移不影响已固定的 baseline 引用。
    assert baselines.get_for_run("run-a")[0]["scoring_pass_id"] == "pass-1"
    with pytest.raises(ValueError, match="immutable"):
        baselines.put({"id": "base-1", "run_id": "run-a",
                       "scoring_pass_id": "pass-2", "metrics": {"accuracy": 0.9},
                       "manifest_digest": "sha256:y"})


def test_evaluation_is_deterministic_and_read_only():
    candidate = _candidate(
        judged=10, scored=10, metric_value=0.9,
        cost={"known": True, "total_usd": 1.0},
    )
    first = evaluate_gate(
        POLICY, candidate, comparison={"eligible": True, "reasons": []},
        evaluated_at="2026-09-19T00:00:00Z",
    )
    second = evaluate_gate(
        POLICY, candidate, comparison={"eligible": True, "reasons": []},
        evaluated_at="2026-09-19T12:00:00Z",
    )
    assert first["passed"] == second["passed"] is True
    assert first["conclusion_hash"] == second["conclusion_hash"]
    # evaluated_at 只进审计元数据，不进结论 hash。
    assert first["evaluated_at"] != second["evaluated_at"]
