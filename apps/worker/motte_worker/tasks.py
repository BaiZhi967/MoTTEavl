from motte_sdk.service import RunService
from motte_storage.repositories import InMemoryRepository
from motte_storage.repositories import SQLiteRepository

_service = RunService(InMemoryRepository())


def configure_service(path):
    global _service
    _service = RunService(SQLiteRepository(path))


def execute_run(run_id, cases=()):
    return _service.execute(run_id, cases)
