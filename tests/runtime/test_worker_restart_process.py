"""以真实子进程验证 Worker 的持久化与重启恢复（阶段 1 验收门）。"""
import json
import os
import subprocess
import sys
from pathlib import Path

from motte_sdk.service import RunService
from motte_storage.run_store import SQLiteRunStore

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = {
    "case-1": {"output": {"n": 1}, "expected": {"n": 1}},
    "case-2": {"output": {"n": 2}, "expected": {"n": 2}},
}


def _run_worker(db_path: Path) -> None:
    package_paths = [
        REPO_ROOT,
        *(REPO_ROOT / path for path in (
            "packages/contracts",
            "packages/sdk-python",
            "packages/provider-runtime",
            "packages/storage",
        )),
    ]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(map(str, package_paths))}
    subprocess.run(
        [sys.executable, "-m", "apps.worker.motte_worker", "--db", str(db_path), "--once"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        timeout=60,
        env=env,
    )


def test_worker_process_executes_and_resumes_after_restart(tmp_path):
    db_path = tmp_path / "runs.db"
    service = RunService(SQLiteRunStore(db_path))
    first = service.create_run("replay@1", {"provider": {"kind": "replay", "fixture": FIXTURE}}, case_ids=list(FIXTURE))

    # 模拟第一个进程在 case-1 后崩溃。
    preparing = service.store.runs.transition(
        first["id"], expected_revision=first["revision"], expected_status="queued",
        status="preparing", event={"type": "preparing"},
    )
    service.store.runs.transition(
        first["id"], expected_revision=preparing["revision"], expected_status="preparing",
        status="running", event={"type": "running"},
    )
    service.store.case_runs.upsert(
        {"run_id": first["id"], "case_id": "case-1", "result": {"n": 1}, "expected": {"n": 1}}
    )

    # 新进程启动：recover + 执行到终态。
    _run_worker(db_path)

    recovered = RunService(SQLiteRunStore(db_path))
    result = recovered.get_run(first["id"])
    assert result["status"] == "completed"
    rows = recovered.store.case_runs.list_for_run(first["id"])
    assert [row["case_id"] for row in rows] == ["case-1", "case-2"]
    assert result["scores"] == [
        {"case_id": "case-1", "passed": True},
        {"case_id": "case-2", "passed": True},
    ]

    # 再跑一次 worker：无事可做，也不产生重复 CaseRun。
    _run_worker(db_path)
    rows = recovered.store.case_runs.list_for_run(first["id"])
    assert [row["case_id"] for row in rows] == ["case-1", "case-2"]


def test_worker_process_picks_up_retry_child_run(tmp_path):
    db_path = tmp_path / "runs.db"
    service = RunService(SQLiteRunStore(db_path))
    run = service.create_run("replay@1", {}, case_ids=[])
    service.cancel(run["id"], reason="operator request")
    child = service.retry(run["id"])
    service.store.runs.update(
        {
            **service.store.runs.get(child["id"]),
            "manifest": {"provider": {"kind": "replay", "fixture": {"case-1": FIXTURE["case-1"]}}},
            "case_ids": ["case-1"],
        },
        expected_revision=child["revision"],
        expected_status="queued",
    )

    _run_worker(db_path)

    result = RunService(SQLiteRunStore(db_path)).get_run(child["id"])
    assert result["status"] == "completed"
    assert result["scores"] == [{"case_id": "case-1", "passed": True}]
    assert json.dumps(result["parent_run_id"]) == json.dumps(run["id"])
