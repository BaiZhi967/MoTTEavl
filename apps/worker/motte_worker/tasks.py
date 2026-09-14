from motte_sdk.service import RunService
from motte_storage.repositories import InMemoryRepository
from motte_storage.repositories import SQLiteRepository

_service = RunService(InMemoryRepository())
_queued: dict[str, tuple[str, tuple[str, ...]]] = {}


def configure_service(path):
    global _service
    _service = RunService(SQLiteRepository(path))


def execute_run(run_id, cases=()):
    return _service.execute(run_id, cases)


def enqueue_run(run_id, cases=()):
    task_id = f"task-{len(_queued) + 1}"
    _queued[task_id] = (run_id, tuple(cases))
    return {"task_id": task_id, "run_id": run_id, "status": "queued"}


def execute_queued_run(task_id):
    run_id, cases = _queued.pop(task_id)
    return execute_run(run_id, cases)
