"""Repository-owned Trace metadata. Only disposable fixtures remove prefixes."""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from motte_storage import trace_retention_models as models
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore

NOW = datetime(2026, 9, 30, 12, 34, 56, 789012, tzinfo=UTC)
FORGED = "1900-01-01T00:00:00+00:00"


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryRunStore()
    if request.param == "sqlite":
        return SQLiteRunStore(tmp_path / "trace.db")
    from motte_storage.postgres import create_postgres_run_store
    return create_postgres_run_store(request.getfixturevalue("isolated_pg_database"), migrate=True)


@pytest.fixture
def fixed_clock(monkeypatch):
    monkeypatch.setattr(models, "utc_now", lambda: NOW)


def test_server_time_cannot_be_forged(store, fixed_clock):
    event = {"run_id": "r", "seq": 999, "stored_at": FORGED, "timestamp": FORGED,
             "nested": {"stored_at": FORGED}}
    result = store.events.append(event)
    expected = {**event, "seq": 1}
    assert result == expected
    assert store.events.list_for_run("r") == [expected]
    assert store.events.list_after("r", 0) == [expected]
    row, = store.events.stored_for_run("r")
    assert row == models.StoredTraceEvent(run_id="r", seq=1, payload=expected, stored_at=NOW)
    assert row.stored_at.tzinfo is UTC
    result["nested"]["stored_at"] = "changed"
    row.payload["nested"]["stored_at"] = "changed"
    assert store.events.stored_for_run("r")[0].payload == expected
    assert store.events.stored_for_run("missing") == []


def test_each_append_reads_the_current_server_clock(store, monkeypatch):
    times = iter([NOW, NOW + timedelta(seconds=7)])
    monkeypatch.setattr(models, "utc_now", lambda: next(times))
    store.events.append({"run_id": "r"})
    store.events.append({"run_id": "r"})
    assert [r.stored_at for r in store.events.stored_for_run("r")] == [
        NOW, NOW + timedelta(seconds=7)]
    assert all("stored_at" not in r for r in store.events.list_for_run("r"))


def test_every_append_path_stamps_time(store, fixed_clock):
    from tests.storage.test_scoring_jobs import job_record, pass_record
    from motte_storage.scoring_jobs import scoring_jobs_for

    # All high-level writers below must converge on the same backend append path.
    store.events.append({"run_id": "r", "type": "direct"})
    run = store.runs.create({"id": "r", "status": "queued"}, event={"type": "create"})
    run = store.runs.update(run, expected_revision=run["revision"], event={"type": "update"})
    store.runs.transition("r", expected_revision=run["revision"], expected_status="queued",
                          status="running", event={"type": "transition"})
    store.runs.create({"id": "claim", "status": "queued"})
    store.runs.claim("claim")
    store.runs.create({"id": "claim-next", "status": "queued"})
    store.runs.claim_next_queued()
    attempt = store.attempts.begin({"run_id": "r", "case_id": "c"})
    attempt = store.attempts.transition(attempt["id"], expected_revision=1,
                                        expected_status="prepared", status="dispatching")
    store.attempts.complete(attempt["id"], expected_revision=attempt["revision"],
                           case_run={"run_id": "r", "case_id": "c"},
                           event={"type": "attempt"})
    store.scoring_passes.append({"id": "pass", "run_id": "r"}, [],
        event={"type": "pass"}, score_events=[{"type": "score"}],
        terminal_event={"type": "terminal"})
    store.runs.create({"id": "quarantine", "status": "running"})
    attempt = store.attempts.begin({"run_id": "quarantine", "case_id": "c"})
    store.attempts.transition(attempt["id"], expected_revision=1,
                             expected_status="prepared", status="dispatching")
    store.attempts.quarantine_indeterminate("quarantine", expected_run_revision=1,
        expected_run_status="running", changes={}, event={"type": "quarantine"})
    store.runs.create({"id": "run-1", "status": "completed"})
    jobs = scoring_jobs_for(store)
    jobs.submit(job_record())
    prepared = jobs.claim("sjob-1")
    published = jobs.publish("sjob-1", expected_revision=prepared["revision"],
        pass_record=pass_record(), scores=[], expected_run_revision=1,
        expected_run_status="completed", events=[{"run_id": "run-1", "type": "job"}])
    assert published["published"]
    rows = [row for run_id in ("r", "claim", "claim-next", "quarantine", "run-1")
            for row in store.events.stored_for_run(run_id)]
    assert len(rows) == 12
    assert {r.payload["type"] for r in rows} == {
        "direct", "create", "update", "transition", "preparing", "attempt",
        "pass", "score", "terminal", "quarantine", "job"}
    assert all(r.stored_at == NOW for r in rows)
    assert all("stored_at" not in r.payload for r in rows)


