from apps.worker.motte_worker.tasks import configure_service, enqueue_run, execute_queued_run
from motte_sdk.service import RunService
from motte_storage.run_store import SQLiteRunStore


def test_worker_queue_transitions_queued_run_to_completed(tmp_path):
    path = tmp_path / "runs.db"
    service = RunService(SQLiteRunStore(path))
    run = service.create_run(
        "replay@1",
        {"provider": {"kind": "replay", "fixture": {"case-1": {"output": 1, "expected": 1}}}},
        case_ids=["case-1"],
    )
    configure_service(path)
    ticket = enqueue_run(run["id"], ["case-1"])
    assert ticket["status"] == "queued"
    result = execute_queued_run(ticket["task_id"])
    assert result["status"] == "completed"
