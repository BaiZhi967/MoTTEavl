from apps.worker.motte_worker.tasks import configure_service, execute_run
from motte_sdk.service import RunService
from motte_storage.repositories import SQLiteRepository


def test_worker_executes_run_created_by_api_repository(tmp_path):
    path = tmp_path / "runs.db"
    service = RunService(SQLiteRepository(path))
    run = service.create_run("replay@1", {})
    configure_service(path)
    result = execute_run(run["id"], ["case-1"])
    assert result["status"] == "completed"
    assert RunService(SQLiteRepository(path)).get_run(run["id"])["status"] == "completed"


def test_trace_events_are_available_after_service_restart(tmp_path):
    path = tmp_path / "runs.db"
    first = RunService(SQLiteRepository(path))
    run = first.create_run("replay@1", {})
    first.execute(run["id"], ["case-1"])
    second = RunService(SQLiteRepository(path))
    assert [event["type"] for event in second.events(run["id"])] == ["queued", "running", "completed"]