def test_interactive_append_uses_server_time(store, fixed_clock):
    from tests.storage.test_interactive_commands import seed, record
    seed(store)
    command, created = store.commands.create_or_get(record(), event={"type": "command"})
    assert created
    store.commands.claim(command["id"], expected_revision=1, session_id="s",
        expected_control_revision=1, worker_token="w", now=datetime.now(UTC),
        event={"type": "claim-command"})
    rows = store.events.stored_for_run("r")
    assert len(rows) == 2
    assert all(r.stored_at == NOW for r in rows)


def test_sequence_does_not_reuse_removed_fixture_prefix(store, fixed_clock):
    for _ in range(4):
        store.events.append({"run_id": "r"})
    # Fixture-only deletion: no product trim operation is introduced by Task 1b.
    if hasattr(store, "dsn"):
        from psycopg import connect
        with connect(store.dsn) as connection:
            connection.execute("DELETE FROM trace_events WHERE run_id='r' AND seq < 4")
    elif hasattr(store.events, "_path"):
        with sqlite3.connect(store.events._path) as connection:
            connection.execute("DELETE FROM trace_events WHERE run_id='r' AND seq < 4")
    else:
        store.events._events["r"] = store.events._events["r"][-1:]
    assert store.events.append({"run_id": "r"})["seq"] == 5
    assert [r.seq for r in store.events.stored_for_run("r")] == [4, 5]


def test_concurrent_append_keeps_metadata_and_payload_identity(store, fixed_clock):
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda i: store.events.append({"run_id": "r", "i": i}), range(18)))
    rows = store.events.stored_for_run("r")
    assert [r.seq for r in rows] == list(range(1, 19))
    assert [r.payload for r in rows] == sorted(results, key=lambda r: r["seq"])
    assert all(r.stored_at == NOW for r in rows)


@pytest.mark.parametrize("raw", [None, "", "bad", "2020-01-01", "2020-01-01T00:00:00", "NaN"])
def test_legacy_invalid_time_is_unknown(store, raw):
    payload = {"run_id": "r", "seq": 1, "stored_at": FORGED}
    if hasattr(store, "dsn"):
        from psycopg import connect
        from psycopg.types.json import Json
        with connect(store.dsn) as connection:
            connection.execute("INSERT INTO trace_events(run_id,seq,payload,stored_at) VALUES (%s,%s,%s,%s)",
                               ("r", 1, Json(payload), raw))
    elif hasattr(store.events, "_path"):
        with sqlite3.connect(store.events._path) as connection:
            connection.execute("INSERT INTO trace_events(run_id,seq,payload,stored_at) VALUES (?,?,?,?)",
                               ("r", 1, json.dumps(payload), raw))
    else:
        store.events._events["r"] = [payload]
        if raw is not None:
            store.events._stored_at[("r", 1)] = raw
        # A legacy memory record with no metadata stays unknown too.
    row, = store.events.stored_for_run("r")
    assert row.stored_at is None and row.payload == payload


@pytest.mark.parametrize("store", ["sqlite", "postgres"], indirect=True)
@pytest.mark.parametrize("field,value", [("run_id", "other"), ("seq", 2), ("seq", True)])
def test_stored_reader_rejects_index_payload_mismatch(store, field, value):
    payload = {"run_id": "r", "seq": 1, field: value}
    if hasattr(store, "dsn"):
        from psycopg import connect
        from psycopg.types.json import Json
        with connect(store.dsn) as connection:
            connection.execute("INSERT INTO trace_events(run_id,seq,payload) VALUES (%s,%s,%s)",
                               ("r", 1, Json(payload)))
    else:
        with sqlite3.connect(store.events._path) as connection:
            connection.execute("INSERT INTO trace_events(run_id,seq,payload) VALUES (?,?,?)",
                               ("r", 1, json.dumps(payload)))
    with pytest.raises(ValueError, match="identity"):
        store.events.stored_for_run("r")


def test_new_0018_migration_preserves_unknown_legacy_time(isolated_pg_database):
    from alembic import command
    from motte_storage.migrations import alembic_config, current
    from motte_storage.postgres import create_postgres_run_store
    from psycopg import connect
    from psycopg.types.json import Json
    dsn = isolated_pg_database
    config = alembic_config(dsn)
    command.upgrade(config, "0017_judge_calibrations")
    payload = {"run_id": "legacy", "seq": 9, "stored_at": FORGED}
    with connect(dsn) as connection:
        connection.execute("INSERT INTO trace_events VALUES (%s,%s,%s)", ("legacy", 9, Json(payload)))
    command.upgrade(config, "0018_trace_retention")
    assert current(dsn) == "0018_trace_retention"
    store = create_postgres_run_store(dsn)
    assert store.events.stored_for_run("legacy")[0].stored_at is None
    assert store.events.list_for_run("legacy") == [payload]
    with connect(dsn) as connection:
        assert connection.execute("SELECT is_nullable,column_default,data_type FROM information_schema.columns "
            "WHERE table_name='trace_events' AND column_name='stored_at'").fetchone() == ("YES", None, "text")
    # Unknown rows can survive downgrade/re-upgrade without a guessed backfill.
    command.downgrade(config, "0017_judge_calibrations")
    command.upgrade(config, "0018_trace_retention")
    assert store.events.stored_for_run("legacy")[0].stored_at is None


