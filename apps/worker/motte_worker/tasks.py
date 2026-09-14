from motte_sdk.service import RunService, build_run_service
from motte_storage.repositories import SQLiteRepository

_service: RunService | None = None
_queued: dict[str, tuple[str, tuple[str, ...]]] = {}


def get_service() -> RunService:
    global _service
    if _service is None:
        _service = build_run_service()
    return _service


def configure_service(path=None) -> None:
    global _service
    _service = RunService(SQLiteRepository(path)) if path is not None else build_run_service()


def execute_run(run_id, cases=()):
    return get_service().execute(run_id, cases)


def enqueue_run(run_id, cases=()):
    task_id = f"task-{len(_queued) + 1}"
    _queued[task_id] = (run_id, tuple(cases))
    return {"task_id": task_id, "run_id": run_id, "status": "queued"}


def execute_queued_run(task_id):
    run_id, cases = _queued.pop(task_id)
    return execute_run(run_id, cases)
