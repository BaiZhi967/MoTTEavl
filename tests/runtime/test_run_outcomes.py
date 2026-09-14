import pytest
from motte_sdk.service import RunService
from motte_storage.run_store import InMemoryRunStore


def test_provider_failure_is_persisted_as_failed():
    def failing(_case):
        raise RuntimeError("provider unavailable")

    service = RunService(InMemoryRunStore(), provider=failing)
    run = service.create_run("replay@1", {})
    result = service.execute(run["id"], ["case-1"])
    assert result["status"] == "failed"
    assert result["error"]["type"] == "RuntimeError"


def test_retry_creates_explicit_child_run():
    service = RunService(InMemoryRunStore())
    run = service.create_run("replay@1", {})
    service.mark_unsupported(run["id"], "MODEL_CAPABILITY_UNSUPPORTED")
    retry = service.retry(run["id"])
    assert retry["status"] == "queued"
    assert retry["parent_run_id"] == run["id"]


def test_unsupported_run_cannot_execute():
    service = RunService(InMemoryRunStore())
    run = service.create_run("vision@1", {})
    service.mark_unsupported(run["id"], "MODEL_CAPABILITY_UNSUPPORTED")
    with pytest.raises(ValueError, match="terminal"):
        service.execute(run["id"], [])