def test_memory_reader_rejects_wrong_run_identity():
    store = InMemoryRunStore()
    store.events._events["r"] = [{"run_id": "other", "seq": 1}]
    with pytest.raises(ValueError, match="identity"):
        store.events.stored_for_run("r")


@pytest.mark.parametrize("raw,expected", [
    ("2020-01-01T08:00:00+08:00", datetime(2020, 1, 1, tzinfo=UTC)),
    ("2999-01-01T00:00:00Z", datetime(2999, 1, 1, tzinfo=UTC)),
])
def test_read_normalizes_aware_time_without_aging_future_rows(store, raw, expected):
    store.events.append({"run_id": "r", "stored_at": FORGED})
    if hasattr(store, "dsn"):
        from psycopg import connect
        with connect(store.dsn) as connection:
            connection.execute("UPDATE trace_events SET stored_at=%s WHERE run_id='r'", (raw,))
    elif hasattr(store.events, "_path"):
        with sqlite3.connect(store.events._path) as connection:
            connection.execute("UPDATE trace_events SET stored_at=? WHERE run_id='r'", (raw,))
    else:
        store.events._stored_at[("r", 1)] = raw
    assert store.events.stored_for_run("r")[0].stored_at == expected


@pytest.mark.parametrize("store", ["sqlite", "postgres"], indirect=True)
def test_timestamp_append_preserves_existing_maintenance_barrier(store, fixed_clock):
    from motte_storage.maintenance import begin_maintenance, end_maintenance
    store.events.append({"run_id": "r"})
    before = store.events.stored_for_run("r")
    lease = begin_maintenance(store)
    try:
        if hasattr(store, "dsn"):
            from psycopg.conninfo import make_conninfo
            from psycopg.errors import LockNotAvailable
            from motte_storage.postgres import _PgTraceEvents
            writer = _PgTraceEvents(make_conninfo(store.dsn, options="-c lock_timeout=100"))
            with pytest.raises(LockNotAvailable):
                writer.append({"run_id": "r"})
        else:
            with pytest.raises(sqlite3.IntegrityError, match="maintenance"):
                store.events.append({"run_id": "r"})
        assert store.events.stored_for_run("r") == before
    finally:
        end_maintenance(store, owner=lease["owner"])
    assert store.events.append({"run_id": "r"})["seq"] == 2


def test_pg_downgrade_preserves_new_trace_time(isolated_pg_database):
    from alembic import command
    from motte_storage.migrations import alembic_config, current
    from motte_storage.postgres import create_postgres_run_store
    store = create_postgres_run_store(isolated_pg_database, migrate=True)
    store.events.append({"run_id": "r"})
    before = store.events.stored_for_run("r")
    with pytest.raises(RuntimeError, match="server-owned Trace times"):
        command.downgrade(alembic_config(store.dsn), "0017_judge_calibrations")
    assert current(store.dsn) == "0018_trace_retention"
    assert store.events.stored_for_run("r") == before


def test_pg_active_maintenance_refuses_downgrade_before_table_lock(isolated_pg_database):
    from alembic import command
    from motte_storage.migrations import alembic_config, current
    from motte_storage.postgres import create_postgres_run_store
    from motte_storage.maintenance import begin_maintenance, end_maintenance
    store = create_postgres_run_store(isolated_pg_database, migrate=True)
    lease = begin_maintenance(store)
    try:
        with pytest.raises(RuntimeError, match="maintenance is active"):
            command.downgrade(alembic_config(store.dsn), "0017_judge_calibrations")
        assert current(store.dsn) == "0018_trace_retention"
    finally:
        end_maintenance(store, owner=lease["owner"])


def test_sqlite_migration_preserves_payload_and_refuses_time_loss(tmp_path):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text
    from motte_storage.migrations import MIGRATIONS_DIR
    path = MIGRATIONS_DIR / "versions" / "0018_trace_retention.py"
    spec = importlib.util.spec_from_file_location("trace_0018", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine(f"sqlite:///{tmp_path / 'trace-migration.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE trace_events (run_id TEXT, seq INTEGER, payload TEXT)"))
            connection.execute(text("INSERT INTO trace_events VALUES ('r', 1, :payload)"),
                               {"payload": json.dumps({"stored_at": FORGED})})
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
                assert connection.execute(text("SELECT stored_at FROM trace_events")).scalar() is None
                module.downgrade()
                module.upgrade()
            connection.execute(text("UPDATE trace_events SET stored_at = :time"), {"time": NOW.isoformat()})
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError, match="server-owned Trace times"):
                module.downgrade()
            assert connection.execute(text("SELECT payload, stored_at FROM trace_events")).one() == (
                json.dumps({"stored_at": FORGED}), NOW.isoformat())
    finally:
        engine.dispose()


