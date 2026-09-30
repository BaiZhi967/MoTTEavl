"""Frozen publications retain real calculator results and exclude maintenance."""
from __future__ import annotations

from copy import deepcopy
import json
import multiprocessing as mp
import sqlite3
from contextlib import closing
from threading import Thread
from types import SimpleNamespace

import pytest

from motte_contracts.hashing import canonical_hash
from motte_sdk.comparisons import ComparisonService
from motte_storage.maintenance import begin_maintenance, end_maintenance
from motte_storage.operation_locks import MaintenanceConflict
from motte_storage.platform import platform_for
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore
from tests.sdk.test_m6_comparison_service import append_pass, make_run, score_row
from tests.sdk.test_m8_statistics_exports import _fixture, _fixed


def _service(store):
    # Keep collection valid before the SDK module exists (TDD).
    from motte_sdk.statistical_reports import StatisticalReportService
    return StatisticalReportService(store)


def _guard(store):
    from motte_storage.statistical_reports import statistical_publication_guard
    return statistical_publication_guard(store)


def _publish(service, **overrides):
    options = {"allowed_factors": [], "baseline_pass_id": "old-base",
               "candidate_pass_id": "old-candidate"}
    options.update(overrides)
    return service.publish("base", "candidate", **options)


def _seed(store):
    for run_id in ("base", "candidate"):
        make_run(store, run_id, case_ids=["a", "b"])
        append_pass(store, run_id, f"old-{run_id}", [
            score_row("a", passed=run_id == "candidate"), score_row("b", passed=True),
        ])
        for case_id in ("a", "b"):
            store.case_runs.upsert({"run_id": run_id, "case_id": case_id, "result": {
                "cost": {"total": 2, "currency": "USD"}, "metering": {"latency_ms": 10},
            }})
    return store


def _open(backend, location):
    if backend == "sqlite":
        return SQLiteRunStore(location)
    from motte_storage.postgres import create_postgres_run_store
    return create_postgres_run_store(location)


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "memory":
        return _seed(InMemoryRunStore())
    if request.param == "sqlite":
        return _seed(SQLiteRunStore(tmp_path / "reports.db"))
    dsn = request.getfixturevalue("isolated_pg_database")
    from motte_storage.migrations import upgrade
    upgrade(dsn)
    return _seed(_open("postgres", dsn))


@pytest.fixture(params=["sqlite", "postgres"])
def persistent_store(request, tmp_path):
    if request.param == "sqlite":
        location = str(tmp_path / "race.db")
    else:
        location = request.getfixturevalue("isolated_pg_database")
        from motte_storage.migrations import upgrade
        upgrade(location)
    return request.param, location, _seed(_open(request.param, location))


