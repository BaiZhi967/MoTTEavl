from motte_sdk.service import RunService
from motte_storage.repositories import InMemoryRepository

_service = RunService(InMemoryRepository())


def execute_run(run_id, cases=()):
    return _service.execute(run_id, cases)
