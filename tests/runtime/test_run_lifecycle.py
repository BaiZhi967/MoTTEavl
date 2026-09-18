import pytest
from motte_sdk.replay_run import ReplayProvider
from motte_sdk.service import TRANSITIONS, RunService, build_run_service
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore


def test_build_run_service_honors_motte_db_path(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_DB_PATH", str(tmp_path / "factory.db"))
    service = build_run_service()
    created = service.create_run("replay@1", {})
    reopened = build_run_service()
    assert reopened.get_run(created["id"])["status"] == "queued"


def test_execute_rejects_case_ids_that_replace_persisted_selection():
    service = RunService(InMemoryRunStore())
    run = service.create_run("replay@1", {}, case_ids=["selected"])
    with pytest.raises(ValueError, match="selected case ids are immutable"):
        service.execute(run["id"], ["other"], provider=lambda _case_id: {"output": 1})
    assert service.get_run(run["id"])["status"] == "queued"


def test_callable_provider_object_with_expected_for_completes_without_unbound_receiver():
    class Provider:
        def __call__(self, _case_id):
            return {"output": 1}

        def expected_for(self, _case_id):
            return None

    service = RunService(InMemoryRunStore())
    run = service.create_run("replay@1", {}, case_ids=["case-1"])
    result = service.execute(run["id"], provider=Provider())
    assert result["status"] == "completed"
    assert result["scores"] == []


def test_execute_emits_full_lifecycle_events(tmp_path):
    fixture = {
        "case-1": {"output": {"n": 1}, "expected": {"n": 1}},
        "case-2": {"output": {"n": 2}, "expected": {"n": 9}},
    }
    service = RunService(SQLiteRunStore(tmp_path / "runs.db"), provider=ReplayProvider(fixture).invoke)
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
        "scoring_pass_created",
        "completed",
    ]
    assert result["scores"] == [
        {"case_id": "case-1", "passed": True},
        {"case_id": "case-2", "passed": False},
    ]


def test_scoring_rejects_case_ids_outside_run_selection(monkeypatch):
    service = RunService(InMemoryRunStore())
    run = service.create_run("replay@1", {}, case_ids=["case-1"])
    monkeypatch.setattr(service, "_score_results", lambda *_args, **_kwargs: [
        {"case_id": "bogus", "passed": True}
    ])

    result = service.execute(run["id"], provider=lambda _case_id: {"output": 1})

    assert result["status"] == "failed"
    assert "outside the run selection" in result["error"]["message"]
    assert service.store.scoring_passes.list_for_run(run["id"]) == []


def test_scoring_rejects_aggregate_counts_outside_selected_cases(monkeypatch):
    from motte_sdk import benchmark_plugins

    service = RunService(InMemoryRunStore())
    run = service.create_run(
        "custom@1", {"benchmark_provenance": {"suite": "custom"}},
        case_ids=["case-1"],
    )
    monkeypatch.setattr(
        benchmark_plugins,
        "aggregate_with_plugin",
        lambda _run, _scores: {"correct": 2, "accuracy": None},
    )
    with pytest.raises(ValueError, match="exceed selected"):
        service._append_scoring_pass(
            run["id"], [{"case_id": "case-1", "passed": True}], source="test"
        )
    assert service.store.scoring_passes.list_for_run(run["id"]) == []


def test_scoring_aggregate_uses_legacy_case_rows_as_selection(monkeypatch):
    from motte_sdk import benchmark_plugins

    service = RunService(InMemoryRunStore())
    run = service.create_run("custom@1", {"benchmark_provenance": {"suite": "custom"}})
    service.store.case_runs.upsert({
        "run_id": run["id"], "case_id": "legacy-case", "result": {"output": 1},
    })
    monkeypatch.setattr(
        benchmark_plugins,
        "aggregate_with_plugin",
        lambda _run, _scores: {"selected": 1, "correct": 1, "accuracy": 1.0},
    )
    scoring_pass = service._append_scoring_pass(
        run["id"], [{"case_id": "legacy-case", "passed": True}], source="test"
    )
    assert scoring_pass["summary"]["aggregate"]["selected"] == 1


def test_scoring_rejects_plugin_owned_scoring_pass_binding(monkeypatch):
    service = RunService(InMemoryRunStore())
    run = service.create_run("replay@1", {}, case_ids=["case-1"])
    monkeypatch.setattr(service, "_score_results", lambda *_args, **_kwargs: [
        {"case_id": "case-1", "passed": True, "scoring_pass_id": "other-pass"}
    ])

    result = service.execute(run["id"], provider=lambda _case_id: {"output": 1})

    assert result["status"] == "failed"
    assert "assigned by the scoring pass" in result["error"]["message"]
    assert service.store.scoring_passes.list_for_run(run["id"]) == []