def test_publish_captures_once_and_get_never_recomputes(store, monkeypatch):
    from motte_eval.statistics import STATISTICAL_POLICY_V1

    service = _service(store)
    calculator = ComparisonService.paired_statistics
    captures = []

    def capture(self, *args, **kwargs):
        result = calculator(self, *args, **kwargs)
        captures.append(deepcopy(result))
        return result

    monkeypatch.setattr(ComparisonService, "paired_statistics", capture)
    first = _publish(service)
    assert len(captures) == 1
    assert first["body"]["result"] == captures[0]
    assert first["body"]["policy"] == STATISTICAL_POLICY_V1
    result = first["body"]["result"]
    # Assert nontrivial, applicable mathematics, not merely contract shape.
    assert result["applicable"] is True
    assert result["statistics"] == {
        "n": 2, "mean_diff": 0.5, "median_diff": 0.5, "min_diff": 0.0, "max_diff": 1.0,
        "interval": {"low": 0.0, "high": 1.0, "method": "task_cluster_bootstrap",
                     "iterations": 2000, "seed": 20260921, "applicable": True},
    }
    assert first["body"]["policy"]["confidence"] == 0.95
    assert result["implementation_version"] == "motte_eval.statistics@1"
    assert _publish(service) == first
    assert len(captures) == 2
    append_pass(store, "base", "new-base", [])
    row = store.case_runs.get("base", "a")
    row["result"]["metering"]["latency_ms"] = 90
    # Simulate synthetic evidence repair/reimport; normal upsert is insert-only.
    if hasattr(store.case_runs, "_rows"):
        store.case_runs._rows[("base", "a")][1]["result"] = row["result"]
    elif getattr(store, "dsn", None):
        from motte_storage.postgres import _connect
        with _connect(store.dsn) as connection:
            connection.execute("UPDATE case_runs SET payload = %s WHERE run_id = %s AND case_id = %s",
                               (json.dumps(row), "base", "a"))
    else:
        with closing(sqlite3.connect(store.runs._path)) as connection, connection:
            connection.execute("UPDATE case_runs SET payload = ? WHERE run_id = ? AND case_id = ?",
                               (json.dumps(row), "base", "a"))
    changed = _publish(service)
    assert len(captures) == 3
    assert changed["report_id"] != first["report_id"]
    assert changed["body"]["result"]["refs"] == result["refs"]
    assert changed["body"]["result"]["input_digest"] != result["input_digest"]

    def forbidden(*args, **kwargs):
        pytest.fail("reading a publication must not recalculate or reread evidence")

    monkeypatch.setattr(ComparisonService, "paired_statistics", forbidden)
    monkeypatch.setattr(store.runs, "get", forbidden)
    monkeypatch.setattr(store.scoring_passes, "get", forbidden)
    monkeypatch.setattr(store.case_runs, "list_for_run", forbidden)
    monkeypatch.setitem(STATISTICAL_POLICY_V1, "bootstrap_seed", 1)
    monkeypatch.setitem(STATISTICAL_POLICY_V1, "confidence", 0.5)
    assert service.get(first["report_id"]) == first
    detached = service.get(first["report_id"])
    detached["body"]["result"]["inputs"]["allowed_factors"].append("mutated")
    assert service.get(first["report_id"]) == first
    with pytest.raises(KeyError, match="unknown"):
        service.get("unknown")


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -float("inf"), -1, True, "12"])
def test_invalid_metering_remains_visible(bad):
    store, calculator = _fixture([
        {"cost": {"total": bad, "currency": "USD"}, "metering": {"latency_ms": bad}},
        {"cost": {"total": 0, "currency": "USD"}, "metering": {"latency_ms": 0}},
    ])
    expected = _fixed(calculator)
    report = _publish(_service(store))
    result = report["body"]["result"]
    assert result == expected
    assert json.loads(json.dumps(report, allow_nan=False)) == report
    assert result["input_digest"] == canonical_hash(result["inputs"])
    for side in ("baseline", "candidate"):
        description = result["descriptive"][side]
        assert description["cost"]["known_cost_count"] == 1
        assert description["cost"]["unknown_cost_count"] == 1
        assert description["cost"]["by_currency"]["USD"]["missing"] == 1
        assert description["latency_ms"]["count"] == 1
        assert description["latency_ms"]["missing"] == 1


@pytest.mark.parametrize("results,known,unknown,currencies", [
    ([{"cost": {"total": 8}}, {}], 0, 2, set()),
    ([None, None], 0, 2, set()),
    ([{"cost": {"total": 1, "currency": "USD"}},
      {"cost": {"total": 7, "currency": "CNY"}}, None], 2, 1, {"USD", "CNY"}),
    ([{"cost": {"total": 999, "currency": "USD", "scope": "judge"}},
      {"judge": {"cost": {"total": 999, "currency": "USD"}}}], 0, 2, {"USD"}),
])
def test_missing_and_mixed_currencies_never_invent_subject_cost(results, known, unknown, currencies):
    store, calculator = _fixture(results)
    expected = _fixed(calculator)
    report = _publish(_service(store))
    assert report["body"]["result"] == expected
    json.dumps(report, allow_nan=False)
    cost = report["body"]["result"]["descriptive"]["baseline"]["cost"]
    assert cost["known_cost_count"] == known
    assert cost["unknown_cost_count"] == unknown
    assert set(cost["by_currency"]) == currencies
    assert "total" not in cost and "total_usd" not in cost
    if known == 0:
        assert all(item["count"] == 0 and item["mean"] is None
                   for item in cost["by_currency"].values())


def _terminal_store(kind):
    store = InMemoryRunStore()
    for run_id in ("base", "candidate"):
        plan = [
            {"trial_id": f"{run_id}-{task}-{repeat}", "task_key": task,
             "repeat_index": repeat, "run_id": run_id,
             "agent_config_hash": "agent-a", "environment_hash": "env-a"}
            for task in ("a", "b") for repeat in range(3)
        ]
        make_run(store, run_id, case_ids=["a", "b"], manifest={
            "benchmark_provenance": {"suite": "terminal-bench-harbor"},
            "task_manifest": {"trials": plan},
        })
        rows = []
        for trial in plan:
            if kind == "missing" and trial["repeat_index"] == 2:
                continue
            rows.append({
                **score_row(trial["task_key"], passed=trial["repeat_index"] < (
                    1 if run_id == "base" else 2)),
                "trial_id": trial["trial_id"], "unit": "trial", "metric_id": "terminal.reward",
                "details": {"repeat_index": trial["repeat_index"], "source_trial_id": (
                    "reused-source" if kind == "reused" and trial["task_key"] == "a"
                    else trial["trial_id"])},
            })
        rows.append({**score_row("a", passed=True), "trial_id": f"{run_id}-retry",
                     "unit": "trial", "metric_id": "terminal.reward"})
        append_pass(store, run_id, f"old-{run_id}", rows)
    return store


