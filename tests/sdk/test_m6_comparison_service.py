"""M6 ComparisonService：ReportSnapshot / Baseline / 版本化 Gate / 回归分类。

协议 docs/protocols/experiments-and-comparison.md §2/§4–§7：固定 Run +
ScoringPass 只读装配，服务本身没有 Provider/Judge 依赖（本文件同样零模型
调用）。数据全部经 InMemoryRunStore + MemoryScoringPasses 真实落库：

- report_snapshot：§2 真值表 9 键 counts、disposition 不变量、snapshot_id
  内容 hash 稳定（同输入两次构造同 id）；
- baseline（§4）：不可变（A09 rescore 后内容与 report_ref 不变）、资格
  formal/diagnostic、引用不完整拒绝、同 id 异内容冲突；
- 默认指针：首次 CAS 设置、期望不符冲突、历史全留；
- 版本化 Gate（§6/§7/§9）：A17 completed 但目标不达 → quality_fail 退 1；
  覆盖不足 → insufficient_evidence 退 5；重复求值同 gate_result_id；
  draft 政策拒绝求值；
- 回归分类（A10）：added/removed 独立列出，fixed/new_failure 只在共同样本。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from motte_contracts.comparison import DISPOSITION_COUNT_KEYS
from motte_sdk.comparisons import ComparisonError, ComparisonService
from motte_storage.baseline_store import BaselineConflict
from motte_storage.run_store import InMemoryRunStore

CREATED_AT = "2026-09-21T00:00:00+00:00"


def make_run(
    store: Any, run_id: str, *, case_ids: list[str], status: str = "completed",
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base_manifest: dict[str, Any] = {
        "evaluation": {"scorer_id": "deterministic", "scorer_version": "1"},
        "model": "subject-model-a",
        "cost": {"known": True, "total_usd": 1.25, "currency": "USD"},
    }
    if manifest is not None:
        base_manifest.update(manifest)
    return store.runs.create({
        "id": run_id,
        "schema_version": 2,
        "revision": 1,
        "scenario_version": "test@1",
        "status": status,
        "manifest": base_manifest,
        "requested_manifest": {},
        "case_ids": list(case_ids),
        "created_at": CREATED_AT,
        "updated_at": CREATED_AT,
    }, event={"run_id": run_id, "type": "queued", "status": status})


def score_row(
    case_id: str, *, passed: bool | None, denominator: bool = True,
    outcome: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "case_id": case_id,
        "metric_id": "accuracy",
        "evaluator_id": "deterministic",
        "evaluator_version": "1",
        "metric_status": "scored",
        "value": 1.0 if passed else 0.0,
        "denominator": denominator,
        "details": {},
    }
    if passed is not None:
        row["passed"] = passed
    if outcome is not None:
        row["outcome"] = outcome
    return row


def append_pass(
    store: Any, run_id: str, pass_id: str, rows: list[dict[str, Any]],
    *, created_at: str = CREATED_AT,
) -> dict[str, Any]:
    return store.scoring_passes.append({
        "id": pass_id,
        "run_id": run_id,
        "scorer_id": "deterministic",
        "scorer_version": "1",
        "created_at": created_at,
        "source": "initial",
        "source_run_revision": 1,
        "summary": {},
    }, rows)


def mixed_store() -> tuple[Any, ComparisonService]:
    """c1 对 / c2 错 / c3 缺样本 / c4 无期望 / c5 调用失败。"""
    store = InMemoryRunStore()
    make_run(store, "run-mixed", case_ids=["c1", "c2", "c3", "c4", "c5"])
    append_pass(store, "run-mixed", "pass-mixed", [
        score_row("c1", passed=True),
        score_row("c2", passed=False),
        score_row("c4", passed=None, denominator=False, outcome="no_expectation"),
        score_row("c5", passed=False, outcome="call_failed"),
    ])
    return store, ComparisonService(store)


def assert_disposition_invariants(snapshot: Any) -> None:
    counts = snapshot.counts
    assert set(counts) == set(DISPOSITION_COUNT_KEYS)
    assert counts["attempted"] + counts["not_attempted"] == counts["selected"]
    observable = (
        counts["judged"] + counts["call_failed"]
        + counts["unknown"] + counts["needs_review"]
    )
    assert observable <= counts["attempted"]


# ----------------------------------------------------------------- snapshot


def test_report_snapshot_counts_dispositions_and_metrics() -> None:
    store, service = mixed_store()
    snapshot = service.report_snapshot("run-mixed")
    assert_disposition_invariants(snapshot)
    assert snapshot.counts == {
        "selected": 5,
        # judged 2 + call_failed 1 + no_expectation 1（c4 已执行，计 attempted）
        "attempted": 4,
        "judged": 2,
        "scored": 1,
        "call_failed": 1,
        "unknown": 0,
        "not_attempted": 1,   # 只有真正未尝试的 c5
        "needs_review": 0,
        "no_expectation": 1,
    }
    dispositions = {
        record.case_id: record for record in snapshot.case_dispositions
    }
    assert sorted(dispositions) == ["c1", "c2", "c3", "c4", "c5"]
    assert dispositions["c1"].disposition == "judged"
    assert dispositions["c1"].detail == "passed"
    assert dispositions["c2"].disposition == "judged"
    assert dispositions["c2"].detail == "failed"
    assert dispositions["c3"].disposition == "not_attempted"
    assert dispositions["c4"].disposition == "no_expectation"
    assert dispositions["c5"].disposition == "call_failed"
    # 伴生逐 case 结论：judged 给合取布尔，其余 None=未知（不是成功）。
    assert service.case_outcomes("run-mixed") == {
        "c1": True, "c2": False, "c3": None, "c4": None, "c5": None,
    }
    # 指标：accuracy 因存在未决样本（c3 缺样本）保持 None；judged_accuracy
    # 是判定样本口径；coverage 仍按 selected；成本来自 manifest。
    assert snapshot.metric_values["accuracy"] is None
    assert snapshot.metric_values["judged_accuracy"] == 0.5
    assert snapshot.metric_values["cost.total_usd"] == 1.25
    assert snapshot.metric_values["cost.per_success_usd"] == 1.25
    assert snapshot.coverage == 0.4
    assert snapshot.denominator == "selected_cases"
    assert snapshot.cost.known is True
    assert snapshot.cost.source == "manifest"
    assert snapshot.cost.total_usd() == 1.25
    assert snapshot.metric_registry_version


def test_report_snapshot_full_run_and_stable_snapshot_id() -> None:
    store = InMemoryRunStore()
    make_run(store, "run-full", case_ids=["c1", "c2", "c3"])
    append_pass(store, "run-full", "pass-full", [
        score_row("c1", passed=True),
        score_row("c2", passed=True),
        score_row("c3", passed=True),
    ])
    service = ComparisonService(store)
    first = service.report_snapshot("run-full")
    second = service.report_snapshot("run-full")
    assert_disposition_invariants(first)
    # snapshot_id 是内容 canonical hash：同输入两次构造同 id。
    assert first.snapshot_id == second.snapshot_id
    assert first.snapshot_id == first.compute_snapshot_id()
    assert first.counts["selected"] == 3
    assert first.counts["judged"] == 3
    assert first.counts["scored"] == 3
    assert first.counts["not_attempted"] == 0
    assert first.metric_values["accuracy"] == 1.0
    assert first.metric_values["judged_accuracy"] == 1.0
    assert first.coverage == 1.0
    assert first.metric_values["cost.per_success_usd"] == round(1.25 / 3, 6)
    # 换一个 pass（rescore）内容变化 → snapshot_id 变化。
    append_pass(store, "run-full", "pass-full-2", [
        score_row("c1", passed=True),
        score_row("c2", passed=False),
        score_row("c3", passed=True),
    ], created_at="2026-09-21T01:00:00+00:00")
    rescored = service.report_snapshot("run-full", scoring_pass_id="pass-full-2")
    assert rescored.snapshot_id != first.snapshot_id
    assert rescored.counts["scored"] == 2
    # 固定旧 pass 的快照不随 current 漂移。
    assert service.report_snapshot("run-full", scoring_pass_id="pass-full") == first


def test_report_snapshot_direct_llm_judged_denominator() -> None:
    store = InMemoryRunStore()
    make_run(
        store, "run-direct", case_ids=["c1", "c2"],
        manifest={
            "benchmark_provenance": {"suite": "direct-llm", "dataset": "ds@1"},
            "cost": {"known": False},
        },
    )
    append_pass(store, "run-direct", "pass-direct", [
        score_row("c1", passed=True),
        score_row("c2", passed=None, denominator=False, outcome="no_expectation"),
    ])
    service = ComparisonService(store)
    snapshot = service.report_snapshot("run-direct")
    assert_disposition_invariants(snapshot)
    assert snapshot.suite == "direct-llm"
    # 质量口径：judged_cases 分母；覆盖仍按 selected。
    assert snapshot.denominator == "judged_cases"
    assert snapshot.metric_values["judged_accuracy"] == 1.0
    assert snapshot.coverage == 0.5
    # 成本未知保持 None（不当 0），unknown_usage_count 记 1。
    assert snapshot.metric_values["cost.total_usd"] is None
    assert snapshot.cost.known is False
    assert snapshot.cost.unknown_usage_count == 1


def test_report_snapshot_keeps_case_currencies_separate_and_usage_unknown() -> None:
    store = InMemoryRunStore()
    make_run(store, "run-currencies", case_ids=["c1", "c2", "c3"],
             manifest={"cost": {"known": False}})
    append_pass(store, "run-currencies", "pass-currencies", [
        score_row("c1", passed=True), score_row("c2", passed=True),
        score_row("c3", passed=True),
    ])
    for case_id, currency, amount in [("c1", "CNY", 2.0), ("c2", "USD", 0.5)]:
        store.case_runs.upsert({
            "run_id": "run-currencies", "case_id": case_id,
            "result": {"cost": {"total": amount, "currency": currency}},
        })
    store.case_runs.upsert({"run_id": "run-currencies", "case_id": "c3", "result": {}})
    snapshot = ComparisonService(store).report_snapshot("run-currencies")
    assert snapshot.cost.totals() == {"CNY": 2.0, "USD": 0.5}
    assert snapshot.cost.unknown_usage_count == 1
    assert snapshot.metric_values["cost.total_usd"] is None


def test_cost_comparison_requires_matching_currency() -> None:
    store = InMemoryRunStore()
    for run_id, currency in [("base", "USD"), ("candidate", "CNY")]:
        make_run(store, run_id, case_ids=["c1"], manifest={"cost": {"known": False}})
        append_pass(store, run_id, f"pass-{run_id}", [score_row("c1", passed=True)])
        store.case_runs.upsert({
            "run_id": run_id, "case_id": "c1",
            "result": {"cost": {"total": 1.0, "currency": currency}},
        })
    result = ComparisonService(store).compare("base", "candidate", allowed_factors=[])
    assert result.metric_eligibility["quality"] is True
    assert result.metric_eligibility["cost"] is False
    assert "COST_CURRENCY_MISMATCH" in result.metric_reasons


def test_paired_statistics_uses_fixed_pass_and_keeps_missing_visible() -> None:
    store = InMemoryRunStore()
    for run_id, outcomes in [("base", [True, False, True]),
                             ("candidate", [True, True, True])]:
        make_run(store, run_id, case_ids=["a", "b", "c"])
        append_pass(store, run_id, f"old-{run_id}", [
            score_row(case_id, passed=value)
            for case_id, value in zip(["a", "b", "c"], outcomes, strict=True)
        ])
    service = ComparisonService(store)
    fixed = service.paired_statistics("base", "candidate", allowed_factors=[],
                                      baseline_pass_id="old-base",
                                      candidate_pass_id="old-candidate")
    assert fixed["applicable"] is True
    assert fixed["unit"] == "task(case)"
    assert fixed["n_selected"] == fixed["n_pairs"] == 3
    assert fixed["statistics"]["mean_diff"] == pytest.approx(1 / 3)
    assert fixed["statistics"]["interval"]["seed"] == 20260921
    assert fixed["refs"]["baseline"]["scoring_pass_id"] == "old-base"
    append_pass(store, "base", "new-base", [score_row("a", passed=False)])
    assert service.paired_statistics("base", "candidate", allowed_factors=[],
                                     baseline_pass_id="old-base",
                                     candidate_pass_id="old-candidate") == fixed
    current = service.paired_statistics("base", "candidate", allowed_factors=[])
    assert current["applicable"] is False
    assert current["reason"] == "missing_or_uncertain_case"
    assert current["missing_pairs"] == 2


def test_paired_statistics_never_treats_terminal_retries_as_trials() -> None:
    store = InMemoryRunStore()
    for run_id in ("base", "candidate"):
        make_run(store, run_id, case_ids=["task-a", "task-b"], manifest={
            "benchmark_provenance": {"suite": "terminal-bench-harbor"},
        })
        append_pass(store, run_id, f"pass-{run_id}", [
            score_row("task-a", passed=True), score_row("task-b", passed=True),
        ])
    result = ComparisonService(store).paired_statistics(
        "base", "candidate", allowed_factors=[],
    )
    assert result["applicable"] is False
    assert result["reason"] == "missing_or_invalid_trial"
    assert result["statistics"] is None
    assert result["unit"] == "task(trials)"
    assert result["trial_aggregation"]["baseline"]["per_task"]["task-a"]["n_planned"] == 0
    assert result["seed"] == 20260921


def test_terminal_statistics_aggregate_only_fixed_planned_valid_trials() -> None:
    store = InMemoryRunStore()
    for run_id, pass_counts in (("base", (1, 1)), ("candidate", (2, 2))):
        plan = [
            {"trial_id": f"{run_id}-{task}-{repeat}", "task_key": task,
             "repeat_index": repeat, "run_id": run_id,
             "agent_config_hash": "agent-a", "environment_hash": "env-a"}
            for task in ("task-a", "task-b") for repeat in range(3)
        ]
        make_run(store, run_id, case_ids=["task-a", "task-b"], manifest={
            "benchmark_provenance": {"suite": "terminal-bench-harbor"},
            "task_manifest": {"trials": plan},
        })
        rows = []
        for task, passed_count in zip(("task-a", "task-b"), pass_counts, strict=True):
            for repeat in range(3):
                rows.append({
                    **score_row(task, passed=repeat < passed_count),
                    "trial_id": f"{run_id}-{task}-{repeat}",
                    "unit": "trial", "metric_id": "terminal.reward",
                    "details": {"repeat_index": repeat},
                })
        rows.append({
            **score_row("task-a", passed=True), "trial_id": f"{run_id}-transport-retry",
            "unit": "trial", "metric_id": "terminal.reward",
        })
        append_pass(store, run_id, f"pass-{run_id}", rows)
    service = ComparisonService(store)
    fixed = service.paired_statistics(
        "base", "candidate", allowed_factors=[],
        baseline_pass_id="pass-base", candidate_pass_id="pass-candidate", k=2,
    )
    assert fixed["unit"] == "task(trials)"
    assert fixed["trial_aggregation"]["baseline"]["per_task"]["task-a"]["pass_at_k"]["value"] == pytest.approx(2 / 3)
    assert fixed["trial_aggregation"]["candidate"]["per_task"]["task-a"]["pass_at_k"]["value"] == 1.0
    assert fixed["trial_aggregation"]["baseline"]["excluded_unplanned_score_rows"] == 1
    assert fixed["trial_aggregation"]["baseline"]["per_task"]["task-a"]["n_planned"] == 3
    assert fixed["n_pairs"] == 2
    assert fixed["statistics"]["mean_diff"] == pytest.approx(1 / 3)
    assert fixed["refs"]["baseline"]["scoring_pass_id"] == "pass-base"
    # A new current pass cannot move a fixed statistical report.
    append_pass(store, "base", "new-base", [])
    assert service.paired_statistics(
        "base", "candidate", allowed_factors=[],
        baseline_pass_id="pass-base", candidate_pass_id="pass-candidate", k=2,
    ) == fixed
    insufficient = service.paired_statistics(
        "base", "candidate", allowed_factors=[],
        baseline_pass_id="pass-base", candidate_pass_id="pass-candidate", k=4,
    )
    assert insufficient["applicable"] is False
    assert insufficient["reason"] == "insufficient_planned_trials"
    assert insufficient["statistics"] is None


def test_terminal_statistics_reject_missing_trial_and_retry_rows() -> None:
    store = InMemoryRunStore()
    for run_id in ("base", "candidate"):
        make_run(store, run_id, case_ids=["task-a", "task-b"], manifest={
            "benchmark_provenance": {"suite": "terminal-bench-harbor"},
            "task_manifest": {"trials": [
                {"trial_id": f"{run_id}-{task}-{repeat}", "task_key": task,
                 "repeat_index": repeat, "run_id": run_id,
                 "agent_config_hash": "agent-a", "environment_hash": "env-a"}
                for task in ("task-a", "task-b") for repeat in range(2)
            ]},
        })
        append_pass(store, run_id, f"pass-{run_id}", [
            {**score_row(task, passed=True), "trial_id": f"{run_id}-{task}-0",
             "unit": "trial", "metric_id": "terminal.reward"}
            for task in ("task-a", "task-b")
        ] + [
            {**score_row("task-a", passed=True), "trial_id": f"{run_id}-retry",
             "unit": "trial", "metric_id": "terminal.reward"},
        ])
    result = ComparisonService(store).paired_statistics(
        "base", "candidate", allowed_factors=[], k=2,
    )
    assert result["applicable"] is False
    assert result["reason"] == "missing_or_invalid_trial"
    assert result["statistics"] is None
    assert result["trial_aggregation"]["baseline"]["per_task"]["task-a"]["n_planned"] == 2
    assert result["trial_aggregation"]["baseline"]["per_task"]["task-a"]["n_valid"] == 1
    assert result["trial_aggregation"]["baseline"]["per_task"]["task-a"]["pass_at_k"]["applicable"] is False


def test_terminal_statistics_reject_reused_upstream_trial_identity() -> None:
    store = InMemoryRunStore()
    for run_id in ("base", "candidate"):
        make_run(store, run_id, case_ids=["task-a", "task-b"], manifest={
            "benchmark_provenance": {"suite": "terminal-bench-harbor"},
            "task_manifest": {"trials": [
                {"trial_id": f"{run_id}-{task}-{repeat}", "task_key": task,
                 "repeat_index": repeat, "run_id": run_id,
                 "agent_config_hash": "agent-a", "environment_hash": "env-a"}
                for task in ("task-a", "task-b") for repeat in range(2)
            ]},
        })
        append_pass(store, run_id, f"pass-{run_id}", [
            {**score_row(task, passed=True), "trial_id": f"{run_id}-{task}-{repeat}",
             "unit": "trial", "metric_id": "reward",
             "details": {"repeat_index": repeat,
                         "source_trial_id": "reused-upstream" if task == "task-a" else f"{task}-{repeat}"}}
            for task in ("task-a", "task-b") for repeat in range(2)
        ])
    result = ComparisonService(store).paired_statistics(
        "base", "candidate", allowed_factors=[], k=2,
    )
    assert result["applicable"] is False
    assert result["trial_aggregation"]["baseline"]["per_task"]["task-a"]["pass_at_k"]["reason"] == "non_independent_trials"
    assert result["trial_aggregation"]["baseline"]["per_task"]["task-b"]["pass_at_k"]["applicable"] is True


def test_report_snapshot_needs_review_run_keeps_uncertain_cases_visible() -> None:
    store = InMemoryRunStore()
    make_run(store, "run-review", case_ids=["c1", "c2"], status="needs_review")
    append_pass(store, "run-review", "pass-review", [
        score_row("c1", passed=True),
        score_row("c2", passed=None),  # 无明确结论
    ])
    service = ComparisonService(store)
    snapshot = service.report_snapshot("run-review")
    assert_disposition_invariants(snapshot)
    dispositions = {
        record.case_id: record.disposition
        for record in snapshot.case_dispositions
    }
    # 简化口径：needs_review Run 里无明确 passed 的样本归 needs_review，
    # 不从分母删除；accuracy 因未决样本保持 None。
    assert dispositions == {"c1": "judged", "c2": "needs_review"}
    assert snapshot.counts["needs_review"] == 1
    assert snapshot.metric_values["accuracy"] is None


# ----------------------------------------------------------------- baseline


def test_baseline_eligibility_and_missing_references() -> None:
    store, service = mixed_store()
    store.runs.create({
        "id": "run-review", "schema_version": 2, "revision": 1,
        "scenario_version": "test@1", "status": "needs_review",
        "manifest": {"evaluation": {"scorer_id": "deterministic", "scorer_version": "1"}},
        "requested_manifest": {}, "case_ids": ["c1"],
        "created_at": CREATED_AT, "updated_at": CREATED_AT,
    }, event={"run_id": "run-review", "type": "queued", "status": "needs_review"})
    append_pass(store, "run-review", "pass-review", [score_row("c1", passed=True)])

    formal = service.create_baseline(
        "base-formal",
        [{"cell_key": None, "run_id": "run-mixed", "scoring_pass_id": "pass-mixed"}],
        policy={"allowed_factors": ["model"]},
        created_by="operator", reason="completed run with a fixed pass",
    )
    assert formal["eligibility"] == "formal"
    assert formal["entries"][0]["ref"]["scoring_pass_id"] == "pass-mixed"
    assert formal["metrics"]["accuracy"] is None  # mixed run 有未决样本
    assert formal["comparison_policy_hash"].startswith("sha256:")

    diagnostic = service.create_baseline(
        "base-diagnostic",
        [{"cell_key": None, "run_id": "run-review", "scoring_pass_id": "pass-review"}],
        policy={"allowed_factors": ["model"]},
        created_by="operator", reason="needs_review run is diagnostic only",
    )
    assert diagnostic["eligibility"] == "diagnostic"

    # 同参数重放幂等：同 id 同内容返回已存快照。
    assert service.create_baseline(
        "base-diagnostic",
        [{"cell_key": None, "run_id": "run-review", "scoring_pass_id": "pass-review"}],
        policy={"allowed_factors": ["model"]},
        created_by="operator", reason="needs_review run is diagnostic only",
    ) == diagnostic

    # 引用不完整：不存在的 run / 不存在的 pass 都拒绝创建。
    with pytest.raises(ComparisonError) as run_missing:
        service.create_baseline(
            "base-bad",
            [{"cell_key": None, "run_id": "no-such-run", "scoring_pass_id": "p"}],
            policy={"allowed_factors": ["model"]},
            created_by="operator", reason="broken reference",
        )
    assert run_missing.value.code == "BASELINE_NOT_FOUND"
    with pytest.raises(ComparisonError) as pass_missing:
        service.create_baseline(
            "base-bad",
            [{"cell_key": None, "run_id": "run-mixed", "scoring_pass_id": "no-such-pass"}],
            policy={"allowed_factors": ["model"]},
            created_by="operator", reason="broken reference",
        )
    assert pass_missing.value.code == "BASELINE_NOT_FOUND"
    assert service.list_baselines()[0]["baseline_id"] in {"base-formal", "base-diagnostic"}
    assert len(service.list_baselines()) == 2


def test_baseline_same_id_different_content_conflicts() -> None:
    store, service = mixed_store()
    service.create_baseline(
        "base-1",
        [{"cell_key": None, "run_id": "run-mixed", "scoring_pass_id": "pass-mixed"}],
        policy={"allowed_factors": ["model"]},
        created_by="operator", reason="first",
    )
    with pytest.raises(BaselineConflict) as conflict:
        service.create_baseline(
            "base-1",
            [{"cell_key": None, "run_id": "run-mixed", "scoring_pass_id": "pass-mixed"}],
            policy={"allowed_factors": ["model"]},
            created_by="operator", reason="different content under the same id",
        )
    assert conflict.value.code == "BASELINE_IMMUTABLE"


def test_baseline_pinned_across_rescore() -> None:
    """A09：baseline 固定 RunReportRef；rescore 追加新 pass 后内容不漂移。"""
    store = InMemoryRunStore()
    make_run(store, "run-a09", case_ids=["c1", "c2"])
    append_pass(store, "run-a09", "pass-1", [
        score_row("c1", passed=True), score_row("c2", passed=True),
    ])
    service = ComparisonService(store)
    baseline = service.create_baseline(
        "base-a09",
        [{"cell_key": None, "run_id": "run-a09", "scoring_pass_id": "pass-1"}],
        policy={"allowed_factors": ["model"]},
        created_by="operator", reason="pin the initial pass",
    )
    pinned_ref = service.report_ref("run-a09", scoring_pass_id="pass-1")

    # rescore：追加第二个 pass，current 指针随之漂移。
    append_pass(store, "run-a09", "pass-2", [
        score_row("c1", passed=False), score_row("c2", passed=True),
    ], created_at="2026-09-21T02:00:00+00:00")
    assert store.scoring_passes.current("run-a09")["id"] == "pass-2"

    # baseline 内容与固定 pass 的引用/快照都不变；不指定 pass 的默认视图才
    # 跟随 current。
    assert service.get_baseline("base-a09") == baseline
    assert service.report_ref("run-a09", scoring_pass_id="pass-1") == pinned_ref
    assert baseline["entries"][0]["ref"] == pinned_ref.model_dump()
    assert service.report_ref("run-a09") != pinned_ref
    snapshot_one = service.report_snapshot("run-a09", scoring_pass_id="pass-1")
    snapshot_two = service.report_snapshot("run-a09", scoring_pass_id="pass-2")
    assert snapshot_one.counts["scored"] == 2
    assert snapshot_two.counts["scored"] == 1
    assert snapshot_one.snapshot_id != snapshot_two.snapshot_id


def test_default_baseline_pointer_cas_and_history() -> None:
    store, service = mixed_store()
    service.create_baseline(
        "base-v1",
        [{"cell_key": None, "run_id": "run-mixed", "scoring_pass_id": "pass-mixed"}],
        policy={"allowed_factors": ["model"]},
        created_by="operator", reason="initial baseline",
    )
    # 首次设置：expected_current=None。
    first = service.set_default_baseline(
        "suite-main", "base-v1", updated_by="operator", reason="initial default",
    )
    assert first["position"] == 1
    assert first["comparison_policy_hash"].startswith("sha256:")

    # 第二个 baseline；错误的 CAS 期望 → 冲突（指针不动）。
    make_run(store, "run-other", case_ids=["c1"])
    append_pass(store, "run-other", "pass-other", [score_row("c1", passed=True)])
    service.create_baseline(
        "base-v2",
        [{"cell_key": None, "run_id": "run-other", "scoring_pass_id": "pass-other"}],
        policy={"allowed_factors": ["model"]},
        created_by="operator", reason="second baseline",
    )
    with pytest.raises(BaselineConflict) as cas:
        service.set_default_baseline(
            "suite-main", "base-v2", updated_by="operator", reason="wrong expectation",
            expected_current="not-the-current-id",
        )
    assert cas.value.code == "CAS_CONFLICT"
    assert service.get_default_baseline("suite-main")["baseline_id"] == "base-v1"

    # 正确 CAS 移动指针；历史保留两条，不覆盖。
    second = service.set_default_baseline(
        "suite-main", "base-v2", updated_by="operator", reason="promote v2",
        expected_current="base-v1",
    )
    assert second["position"] == 2
    assert service.get_default_baseline("suite-main")["baseline_id"] == "base-v2"
    history = service.default_baseline_history("suite-main")
    assert [item["baseline_id"] for item in history] == ["base-v1", "base-v2"]
    assert [item["position"] for item in history] == [1, 2]
    assert service.get_default_baseline("empty-scope") is None


# ------------------------------------------------------------ gate policies


def gate_policy_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "policy_id": "pol-m6",
        "version": "1",
        "lifecycle": "published",
        "rules": [{
            "rule_id": "acc", "kind": "metric_threshold",
            "metric_id": "accuracy", "operator": "gte", "threshold": 0.9,
        }],
        "created_by": "operator",
        "reason": "M6 acceptance",
    }
    payload.update(overrides)
    return payload


def test_publish_gate_policy_validation_and_idempotence() -> None:
    store, service = mixed_store()
    # 非法规则：metric_threshold 缺 threshold → 契约拒绝。
    with pytest.raises(ComparisonError) as invalid:
        service.publish_gate_policy(gate_policy_payload(rules=[{
            "rule_id": "acc", "kind": "metric_threshold",
            "metric_id": "accuracy", "operator": "gte",
        }]))
    assert invalid.value.code == "GATE_POLICY_INVALID"
    # 未知 metric：注册表查无此身 → 拒绝发布。
    with pytest.raises(ComparisonError) as unknown:
        service.publish_gate_policy(gate_policy_payload(rules=[{
            "rule_id": "acc", "kind": "metric_threshold",
            "metric_id": "no.such.metric", "operator": "gte", "threshold": 0.5,
        }]))
    assert unknown.value.code == "GATE_METRIC_UNKNOWN"
    # operator 与 metric direction 冲突同样在发布期拒绝。
    with pytest.raises(ComparisonError) as direction:
        service.publish_gate_policy(gate_policy_payload(rules=[{
            "rule_id": "acc", "kind": "metric_threshold",
            "metric_id": "accuracy", "operator": "lte", "threshold": 0.5,
        }]))
    assert direction.value.code == "GATE_POLICY_INVALID"

    published = service.publish_gate_policy(gate_policy_payload())
    assert published["policy_id"] == "pol-m6"
    assert published["lifecycle"] == "published"
    # 合法政策重复发布幂等（同内容返回已存版本）。
    again = service.publish_gate_policy(gate_policy_payload())
    assert again == published
    assert store.gate_store.get_policy("pol-m6", "1") == published


def test_evaluate_gate_versioned_quality_fail_exit_code() -> None:
    """A17：Run completed 只代表执行终态；目标不达 → quality_fail 退 1。"""
    store = InMemoryRunStore()
    make_run(store, "run-a17", case_ids=["q1", "q2"])
    append_pass(store, "run-a17", "pass-a17", [
        score_row("q1", passed=True), score_row("q2", passed=False),
    ])
    service = ComparisonService(store)
    service.publish_gate_policy(gate_policy_payload(
        policy_id="pol-a17", rules=[{
            "rule_id": "acc", "kind": "metric_threshold",
            "metric_id": "accuracy", "operator": "gte", "threshold": 0.9,
        }],
    ))
    result = service.evaluate_gate_versioned(
        policy_id="pol-a17", policy_version="1", run_id="run-a17",
    )
    assert result["decision"] == "quality_fail"
    assert result["exit_code"] == 1
    statuses = {rule["rule_id"]: rule["status"] for rule in result["rule_results"]}
    assert statuses["acc"] == "fail"
    assert result["candidates"][0]["run_id"] == "run-a17"
    assert result["candidates"][0]["scoring_pass_id"] == "pass-a17"


def test_evaluate_gate_versioned_coverage_insufficient_exit_code() -> None:
    store, service = mixed_store()  # coverage 0.4
    service.publish_gate_policy(gate_policy_payload(
        policy_id="pol-cov", rules=[{
            "rule_id": "cov", "kind": "coverage", "min_coverage": 1.0,
        }],
    ))
    result = service.evaluate_gate_versioned(
        policy_id="pol-cov", policy_version="1", run_id="run-mixed",
    )
    assert result["decision"] == "insufficient_evidence"
    assert result["exit_code"] == 5
    assert "coverage" in result["rule_results"][0]["reason"]


def test_evaluate_gate_versioned_repeat_is_equivalent_and_rejections() -> None:
    store = InMemoryRunStore()
    make_run(store, "run-rep", case_ids=["q1", "q2"])
    append_pass(store, "run-rep", "pass-rep", [
        score_row("q1", passed=True), score_row("q2", passed=True),
    ])
    service = ComparisonService(store)
    service.publish_gate_policy(gate_policy_payload(
        policy_id="pol-rep", rules=[{
            "rule_id": "acc", "kind": "metric_threshold",
            "metric_id": "accuracy", "operator": "gte", "threshold": 0.5,
        }],
    ))
    first = service.evaluate_gate_versioned(
        policy_id="pol-rep", policy_version="1", run_id="run-rep",
        evaluated_at="2026-09-21T00:00:00+00:00",
    )
    second = service.evaluate_gate_versioned(
        policy_id="pol-rep", policy_version="1", run_id="run-rep",
        evaluated_at="2026-09-21T00:00:00+00:00",
    )
    assert first["decision"] == "pass" and first["exit_code"] == 0
    # 重复求值产生等价结论：同 gate_result_id 幂等落盘，不改写原结果。
    assert second["gate_result_id"] == first["gate_result_id"]
    assert store.gate_store.get_result(first["gate_result_id"]) is not None

    # 政策不存在 / draft 政策 / 非终态 Run 都在求值前拒绝。
    with pytest.raises(ComparisonError) as missing:
        service.evaluate_gate_versioned(
            policy_id="no-such-policy", policy_version="1", run_id="run-rep",
        )
    assert missing.value.code == "GATE_POLICY_NOT_FOUND"

    service.publish_gate_policy(gate_policy_payload(
        policy_id="pol-draft", lifecycle="draft",
    ))
    with pytest.raises(ComparisonError) as draft:
        service.evaluate_gate_versioned(
            policy_id="pol-draft", policy_version="1", run_id="run-rep",
        )
    assert draft.value.code == "GATE_POLICY_DRAFT"

    make_run(store, "run-live", case_ids=["q1"], status="running")
    append_pass(store, "run-live", "pass-live", [score_row("q1", passed=True)])
    with pytest.raises(ComparisonError) as live:
        service.evaluate_gate_versioned(
            policy_id="pol-rep", policy_version="1", run_id="run-live",
        )
    assert live.value.code == "RUN_NOT_TERMINAL"


def test_evaluate_gate_versioned_with_baseline_delta() -> None:
    """baseline 路径：BaselineSnapshot 固定 entry + 同一比较算法。"""
    store = InMemoryRunStore()
    make_run(store, "run-base", case_ids=["q1", "q2"])
    append_pass(store, "run-base", "pass-base", [
        score_row("q1", passed=True), score_row("q2", passed=False),
    ])
    make_run(store, "run-cand", case_ids=["q1", "q2"])
    append_pass(store, "run-cand", "pass-cand", [
        score_row("q1", passed=True), score_row("q2", passed=True),
    ])
    service = ComparisonService(store)
    service.create_baseline(
        "base-delta",
        [{"cell_key": None, "run_id": "run-base", "scoring_pass_id": "pass-base"}],
        policy={"allowed_factors": ["model"]},
        created_by="operator", reason="baseline for delta gating",
    )
    service.publish_gate_policy(gate_policy_payload(
        policy_id="pol-delta", rules=[{
            "rule_id": "no-reg", "kind": "baseline_delta",
            "metric_id": "accuracy", "max_regression": 0.1,
        }],
    ))
    result = service.evaluate_gate_versioned(
        policy_id="pol-delta", policy_version="1", run_id="run-cand",
        baseline_id="base-delta",
    )
    assert result["decision"] == "pass", result
    assert result["exit_code"] == 0
    assert result["baseline"] == {
        "run_id": "run-base", "scoring_pass_id": "pass-base",
    }
    # baseline_id 不存在 → BASELINE_NOT_FOUND。
    with pytest.raises(ComparisonError) as missing:
        service.evaluate_gate_versioned(
            policy_id="pol-delta", policy_version="1", run_id="run-cand",
            baseline_id="no-such-baseline",
        )
    assert missing.value.code == "BASELINE_NOT_FOUND"


# --------------------------------------------------------- 回归分类（A10）


def test_classify_regression_fixed_new_failure_and_added() -> None:
    store = InMemoryRunStore()
    make_run(store, "run-reg-base", case_ids=["a", "b", "c"])
    append_pass(store, "run-reg-base", "pass-reg-base", [
        score_row("a", passed=True),
        score_row("b", passed=False),
        score_row("c", passed=True),
    ])
    make_run(store, "run-reg-cand", case_ids=["a", "b", "c", "d"])
    append_pass(store, "run-reg-cand", "pass-reg-cand", [
        score_row("a", passed=True),
        score_row("b", passed=True),
        score_row("c", passed=False),
        score_row("d", passed=True),
    ])
    service = ComparisonService(store)
    report = service.classify_regression("run-reg-base", "run-reg-cand")
    assert report["classification"] == {
        "a": "persistent_pass",
        "b": "fixed",
        "c": "new_failure",
        "d": "added_case",
    }
    assert report["counts"]["fixed"] == 1
    assert report["counts"]["new_failure"] == 1
    # added 独立列出，不与共同样本的分类混算。
    assert report["counts"]["added_case"] == 1
    assert report["counts"]["removed_case"] == 0
    assert report["has_improvements"] is True
    assert report["has_regressions"] is True
    assert report["baseline_ref"]["scoring_pass_id"] == "pass-reg-base"
    assert report["candidate_ref"]["scoring_pass_id"] == "pass-reg-cand"


def test_classify_regression_removed_is_never_new_failure() -> None:
    store = InMemoryRunStore()
    make_run(store, "run-drop-base", case_ids=["x", "y"])
    append_pass(store, "run-drop-base", "pass-drop-base", [
        score_row("x", passed=True), score_row("y", passed=True),
    ])
    make_run(store, "run-drop-cand", case_ids=["x"])
    append_pass(store, "run-drop-cand", "pass-drop-cand", [score_row("x", passed=True)])
    service = ComparisonService(store)
    report = service.classify_regression("run-drop-base", "run-drop-cand")
    assert report["classification"] == {
        "x": "persistent_pass", "y": "removed_case",
    }
    assert report["counts"]["removed_case"] == 1
    assert report["counts"]["added_case"] == 0
    assert report["counts"]["new_failure"] == 0
    assert report["counts"]["fixed"] == 0
    assert report["has_regressions"] is True
    assert report["has_improvements"] is False


def test_service_reads_do_not_mutate_stored_facts() -> None:
    """快照 / baseline / gate 全程只读：重复读取不改变既有存储事实。"""
    store, service = mixed_store()
    before = deepcopy(store.scoring_passes.get("pass-mixed"))
    service.report_snapshot("run-mixed")
    service.case_outcomes("run-mixed")
    service.classify_regression("run-mixed", "run-mixed")
    assert store.scoring_passes.get("pass-mixed") == before


def test_deleted_evidence_lowers_new_eligibility_not_old_results() -> None:
    """A19/G21：证据（score sets）被删除后，新求值资格降低；已持久结论不变。

    工件/证据删除只影响**新**计算的输入；旧 GateResult bytes/hash 与
    baseline 引用不漂移，不从当前配置补历史事实。
    """
    store = InMemoryRunStore()
    make_run(store, "run-a19", case_ids=["k1", "k2"])
    append_pass(store, "run-a19", "pass-a19", [
        score_row("k1", passed=True), score_row("k2", passed=True),
    ])
    service = ComparisonService(store)
    service.publish_gate_policy(gate_policy_payload(
        policy_id="pol-a19",
        rules=[{
            "rule_id": "acc", "kind": "metric_threshold",
            "metric_id": "accuracy", "operator": "gte", "threshold": 0.5,
        }],
    ))
    first = service.evaluate_gate_versioned(
        policy_id="pol-a19", policy_version="1", run_id="run-a19",
        scoring_pass_id="pass-a19",
    )
    assert first["decision"] == "pass"
    stored_first = deepcopy(store.gate_store.get_result(first["gate_result_id"]))

    # 模拟证据删除：清空该 pass 的 score sets（import-like 缺字段同类）。
    store.score_sets._sets.pop("pass-a19", None)

    again = service.evaluate_gate_versioned(
        policy_id="pol-a19", policy_version="1", run_id="run-a19",
        scoring_pass_id="pass-a19",
    )
    assert again["decision"] == "insufficient_evidence"
    assert again["gate_result_id"] != first["gate_result_id"]
    # 已持久结论原样保留（bytes/hash 不漂移）。
    assert store.gate_store.get_result(first["gate_result_id"]) == stored_first