# Task 2: all inputs are disposable fixtures; planning must be read-only.
OLD = NOW - timedelta(days=10)
CUTOFF = NOW - timedelta(days=3)


def planner():
    import importlib
    return importlib.import_module("motte_storage.trace_retention")


def seed_plan_run(store, run_id="r", *, times=None, seqs=None, status="completed", **extra):
    times = [OLD] * 4 if times is None else times
    seqs = list(range(1, len(times) + 1)) if seqs is None else seqs
    store.runs.create({"id": run_id, "status": status, **extra})
    rows = [(seq, {"run_id": run_id, "seq": seq, "type": "evidence", "stored_at": FORGED},
             time.isoformat() if isinstance(time, datetime) else time)
            for seq, time in zip(seqs, times, strict=True)]
    if hasattr(store, "dsn"):
        from psycopg import connect
        from psycopg.types.json import Json
        with connect(store.dsn) as connection, connection.cursor() as cursor:
            cursor.executemany("INSERT INTO trace_events(run_id,seq,payload,stored_at) VALUES (%s,%s,%s,%s)",
                               [(run_id, seq, Json(payload), time) for seq, payload, time in rows])
    elif hasattr(store.events, "_path"):
        with sqlite3.connect(store.events._path) as connection:
            connection.executemany("INSERT INTO trace_events(run_id,seq,payload,stored_at) VALUES (?,?,?,?)",
                                   [(run_id, seq, json.dumps(payload), time) for seq, payload, time in rows])
    else:
        store.events._events[run_id] = [payload for _, payload, _ in rows]
        store.events._stored_at.update({(run_id, seq): time for seq, _, time in rows})


def enabled_plan(store):
    return planner().plan_trace_retention(store, config=models.TraceRetentionConfig(
        enabled=True, retention_days=3))


def test_disabled_and_invalid_policy(store, fixed_clock):
    plan = planner().plan_trace_retention(store, config=models.TraceRetentionConfig())
    assert plan.cutoff is None and plan.prefixes == []
    assert plan.config.retention_days is None
    for days in (None, 0, -1, 1.5, True):
        with pytest.raises(ValueError):
            planner().plan_trace_retention(store, config=models.TraceRetentionConfig(
                enabled=True, retention_days=days))
    with pytest.raises(ValueError):
        planner().plan_trace_retention(store, config=models.TraceRetentionConfig.model_construct(
            enabled=True, retention_days=True))


@pytest.mark.parametrize("times,seqs,last", [
    ([OLD] * 4, None, 3), ([OLD, None, OLD, OLD], None, 1),
    ([OLD, CUTOFF, OLD, OLD], None, 1), ([OLD, NOW, OLD, OLD], None, 1),
    ([OLD, NOW + timedelta(days=2), OLD, OLD], None, 1),
    ([OLD, "2020-01-01T00:00:00", OLD, OLD], None, 1),
    ([OLD, "not-time", OLD, OLD], None, 1),
    ([OLD], None, None), ([None, OLD, OLD], None, None),
    ([OLD, OLD, OLD], [1, 3, 4], 1), ([OLD, OLD], [2, 3], None),
])
def test_prefix_stops_at_first_unsafe_row(store, fixed_clock, times, seqs, last):
    seed_plan_run(store, times=times, seqs=seqs)
    plan = enabled_plan(store)
    if last is None:
        assert plan.prefixes == []
    else:
        prefix, = plan.prefixes
        assert (prefix.first_seq, prefix.last_seq, prefix.event_count) == (1, last, last)
        assert prefix.keep_seq == max(seqs or range(1, len(times) + 1))
    assert plan.cutoff == CUTOFF


def test_ownership_is_not_a_pin(store, fixed_clock):
    from motte_storage.scoring_jobs import scoring_jobs_for
    from tests.storage.test_scoring_jobs import job_record
    seed_plan_run(store)
    store.case_runs.upsert({"run_id": "r", "case_id": "c", "result": {"output": "ordinary"}})
    store.scoring_passes.append({"id": "p", "run_id": "r"}, [{"case_id": "c", "passed": True}])
    scoring_jobs_for(store).submit(job_record(owner={"kind": "subject", "run_id": "r"}))
    protection = planner().collect_trace_protection(store)
    assert "r" not in protection.run_ids
    assert [p.run_id for p in enabled_plan(store).prefixes] == ["r"]


