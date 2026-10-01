"""Publication-owned Run/Pass/Artifact pins are complete and fail closed."""
from __future__ import annotations

from copy import deepcopy
import multiprocessing as mp
import sqlite3
from contextlib import closing

import pytest

from motte_contracts.hashing import canonical_hash
from motte_contracts.statistical_reports import statistical_report_id
from motte_storage import artifact_refs
from motte_storage.maintenance import begin_maintenance, end_maintenance
from motte_storage.operation_locks import MaintenanceConflict
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore
from motte_storage.statistical_reports import StatisticalReportCorrupt
from tests.contract.test_statistical_reports import _body
from tests.sdk.test_statistical_reports import _open, _seed, _publish, _service, _finish_process
from tests.security.test_gc_retention import _old_artifact


def report_body(*, runs=("run-baseline", "run-candidate"), passes=("pass-baseline", "pass-candidate"),
                evidence=None):
    body = _body()
    for side, run_id, pass_id in zip(("baseline", "candidate"), runs, passes, strict=True):
        body["result"]["refs"][side].update(run_id=run_id, scoring_pass_id=pass_id)
    body["result"]["inputs"]["refs"] = deepcopy(body["result"]["refs"])
    body["result"]["input_digest"] = canonical_hash(body["result"]["inputs"])
    if evidence is not None:
        body["result"]["diagnostic"] = evidence
    return body


def put_report(store, **kwargs):
    body = report_body(**kwargs)
    return store.statistical_reports.put(statistical_report_id(body), body)


def damage_report_reader(store, monkeypatch, failure):
    """Inject an actual persisted integrity failure, or an I/O failure at list/get."""
    repository = store.statistical_reports
    if failure == "integrity":
        with closing(sqlite3.connect(store.runs._path)) as connection, connection:
            connection.execute("UPDATE statistical_reports SET body = '{}' ")
        return StatisticalReportCorrupt
    if failure == "conflict":
        put_report(store, evidence={"nested": {"artifact_refs": [
            {"artifact_id": "conflict.bin", "sha256": "a" * 64},
            {"artifact_id": "./conflict.bin", "sha256": "b" * 64},
        ]}})
        return artifact_refs.ArtifactReferenceConflict

    def unavailable(*args, **kwargs):
        raise OSError("statistical report read unavailable")

    if failure == "get":
        monkeypatch.setattr(type(repository), "get", unavailable)

        def via_get(self, *, limit=None):
            return [self.get("unavailable")]

        monkeypatch.setattr(type(repository), "list", via_get)
    else:
        monkeypatch.setattr(type(repository), "list", unavailable)
    return OSError


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def report_store(request, tmp_path):
    if request.param == "memory":
        return InMemoryRunStore()
    if request.param == "sqlite":
        return SQLiteRunStore(tmp_path / "references.db")
    from motte_storage.migrations import upgrade
    dsn = request.getfixturevalue("isolated_pg_database")
    # Unified reference scanning needs every integrated evidence repository.
    upgrade(dsn)
    return _open("postgres", dsn)


def test_all_reports_scanned_and_failure_aborts(report_store, monkeypatch):
    publications = [put_report(report_store, runs=(f"run-{index}", "candidate"),
                               passes=(f"pass-{index}", "candidate-pass"),
                               evidence={"artifact_id": f"report-{index}.bin"})
                    for index in range(106)]
    publications.sort(key=lambda row: row["report_id"])
    sentinel = publications[-1]["body"]["result"]
    original = report_store.statistical_reports.list
    calls = []

    def capped_by_default(*, limit=100):
        calls.append(limit)
        return original(limit=limit)

    monkeypatch.setattr(report_store.statistical_reports, "list", capped_by_default)
    runs = artifact_refs.referenced_run_ids(report_store)
    assert sentinel["refs"]["baseline"]["run_id"] in runs
    assert runs == {f"run-{index}" for index in range(106)} | {"candidate"}
    passes = artifact_refs.referenced_pass_ids(report_store)
    assert sentinel["refs"]["baseline"]["scoring_pass_id"] in passes
    assert passes == {f"pass-{index}" for index in range(106)} | {"candidate-pass"}
    refs = artifact_refs.referenced_artifact_hashes(report_store)
    assert refs == {f"report-{index}.bin": None for index in range(106)}
    from motte_storage.maintenance import _store_counts
    assert _store_counts(report_store)["statistical_reports"] == 106
    assert calls == [None, None, None, None]

    def fail(*, limit=None):
        raise OSError("report enumeration failed")

    monkeypatch.setattr(report_store.statistical_reports, "list", fail)
    for scan in (artifact_refs.referenced_run_ids, artifact_refs.referenced_pass_ids,
                 artifact_refs.referenced_artifact_hashes):
        with pytest.raises(OSError, match="report enumeration failed"):
            scan(report_store)


@pytest.mark.parametrize("missing", [None, object()])
def test_missing_report_repository_fails_closed(report_store, missing):
    report_store.statistical_reports = missing
    with pytest.raises(AttributeError, match="statistical_reports.list"):
        list(artifact_refs.iter_artifact_records(report_store))


