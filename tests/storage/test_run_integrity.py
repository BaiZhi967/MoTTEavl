"""RunStore integrity guarantees across SQLite and in-memory backends."""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from motte_storage.integrity import RunConflictError, new_run_id
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore


@pytest.fixture(params=["sqlite", "memory"])
def store(request, tmp_path):
    if request.param == "sqlite":
        return SQLiteRunStore(tmp_path / "runs.db")
    return InMemoryRunStore()


def test_create_only_inserts_and_persists_event_atomically(store):
    created = store.runs.create(
        {"id": "run-duplicate", "status": "queued", "parent_run_id": "run-parent"},
        event={"type": "queued"},
    )
    assert created == {"id": "run-duplicate", "status": "queued", "parent_run_id": "run-parent",
                       "revision": 1, "schema_version": 2}
    assert store.events.list_for_run(created["id"]) == [
        {"run_id": "run-duplicate", "seq": 1, "type": "queued"}
    ]
    with pytest.raises(RunConflictError):
        store.runs.create({"id": "run-duplicate", "status": "queued", "parent_run_id": "other"},
                          event={"type": "queued"})
    assert store.runs.get(created["id"]) == created
    assert len(store.events.list_for_run(created["id"])) == 1
    created["parent_run_id"] = "client-mutated"
    assert store.runs.get(created["id"])["parent_run_id"] == "run-parent"


def test_revision_and_status_compare_and_swap(store):
    created = store.runs.create({"id": "run-cas", "status": "queued", "manifest": {}})
    updated = store.runs.update({**created, "manifest": {"seed": 2}}, expected_revision=1,
                                expected_status="queued", event={"type": "updated"})
    assert updated["revision"] == 2
    with pytest.raises(RunConflictError):
        store.runs.update({**created, "manifest": {"seed": 3}}, expected_revision=1)
    with pytest.raises(RunConflictError):
        store.runs.transition("run-cas", expected_revision=2, expected_status="running",
                              status="failed", event={"type": "failed"})
    with pytest.raises(ValueError, match="state changes require"):
        store.runs.update({**updated, "status": "failed"}, expected_revision=2)
    transitioned = store.runs.transition(
        "run-cas", expected_revision=2, expected_status="queued", status="running",
        changes={"owner": "worker-1"}, event={"type": "running"},
    )
    assert (transitioned["revision"], transitioned["status"], transitioned["owner"]) == (
        3, "running", "worker-1")
    assert [item["type"] for item in store.events.list_for_run("run-cas")] == ["updated", "running"]
    assert store.runs.get("run-cas") == transitioned


def test_claim_by_id_and_queue_claim_persist_preparing_event(store):
    assert store.runs.claim("missing") is None
    first = store.runs.create({"id": "run-first", "status": "queued"})
    store.runs.create({"id": "run-second", "status": "queued"})
    claimed = store.runs.claim("run-second")
    assert claimed["revision"] == 2 and claimed["status"] == "preparing"
    assert store.events.list_for_run("run-second") == [
        {"run_id": "run-second", "seq": 1, "type": "preparing", "status": "preparing"}
    ]
    with pytest.raises(RunConflictError):
        store.runs.claim("run-second")
    assert store.runs.claim_next_queued()["id"] == first["id"]
    assert store.runs.claim_next_queued() is None
    assert store.events.list_for_run("run-first")[0]["type"] == "preparing"


def test_terminal_transition_race_allows_one_winner(store):
    created = store.runs.create({"id": "run-race", "status": "running"})

    def finish(status):
        try:
            return store.runs.transition(
                created["id"], expected_revision=1, expected_status="running", status=status,
                event={"type": status},
            )["status"]
        except RunConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(finish, ("completed", "cancelled")))
    assert outcomes.count("conflict") == 1
    assert set(outcomes) & {"completed", "cancelled"}
    assert len(store.events.list_for_run(created["id"])) == 1


def test_concurrent_create_ids_are_unique_and_legacy_allocator_ignores_uuid(store):
    ids = [new_run_id() for _ in range(100)]
    assert len(set(ids)) == 100
    with ThreadPoolExecutor(max_workers=8) as pool:
        created = list(pool.map(lambda run_id: store.runs.create({"id": run_id, "status": "queued"}), ids))
    assert len({run["id"] for run in created}) == len(store.runs.list()) == 100
    assert store.runs.next_run_id() == "run-1"
    store.runs.save({"id": "run-1", "status": "queued"})
    assert store.runs.next_run_id() == "run-2"