@pytest.mark.parametrize("kind", ["qualified", "missing", "reused"])
def test_terminal_k2_preserves_actual_trial_qualification_and_excludes_retries(kind):
    store = _terminal_store(kind)
    expected = _fixed(ComparisonService(store), k=2)
    report = _publish(_service(store), k=2)
    result = report["body"]["result"]
    assert result == expected
    json.dumps(report, allow_nan=False)
    aggregation = result["trial_aggregation"]["baseline"]
    assert aggregation["excluded_unplanned_score_rows"] == 1
    task = aggregation["per_task"]["a"]
    assert task["n_planned"] == 3
    if kind == "qualified":
        assert result["applicable"] is True
        assert result["n_pairs"] == 2 and result["missing_pairs"] == 0
        assert task["pass_at_k"]["value"] == pytest.approx(2 / 3)
        assert result["statistics"]["mean_diff"] == pytest.approx(1 / 3)
    else:
        assert result["applicable"] is False
        assert result["statistics"] is None
        assert result["missing_pairs"] > 0
        assert result["reason"] == "missing_or_invalid_trial"
        if kind == "reused":
            assert task["pass_at_k"]["reason"] == "non_independent_trials"
        else:
            assert task["n_valid"] == 2


def test_current_moves_during_capture(monkeypatch):
    store = _terminal_store("qualified")
    expected = _fixed(ComparisonService(store), k=2)
    report_ref = ComparisonService.report_ref
    switched = set()

    def switching_ref(self, run_id, *, scoring_pass_id=None):
        ref = report_ref(self, run_id, scoring_pass_id=scoring_pass_id)
        if run_id not in switched:
            switched.add(run_id)
            append_pass(store, run_id, f"new-{run_id}", [])
        return ref

    monkeypatch.setattr(ComparisonService, "report_ref", switching_ref)
    report = _service(store).publish("base", "candidate", allowed_factors=[], k=2)
    assert report["body"]["result"] == expected
    for run_id in ("base", "candidate"):
        assert store.scoring_passes.current(run_id)["id"] == f"new-{run_id}"
    assert report["body"]["result"]["applicable"] is True


@pytest.mark.parametrize("overrides", [
    {"baseline_pass_id": "old-candidate"}, {"candidate_pass_id": "old-base"},
    {"baseline_pass_id": "missing"}, {"candidate_pass_id": "missing"},
    {"k": 0}, {"k": -1}, {"k": True}, {"k": "2"}, {"k": 1.5},
    {"allowed_factors": [""]}, {"baseline_pass_id": " "},
])
def test_invalid_pass_or_request_never_creates_report(overrides):
    store = _seed(InMemoryRunStore())
    with pytest.raises((ValueError, KeyError)):
        _publish(_service(store), **overrides)
    assert store.statistical_reports.list() == []


def test_allowed_factors_are_normalized_before_calculation():
    store = _seed(InMemoryRunStore())
    service = _service(store)
    first = _publish(service, allowed_factors=("model", "model"))
    assert first["body"]["result"]["inputs"]["allowed_factors"] == ["model"]
    assert first == _publish(service, allowed_factors=["model"])


def test_publication_guard_releases_on_exception(store):
    with pytest.raises(RuntimeError, match="capture failed"):
        with _guard(store):
            raise RuntimeError("capture failed")
    # Another thread needs to acquire the same memory lock; re-entry alone would
    # not detect a leaked RLock. A fresh connection/process sees SQL/file locks.
    results = []

    def publish():
        try:
            results.append(_publish(_service(store)))
        except BaseException as error:
            results.append(error)

    worker = Thread(target=publish, daemon=True)
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert len(results) == 1 and isinstance(results[0], dict), results
    if getattr(store.runs, "_path", None) or getattr(store, "dsn", None):
        lease = begin_maintenance(store)
        end_maintenance(store, owner=lease["owner"])


def test_active_metadata_refuses_publication_but_keeps_get_read_only(store):
    service = _service(store)
    report = _publish(service)
    platform_for(store).meta.set("maintenance", "active")
    try:
        with pytest.raises(MaintenanceConflict):
            _publish(service)
        assert service.get(report["report_id"]) == report
        assert store.statistical_reports.list() == [report]
    finally:
        platform_for(store).meta.delete("maintenance")


def test_memory_guard_uses_the_run_store_lock():
    store = InMemoryRunStore()
    observations = []

    def try_run_lock():
        acquired = store.runs._lock.acquire(blocking=False)
        observations.append(acquired)
        if acquired:
            store.runs._lock.release()

    with _guard(store):
        thread = Thread(target=try_run_lock)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert observations == [False]


