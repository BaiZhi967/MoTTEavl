from pathlib import Path

from motte_sdk.service import RunService, build_run_service
from motte_storage.run_store import SQLiteRunStore

from .coordination import worker_execution_lock
from .reporting import WorkerReporter
from .runtime import WorkerLoop

_service: RunService | None = None
_worker: WorkerLoop | None = None
_db_path: str | Path | None = None
_queued: dict[str, tuple[str, tuple[str, ...]]] = {}


def get_service() -> RunService:
    global _service
    if _service is None:
        _service = build_run_service()
    return _service


def configure_service(path=None) -> None:
    global _db_path, _service, _worker
    _db_path = path
    _service = RunService(SQLiteRunStore(path)) if path is not None else build_run_service()
    _worker = None


def get_worker() -> WorkerLoop:
    global _worker
    if _worker is None:
        _worker = WorkerLoop(
            get_service(), reporter=WorkerReporter(enabled=False), execution_lock_held=True
        )
    return _worker


def execute_run(run_id, cases=()):
    """Celery/local task entry that uses the same claim and backend Dispatcher as WorkerLoop."""
    service = get_service()
    run = service.get_run(run_id)
    requested = list(cases)
    if requested and requested != list(run.get("case_ids") or []):
        raise ValueError("task case ids must match the persisted run selection")
    if run["status"] in RunService.TERMINAL:
        return run
    with worker_execution_lock(_db_path):
        worker = get_worker()
        worker.recover_interrupted()
        result = worker.claim_and_execute(run_id)
    if result is None:
        raise ValueError(f"run is not queued: {run_id}")
    return result


def enqueue_run(run_id, cases=()):
    task_id = f"task-{len(_queued) + 1}"
    _queued[task_id] = (run_id, tuple(cases))
    return {"task_id": task_id, "run_id": run_id, "status": "queued"}


def execute_queued_run(task_id):
    run_id, cases = _queued.pop(task_id)
    return execute_run(run_id, cases)