@pytest.mark.parametrize("holder", ["score", "pass", "invocation", "job", "event"])
def test_nested_event_reference_protection_is_precise(store, fixed_clock, holder):
    from tests.storage.test_scoring_jobs import invocation_record, job_record
    seed_plan_run(store)
    evidence = {"kind": "event", "run_id": "r", "locator": "2"}
    if holder == "score":
        store.scoring_passes.append({"id": "p", "run_id": "r"},
            [{"case_id": "c", "passed": True, "details": {"evidence": [evidence]}}])
    elif holder == "pass":
        store.scoring_passes.append({"id": "p", "run_id": "r", "source": {"nested": evidence}}, [])
    elif holder == "invocation":
        row = invocation_record(owner={"kind": "subject", "run_id": "r", "case_id": "c"})
        row["request_summary"] = {"nested": [evidence]}
        store.invocations.create(row)
    elif holder == "job":
        row = job_record(owner={"kind": "subject", "run_id": "r"})
        row["input_snapshot"] = {"nested": [evidence]}
        store.scoring_jobs.submit(row)
    else:
        store.events.append({"run_id": "r", "nested": [evidence]})
    protection = planner().collect_trace_protection(store)
    assert "r" not in protection.run_ids
    assert protection.event_seqs["r"] == frozenset({2})
    assert enabled_plan(store).prefixes[0].last_seq == 1


def test_all_protection_roots(store, fixed_clock):
    from motte_storage.platform import platform_for
    from tests.storage.test_m6_stores import _snapshot, _spec, _cell
    from tests.storage.test_statistical_report_references import put_report
    from tests.evaluators.test_m8_calibration_records import import_fixture
    from motte_eval.calibration import build_calibration_set, sample_content_sha256, CalibrationSample
    from motte_eval.calibration_records import CalibrationImport
    names = ["manifest", "ledger", "legacy", "baseline", "gate", "report", "other-report",
             "judge", "experiment", "cross", "pass-only", "active", "unclaimed"]
    for name in names:
        seed_plan_run(store, name, status="running" if name == "active" else "completed",
                      manifest={"import_source": {"import_id": "old"}} if name == "manifest" else {})
        store.scoring_passes.append({"id": "pass-" + name, "run_id": name}, [])
    ledger = platform_for(store).imports
    ledger.begin_import({"import_id": "i", "manifest_sha256": "a" * 64})
    ledger.put_mapping({"mapping_key": "m", "import_id": "i", "target_type": "run", "target_id": "ledger"})
    store.baselines.put({"id": "legacy", "run_id": "legacy", "scoring_pass_id": "pass-legacy", "metrics": {}})
    baseline = _snapshot()
    baseline["entries"][0]["ref"].update(run_id="baseline", scoring_pass_id="pass-baseline")
    store.baseline_store.put(baseline)
    store.gate_store.put_result({"gate_result_id": "g", "policy_id": "policy", "run_id": "gate"})
    store.gate_store.put_result({"gate_result_id": "p", "policy_id": "policy", "source_pass_id": "pass-pass-only"})
    put_report(store, runs=("report", "other-report"), passes=("pass-report", "pass-other-report"))
    draft = import_fixture()
    raw = draft.calibration.samples[0].model_dump()
    raw["observation"] = {"provenance": {"source_run_id": "judge"}}
    sample = CalibrationSample.model_construct(**raw)
    raw["content_sha256"] = sample_content_sha256(sample)
    calibration = build_calibration_set(**{
        **draft.calibration.model_dump(exclude={"schema_version", "content_sha256", "samples"}),
        "samples": [CalibrationSample.model_validate(raw)],
    })
    store.calibrations.put_version(CalibrationImport(calibration=calibration, pairs=draft.pairs).to_version())
    store.experiments.put_spec(_spec())
    store.experiments.put_cell({**_cell("cell"), "run_id": "experiment"})
    store.case_runs.upsert({"run_id": "unclaimed", "case_id": "c", "source": {"run_id": "cross"}})
    protection = planner().collect_trace_protection(store)
    assert set(names) - {"unclaimed"} <= protection.run_ids
    assert "unclaimed" not in protection.run_ids
    assert [p.run_id for p in enabled_plan(store).prefixes] == ["unclaimed"]
    # Moving a Run's mutable current pointer cannot remove a frozen report pin.
    run = store.runs.get("report")
    store.runs.update({**run, "current_scoring_pass_id": None}, expected_revision=run["revision"])
    assert "report" in planner().collect_trace_protection(store).run_ids


@pytest.mark.parametrize("bad", [
    {"kind": "event", "run_id": "r", "locator": "0"},
    {"kind": "event", "run_id": "r", "locator": True},
    {"kind": "event", "run_id": "r"},
    {"kind": "event", "locator": "2"},
    {"run_ids": "r"}, {"source_run_id": 123}, {"source_pass_id": "missing"},
])
def test_malformed_reference_errors_fail_closed(store, fixed_clock, bad):
    seed_plan_run(store)
    store.gate_store.put_result({"gate_result_id": "bad", "policy_id": "policy", "nested": bad})
    with pytest.raises((ValueError, LookupError)):
        enabled_plan(store)


