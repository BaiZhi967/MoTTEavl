import json

import pytest
from apps.worker.motte_worker.reporting import WorkerReporter
from motte_sdk.service import RunService
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore


def test_create_run_persists_queued_manifest():
    service = RunService(InMemoryRunStore())
    run = service.create_run("scenario@1", {"model": {"id": "replay"}, "seed": 7})
    assert run["status"] == "queued"
    assert service.get_run(run["id"])["manifest"]["seed"] == 7


def test_execute_is_idempotent_and_emits_trace():
    service = RunService(InMemoryRunStore())
    run = service.create_run("scenario@1", {})
    first = service.execute(
        run["id"], ["case-1"], provider=lambda case_id: {"case_id": case_id}
    )
    second = service.execute(run["id"], ["case-1"])
    assert first == second
    assert first["status"] == "completed"
    assert len(service.events(run["id"])) >= 2


def test_cancel_prevents_execution():
    service = RunService(InMemoryRunStore())
    run = service.create_run("scenario@1", {})
    assert service.cancel(run["id"])["status"] == "cancelled"
    with pytest.raises(ValueError, match="terminal"):
        service.execute(run["id"], ["case-1"])


def test_rescore_does_not_call_provider():
    calls = []
    service = RunService(InMemoryRunStore(), provider=lambda case: calls.append(case) or {"ok": True})
    run = service.create_run("scenario@1", {})
    service.execute(run["id"], ["case-1"])
    assert calls == ["case-1"]
    assert service.rescore(run["id"])["status"] == "completed"
    assert calls == ["case-1"]


def test_service_uses_persistent_repository_for_new_instance(tmp_path):
    path = tmp_path / "runs.db"
    first = RunService(SQLiteRunStore(path))
    created = first.create_run("scenario@1", {"seed": 3})
    second = RunService(SQLiteRunStore(path))
    assert second.get_run(created["id"])["manifest"]["seed"] == 3
    next_run = second.create_run("scenario@1", {})
    assert next_run["id"] != created["id"]




def test_event_and_progress_observers_receive_safe_persisted_details(capsys):
    reporter = WorkerReporter()
    service = RunService(
        InMemoryRunStore(),
        event_observer=reporter.observe_event,
        progress_observer=reporter.observe_progress,
    )
    run = service.create_run("replay@1", {}, case_ids=["case-1"])
    result = service.execute(run["id"], provider=lambda _: {
        "content": "private answer",
        "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        "cost": {"total": 0.25, "currency": "CNY", "price_table_version": "test"},
    })
    assert result["status"] == "completed"
    output = capsys.readouterr().err
    lines = [json.loads(line) for line in output.splitlines()]
    assert lines[0]["event"] == "queued"
    assert any(line["event"] == "case_started" and line["ordinal"] == 1 for line in lines)
    finished = next(line for line in lines if line["event"] == "case_finished")
    assert finished["prompt_tokens"] == 2
    assert finished["cost_total"] == 0.25
    assert finished["cost_currency"] == "CNY"
    assert "private answer" not in output
    trace = service.events(run["id"])
    assert [event["seq"] for event in trace] == list(range(1, len(trace) + 1))


def test_observer_failure_does_not_change_run_execution():
    seen = []

    def broken(event):
        seen.append(event)
        raise RuntimeError("observer must not break runs")

    service = RunService(InMemoryRunStore(), event_observer=broken, progress_observer=broken)
    run = service.create_run("replay@1", {}, case_ids=["case-1"])
    result = service.execute(run["id"], provider=lambda _: {"ok": True})
    assert result["status"] == "completed"
    assert seen
    assert [event["type"] for event in service.events(run["id"])] == [
        "queued", "preparing", "running", "model_response", "collecting", "scoring",
        "scoring_pass_created", "completed",
    ]


def test_execute_replay_persists_scores_and_trace(tmp_path):
    from motte_sdk.replay_run import ReplayProvider

    fixture = {"case-1": {"output": {"name": "Ada"}, "expected": {"name": "Ada"}}}
    service = RunService(SQLiteRunStore(tmp_path / "runs.db"), provider=ReplayProvider(fixture).invoke)
    run = service.create_run("replay@1", {})
    result = service.execute(run["id"], ["case-1"])
    assert result["scores"] == [{"case_id": "case-1", "passed": True}]
    assert service.events(run["id"])[-1]["type"] == "completed"
