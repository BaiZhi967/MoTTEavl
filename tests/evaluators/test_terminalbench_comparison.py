"""review R18：Terminal-Bench 的 Gate 指标在共享服务里有后端实现。

反例与期望：

- ``valid_trial_pass_rate`` / ``valid_trial_coverage`` 在 Gate 注册表里（不再报
  ``unsupported metric``），分母分别是有效 Trial 与计划 Trial；
- ``candidate_summary`` 优先用 ScoringPass 冻结的 trial aggregate，缺 aggregate
  时按该 pass 的 ScoreSet + 冻结计划重算同一口径；
- 质量不足或覆盖不足都必须明确拒绝（共用 ``evaluate_gate``，不另造实现）；
- Trial 成本全部未知时 ``cost.total_usd`` 保持 None，不填 0。

非 Terminal-Bench Run 的 accuracy/cost 行为由 ``tests/sdk`` 与
``test_benchmark_gate_lite.py`` 回归，这里只覆盖 Trial 口径。
"""
from __future__ import annotations

from typing import Any

from motte_eval.coverage import coverage_summary
from motte_eval.gates import evaluate_gate
from motte_eval.harbor import trial_row
from motte_sdk.comparisons import ComparisonService
from motte_sdk.service import RunService
from motte_storage.run_store import InMemoryRunStore

SUITE = "terminal-bench-harbor"
PARSER_VERSION = "harbor-terminal-bench-parser@1"
TASK_KEYS = ("hello-pass", "hello-fail")

#: Web 比较页配置的同一 policy（apps/web 的 COMPARE_POLICY）。
TB_POLICY: dict[str, Any] = {
    "metric": "valid_trial_pass_rate",
    "op": "gte",
    "threshold": 0.5,
    "required_coverage": 1.0,
    "require_cost_known": False,
    "require_comparable": False,
}


def _trial_payload(
    task_key: str, repeat: int, reward: float | None, *,
    status: str = "scored", disposition: str = "succeeded", cost: float | None = None,
) -> dict[str, Any]:
    return {
        "trial_id": f"trial-{task_key}-{repeat}",
        "task_key": task_key,
        "repeat_index": repeat,
        "disposition": disposition,
        "verifier_observation": {
            "status": status,
            "rewards": {} if reward is None else {"reward": reward},
        },
        "usage": {
            "cost_usd": cost,
            "coverage": "observed" if cost is not None else "unavailable",
        },
        "termination": {"timings": {"agent_execution_sec": 1.0 + repeat, "verifier_sec": 0.5}},
        "parser_version": PARSER_VERSION,
    }


def _plans() -> list[dict[str, Any]]:
    return [
        {
            "trial_id": f"trial-{task_key}-{repeat}",
            "run_id": "run-tb",
            "task_key": task_key,
            "repeat_index": repeat,
            "agent_config_hash": "sha256:agent",
            "environment_hash": "sha256:env",
        }
        for task_key in TASK_KEYS for repeat in range(2)
    ]


def _manifest(plans: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "model": "m1",
        "execution": {"backend_id": "external-benchmark", "backend_version": "1"},
        "benchmark_provenance": {
            "suite": SUITE, "plugin_version": "1", "id": "terminal-bench",
            "version": "harbor-0.23.0", "dataset_revision": "rev-1",
            "selected_count": len(TASK_KEYS), "aggregation": "first-trial",
            "parser_version": PARSER_VERSION,
        },
        "evaluation": {
            "suite": SUITE, "plugin_version": "1", "scorer": "harbor-terminal-bench",
            "scorer_version": PARSER_VERSION,
        },
        "external_benchmark": {
            "adapter_id": SUITE, "adapter_version": "1",
            "runner_version": "harbor-0.23.0", "dataset_revision": "rev-1",
            "environment_digest": "sha256:env",
            "profile": {"benchmark_id": "terminal-bench", "n_trials": 2},
            "limits": {},
            "runner_config": {"plan": {"trials": plans}, "trials": plans},
        },
        "task_manifest": {"task_keys": list(TASK_KEYS), "trials": plans},
        "case_expectations": {},
    }


