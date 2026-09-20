"""review round-2：公共执行链路的取消、retry、跨 Run 身份与逐条隔离。

对应报告 `docs/verification/M3-review-round2-2026-09-20.md` 的 M3-R2-01/02/03/09。
全部走**正式服务入口**（`RunService.create_run` / `execute_external_job` /
`retry` / `cancel`）与真实 DurableExternalJobRunner + fixture adapter，
不使用 supervisor 单测替代整链。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from motte_sdk import terminalbench as tb
from motte_storage.artifacts import ArtifactStore
from motte_storage.external_jobs import MemoryExternalJobs
from motte_sdk.external_jobs import DurableExternalJobRunner, ExternalJobSupervisor
from motte_benchmark.harbor.parser import PARSER_VERSION

from tests.integration.test_harbor_job_fixture import (  # noqa: E402 - 复用夹具脚手架
    SAMPLES,
    TASKS_ROOT,
    FixtureHarborAdapter,
    _build_store,
)

SAMPLE = "pass-fail-2x2"


def _inputs_for(
    record: dict, *, run_id: str, n_trials: int = 2,
) -> dict[str, Any]:
    """按**目标 Run 身份**构造冻结输入（Trial 身份由 run_id 派生）。"""
    return tb.build_run_inputs(
        record=record, run_id=run_id, job_id=f"job-{run_id}",
        profile=tb.terminal_bench_profile(n_trials=n_trials),
    )


def _runner(service, tmp_path: Path, *, sample: str = SAMPLE, task_root: Path = TASKS_ROOT):
    adapter = FixtureHarborAdapter(sample and SAMPLES / sample, argv=["/bin/true"],
                                   data_root=str(task_root))
    job_store = MemoryExternalJobs()
    runner = DurableExternalJobRunner(
        ExternalJobSupervisor(adapter, poll_interval_seconds=0.01, sleep=lambda _s: None),
        job_store, artifacts=ArtifactStore(str(tmp_path / "artifacts")),
        work_root=str(tmp_path / "jobs"), parser_version=PARSER_VERSION,
    )
    return adapter, job_store, runner


def _create(service, inputs: dict, *, run_id: str) -> dict:
    return service.create_run(
        inputs["scenario_version"], inputs["manifest"], inputs["case_ids"], run_id=run_id,
    )


# ---------------------------------------------------------------- M3-R2-01


def test_cancellation_keeps_frozen_results_that_arrived_before_the_import(
    tmp_path: Path,
) -> None:
    """取消不能丢掉已冻结结果：job_entry 返回前落取消请求，4 条结果仍要落库。

    反例（review）：正常对照 completed/4 Trial/4 scores；修复前取消场景
    cancelled/0 Trial/0 scores，即使 outcome 明确包含 4 条完成结果。
    这是"延迟取消"路径（本进程无 interrupter）：执行者先导入冻结结果再终态化。
    """
    service, record = _build_store(tmp_path)
    inputs = _inputs_for(record, run_id="run-cancel-import")
    _create(service, inputs, run_id="run-cancel-import")
    run = service._load("run-cancel-import")
    _adapter, _job_store, runner = _runner(service, tmp_path)

    def job_entry(_run: dict) -> dict:
        outcome = runner(_run)
        # 结果已在手、尚未导入时，操作员请求取消。
        service.cancel("run-cancel-import", reason="operator-during-import")
        return outcome

    view = service.execute_external_job("run-cancel-import", run, job_entry=job_entry)
    assert view["status"] == "cancelled"

    rows = service.store.trials.list_for_run("run-cancel-import")
    assert len(rows) == 4, "计划单元不能因取消而消失"
    with_results = [row for row in rows if row.get("result")]
    assert len(with_results) == 4, "已冻结的 4 条结果必须全部落库"
    assert all(
        not (row["result"] or {}).get("synthesized_by") for row in with_results
    ), "已完成单元必须是真实证据，不能是平台占位"

    passes = service.store.scoring_passes.list_for_run("run-cancel-import")
    assert passes, "取消也要给出可审计的评分结论"
    assert passes[-1]["summary"]["scores"] == 4, "已冻结的 Trial 必须进入结论"
    assert service.get_run("run-cancel-import")["status"] == "cancelled", "终态不复活"


def test_immediate_cancellation_placeholders_are_replaced_by_real_evidence(
    tmp_path: Path,
) -> None:
    """立即取消（同进程有 interrupter）：终态占位不得阻碍后续真实结果导入。

    review 原文："若预先创建计划，提前写入的 synthetic 终态还会阻碍后续真实
    结果导入"。这里取消发生在执行中——cancel() 走 interrupter 分支立刻终态化，
    给 4 个单元写 cancelled 占位；执行者随后导入真实冻结结果，占位必须被替换
    并留下审计。
    """
    service, record = _build_store(tmp_path)
    inputs = _inputs_for(record, run_id="run-cancel-immediate")
    _create(service, inputs, run_id="run-cancel-immediate")
    run = service._load("run-cancel-immediate")
    _adapter, _job_store, runner = _runner(service, tmp_path)

    def job_entry(_run: dict) -> dict:
        outcome = runner(_run)
        service.cancel("run-cancel-immediate", reason="operator-immediate")
        return outcome

    job_entry.interrupt_run = lambda _run_id: None  # type: ignore[attr-defined]

    view = service.execute_external_job(
        "run-cancel-immediate", run, job_entry=job_entry,
    )
    assert view["status"] == "cancelled"

    rows = service.store.trials.list_for_run("run-cancel-immediate")
    assert len(rows) == 4
    assert all(row.get("result") for row in rows)
    assert all(
        not (row["result"] or {}).get("synthesized_by") for row in rows
    ), "真实冻结结果必须替换掉取消占位"

    events = {event["type"]: event for event in service.events("run-cancel-immediate")}
    assert "trial_placeholders_replaced" in events, sorted(events)
    assert events["trial_placeholders_replaced"]["count"] >= 1
    assert "external_job_results_after_terminal" in events
    passes = service.store.scoring_passes.list_for_run("run-cancel-immediate")
    assert any(item["summary"].get("scores") == 4 for item in passes), [
        (item["source"], item["summary"].get("scores")) for item in passes
    ]


def test_cancellation_marks_only_units_without_results(tmp_path: Path) -> None:
    """部分完成时取消：已完成的保留真实结果，未产出的补 cancelled 占位。"""
    service, record = _build_store(tmp_path)
    inputs = _inputs_for(record, run_id="run-cancel-partial")
    _create(service, inputs, run_id="run-cancel-partial")
    run = service._load("run-cancel-partial")
    _adapter, _job_store, runner = _runner(service, tmp_path)

    def job_entry(_run: dict) -> dict:
        outcome = runner(_run)
        # 只保留两条结果（模拟"Job 被中断，只有部分 Trial 产出证据"）。
        outcome["results"] = [
            row for row in outcome["results"]
            if row["output"]["repeat_index"] == 0
        ]
        service.cancel("run-cancel-partial", reason="operator-mid-job")
        return outcome

    view = service.execute_external_job("run-cancel-partial", run, job_entry=job_entry)
    assert view["status"] == "cancelled"
    rows = service.store.trials.list_for_run("run-cancel-partial")
    assert len(rows) == 4
    real = [row for row in rows if (row.get("result") or {}).get("synthesized_by") is None]
    placeholders = [
        row for row in rows if (row.get("result") or {}).get("synthesized_by")
    ]
    assert len(real) == 2, [row["trial_id"] for row in real]
    assert len(placeholders) == 2
    assert {row["result"]["disposition"] for row in placeholders} == {"cancelled"}
    # 覆盖分母仍然完整：2/4 有效。
    aggregate = tb.task_rows(
        service.store, "run-cancel-partial", manifest=run.get("manifest") or {},
    )[0]["aggregate"]
    assert aggregate["selected_trials"] == 4
    assert aggregate["valid_trials"] == 2
    assert aggregate["valid_trial_coverage"] == 0.5


def test_cross_process_cancel_request_is_consumed_after_import(tmp_path: Path) -> None:
    """另一实例落下的取消请求：Worker 导入冻结结果后再终态化（不丢证据）。"""
    service, record = _build_store(tmp_path)
    inputs = _inputs_for(record, run_id="run-cancel-cross")
    _create(service, inputs, run_id="run-cancel-cross")
    run = service._load("run-cancel-cross")
    _adapter, _job_store, runner = _runner(service, tmp_path)
    other = type(service)(service.store)  # 第二个实例：API/Worker 分进程形态

    def job_entry(_run: dict) -> dict:
        outcome = runner(_run)
        # 另一实例只请求取消（无 interrupter）：请求落在存储里。
        requested = other.cancel("run-cancel-cross", reason="cross-process")
        assert requested["status"] in {"running", "cancelled"}
        return outcome

    view = service.execute_external_job("run-cancel-cross", run, job_entry=job_entry)
    assert view["status"] == "cancelled"
    rows = service.store.trials.list_for_run("run-cancel-cross")
    assert len(rows) == 4
    assert all(row.get("result") for row in rows)
    assert all(
        (row["result"] or {}).get("synthesized_by") is None for row in rows
    ), "冻结结果不能被取消占位顶掉"


# ---------------------------------------------------------------- M3-R2-02


def test_retry_refreezes_child_trial_and_job_identity(tmp_path: Path) -> None:
    """retry 必须重新派生 Trial/Job 身份：父子 Trial ID 不相交、各自有结果。"""
    service, record = _build_store(tmp_path)
    inputs = _inputs_for(record, run_id="run-parent")
    _create(service, inputs, run_id="run-parent")
    # 让父 Run 进入可 retry 的终态（failed）。
    service._transition("run-parent", "preparing")
    service._transition("run-parent", "running")
    service._fail("run-parent", ValueError("synthetic parent failure"))
    assert service.get_run("run-parent")["status"] == "failed"

    child = service.retry("run-parent")
    assert child["parent_run_id"] == "run-parent"
    child_id = child["id"]
    assert child_id != "run-parent"

    parent_trials = {str(item["trial_id"]) for item in inputs["trials"]}
    child_manifest = child["manifest"]
    child_plan = child_manifest["external_benchmark"]["runner_config"]["plan"]["trials"]
    child_trials = {str(item["trial_id"]) for item in child_plan}
    assert child_trials and parent_trials.isdisjoint(child_trials), "Trial ID 必须不相交"
    assert {str(item["run_id"]) for item in child_plan} == {child_id}
    assert {
        str(item["run_id"])
        for item in child_manifest["task_manifest"]["trials"]
    } == {child_id}
    assert child_manifest["external_benchmark"]["runner_config"]["harbor"]["job"][
        "job_name"
    ] != inputs["manifest"]["external_benchmark"]["runner_config"]["harbor"]["job"]["job_name"]

    # 子 Run 走完公共执行：自己的 Trial 全部有结果、有自己的分数。
    run = service._load(child_id)
    _adapter2, _job_store2, runner = _runner(service, tmp_path)
    view = service.execute_external_job(child_id, run, job_entry=lambda _r: runner(_r))
    assert view["status"] == "completed", view.get("error")
    child_rows = service.store.trials.list_for_run(child_id)
    assert len(child_rows) == 4
    assert {str(row["trial_id"]) for row in child_rows} == child_trials
    assert all(row.get("result") for row in child_rows)
    assert len(view["scores"]) == 4

    # 父 Run 的证据不受影响（没有自己的结果被改写/覆盖）。
    parent_rows = service.store.trials.list_for_run("run-parent")
    assert all(row.get("result") is None for row in parent_rows)


# ---------------------------------------------------------------- M3-R2-03


def _foreign_outcome(service, inputs: dict, *, trial_id: str) -> dict:
    """构造一条"指向别的 Run 的 Trial"的 Runner 行。"""
    plan = inputs["manifest"]["external_benchmark"]["runner_config"]["plan"]["trials"][0]
    return {
        "job_status": "settled",
        "results": [{
            "case_id": str(plan["task_key"]),
            "stable_case_key": f"{plan['task_key']}#{plan['repeat_index']}",
            "status": "succeeded",
            "output": {
                "trial_id": trial_id,
                "task_key": str(plan["task_key"]),
                "repeat_index": int(plan["repeat_index"]),
                "disposition": "succeeded",
                "verifier_observation": {"status": "scored", "rewards": {"reward": 1.0}},
                "usage": {},
                "coverage": {},
                "parser_version": PARSER_VERSION,
            },
        }],
        "error": None,
        "handle": {},
        "cursor": {},
    }


def test_trial_from_another_run_is_quarantined_not_written(tmp_path: Path) -> None:
    """指向另一个 Run 的 Trial ID：隔离且**不改动**那个 Run 的任何记录。"""
    service, record = _build_store(tmp_path)
    # victim：另一个 Run 的合法计划，先落库并写入一条真实结果。
    victim_inputs = _inputs_for(record, run_id="run-victim")
    _create(service, victim_inputs, run_id="run-victim")
    victim_trial = str(victim_inputs["trials"][0]["trial_id"])
    service.store.trials.create_plans([dict(item) for item in victim_inputs["trials"]])
    victim_before = service.store.trials.get(victim_trial)
    assert victim_before is not None

    # review：自己的合法计划 + 一条指向 victim Trial 的 outcome。
    inputs = _inputs_for(record, run_id="run-review")
    _create(service, inputs, run_id="run-review")
    run = service._load("run-review")
    outcome = _foreign_outcome(service, inputs, trial_id=victim_trial)
    view = service.execute_external_job("run-review", run, job_entry=lambda _r: outcome)

    assert view["status"] == "failed", "跨 Run 身份必须让 Run 显式失败"
    errors = [row["result"]["error"] for row in view["cases"]
              if isinstance(row.get("result"), dict) and row["result"].get("error")]
    assert any(
        (error or {}).get("code") == "EXTERNAL_UNMAPPED_RESULTS"
        for error in errors + [view.get("error") or {}]
    ), errors + [view.get("error")]
    # victim 的记录逐字不变。
    assert service.store.trials.get(victim_trial) == victim_before
    # 自己的计划单元没有被别人的结果顶掉。
    own = service.store.trials.list_for_run("run-review")
    assert own and all(row.get("result") is None for row in own)


def test_unknown_trial_id_is_quarantined_and_run_is_not_falsely_completed(
    tmp_path: Path,
) -> None:
    """不存在的 Trial ID：显式隔离，Run 不能假装完成。"""
    service, record = _build_store(tmp_path)
    inputs = _inputs_for(record, run_id="run-unknown-trial")
    _create(service, inputs, run_id="run-unknown-trial")
    run = service._load("run-unknown-trial")
    outcome = _foreign_outcome(service, inputs, trial_id="trial-does-not-exist")
    view = service.execute_external_job(
        "run-unknown-trial", run, job_entry=lambda _r: outcome,
    )
    assert view["status"] == "failed"
    own = service.store.trials.list_for_run("run-unknown-trial")
    assert own and all(row.get("result") is None for row in own)


def test_payload_disagreeing_with_the_frozen_plan_is_quarantined(tmp_path: Path) -> None:
    """payload 自称的 repeat_index 与冻结计划不符：隔离，不写入。"""
    service, record = _build_store(tmp_path)
    inputs = _inputs_for(record, run_id="run-mismatch")
    _create(service, inputs, run_id="run-mismatch")
    run = service._load("run-mismatch")
    trial_id = str(inputs["trials"][0]["trial_id"])
    outcome = _foreign_outcome(service, inputs, trial_id=trial_id)
    outcome["results"][0]["output"]["repeat_index"] = 99  # 与冻结计划不符
    view = service.execute_external_job("run-mismatch", run, job_entry=lambda _r: outcome)
    assert view["status"] == "failed"
    assert service.store.trials.get(trial_id)["result"] is None


# ---------------------------------------------------------------- M3-R2-09


def test_one_invalid_payload_does_not_lose_its_siblings(tmp_path: Path) -> None:
    """单条非法 payload：拒绝它，但合法兄弟结果继续落库（逐条隔离）。"""
    service, record = _build_store(tmp_path)
    inputs = _inputs_for(record, run_id="run-partial-invalid")
    _create(service, inputs, run_id="run-partial-invalid")
    run = service._load("run-partial-invalid")
    _adapter, _job_store, runner = _runner(service, tmp_path)

    def job_entry(_run: dict) -> dict:
        outcome = runner(_run)
        # 排序第二条 payload 带错 run_id（身份错配，必须被拒绝）。
        ordered = sorted(
            outcome["results"], key=lambda row: (row["output"]["task_key"],
                                                 row["output"]["repeat_index"]),
        )
        ordered[1]["output"]["run_id"] = "run-somewhere-else"
        return outcome

    view = service.execute_external_job(
        "run-partial-invalid", run, job_entry=job_entry,
    )
    assert view["status"] == "failed", "部分成功不能掩盖被拒绝的记录"
    codes = [
        (row["result"] or {}).get("error", {}).get("code")
        for row in view["cases"] if isinstance(row.get("result"), dict)
    ]
    assert "TRIAL_IMPORT_INVALID" in [view.get("error", {}).get("code")] + codes or any(
        "TRIAL_IMPORT_INVALID" in json.dumps(row.get("result") or {})
        for row in view["cases"]
    ), [view.get("error"), codes]
    rows = service.store.trials.list_for_run("run-partial-invalid")
    stored = [row for row in rows if (row.get("result") or {}).get("synthesized_by") is None
              and row.get("result") is not None]
    placeholders = [row for row in rows if (row.get("result") or {}).get("synthesized_by")]
    assert len(stored) == 3, "合法兄弟结果必须保留"
    assert len(placeholders) == 1, "被拒绝的单元要有明确处置"
    assert placeholders[0]["result"]["disposition"] == "indeterminate"
    # 派生视图只消费已接受记录：三个 Task 行都来自已落库事实。
    responded = [row for row in view["cases"] if row.get("outcome") == "responded"]
    assert len(responded) == 2