def test_attempt_is_revisioned_and_completion_persists_case_and_event(store):
    run = store.runs.create({"id": "run-attempt", "status": "running"})
    attempt = store.attempts.begin({"run_id": run["id"], "case_id": "a", "attempt_no": 1})
    with pytest.raises(RunConflictError):
        store.attempts.begin({"run_id": run["id"], "case_id": "a", "attempt_no": 1})
    assert store.attempts.list_open(run["id"]) == [attempt]
    dispatching = store.attempts.transition(attempt["id"], expected_revision=1,
                                            expected_status="prepared", status="dispatching")
    assert dispatching["revision"] == 2
    result = {"run_id": run["id"], "case_id": "a", "result": {"output": 3}}
    succeeded = store.attempts.complete(attempt["id"], expected_revision=2, case_run=result,
                                        event={"type": "model_response", "case_id": "a"})
    assert succeeded["revision"] == 3 and succeeded["status"] == "succeeded"
    assert store.case_runs.get(run["id"], "a") == result
    assert [e["type"] for e in store.events.list_for_run(run["id"])] == ["model_response"]
    assert store.attempts.list_open(run["id"]) == []
    with pytest.raises(RunConflictError):
        store.attempts.complete(attempt["id"], expected_revision=2, case_run=result)


def test_attempt_dispatch_rejects_cross_run_binding(store):
    run_a = store.runs.create({"id": "run-dispatch-a", "status": "preparing"})
    run_b = store.runs.create({"id": "run-dispatch-b", "status": "preparing"})
    attempt = store.attempts.begin({"run_id": run_a["id"], "case_id": "a"})
    with pytest.raises(RunConflictError, match="another run"):
        store.attempts.dispatch(
            attempt["id"], expected_revision=attempt["revision"],
            run_id=run_b["id"], expected_run_revision=run_b["revision"],
            expected_run_status=run_b["status"],
        )
    assert store.attempts.get(attempt["id"])["status"] == "prepared"


def test_attempt_recovery_marks_only_dispatching_indeterminate(store):
    store.runs.create({"id": "run-recover", "status": "running"})
    a = store.attempts.begin({"run_id": "run-recover", "case_id": "a"})
    b = store.attempts.begin({"run_id": "run-recover", "case_id": "b"})
    dispatching = store.attempts.transition(a["id"], expected_revision=1,
                                            expected_status="prepared", status="dispatching")
    assert store.attempts.mark_indeterminate("run-recover") == [
        {**dispatching, "revision": 3, "status": "indeterminate"}
    ]
    assert store.attempts.mark_indeterminate("run-recover") == []
    assert {row["id"] for row in store.attempts.list_open("run-recover")} == {a["id"], b["id"]}
    with pytest.raises(ValueError, match="invalid transition"):
        store.attempts.transition(a["id"], expected_revision=3,
                                  expected_status="indeterminate", status="dispatching")


def test_attempt_quarantine_updates_attempt_run_and_event_atomically(store):
    run = store.runs.create({"id": "run-quarantine", "status": "running"})
    attempt = store.attempts.begin({"run_id": run["id"], "case_id": "case-1"})
    dispatching = store.attempts.transition(
        attempt["id"], expected_revision=1, expected_status="prepared", status="dispatching"
    )
    event = {
        "type": "needs_review", "status": "needs_review",
        "attempt_ids": [attempt["id"]],
    }
    with pytest.raises(RunConflictError):
        store.attempts.quarantine_indeterminate(
            run["id"], expected_run_revision=99, expected_run_status="running",
            changes={"error": {"code": "CALL_OUTCOME_INDETERMINATE"}}, event=event,
        )
    assert store.attempts.get(attempt["id"]) == dispatching
    assert store.runs.get(run["id"])["status"] == "running"
    assert store.events.list_for_run(run["id"]) == []

    quarantined = store.attempts.quarantine_indeterminate(
        run["id"], expected_run_revision=run["revision"], expected_run_status="running",
        changes={"error": {"code": "CALL_OUTCOME_INDETERMINATE"}}, event=event,
    )
    assert quarantined[0]["status"] == "indeterminate"
    recovered = store.runs.get(run["id"])
    assert recovered["status"] == "needs_review" and recovered["revision"] == 2
    assert store.events.list_for_run(run["id"])[-1]["type"] == "needs_review"


def test_score_contract_rejects_unknown_fields_before_persistence(store):
    run = store.runs.create({"id": "run-score-contract", "status": "queued"})
    with pytest.raises(ValueError, match="public contract"):
        store.scores.replace_for_run(
            run["id"], [{"case_id": "case-1", "passed": True, "unexpected": "value"}]
        )
    assert store.scores.list_for_run(run["id"]) == []