def _terminal_bench_run(
    service: RunService, payloads: list[dict[str, Any]], *,
    skip_aggregate: bool = False, scored_payloads: list[dict[str, Any]] | None = None,
    drop_task_manifest_trials: bool = False,
) -> str:
    """按生产形态建 Run + ScoreSet：Trial 行来自 TrialResult 投影，计划随 manifest 冻结。"""
    plans = _plans()
    manifest = _manifest(plans)
    if drop_task_manifest_trials:
        manifest["task_manifest"].pop("trials", None)
    run = service.create_run(
        f"{SUITE}@1", manifest, case_ids=list(TASK_KEYS), run_id="run-tb",
    )
    for payload in payloads:
        service.store.case_runs.upsert({
            "run_id": run["id"],
            "case_id": f"{payload['task_key']}#{payload['repeat_index']}",
            "outcome": "responded",
            "result": payload,
        })
    service._append_scoring_pass(
        run["id"], [trial_row(payload) for payload in (scored_payloads or payloads)],
        source="test-fixation", final_status="completed",
        skip_aggregate=skip_aggregate,
    )
    return str(run["id"])


def _all_pass_payloads() -> list[dict[str, Any]]:
    return [
        _trial_payload("hello-pass", 0, 1.0),
        _trial_payload("hello-pass", 1, 1.0),
        _trial_payload("hello-fail", 0, 1.0),
        _trial_payload("hello-fail", 1, 1.0),
    ]


def _mixed_payloads() -> list[dict[str, Any]]:
    """3 个有效（2 通过 1 失败）+ 1 个 Verifier 错误：质量 2/3、覆盖 3/4。"""
    return [
        _trial_payload("hello-pass", 0, 0.0, disposition="failed"),
        _trial_payload("hello-pass", 1, 1.0),
        _trial_payload("hello-fail", 0, 1.0),
        _trial_payload(
            "hello-fail", 1, None, status="verifier_error", disposition="indeterminate",
        ),
    ]


