from apps.worker.motte_worker.runtime import WorkerLoop
from apps.worker.motte_worker.tasks import configure_service
from motte_sdk.service import RunService
from motte_storage.run_store import SQLiteRunStore

REPLAY_MANIFEST = {
    "provider": {
        "kind": "replay",
        "fixture": {
            "case-1": {"output": {"n": 1}, "expected": {"n": 1}},
            "case-2": {"output": {"n": 2}, "expected": {"n": 2}},
        },
    }
}


def test_worker_executes_replay_run_created_by_api(tmp_path):
    path = tmp_path / "runs.db"
    service = RunService(SQLiteRunStore(path))
    run = service.create_run("replay@1", REPLAY_MANIFEST, case_ids=["case-1", "case-2"])
    worker = WorkerLoop(RunService(SQLiteRunStore(path)))
    result = worker.claim_and_execute()
    assert result["id"] == run["id"]
    assert result["status"] == "completed"
    assert result["scores"] == [
        {"case_id": "case-1", "passed": True},
        {"case_id": "case-2", "passed": True},
    ]
    assert worker.claim_and_execute() is None


def test_claim_is_exclusive_between_polls(tmp_path):
    path = tmp_path / "runs.db"
    service = RunService(SQLiteRunStore(path))
    service.create_run("replay@1", REPLAY_MANIFEST, case_ids=["case-1"])
    store = SQLiteRunStore(path)
    first = store.runs.claim_next_queued()
    assert first["status"] == "preparing"
    assert store.runs.claim_next_queued() is None


def test_worker_recovers_interrupted_run_after_restart(tmp_path):
    path = tmp_path / "runs.db"
    service = RunService(SQLiteRunStore(path))
    run = service.create_run("replay@1", REPLAY_MANIFEST, case_ids=["case-1", "case-2"])

    # 模拟 Worker 在 case-1 完成后、case-2 之前崩溃：状态卡在 running。
    crashed = service.store.runs.get(run["id"])
    crashed["status"] = "running"
    service.store.runs.save(crashed)
    service.store.case_runs.upsert(
        {
            "run_id": run["id"],
            "case_id": "case-1",
            "result": {"n": 1},
            "expected": {"n": 1},
        }
    )

    worker = WorkerLoop(RunService(SQLiteRunStore(path)))
    assert worker.recover_interrupted() == [run["id"]]
    result = worker.claim_and_execute()
    assert result["status"] == "completed"
    rows = worker.service.store.case_runs.list_for_run(run["id"])
    assert [row["case_id"] for row in rows] == ["case-1", "case-2"]
    assert rows[0]["result"] == {"n": 1}
    assert result["scores"] == [
        {"case_id": "case-1", "passed": True},
        {"case_id": "case-2", "passed": True},
    ]


def test_worker_skips_terminal_runs(tmp_path):
    path = tmp_path / "runs.db"
    service = RunService(SQLiteRunStore(path))
    run = service.create_run("replay@1", REPLAY_MANIFEST, case_ids=["case-1"])
    service.cancel(run["id"], reason="operator request")
    worker = WorkerLoop(RunService(SQLiteRunStore(path)))
    assert worker.claim_and_execute() is None


def test_celery_task_executes_run(tmp_path):
    from apps.worker.motte_worker.celery_app import execute_run_task

    path = tmp_path / "runs.db"
    service = RunService(SQLiteRunStore(path))
    run = service.create_run("replay@1", REPLAY_MANIFEST, case_ids=["case-1"])
    configure_service(path)
    result = execute_run_task.run(run["id"], ["case-1"])
    assert result["status"] == "completed"