def test_collect_referenced_pass_ids_recurses_without_false_positives():
    found = {"preexisting"}
    artifact_refs.collect_referenced_pass_ids({
        "scoring_pass_id": "selected", "source_pass_id": "source", "previous_pass_id": "previous",
        "nested": [{"target_type": "scoring_pass", "target_id": "mapped"},
                   {"deeper": {"scoring_pass_id": "nested"}}],
        "artifact": {"id": "not-a-pass", "target_type": "run", "target_id": "run"},
        "bad": [{"source_pass_id": None}, {"previous_pass_id": 8}, {"scoring_pass_id": False}],
    }, found)
    assert found == {"preexisting", "selected", "source", "previous", "mapped", "nested"}


def test_report_independent_ownership_survives_run_and_import_exclusions(report_store):
    report = put_report(report_store)
    options = {"exclude_run_ids": {"run-baseline", "run-candidate"},
               "exclude_import_ids": {"import-owner"}}
    rows = list(artifact_refs._iter_artifact_records_with_owners(report_store, **options))
    assert (report, None, None) in rows
    assert artifact_refs.referenced_run_ids(report_store, **options) == {
        "run-baseline", "run-candidate"}
    assert artifact_refs.referenced_pass_ids(report_store, **options) == {
        "pass-baseline", "pass-candidate"}


def _capture_with_artifact(backend, location, artifact, ready, release, outcomes):
    """Run real calculation with a synthetic nested evidence ref at the capture boundary."""
    from motte_sdk.comparisons import ComparisonService

    try:
        store = _open(backend, location)
        original = ComparisonService.paired_statistics

        def paused(self, *args, **kwargs):
            ready.set()
            assert release.wait(20), "parent did not release capture"
            result = original(self, *args, **kwargs)
            result["diagnostic"] = {"artifact_refs": [artifact]}
            return result

        ComparisonService.paired_statistics = paused
        outcomes.put(("ok", _publish(_service(store))))
    except MaintenanceConflict:
        outcomes.put(("conflict", None))
    except BaseException as error:
        outcomes.put(("error", repr(error)))
        raise


@pytest.fixture(params=["sqlite", "postgres"])
def persistent_report_store(request, tmp_path):
    if request.param == "sqlite":
        location = str(tmp_path / "race.db")
    else:
        from motte_storage.migrations import upgrade
        location = request.getfixturevalue("isolated_pg_database")
        upgrade(location)
    return request.param, location, _seed(_open(request.param, location))


def test_publication_blocks_gc_apply_race(persistent_report_store, tmp_path):
    from motte_storage.gc import apply_gc, plan_gc

    backend, location, store = persistent_report_store
    root = tmp_path / "artifacts"
    artifact = _old_artifact(root, "report-only.bin", b"report evidence")
    _old_artifact(root, "orphan.bin", b"deletable control")
    plan = plan_gc(store, root, artifact_ttl_days=1)
    assert {item["artifact_id"] for item in plan.deletable} == {"report-only.bin", "orphan.bin"}
    context = mp.get_context("spawn")
    ready, release, outcomes = context.Event(), context.Event(), context.Queue()
    process = context.Process(target=_capture_with_artifact, args=(
        backend, location, {"artifact_id": artifact.id, "sha256": artifact.sha256},
        ready, release, outcomes,
    ))
    process.start()
    try:
        assert ready.wait(20), "publication never reached calculation"
        with pytest.raises(MaintenanceConflict):
            apply_gc(store, root, plan, confirm=True)
        assert (root / artifact.id).exists() and (root / "orphan.bin").exists()
        assert store.statistical_reports.list() == []
        release.set()
        kind, report = outcomes.get(timeout=25)
        process.join(timeout=10)
        assert process.exitcode == 0 and kind == "ok", report
        assert store.statistical_reports.get(report["report_id"]) == report
        result = apply_gc(store, root, plan, confirm=True)
        assert result["deleted"] == 1
        assert (root / artifact.id).read_bytes() == b"report evidence"
        assert not (root / "orphan.bin").exists()
        assert {item["artifact_id"] for item in plan_gc(store, root).protected} == {artifact.id}
    finally:
        _finish_process(process, release, outcomes)


def test_gc_maintenance_first_rejects_publication_without_capture(persistent_report_store, tmp_path):
    backend, location, store = persistent_report_store
    context = mp.get_context("spawn")
    ready, release, outcomes = context.Event(), context.Event(), context.Queue()
    lease = begin_maintenance(store, reason="gc", artifacts_root=tmp_path / "artifacts")
    process = context.Process(target=_capture_with_artifact, args=(
        backend, location, {"artifact_id": "late.bin"}, ready, release, outcomes,
    ))
    process.start()
    try:
        assert outcomes.get(timeout=25) == ("conflict", None)
        process.join(timeout=10)
        assert process.exitcode == 0
        assert not ready.is_set()
        assert store.statistical_reports.list() == []
    finally:
        _finish_process(process, release, outcomes)
        end_maintenance(store, owner=lease["owner"])