def test_scoring_passes_are_immutable_and_current_tracks_revision(store):
    run = store.runs.create({"id": "run-scores", "status": "completed"})
    store.scores.replace_for_run(run["id"], [{"case_id": "a", "passed": True}])
    for index, passed in enumerate((True, False, True), start=1):
        scoring_pass = store.scoring_passes.append(
            {"id": f"pass-{index}", "run_id": run["id"], "scorer_version": f"v{index}"},
            [{"case_id": "a", "passed": passed}],
            expected_run_revision=index, expected_run_status="completed",
            event={"type": "scored", "scoring_pass_id": f"pass-{index}"},
        )
        assert store.scoring_passes.get(scoring_pass["id"]) == scoring_pass
        assert store.scoring_passes.current(run["id"]) == scoring_pass
        assert store.runs.get(run["id"])["revision"] == index + 1
    assert [record["id"] for record in store.scoring_passes.list_for_run(run["id"])] == [
        "pass-1", "pass-2", "pass-3"
    ]
    assert [store.score_sets.get(f"pass-{i}", "a")["passed"] for i in (1, 2, 3)] == [
        True, False, True
    ]
    assert store.scores.list_for_run(run["id"]) == [{"case_id": "a", "passed": True}]
    canonical = store.scoring_passes.get("pass-3")
    assert canonical["scores"] == [{"case_id": "a", "passed": True}]
    with pytest.raises(RunConflictError):
        store.scoring_passes.append({"id": "pass-2", "run_id": run["id"]},
                                    [{"case_id": "a", "passed": True}])
    with pytest.raises(RunConflictError):
        store.scoring_passes.append(
            {"id": "pass-stale", "run_id": run["id"]}, [{"case_id": "a", "passed": False}],
            expected_run_revision=1, expected_run_status="completed",
        )
    assert store.scoring_passes.get("pass-stale") is None
    with pytest.raises(ValueError, match="distinct"):
        store.scoring_passes.append({"id": "pass-duplicate", "run_id": run["id"]},
                                    [{"case_id": "a"}, {"case_id": "a"}])
    assert store.scoring_passes.get("pass-duplicate") is None
    assert len(store.events.list_for_run(run["id"])) == 3


def test_command_has_explicit_delivery_states(store):
    command = store.commands.create({"id": "cmd-1", "run_id": "run-commands", "content": "next"})
    with pytest.raises(RunConflictError):
        store.commands.create({"id": "cmd-1", "run_id": "run-commands"})
    with pytest.raises(ValueError, match="invalid transition"):
        store.commands.transition(command["id"], expected_revision=1,
                                  expected_status="queued", status="acknowledged")
    delivered = store.commands.transition(command["id"], expected_revision=1,
                                          expected_status="queued", status="delivered",
                                          changes={"backend_message_id": "m-1"})
    assert delivered["revision"] == 2
    with pytest.raises(RunConflictError):
        store.commands.transition(command["id"], expected_revision=1,
                                  expected_status="queued", status="failed")
    ack = store.commands.transition(command["id"], expected_revision=2,
                                    expected_status="delivered", status="acknowledged")
    assert ack["backend_message_id"] == "m-1" and ack["revision"] == 3
    assert store.commands.get(command["id"]) == ack
    assert store.commands.list_for_run(command["run_id"]) == [ack]
    assert store.commands.list(command["run_id"]) == [ack]
    with pytest.raises(ValueError, match="invalid transition"):
        store.commands.transition(command["id"], expected_revision=3,
                                  expected_status="acknowledged", status="queued")


def test_sqlite_upgrade_preserves_legacy_run_and_scores(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        connection.execute("INSERT INTO runs(id, payload) VALUES ('run-7', '{\"id\":\"run-7\",\"status\":\"queued\"}')")
        connection.execute("CREATE TABLE scores (run_id TEXT, case_id TEXT, ordinal INTEGER, payload TEXT)")
        connection.execute("INSERT INTO scores VALUES ('run-7', 'a', 0, '{\"case_id\":\"a\",\"passed\":true}')")
    first = SQLiteRunStore(path)
    second = SQLiteRunStore(path)
    legacy = first.runs.get("run-7")
    assert legacy["revision"] == 0 and legacy["schema_version"] == 1
    assert second.scores.list_for_run("run-7") == [{"case_id": "a", "passed": True}]
    upgraded = second.runs.transition("run-7", expected_revision=0,
                                      expected_status="queued", status="running")
    assert upgraded["revision"] == 1 and upgraded["schema_version"] == 2
    assert SQLiteRunStore(path).runs.get("run-7") == upgraded
    assert first.runs.next_run_id() == "run-8"


def test_sqlite_failed_event_insert_rolls_back_run(tmp_path):
    store = SQLiteRunStore(tmp_path / "atomic.db")
    with pytest.raises(TypeError):
        store.runs.create({"id": "run-bad", "status": "queued"},
                          event={"type": "queued", "unserializable": {1, 2}})
    assert store.runs.get("run-bad") is None
    with pytest.raises(TypeError):
        store.scoring_passes.append(
            {"id": "pass-bad", "run_id": "run-any"}, [{"case_id": "a"}],
            event={"type": "scored", "unserializable": {1, 2}},
        )
    assert store.scoring_passes.get("pass-bad") is None