def _publish_in_process(backend, location, phase, ready, release, outcomes):
    try:
        store = _open(backend, location)
        if phase == "capture":
            original = ComparisonService.paired_statistics

            def paused(self, *args, **kwargs):
                ready.set()
                assert release.wait(20), "parent did not release capture"
                return original(self, *args, **kwargs)

            ComparisonService.paired_statistics = paused
        elif phase == "insert":
            original = store.statistical_reports.put

            def paused(*args, **kwargs):
                ready.set()
                assert release.wait(20), "parent did not release insertion"
                return original(*args, **kwargs)

            store.statistical_reports.put = paused
        outcomes.put(("ok", _publish(_service(store))))
    except MaintenanceConflict:
        outcomes.put(("conflict", None))
    except BaseException as error:
        outcomes.put(("error", repr(error)))
        raise


def _finish_process(process, release, outcomes):
    release.set()
    process.join(timeout=25)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
    outcomes.close()


@pytest.mark.parametrize("phase", ["capture", "insert"])
def test_publication_excludes_maintenance_in_another_process(persistent_store, phase):
    backend, location, store = persistent_store
    context = mp.get_context("spawn")
    ready, release, outcomes = context.Event(), context.Event(), context.Queue()
    process = context.Process(target=_publish_in_process,
                              args=(backend, location, phase, ready, release, outcomes))
    process.start()
    try:
        assert ready.wait(20), "publication did not reach checkpoint"
        with pytest.raises(MaintenanceConflict):
            begin_maintenance(store, reason="gc")
        assert store.statistical_reports.list() == []
        release.set()
        kind, report = outcomes.get(timeout=25)
        process.join(timeout=10)
        assert process.exitcode == 0 and kind == "ok", report
        assert _service(store).get(report["report_id"]) == report
        lease = begin_maintenance(store, reason="gc")
        end_maintenance(store, owner=lease["owner"])
    finally:
        _finish_process(process, release, outcomes)


def test_maintenance_excludes_publication_in_another_process(persistent_store):
    backend, location, store = persistent_store
    lease = begin_maintenance(store, reason="rollback")
    context = mp.get_context("spawn")
    ready, release, outcomes = context.Event(), context.Event(), context.Queue()
    process = context.Process(target=_publish_in_process,
                              args=(backend, location, "capture", ready, release, outcomes))
    process.start()
    try:
        assert outcomes.get(timeout=25) == ("conflict", None)
        process.join(timeout=10)
        assert process.exitcode == 0
        assert not ready.is_set(), "calculator ran under active maintenance"
        assert store.statistical_reports.list() == []
    finally:
        _finish_process(process, release, outcomes)
        end_maintenance(store, owner=lease["owner"])
    assert _publish(_service(store))["body"]["result"]["applicable"] is True


@pytest.mark.parametrize("acquired,active", [(True, False), (False, False), (True, True)])
def test_postgres_guard_uses_live_shared_session_lock_and_closes_on_all_paths(monkeypatch, acquired, active):
    from motte_storage import maintenance, postgres

    events = []

    class Connection:
        autocommit = False

        def execute(self, sql, params):
            assert self.autocommit is True
            events.append((sql, params))
            return SimpleNamespace(fetchone=lambda: (acquired,))

        def close(self):
            events.append("closed")

    connection = Connection()
    monkeypatch.setattr(postgres, "_connect", lambda dsn: connection)
    monkeypatch.setattr(maintenance, "maintenance_status", lambda store:
                        events.append("metadata") or {"active": active})
    if not acquired or active:
        with pytest.raises(MaintenanceConflict):
            with _guard(SimpleNamespace(dsn="disposable-test")):
                pytest.fail("guard yielded despite exclusion")
    else:
        with pytest.raises(RuntimeError, match="capture failed"):
            with _guard(SimpleNamespace(dsn="disposable-test")):
                assert events[-1] == "metadata" and "closed" not in events
                raise RuntimeError("capture failed")
    assert events[0] == (
        "SELECT pg_try_advisory_lock_shared(hashtext(%s))", ("motteavl:maintenance",),
    )
    assert events[-1] == "closed"
    assert ("metadata" in events) is acquired


def test_publishers_share_the_persistent_guard(persistent_store):
    backend, location, store = persistent_store
    context = mp.get_context("spawn")
    ready, release, outcomes = context.Event(), context.Event(), context.Queue()
    process = context.Process(target=_publish_in_process,
                              args=(backend, location, "unpaused", ready, release, outcomes))
    try:
        with _guard(store):
            process.start()
            kind, report = outcomes.get(timeout=25)
            process.join(timeout=10)
            assert process.exitcode == 0 and kind == "ok", report
            assert _service(store).get(report["report_id"]) == report
    finally:
        _finish_process(process, release, outcomes)