@pytest.mark.parametrize("repository,method", [
    ("scores", "list_for_run"), ("events", "stored_for_run"),
    ("statistical_reports", "list"), ("calibrations", "iter_records"),
    ("baseline_store", "list"), ("gate_store", "list_results"),
    ("experiments", "list_cells"), ("scoring_jobs", "list_by_status"),
])
def test_reference_reader_errors_fail_closed(store, fixed_clock, monkeypatch, repository, method):
    from tests.storage.test_m6_stores import _spec
    seed_plan_run(store)
    store.experiments.put_spec(_spec())
    def unavailable(*args, **kwargs):
        raise OSError("required reader unavailable")
    monkeypatch.setattr(getattr(store, repository), method, unavailable)
    with pytest.raises(OSError, match="required reader"):
        enabled_plan(store)


def test_unbounded_protection_includes_10001_pins(store, fixed_clock):
    seed_plan_run(store, "sentinel")
    records = [{"gate_result_id": f"g-{i}", "policy_id": "policy", "run_id": f"pin-{i}"}
               for i in range(10001)]
    records[0]["run_id"] = "sentinel"  # Beyond the newest 10,000 result window.
    if hasattr(store, "dsn"):
        from psycopg import connect
        from psycopg.types.json import Json
        with connect(store.dsn) as connection, connection.cursor() as cursor:
            cursor.executemany("INSERT INTO gate_results(gate_result_id,policy_id,payload) VALUES (%s,%s,%s)",
                               [(r["gate_result_id"], "policy", Json(r)) for r in records])
    elif hasattr(store.runs, "_path"):
        with sqlite3.connect(store.runs._path) as connection:
            connection.executemany("INSERT INTO gate_results(gate_result_id,policy_id,payload) VALUES (?,?,?)",
                                   [(r["gate_result_id"], "policy", json.dumps(r)) for r in records])
    else:
        for record in records:
            store.gate_store.put_result(record)
    protection = planner().collect_trace_protection(store)
    assert len(protection.run_ids) == 10001
    assert "sentinel" in protection.run_ids
    assert enabled_plan(store).prefixes == []


def test_plan_identity_binds_saved_cutoff_and_source(store, fixed_clock, monkeypatch):
    from motte_contracts.identity import canonical_sha256
    seed_plan_run(store)
    first = enabled_plan(store)
    assert enabled_plan(store) == first
    assert first.plan_id == canonical_sha256(first.model_dump(mode="json", exclude={"plan_id"}))
    prefix, = first.prefixes
    assert prefix.events_sha256 == canonical_sha256([
        row.model_dump(mode="json") for row in store.events.stored_for_run("r")[:3]])
    monkeypatch.setattr(models, "utc_now", lambda: NOW + timedelta(days=2))
    assert planner()._plan_at_cutoff(store, config=first.config, cutoff=first.cutoff) == first
    assert enabled_plan(store).plan_id != first.plan_id
    assert "postgresql:" not in first.model_dump_json() and "password" not in first.model_dump_json()
    run = store.runs.get("r")
    store.runs.update({**run, "note": "revision changed"}, expected_revision=run["revision"])
    assert planner()._plan_at_cutoff(store, config=first.config, cutoff=first.cutoff).plan_id != first.plan_id


