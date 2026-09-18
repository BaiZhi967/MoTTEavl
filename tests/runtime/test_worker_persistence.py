from apps.worker.motte_worker.tasks import configure_service, execute_run
from motte_sdk.service import RunService
from motte_storage.run_store import SQLiteRunStore


def test_worker_executes_run_created_by_api_repository(tmp_path):
    path = tmp_path / "runs.db"
    service = RunService(SQLiteRunStore(path))
    run = service.create_run(
        "replay@1",
        {"provider": {"kind": "replay", "fixture": {"case-1": {"output": 1, "expected": 1}}}},
        case_ids=["case-1"],
    )
    configure_service(path)
    result = execute_run(run["id"], ["case-1"])
    assert result["status"] == "completed"
    assert RunService(SQLiteRunStore(path)).get_run(run["id"])["status"] == "completed"


def test_trace_events_are_available_after_service_restart(tmp_path):
    path = tmp_path / "runs.db"
    first = RunService(SQLiteRunStore(path))
    run = first.create_run("replay@1", {})
    first.execute(run["id"], ["case-1"], provider=lambda case_id: {"case_id": case_id})
    second = RunService(SQLiteRunStore(path))
    assert [event["type"] for event in second.events(run["id"])] == [
        "queued",
        "preparing",
        "running",
        "model_response",
        "collecting",
        "scoring",
        "scoring_pass_created",
        "completed",
    ]
