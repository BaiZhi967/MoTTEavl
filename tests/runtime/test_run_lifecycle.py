import pytest
from motte_sdk.replay_run import ReplayProvider
from motte_sdk.service import TRANSITIONS, RunService, build_run_service
from motte_storage.repositories import InMemoryRepository, SQLiteRepository


def test_build_run_service_honors_motte_db_path(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_DB_PATH", str(tmp_path / "factory.db"))
    service = build_run_service()
    created = service.create_run("replay@1", {})
    reopened = build_run_service()
    assert reopened.get_run(created["id"])["status"] == "queued"


def test_execute_emits_full_lifecycle_events(tmp_path):
    fixture = {
        "case-1": {"output": {"n": 1}, "expected": {"n": 1}},
        "case-2": {"output": {"n": 2}, "expected": {"n": 9}},
    }
    service = RunService(SQLiteRepository(tmp_path / "runs.db"), provider=ReplayProvider(fixture).invoke)
    run = service.create_run("replay@1", {})
    result = service.execute(run["id"], fixture.keys())
    assert [event["type"] for event in service.events(run["id"])] == [
        "queued",
        "preparing",
        "running",
        "model_response",
        "model_response",
        "collecting",
        "scoring",
        "score",
        "score",
        "completed",
    ]
    assert result["scores"] == [
        {"case_id": "case-1", "passed": True},
        {"case_id": "case-2", "passed": False},
    ]


def test_cancel_records_reason_and_blocks_remaining_cases():
    service = RunService(InMemoryRepository())
    run = service.create_run("replay@1", {}, case_ids=["case-1", "case-2", "case-3"])
    invoked: list[str] = []

    def provider(case_id):
        invoked.append(case_id)
        if case_id == "case-2":
            service.cancel(run["id"], reason="operator request")
        return {"case_id": case_id}

    result = service.execute(run["id"], provider=provider)
    assert result["status"] == "cancelled"
    assert invoked == ["case-1", "case-2"]
    persisted = service.get_run(run["id"])
    assert [entry["case_id"] for entry in persisted["cases"]] == ["case-1", "case-2"]
    assert persisted["cancellation"] == {"reason": "operator request"}
    cancelled_event = [event for event in service.events(run["id"]) if event["type"] == "cancelled"][0]
    assert cancelled_event["reason"] == "operator request"


def test_cancel_is_idempotent_on_terminal_run():
    service = RunService(InMemoryRepository())
    run = service.create_run("replay@1", {})
    service.cancel(run["id"], reason="stop")
    again = service.cancel(run["id"], reason="second")
    assert again["status"] == "cancelled"
    assert service.get_run(run["id"])["cancellation"] == {"reason": "stop"}


def test_retry_inherits_case_ids_and_creates_child_run():
    service = RunService(InMemoryRepository())
    run = service.create_run("replay@1", {}, case_ids=["case-1"])

    def failing(_case):
        raise RuntimeError("boom")

    service.execute(run["id"], provider=failing)
    child = service.retry(run["id"])
    assert child["status"] == "queued"
    assert child["parent_run_id"] == run["id"]
    assert child["case_ids"] == ["case-1"]
    assert service.get_run(run["id"])["status"] == "failed"


def test_retry_rejects_non_retryable_run():
    service = RunService(InMemoryRepository())
    run = service.create_run("replay@1", {})
    with pytest.raises(ValueError, match="retried"):
        service.retry(run["id"])


def test_profile_stale_blocks_execution_and_is_retryable():
    service = RunService(InMemoryRepository())
    run = service.create_run("replay@1", {})
    stale = service.mark_profile_stale(run["id"], reason="model profile updated")
    assert stale["status"] == "profile_stale"
    assert stale["error"] == {"code": "PROFILE_STALE", "message": "model profile updated"}
    with pytest.raises(ValueError, match="terminal"):
        service.execute(run["id"], [])
    child = service.retry(run["id"])
    assert child["parent_run_id"] == run["id"]


def test_rescore_recomputes_scores_without_provider_calls():
    calls: list[str] = []

    def tracking(case_id):
        calls.append(case_id)
        return {"n": 1}

    service = RunService(InMemoryRepository(), provider=tracking)
    run = service.create_run("replay@1", {})
    service.execute(run["id"], ["case-1"], expectations={"case-1": {"n": 1}})
    assert calls == ["case-1"]
    rescored = service.rescore(run["id"])
    assert rescored["scores"] == [{"case_id": "case-1", "passed": True}]
    assert rescored["rescored"] is True
    assert calls == ["case-1"]
    event_types = [event["type"] for event in service.events(run["id"])]
    assert "rescored" in event_types
    assert "score" in event_types


def test_invalid_transition_is_rejected():
    service = RunService(InMemoryRepository())
    run = service.create_run("replay@1", {})
    with pytest.raises(ValueError, match="invalid transition"):
        service._transition(run["id"], "completed")
    assert "completed" not in TRANSITIONS["queued"]


def test_completed_run_reexecution_returns_same_result():
    service = RunService(InMemoryRepository())
    run = service.create_run("replay@1", {})
    first = service.execute(run["id"], ["case-1"])
    second = service.execute(run["id"], ["case-1"])
    assert first == second
    assert service.get_run(run["id"])["case_ids"] == ["case-1"]