def test_protection_plan_does_not_write_sqlite(tmp_path, fixed_clock, monkeypatch):
    store = SQLiteRunStore(tmp_path / "read-only.db")
    seed_plan_run(store)
    connect = sqlite3.connect
    denied = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
              sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_DROP_TABLE,
              sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_CREATE_TRIGGER}
    def read_only(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_authorizer(lambda action, *unused:
                                 sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK)
        return connection
    monkeypatch.setattr(sqlite3, "connect", read_only)
    assert enabled_plan(store).prefixes[0].last_seq == 3


@pytest.mark.parametrize("bad", [True, -1, 1.5, "2"])
def test_reference_list_limits_reject_bad_values(store, bad):
    for reader in (store.baseline_store.list, store.gate_store.list_results):
        with pytest.raises(ValueError, match="limit"):
            reader(limit=bad)


def test_reference_list_limits_allow_unbounded_and_zero(store):
    from tests.storage.test_m6_stores import _snapshot
    store.baseline_store.put(_snapshot())
    store.gate_store.put_result({"gate_result_id": "g", "policy_id": "p"})
    for reader in (store.baseline_store.list, store.gate_store.list_results):
        assert len(reader(limit=None)) == 1
        assert reader(limit=0) == []


@pytest.mark.parametrize("bad", [
    {"kind": "artifact", "locator": "evidence.bin"},
    {"kind": "invocation", "locator": "inv-1"},
    {"raw_ref": "../escape.bin"},
    {"artifact_id": "/absolute.bin"},
])
def test_incomplete_reference_shapes_fail_closed(store, fixed_clock, bad):
    seed_plan_run(store)
    store.gate_store.put_result({"gate_result_id": "bad", "policy_id": "p", "evidence": bad})
    with pytest.raises(ValueError):
        enabled_plan(store)


def test_nested_owner_reference_is_independent_not_ordinary_ownership(store, fixed_clock):
    seed_plan_run(store)
    store.case_runs.upsert({"run_id": "r", "case_id": "c", "source": {"owner": {"run_id": "r"}}})
    assert "r" in planner().collect_trace_protection(store).run_ids
    assert enabled_plan(store).prefixes == []


def test_reference_coupled_run_and_pass_identity_must_agree(store, fixed_clock):
    seed_plan_run(store)
    seed_plan_run(store, "other")
    store.scoring_passes.append({"id": "p-other", "run_id": "other"}, [])
    store.gate_store.put_result({"gate_result_id": "bad", "policy_id": "p",
                                 "ref": {"run_id": "r", "scoring_pass_id": "p-other"}})
    with pytest.raises(ValueError, match="identity"):
        enabled_plan(store)


def test_prefix_revalidates_unchecked_stored_row_identity(store, fixed_clock, monkeypatch):
    seed_plan_run(store)
    rows = store.events.stored_for_run("r")
    rows[0] = rows[0].model_copy(update={"payload": {"run_id": "other", "seq": 1}})
    monkeypatch.setattr(store.events, "stored_for_run", lambda run_id: rows)
    with pytest.raises(ValueError, match="identity"):
        enabled_plan(store)


@pytest.mark.parametrize("status,eligible", [
    ("completed", True), ("failed", True), ("cancelled", True), ("unsupported", True),
    ("profile_stale", True), ("needs_review", False), ("running", False),
    ("queued", False), ("unknown", False), ("scoring", False),
])
def test_prefix_status_allowlist_is_exact(fixed_clock, status, eligible):
    store = InMemoryRunStore()
    seed_plan_run(store, status=status)
    assert bool(enabled_plan(store).prefixes) is eligible


@pytest.mark.parametrize("repository", ["calibrations", "statistical_reports", "scoring_jobs",
                                        "baseline_store", "external_jobs"])
def test_missing_reference_repository_fails_closed(store, fixed_clock, monkeypatch, repository):
    seed_plan_run(store)
    monkeypatch.setattr(store, repository, None)
    with pytest.raises(AttributeError, match="required"):
        enabled_plan(store)


def test_missing_memory_import_ledger_is_not_fabricated(fixed_clock):
    store = InMemoryRunStore()
    seed_plan_run(store)
    store._motte_platform_stores = None
    with pytest.raises(AttributeError, match="ledger"):
        enabled_plan(store)


def test_missing_sqlite_reference_table_is_not_recreated(tmp_path, fixed_clock):
    store = SQLiteRunStore(tmp_path / "missing-ledger.db")
    seed_plan_run(store)
    with sqlite3.connect(store.runs._path) as connection:
        connection.execute("DROP TABLE motte_imports")  # Corrupt only this disposable fixture.
    with pytest.raises(sqlite3.OperationalError, match="motte_imports"):
        enabled_plan(store)
    with sqlite3.connect(store.runs._path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='motte_imports'").fetchall() == []


def test_plan_identity_changes_with_payload_time_config_and_protection(store, fixed_clock):
    seed_plan_run(store)
    first = enabled_plan(store)
    changed_config = planner()._plan_at_cutoff(store,
        config=models.TraceRetentionConfig(enabled=True, retention_days=4), cutoff=first.cutoff)
    assert changed_config.prefixes == first.prefixes and changed_config.plan_id != first.plan_id
    # Pinning the already-retained highest row still binds a changed protection set.
    store.gate_store.put_result({"gate_result_id": "g", "policy_id": "p",
        "evidence": {"kind": "event", "run_id": "r", "locator": "4"}})
    protected = enabled_plan(store)
    assert protected.prefixes == first.prefixes
    assert protected.protection_sha256 != first.protection_sha256
    assert protected.plan_id != first.plan_id
    # Alter only a disposable fixture's first server timestamp, then its payload.
    row = store.events.stored_for_run("r")[0]
    for payload, stored_at in [(row.payload, OLD - timedelta(seconds=1)),
                               ({**row.payload, "note": "changed evidence"}, OLD)]:
        if hasattr(store, "dsn"):
            from psycopg import connect
            from psycopg.types.json import Json
            with connect(store.dsn) as connection:
                connection.execute("UPDATE trace_events SET payload=%s, stored_at=%s WHERE run_id='r' AND seq=1",
                                   (Json(payload), stored_at.isoformat()))
        elif hasattr(store.runs, "_path"):
            with sqlite3.connect(store.runs._path) as connection:
                connection.execute("UPDATE trace_events SET payload=?, stored_at=? WHERE run_id='r' AND seq=1",
                                   (json.dumps(payload), stored_at.isoformat()))
        else:
            store.events._events["r"][0] = payload
            store.events._stored_at[("r", 1)] = stored_at
        changed = enabled_plan(store)
        assert changed.plan_id != protected.plan_id
        assert changed.prefixes[0].events_sha256 != protected.prefixes[0].events_sha256


def test_plan_identity_distinguishes_stores_and_reopens_same_sqlite(tmp_path, fixed_clock):
    left, right = InMemoryRunStore(), InMemoryRunStore()
    for store in (left, right):
        seed_plan_run(store)
    assert enabled_plan(left).store_identity_sha256 != enabled_plan(right).store_identity_sha256
    path = tmp_path / "identity.db"
    original = SQLiteRunStore(path)
    seed_plan_run(original)
    assert enabled_plan(original) == enabled_plan(SQLiteRunStore(path))


def test_orphan_import_mapping_reference_fails_closed(store, fixed_clock):
    from motte_storage.platform import platform_for
    seed_plan_run(store)
    platform_for(store).imports.put_mapping({"mapping_key": "orphan", "import_id": "absent",
                                             "target_type": "run", "target_id": "r"})
    with pytest.raises(ValueError, match="coverage|ownership|orphan"):
        enabled_plan(store)


@pytest.mark.parametrize("owner", ["absent", "calibration:fake-prefix"])
def test_orphan_cross_run_pass_reference_fails_closed(store, fixed_clock, owner):
    seed_plan_run(store)
    store.scoring_passes.append({"id": "orphan", "run_id": owner, "source_run_id": "r"}, [])
    with pytest.raises(ValueError, match="coverage|ownership|orphan"):
        enabled_plan(store)


def test_omitted_owned_reference_row_fails_closed(store, fixed_clock, monkeypatch):
    seed_plan_run(store)
    store.scoring_passes.append({"id": "hidden", "run_id": "r", "source_run_id": "r"}, [])
    monkeypatch.setattr(store.scoring_passes, "list_for_run", lambda run_id: [])
    with pytest.raises(ValueError, match="coverage|ownership|orphan"):
        enabled_plan(store)


def test_verified_parentless_calibration_reference_is_enumerated(store, fixed_clock):
    from tests.storage.test_scoring_jobs import job_record, pass_record, invocation_record
    seed_plan_run(store)
    owner = {"kind": "calibration", "calibration_job_id": "cal"}
    job = job_record(owner=owner)
    store.scoring_jobs.submit(job)
    prepared = store.scoring_jobs.claim(job["job_id"])
    invocation = invocation_record(owner={**owner, "sample_id": "sample"})
    invocation["request_summary"] = {"source_run_id": "r"}
    store.invocations.create(invocation)
    store.scoring_jobs.publish(job["job_id"], expected_revision=prepared["revision"],
        pass_record=pass_record(run_id="calibration:cal"), scores=[],
        events=[{"run_id": "calibration:cal", "type": "published", "source_run_id": "r"}])
    assert "r" in planner().collect_trace_protection(store).run_ids
    assert enabled_plan(store).prefixes == []


def test_benchmark_provenance_is_independent_reference_protection(store, fixed_clock):
    seed_plan_run(store)
    store.benchmark_datasets.put({"benchmark_id": "b", "dataset_revision": "1", "state": "ready",
                                  "provenance": {"source_run_id": "r"}})
    assert "r" in planner().collect_trace_protection(store).run_ids
    assert enabled_plan(store).prefixes == []


def test_subject_job_reference_owner_identity_must_agree(store, fixed_clock, monkeypatch):
    from tests.storage.test_scoring_jobs import job_record
    seed_plan_run(store)
    store.scoring_jobs.submit(job_record(owner={"kind": "subject", "run_id": "r"}))
    rows = store.scoring_jobs.list_by_status()
    rows[0]["owner_ref"] = "run:forged"
    monkeypatch.setattr(store.scoring_jobs, "list_by_status", lambda: rows)
    with pytest.raises(ValueError, match="ownership"):
        enabled_plan(store)


def test_reference_sequence_shape_has_backend_parity(store, fixed_clock):
    seed_plan_run(store)
    seed_plan_run(store, "holder")
    store.case_runs.upsert({"run_id": "holder", "case_id": "c",
                            "source": ({"run_id": "r"},)})
    assert "r" in planner().collect_trace_protection(store).run_ids
    assert "r" not in {prefix.run_id for prefix in enabled_plan(store).prefixes}


def test_read_only_reference_traversal_never_reconstructs_missing_jobs(tmp_path, monkeypatch):
    from motte_storage.artifact_refs import _iter_artifact_records_with_owners
    import motte_storage.scoring_jobs as jobs
    store = SQLiteRunStore(tmp_path / "missing-jobs.db")
    store.scoring_jobs = None
    def forbidden_factory(_store):
        raise AssertionError("read-only scan attempted repository/schema construction")
    monkeypatch.setattr(jobs, "scoring_jobs_for", forbidden_factory)
    with pytest.raises(AttributeError, match="required"):
        list(_iter_artifact_records_with_owners(store, read_only=True))