def test_terminal_benchmark_falls_back_when_plugin_returns_malformed_scores(monkeypatch):
    service = RunService(InMemoryRunStore())
    run = service.create_run(
        "gsm8k@1",
        {"benchmark_provenance": {"suite": "gsm8k"}},
        case_ids=["case-1"],
    )
    monkeypatch.setattr(service, "_score_results", lambda *_args, **_kwargs: [None])

    result = service.cancel(run["id"], reason="operator request")

    assert result["status"] == "cancelled"
    assert result["scores"] == [{
        "case_id": "case-1", "passed": None, "details": {"code": "SCORING_UNAVAILABLE"}
    }]
    assert result["cases"] == [{
        "run_id": run["id"], "case_id": "case-1", "outcome": "not_attempted", "result": None
    }]


def test_scoring_failure_is_terminal_and_does_not_leave_run_in_scoring(monkeypatch):
    service = RunService(InMemoryRunStore())
    run = service.create_run("replay@1", {}, case_ids=["case-1"])
    monkeypatch.setattr(
        service,
        "_score_results",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("scorer exploded")),
    )

    result = service.execute(run["id"], provider=lambda _case_id: {"output": 1})

    assert result["status"] == "failed"
    assert service.get_run(run["id"])["status"] == "failed"
    assert service.store.scoring_passes.list_for_run(run["id"]) == []
    assert service.events(run["id"])[-1]["type"] == "failed"


def test_cancel_records_reason_and_blocks_remaining_cases():
    service = RunService(InMemoryRunStore())
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
    service = RunService(InMemoryRunStore())
    run = service.create_run("replay@1", {})
    service.cancel(run["id"], reason="stop")
    again = service.cancel(run["id"], reason="second")
    assert again["status"] == "cancelled"
    assert service.get_run(run["id"])["cancellation"] == {"reason": "stop"}


def test_retry_inherits_case_ids_and_creates_child_run():
    service = RunService(InMemoryRunStore())
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
    service = RunService(InMemoryRunStore())
    run = service.create_run("replay@1", {})
    with pytest.raises(ValueError, match="retried"):
        service.retry(run["id"])


def test_profile_stale_blocks_execution_and_is_retryable():
    service = RunService(InMemoryRunStore())
    run = service.create_run("replay@1", {})
    stale = service.mark_profile_stale(run["id"], reason="model profile updated")
    assert stale["status"] == "profile_stale"
    assert stale["error"] == {"code": "PROFILE_STALE", "message": "model profile updated"}
    with pytest.raises(ValueError, match="terminal"):
        service.execute(run["id"], [])
    child = service.retry(run["id"], refreshed_manifest={})
    assert child["parent_run_id"] == run["id"]


def test_rescore_recomputes_scores_without_provider_calls():
    calls: list[str] = []

    def tracking(case_id):
        calls.append(case_id)
        return {"n": 1}

    service = RunService(InMemoryRunStore(), provider=tracking)
    run = service.create_run("replay@1", {})
    original = service.execute(run["id"], ["case-1"], expectations={"case-1": {"n": 1}})
    assert calls == ["case-1"]
    original_pass = original["current_scoring_pass_id"]
    rescored = service.rescore(run["id"])
    assert rescored["scores"] == [{"case_id": "case-1", "passed": True}]
    assert rescored["current_scoring_pass_id"] != original_pass
    assert len(service.store.scoring_passes.list_for_run(run["id"])) == 2
    assert calls == ["case-1"]
    event_types = [event["type"] for event in service.events(run["id"])]
    assert "rescored" in event_types
    assert "score" in event_types


def test_invalid_transition_is_rejected():
    service = RunService(InMemoryRunStore())
    run = service.create_run("replay@1", {})
    with pytest.raises(ValueError, match="invalid transition"):
        service._transition(run["id"], "completed")
    assert "completed" not in TRANSITIONS["queued"]


def test_completed_run_reexecution_returns_same_result():
    service = RunService(InMemoryRunStore())
    run = service.create_run("replay@1", {})
    first = service.execute(
        run["id"], ["case-1"], provider=lambda case_id: {"case_id": case_id}
    )
    second = service.execute(run["id"], ["case-1"])
    assert first == second
    assert service.get_run(run["id"])["case_ids"] == ["case-1"]


def test_repeated_execution_does_not_duplicate_case_runs(tmp_path):
    fixture = {
        "case-1": {"output": {"n": 1}, "expected": {"n": 1}},
        "case-2": {"output": {"n": 2}, "expected": {"n": 2}},
    }
    service = RunService(SQLiteRunStore(tmp_path / "runs.db"), provider=ReplayProvider(fixture).invoke)
    run = service.create_run("replay@1", {}, case_ids=list(fixture))
    service.execute(run["id"])
    again = service.execute(run["id"])
    assert again["status"] == "completed"
    rows = service.store.case_runs.list_for_run(run["id"])
    assert [row["case_id"] for row in rows] == ["case-1", "case-2"]
    assert again["scores"] == [
        {"case_id": "case-1", "passed": True},
        {"case_id": "case-2", "passed": True},
    ]