def _gate_rules(conclusion: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {rule["id"]: rule for rule in conclusion["rules"]}


def test_gate_registry_accepts_the_trial_metrics() -> None:
    """review R18 的最小复现：registry 只有 accuracy/cost 时，即便覆盖=1、通过率=1
    也必须能按 metric 身份求值，而不是报 ``unsupported metric``。"""
    candidate = coverage_summary(
        denominator="planned_trials", selected=2, judged=2, scored=2, attempted=2,
        metric_values={
            "valid_trial_pass_rate": 1.0, "valid_trial_coverage": 1.0,
            "cost.total_usd": None,
        },
    )
    conclusion = evaluate_gate(
        {"metric": "valid_trial_pass_rate", "op": "gte", "threshold": 0.5,
         "required_coverage": 1.0},
        candidate,
    )
    assert conclusion["passed"] is True, conclusion["rules"]
    coverage_conclusion = evaluate_gate(
        {"metric": "valid_trial_coverage", "op": "gte", "threshold": 1.0,
         "required_coverage": 0.0},
        candidate,
    )
    assert coverage_conclusion["passed"] is True, coverage_conclusion["rules"]
    assert "unsupported metric" not in _gate_rules(conclusion)["metric_threshold"]["reason"]


def test_terminal_bench_gate_metric_is_registered_and_passes_on_full_evidence() -> None:
    """全量有效：覆盖 1.0、通过率 1.0 → Gate 通过（不再 unsupported metric）。"""
    service = RunService(InMemoryRunStore())
    run_id = _terminal_bench_run(service, _all_pass_payloads())
    comparisons = ComparisonService(service.store)

    candidate = comparisons.candidate_summary(run_id)
    assert candidate["denominator"] == "planned_trials", "覆盖分母是计划 Trial"
    assert candidate["denominator_value"] == 4
    assert candidate["selected"] == 4 and candidate["judged"] == 4
    assert candidate["coverage"] == 1.0
    assert candidate["metric_values"]["valid_trial_pass_rate"] == 1.0
    assert candidate["metric_values"]["valid_trial_coverage"] == 1.0
    assert candidate["metric_values"]["cost.total_usd"] is None, "未知成本保持 None"

    gate = comparisons.evaluate_gate(run_id, policy=dict(TB_POLICY))
    assert gate["metric_id"] == "valid_trial_pass_rate"
    assert gate["passed"] is True, gate["rules"]
    rules = _gate_rules(gate)
    assert rules["metric_threshold"]["passed"] is True
    assert rules["metric_threshold"]["reason"].startswith("valid_trial_pass_rate=1.0")
    assert rules["coverage"]["passed"] is True


def test_terminal_bench_gate_rejects_quality_and_coverage() -> None:
    """质量不足与覆盖不足（Verifier 错误占掉计划分母）都必须拒绝。"""
    service = RunService(InMemoryRunStore())
    run_id = _terminal_bench_run(service, _mixed_payloads())
    comparisons = ComparisonService(service.store)

    candidate = comparisons.candidate_summary(run_id)
    assert candidate["denominator"] == "planned_trials"
    assert candidate["denominator_value"] == 4
    assert candidate["judged"] == 3 and candidate["unknown"] == 1
    assert candidate["coverage"] == 0.75
    assert candidate["metric_values"]["valid_trial_pass_rate"] == round(2 / 3, 6)
    assert candidate["metric_values"]["valid_trial_coverage"] == 0.75

    gate = comparisons.evaluate_gate(run_id, policy=dict(TB_POLICY))
    assert gate["passed"] is False
    rules = _gate_rules(gate)
    # 通过率 2/3 达到 0.5，但覆盖 3/4 < 1.0 → 证据不足，不放行。
    assert rules["metric_threshold"]["passed"] is True
    assert rules["coverage"]["passed"] is False
    assert "0.75" in rules["coverage"]["reason"]

    strict = comparisons.evaluate_gate(
        run_id, policy={**TB_POLICY, "threshold": 1.0, "required_coverage": 0.5},
    )
    assert strict["passed"] is False
    assert _gate_rules(strict)["metric_threshold"]["passed"] is False
    assert _gate_rules(strict)["coverage"]["passed"] is True


def test_terminal_bench_summary_recomputes_without_a_frozen_aggregate() -> None:
    """缺 pass aggregate 时按 ScoreSet + 冻结计划重算同一口径（分母仍是计划 Trial）。"""
    service = RunService(InMemoryRunStore())
    run_id = _terminal_bench_run(service, _all_pass_payloads(), skip_aggregate=True)
    record = service.store.scoring_passes.current(run_id)
    assert "aggregate" not in (record.get("summary") or {}), "反例前提：pass 没有冻结聚合"

    comparisons = ComparisonService(service.store)
    candidate = comparisons.candidate_summary(run_id)
    assert candidate["denominator"] == "planned_trials"
    assert candidate["denominator_value"] == 4
    assert candidate["selected"] == 4 and candidate["judged"] == 4
    assert candidate["coverage"] == 1.0
    assert candidate["metric_values"]["valid_trial_pass_rate"] == 1.0
    assert candidate["metric_values"]["valid_trial_coverage"] == 1.0
    assert candidate["metric_values"]["cost.total_usd"] is None

    gate = comparisons.evaluate_gate(run_id, policy=dict(TB_POLICY))
    assert gate["passed"] is True, gate["rules"]


def test_terminal_bench_summary_recomputes_from_the_trial_score_rows() -> None:
    """缺 aggregate 时按 Trial 行重算：``denominator=False`` 的行只降覆盖，不当 0。"""
    service = RunService(InMemoryRunStore())
    run_id = _terminal_bench_run(service, _mixed_payloads(), skip_aggregate=True)
    record = service.store.scoring_passes.current(run_id)
    rows = service.store.score_sets.list_for_pass(record["id"])
    assert [row["unit"] for row in rows] == ["trial"] * 4
    assert sorted(row["denominator"] for row in rows) == [False, True, True, True]
    assert sorted(str(row["passed"]) for row in rows) == ["False", "None", "True", "True"]

    candidate = ComparisonService(service.store).candidate_summary(run_id)
    assert candidate["denominator"] == "planned_trials"
    assert candidate["denominator_value"] == 4
    assert candidate["judged"] == 3 and candidate["unknown"] == 1
    assert candidate["coverage"] == 0.75
    assert candidate["metric_values"]["valid_trial_pass_rate"] == round(2 / 3, 6)
    assert candidate["metric_values"]["valid_trial_coverage"] == 0.75
    assert candidate["metric_values"]["cost.total_usd"] is None, "ScoreSet 没有成本证据"


def test_terminal_bench_plan_falls_back_to_the_runner_config() -> None:
    """task_manifest 缺计划时，分母退回 external_benchmark.runner_config.plan。"""
    service = RunService(InMemoryRunStore())
    run_id = _terminal_bench_run(
        service, _all_pass_payloads(), skip_aggregate=True,
        drop_task_manifest_trials=True,
    )
    candidate = ComparisonService(service.store).candidate_summary(run_id)
    assert candidate["denominator"] == "planned_trials"
    assert candidate["denominator_value"] == 4
    assert candidate["coverage"] == 1.0
    assert candidate["metric_values"]["valid_trial_pass_rate"] == 1.0


def test_terminal_bench_summary_prefers_the_frozen_aggregate() -> None:
    """pass 冻结的 aggregate 是权威事实；ScoreSet 只是缺 aggregate 时的缺省（R12/R18）。"""
    service = RunService(InMemoryRunStore())
    payloads = _all_pass_payloads()
    # 反例构造：ScoreSet 只固定了半个 Trial 集，冻结 aggregate 仍是全量。
    run_id = _terminal_bench_run(service, payloads, scored_payloads=payloads[:2])
    record = service.store.scoring_passes.current(run_id)
    assert record["summary"]["aggregate"]["selected_trials"] == 4
    assert len(service.store.score_sets.list_for_pass(record["id"])) == 2

    candidate = ComparisonService(service.store).candidate_summary(run_id)
    assert candidate["denominator_value"] == 4, "分母取冻结计划，不取 ScoreSet 行数"
    assert candidate["judged"] == 4
    assert candidate["coverage"] == 1.0


def test_terminal_bench_cost_is_reported_only_when_complete() -> None:
    """成本来自 aggregate["cost"]：混合未知 → None 且成本门不可通过。"""
    service = RunService(InMemoryRunStore())
    complete = _terminal_bench_run(service, [
        _trial_payload("hello-pass", 0, 1.0, cost=1.0),
        _trial_payload("hello-pass", 1, 1.0, cost=0.5),
        _trial_payload("hello-fail", 0, 1.0, cost=0.25),
        _trial_payload("hello-fail", 1, 1.0, cost=0.25),
    ])
    known = ComparisonService(service.store).candidate_summary(complete)
    assert known["metric_values"]["cost.total_usd"] == 2.0
    assert known["cost"]["known"] is True and known["cost_passable"] is True

    service = RunService(InMemoryRunStore())
    mixed = _terminal_bench_run(service, [
        _trial_payload("hello-pass", 0, 1.0, cost=1.0),
        _trial_payload("hello-pass", 1, 1.0),
        _trial_payload("hello-fail", 0, 1.0, cost=0.25),
        _trial_payload("hello-fail", 1, 1.0, cost=0.25),
    ])
    unhealthy = ComparisonService(service.store).candidate_summary(mixed)
    assert unhealthy["metric_values"]["cost.total_usd"] is None
    assert unhealthy["cost"]["known"] is False and unhealthy["cost_passable"] is False
    gate = ComparisonService(service.store).evaluate_gate(
        mixed, policy={**TB_POLICY, "require_cost_known": True},
    )
    assert gate["passed"] is False
    assert _gate_rules(gate)["cost_known"]["passed"] is False


def test_terminal_bench_gate_refuses_without_any_valid_trial() -> None:
    """全部 Trial 都是 Verifier 错误：通过率不适用 → 明确 insufficient，不填 0。"""
    service = RunService(InMemoryRunStore())
    payloads = [
        _trial_payload(task_key, repeat, None, status="verifier_error",
                       disposition="indeterminate")
        for task_key in TASK_KEYS for repeat in range(2)
    ]
    run_id = _terminal_bench_run(service, payloads)
    comparisons = ComparisonService(service.store)

    candidate = comparisons.candidate_summary(run_id)
    assert candidate["coverage"] == 0.0, "计划 Trial 都在分母里，无效 Trial 不能消失"
    assert candidate["metric_values"]["valid_trial_pass_rate"] is None
    gate = comparisons.evaluate_gate(run_id, policy=dict(TB_POLICY))
    assert gate["passed"] is False
    assert "valid_trial_pass_rate" in _gate_rules(gate)["metric_threshold"]["reason"]
