from apps.worker.motte_worker.runtime import WorkerLoop
from apps.worker.motte_worker.tasks import configure_service
from motte_contracts.run import Run
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


def _stderr_events(capsys):
    """Worker 进度日志：stderr 上一行一个 JSON 对象。"""
    import json

    return [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()]


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
    # Worker 抢占路径与直连执行路径的事件序列必须一致。
    assert [event["type"] for event in worker.service.events(run["id"])] == [
        "queued",
        "preparing",
        "running",
        "model_response",
        "model_response",
        "collecting",
        "scoring",
        "score",
        "score",
        "scoring_pass_created",
        "completed",
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
    preparing = service.store.runs.transition(
        run["id"], expected_revision=run["revision"], expected_status="queued",
        status="preparing", event={"type": "preparing"},
    )
    service.store.runs.transition(
        run["id"], expected_revision=preparing["revision"], expected_status="preparing",
        status="running", event={"type": "running"},
    )
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


def test_worker_once_on_empty_queue_is_silent(tmp_path, capsys):
    """空队列不刷屏：--once 处理完就退出，stdout/stderr 都不写。"""
    from apps.worker.motte_worker.__main__ import main

    assert main(["--db", str(tmp_path / "runs.db"), "--once"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_worker_quiet_suppresses_progress_log(tmp_path, capsys):
    from apps.worker.motte_worker.__main__ import main

    path = tmp_path / "runs.db"
    RunService(SQLiteRunStore(path)).create_run("replay@1", REPLAY_MANIFEST, case_ids=["case-1"])
    assert main(["--db", str(path), "--once", "--quiet"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_worker_interrupt_after_dispatch_requires_review(tmp_path, capsys, monkeypatch):
    """A dispatched call with no durable result is never replayed automatically."""
    from apps.worker.motte_worker.__main__ import main
    from motte_sdk.dispatcher import RunDispatcher

    path = tmp_path / "runs.db"
    paid_manifest = {
        **REPLAY_MANIFEST,
        "execution": {
            "backend_id": "direct-llm", "backend_version": "1",
            "capabilities": {"interactive": False, "safe_to_repeat": False},
        },
    }
    run = RunService(SQLiteRunStore(path)).create_run(
        "direct-llm@1", paid_manifest, case_ids=["case-1", "case-2"])

    def interrupted(dispatcher, claimed):
        dispatcher.service._begin_case_attempt(claimed, "case-1")
        raise KeyboardInterrupt

    monkeypatch.setattr(RunDispatcher, "execute_claimed", interrupted)
    assert main(["--db", str(path)]) == 130
    events = _stderr_events(capsys)
    assert events[-1] == {"component": "worker", "event": "worker_stopped",
                          "reason": "interrupted"}
    store = RunService(SQLiteRunStore(path)).store
    assert store.runs.get(run["id"])["status"] == "preparing"
    assert store.attempts.list_open(run["id"])[0]["status"] == "dispatching"
    assert store.case_runs.list_for_run(run["id"]) == []

    monkeypatch.undo()
    assert main(["--db", str(path), "--once"]) == 0
    recovered = RunService(SQLiteRunStore(path)).store
    recovered_run = recovered.runs.get(run["id"])
    assert recovered_run["status"] == "needs_review"
    assert Run.model_validate(recovered_run).error.details["attempt_ids"]
    assert recovered.attempts.list_for_run(run["id"])[0]["status"] == "indeterminate"
    assert recovered.case_runs.list_for_run(run["id"]) == []


def test_safe_replay_attempt_is_requeued_after_interrupt(tmp_path):
    service = RunService(SQLiteRunStore(tmp_path / "safe-replay.db"))
    run = service.create_run("replay@1", REPLAY_MANIFEST, case_ids=["case-1"])
    claimed = service.store.runs.claim(run["id"])
    attempt = service._begin_case_attempt(claimed, "case-1")

    requeued = WorkerLoop(service).recover_interrupted()
    assert requeued == [run["id"]]
    assert service.store.runs.get(run["id"])["status"] == "queued"
    assert service.store.attempts.get(attempt["id"])["status"] == "failed"
    assert WorkerLoop(service).claim_and_execute(run["id"])["status"] == "completed"


def test_worker_interrupt_during_once_also_returns_130(tmp_path, capsys, monkeypatch):
    """--once 与长轮询共用同一段 try：两条路径的中断处理必须一致。"""
    from apps.worker.motte_worker.__main__ import main
    from motte_sdk.dispatcher import RunDispatcher

    def interrupted(dispatcher, claimed):
        raise KeyboardInterrupt

    path = tmp_path / "runs.db"
    RunService(SQLiteRunStore(path)).create_run("replay@1", REPLAY_MANIFEST, case_ids=["case-1"])
    monkeypatch.setattr(RunDispatcher, "execute_claimed", interrupted)
    assert main(["--db", str(path), "--once"]) == 130
    assert _stderr_events(capsys)[-1]["reason"] == "interrupted"
