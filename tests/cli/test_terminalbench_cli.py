"""review R17：CLI ``status`` 必须和 API 用同一份冻结计划与分母。

反例（review 原文）：同一临时库冻结 2 Task × 2 Trial、mean-success，只有一个
Trial 有结果。API 为 ``mean-success/2 Task/4 Trial/coverage=.25``；CLI 为
``first-trial/1 Task/1 Trial/coverage=1.0``——因为 ``status`` 没有把冻结
manifest 传给 ``task_rows``，计划里的 Trial 从分母消失了。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from motte_benchmark.harbor.tasks import prepare_task_manifest
from motte_cli.main import main
from motte_sdk import terminalbench as tb
from motte_sdk.service import build_run_service

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "benchmarks" / "harbor" / "tasks"
RUN_ID = "run-cli-status"


def _seeded_run(db_path: Path) -> tuple[str, str]:
    """冻结 2 Task × 2 Trial、mean-success，只落一个 Trial 的结果。"""
    service = build_run_service(db_path)
    manifest = prepare_task_manifest(
        FIXTURE_ROOT, source_id="motte-harbor-fixtures", dataset_revision="fixtures-2026-09-20",
    )
    manifest.pop("prepared_at", None)
    record = {
        "manifest": manifest, "manifest_hash": manifest["manifest_hash"],
        "dataset_revision": "fixtures-2026-09-20", "task_root": str(FIXTURE_ROOT),
        "source_kind": "local",
    }
    profile = tb.terminal_bench_profile(n_trials=2, aggregation="mean-success")
    inputs = tb.build_run_inputs(
        record=record, run_id=RUN_ID, job_id="job-cli", profile=profile,
    )
    service.create_run(
        tb.SCENARIO_VERSION, inputs["manifest"], inputs["case_ids"], run_id=RUN_ID,
    )
    plans = inputs["trials"]
    service.store.trials.create_plans([dict(item) for item in plans])
    scored = plans[0]
    service.store.trials.put_result(
        str(scored["trial_id"]),
        {
            "trial_id": str(scored["trial_id"]),
            "disposition": "succeeded",
            "termination": {"reason": "completed"},
            "verifier_observation": {
                "status": "scored", "rewards": {"reward": 1.0}, "error": None,
            },
            "artifact_refs": [], "usage": {"cost_usd": None}, "coverage": {},
            "parser_version": "harbor-terminal-bench-parser@1",
        },
        source_hash="sha256:cli", parser_version="harbor-terminal-bench-parser@1",
    )
    return RUN_ID, str(scored["task_key"])


def test_cli_status_uses_the_frozen_plan_and_its_denominator(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "runs.db"
    run_id, scored_task = _seeded_run(db_path)
    capsys.readouterr()

    assert main(["terminal-bench", "status", "--run-id", run_id, "--json", "--db", str(db_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    rows = payload["items"]

    # 冻结计划里的**两个** Task 都必须出现：缺结果的 Task 不能从计划分母消失。
    assert len(rows) == 2, [row["task_key"] for row in rows]
    assert {row["task_key"] for row in rows} != {scored_task} or len(rows) == 2
    for row in rows:
        assert row["planned_trials"] == 2

    aggregate = rows[0]["aggregate"]
    assert aggregate["aggregation"] == "mean-success", "聚合规则来自冻结 provenance"
    assert aggregate["selected_trials"] == 4, "分母是计划 Trial 数，不是有结果的行数"
    assert aggregate["valid_trials"] == 1
    assert aggregate["valid_trial_coverage"] == 0.25
    assert payload["status"] == "queued"


def test_cli_status_matches_the_api_for_the_same_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """同一 Run：CLI 与 API 视图给出同一聚合与分母（review R17 的对账）。"""
    db_path = tmp_path / "runs.db"
    run_id, _scored_task = _seeded_run(db_path)
    capsys.readouterr()
    assert main(["terminal-bench", "status", "--run-id", run_id, "--json", "--db", str(db_path)]) == 0
    cli = json.loads(capsys.readouterr().out)

    service = build_run_service(db_path)
    run = service.get_run(run_id)
    api_rows = tb.task_rows(service.store, run_id, manifest=run.get("manifest") or {})
    api_aggregate = api_rows[0]["aggregate"]

    cli_aggregate = cli["items"][0]["aggregate"]
    for key in ("aggregation", "selected_trials", "valid_trials",
                "valid_trial_coverage", "valid_trial_pass_rate"):
        assert cli_aggregate[key] == api_aggregate[key], key
    assert [row["task_key"] for row in cli["items"]] == [
        row["task_key"] for row in api_rows
    ]


def test_cli_status_reports_unknown_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "runs.db"
    build_run_service(db_path)
    capsys.readouterr()
    assert main([
        "terminal-bench", "status", "--run-id", "run-missing", "--json", "--db", str(db_path),
    ]) == 2, "未知 Run 是结构化错误（与 CLI 其余命令一致的非零退出码）"
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"]["code"] == "RUN_NOT_FOUND"
