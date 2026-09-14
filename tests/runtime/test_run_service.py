import pytest
from motte_sdk.service import RunService
from motte_storage.repositories import InMemoryRepository


def test_create_run_persists_queued_manifest():
    service = RunService(InMemoryRepository())
    run = service.create_run("scenario@1", {"model": {"id": "replay"}, "seed": 7})
    assert run["status"] == "queued"
    assert service.get_run(run["id"])["manifest"]["seed"] == 7


def test_execute_is_idempotent_and_emits_trace():
    service = RunService(InMemoryRepository())
    run = service.create_run("scenario@1", {})
    first = service.execute(run["id"], ["case-1"])
    second = service.execute(run["id"], ["case-1"])
    assert first == second
    assert first["status"] == "completed"
    assert len(service.events(run["id"])) >= 2


def test_cancel_prevents_execution():
    service = RunService(InMemoryRepository())
    run = service.create_run("scenario@1", {})
    assert service.cancel(run["id"])["status"] == "cancelled"
    with pytest.raises(ValueError, match="terminal"):
        service.execute(run["id"], ["case-1"])


def test_rescore_does_not_call_provider():
    calls = []
    service = RunService(InMemoryRepository(), provider=lambda case: calls.append(case) or {"ok": True})
    run = service.create_run("scenario@1", {})
    service.execute(run["id"], ["case-1"])
    assert calls == ["case-1"]
    assert service.rescore(run["id"])["status"] == "completed"
    assert calls == ["case-1"]
