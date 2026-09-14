from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore


def test_sqlite_store_survives_reopen(tmp_path):
    store = SQLiteRunStore(tmp_path / "runs.db")
    store.runs.save({"id": "run-1", "status": "queued"})
    store.case_runs.upsert({"run_id": "run-1", "case_id": "case-1", "result": {"n": 1}})
    store.events.append({"run_id": "run-1", "type": "queued"})

    reopened = SQLiteRunStore(tmp_path / "runs.db")
    assert reopened.runs.get("run-1")["status"] == "queued"
    assert reopened.case_runs.list_for_run("run-1")[0]["case_id"] == "case-1"
    assert reopened.events.list_for_run("run-1")[0]["type"] == "queued"


def test_trace_events_have_unique_monotonic_seq(tmp_path):
    store = SQLiteRunStore(tmp_path / "runs.db")
    first = store.events.append({"run_id": "run-1", "type": "queued"})
    second = store.events.append({"run_id": "run-1", "type": "running"})
    other_run = store.events.append({"run_id": "run-2", "type": "queued"})
    assert (first["seq"], second["seq"]) == (1, 2)
    assert other_run["seq"] == 1
    seqs = [event["seq"] for event in store.events.list_for_run("run-1")]
    assert seqs == [1, 2]


def test_case_run_upsert_is_idempotent_per_case(tmp_path):
    store = SQLiteRunStore(tmp_path / "runs.db")
    first = store.case_runs.upsert({"run_id": "run-1", "case_id": "case-1", "result": {"n": 1}})
    again = store.case_runs.upsert({"run_id": "run-1", "case_id": "case-1", "result": {"n": 999}})
    assert first == again
    rows = store.case_runs.list_for_run("run-1")
    assert len(rows) == 1
    assert rows[0]["result"] == {"n": 1}
    store.case_runs.upsert({"run_id": "run-1", "case_id": "case-2", "result": {"n": 2}})
    assert [row["case_id"] for row in store.case_runs.list_for_run("run-1")] == ["case-1", "case-2"]


def test_scores_are_replaced_per_run(tmp_path):
    store = SQLiteRunStore(tmp_path / "runs.db")
    store.scores.replace_for_run("run-1", [{"case_id": "case-1", "passed": True}])
    store.scores.replace_for_run("run-1", [{"case_id": "case-1", "passed": False}, {"case_id": "case-2", "passed": True}])
    assert store.scores.list_for_run("run-1") == [
        {"case_id": "case-1", "passed": False},
        {"case_id": "case-2", "passed": True},
    ]


def test_events_after_supports_seq_resume(tmp_path):
    store = SQLiteRunStore(tmp_path / "runs.db")
    for event_type in ("queued", "preparing", "running"):
        store.events.append({"run_id": "run-1", "type": event_type})
    tail = store.events.list_after("run-1", 2)
    assert [event["seq"] for event in tail] == [3]


def test_in_memory_store_matches_sqlite_semantics():
    store = InMemoryRunStore()
    assert store.case_runs.upsert({"run_id": "run-1", "case_id": "case-1", "result": 1}) == (
        store.case_runs.upsert({"run_id": "run-1", "case_id": "case-1", "result": 2})
    )
    assert store.runs.next_run_id() == "run-1"
    store.runs.save({"id": "run-1", "status": "queued"})
    assert store.runs.next_run_id() == "run-2"
