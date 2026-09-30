"""Immutable repositories share content identity, strict reads and detached values."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, UTC
import importlib.util
import json
import multiprocessing as mp
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from motte_contracts.hashing import canonical_hash, canonical_json
from motte_contracts.statistical_reports import statistical_report_id
from motte_storage.migrations import MIGRATIONS_DIR
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore
from tests.contract.test_statistical_reports import _body


def _reports():
    # Keep collection working before the new repository module exists (TDD).
    from motte_storage import statistical_reports
    return statistical_reports


def _rewrite(repo, old_id, *, report_id=None, body=None, published_at=None):
    """Simulate disk corruption, never use a public mutation API."""
    if isinstance(repo, _reports().MemoryStatisticalReports):
        row = repo._rows.pop(old_id)
        row = (report_id or row[0], body if body is not None else row[1],
               published_at if published_at is not None else row[2])
        repo._rows[row[0]] = row
        return
    if isinstance(repo, _reports().SQLiteStatisticalReports):
        connection = sqlite3.connect(repo._path)
        marker = "?"
    else:
        import psycopg
        connection = psycopg.connect(repo._dsn)
        marker = "%s"
    try:
        with connection:
            row = connection.execute(
                f"SELECT report_id, body, published_at FROM statistical_reports WHERE report_id={marker}",
                (old_id,),
            ).fetchone()
            connection.execute(
                f"UPDATE statistical_reports SET report_id={marker}, body={marker}, "
                f"published_at={marker} WHERE report_id={marker}",
                (report_id or row[0], body if body is not None else row[1],
                 published_at if published_at is not None else row[2], old_id),
            )
    finally:
        connection.close()


class _RepositoryContract:
    def test_put_replay_and_detachment(self, repository):
        body = _body()
        original = deepcopy(body)
        report_id = statistical_report_id(body)
        before = datetime.now(UTC)
        first = repository.put(report_id, body)
        after = datetime.now(UTC)
        assert before <= datetime.fromisoformat(first["published_at"]) <= after
        assert set(first) == {"report_id", "published_at", "body"}
        assert first["body"] == original
        expected = deepcopy(first)
        body["result"]["descriptive"]["baseline"]["tamper"] = True
        first["body"]["policy"]["confidence"] = 0.1
        assert repository.put(report_id, original) == expected
        reordered = json.loads(json.dumps(original, sort_keys=True))
        assert repository.put(report_id, reordered) == expected
        fetched = repository.get(report_id)
        fetched["body"]["result"]["inputs"]["allowed_factors"].append("tamper")
        listed = repository.list()
        listed[0]["body"]["result"]["descriptive"]["baseline"]["tamper"] = True
        assert repository.get(report_id) == expected
        assert repository.list() == [expected]
        assert repository.get("unknown") is None
        assert not hasattr(repository, "delete") and not hasattr(repository, "update")

    def test_sorted_unbounded_and_limited_list(self, repository):
        expected = []
        for index in range(105):
            body = _body()
            body["result"]["diagnostic"] = index
            expected.append(repository.put(statistical_report_id(body), body))
        expected.sort(key=lambda item: item["report_id"])
        assert repository.list() == repository.list(limit=None) == expected
        assert repository.list(limit=0) == []
        assert repository.list(limit=1) == expected[:1]
        assert repository.list(limit=103) == expected[:103]
        assert repository.list(limit=1000) == expected
        assert repository.list(limit=2 ** 100) == expected

    @pytest.mark.parametrize("limit", [-1, True, False, 1.0, "1"])
    def test_invalid_limit_rejects(self, repository, limit):
        with pytest.raises(ValueError, match="limit"):
            repository.list(limit=limit)

    def test_conflicting_identity_preserves_first_publication(self, repository):
        body = _body()
        report_id = statistical_report_id(body)
        first = repository.put(report_id, body)
        body["result"]["statistics"]["mean_diff"] = 0.5
        with pytest.raises(_reports().StatisticalReportConflict):
            repository.put(report_id, body)
        assert repository.get(report_id) == first
        assert repository.list() == [first]

    @pytest.mark.parametrize("corruption", ["body", "input_digest", "id", "json", "timestamp"])
    def test_corruption_fails_get_list_and_replay_without_repair(self, repository, corruption):
        body = _body()
        report_id = statistical_report_id(body)
        repository.put(report_id, body)
        changed = deepcopy(body)
        raw = None
        timestamp = None
        lookup_id = report_id
        if corruption == "body":
            changed["result"]["descriptive"]["baseline"]["changed"] = True
            raw = canonical_json(changed)
        elif corruption == "input_digest":
            changed["result"]["input_digest"] = "sha256:wrong"
            raw = canonical_json(changed)
            # Rehash the bad body: the inner digest check must still reject it.
            lookup_id = statistical_report_id(changed)
        elif corruption == "id":
            lookup_id = "stat-report-" + "0" * 64
        elif corruption == "json":
            raw = "{broken json"
        else:
            timestamp = "not-a-date"
        _rewrite(repository, report_id, report_id=lookup_id, body=raw, published_at=timestamp)
        for read in (lambda: repository.get(lookup_id), lambda: repository.list()):
            with pytest.raises(_reports().StatisticalReportCorrupt):
                read()
        if lookup_id == report_id:
            with pytest.raises(_reports().StatisticalReportCorrupt):
                repository.put(report_id, body)
        else:
            with pytest.raises(ValueError):
                repository.put(lookup_id, body)
        # The failed replay must not replace the damaged row.
        with pytest.raises(_reports().StatisticalReportCorrupt):
            repository.get(lookup_id)

    @pytest.mark.parametrize("encoding", ["whitespace", "duplicate_key"])
    def test_noncanonical_persisted_bytes_fail_closed(self, repository, encoding):
        body = _body()
        report_id = statistical_report_id(body)
        repository.put(report_id, body)
        raw = (json.dumps(body) if encoding == "whitespace"
               else canonical_json(body)[:-1] + ',"schema_version":1}')
        assert json.loads(raw) == body
        _rewrite(repository, report_id, body=raw)
        for read in (lambda: repository.get(report_id), lambda: repository.list(),
                     lambda: repository.put(report_id, body)):
            with pytest.raises(_reports().StatisticalReportCorrupt):
                read()

    @pytest.mark.parametrize("corruption", ["nonfinite", "digest", "shape"])
    def test_invalid_submission_leaves_no_row(self, repository, corruption):
        body = _body()
        if corruption == "nonfinite":
            body["result"]["bad"] = float("nan")
            report_id = "stat-report-bad"
        else:
            if corruption == "digest":
                body["result"]["input_digest"] = "sha256:bad"
            else:
                del body["result"]["inputs"]
            report_id = statistical_report_id(body)
        with pytest.raises(ValueError):
            repository.put(report_id, body)
        assert repository.list() == []

    def test_canonical_float_identity_and_round_trip(self, repository):
        publications = []
        for value in (-0.0, 0.0, 1.0, 1):
            body = _body()
            body["result"]["diagnostic"] = value
            publication = repository.put(statistical_report_id(body), body)
            assert canonical_json(repository.get(publication["report_id"])["body"]) == canonical_json(body)
            publications.append(publication)
        assert len({item["report_id"] for item in publications}) == 4
        assert len(repository.list()) == 4

    def test_parallel_publication_converges(self, repository):
        barrier = Barrier(8)
        body = _body()
        report_id = statistical_report_id(body)

        def publish(_):
            if isinstance(repository, _reports().MemoryStatisticalReports):
                independent = repository
            elif isinstance(repository, _reports().SQLiteStatisticalReports):
                independent = _reports().SQLiteStatisticalReports(repository._path)
            else:
                independent = _reports().PgStatisticalReports(repository._dsn)
            barrier.wait(timeout=10)
            return independent.put(report_id, body)

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(publish, range(8)))
        assert all(item == results[0] for item in results)
        assert repository.list() == [results[0]]


class TestMemoryAndSQLiteReports(_RepositoryContract):
    @pytest.fixture(params=["memory", "sqlite"])
    def repository(self, request, tmp_path):
        store = (InMemoryRunStore() if request.param == "memory"
                 else SQLiteRunStore(tmp_path / "reports.db"))
        assert getattr(store, "statistical_reports", None) is not None
        return store.statistical_reports


def test_memory_repository_uses_the_shared_run_store_lock():
    store = InMemoryRunStore()
    assert store.statistical_reports._lock is store.runs._lock



@pytest.mark.parametrize("boundary", ["get", "list", "replay"])
def test_memory_corrupt_index_association_fails_closed_without_repair(boundary):
    repository = InMemoryRunStore().statistical_reports
    body_a, body_b = _body(), _body()
    body_b["result"]["diagnostic"] = "another valid publication"
    id_a, id_b = statistical_report_id(body_a), statistical_report_id(body_b)
    repository.put(id_a, body_a)
    expected_b = repository.put(id_b, body_b)
    repository._rows[id_a] = repository._rows[id_b]
    damaged = dict(repository._rows)
    with pytest.raises(_reports().StatisticalReportCorrupt):
        if boundary == "get":
            repository.get(id_a)
        elif boundary == "list":
            repository.list()
        else:
            repository.put(id_a, body_a)
    assert repository._rows == damaged
    assert repository.get(id_b) == expected_b


def test_sqlite_reopen_preserves_canonical_bytes_and_first_timestamp(tmp_path):
    path = str(tmp_path / "reports.db")
    body = _body()
    body["result"]["diagnostic"] = {"negative_zero": -0.0, "float": 1.0, "text": "保留"}
    first = SQLiteRunStore(path).statistical_reports.put(statistical_report_id(body), body)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT body FROM statistical_reports").fetchone()[0] == canonical_json(body)
    reopened = SQLiteRunStore(path).statistical_reports
    assert reopened.get(first["report_id"]) == reopened.put(first["report_id"], body) == first


def _publish_in_process(path, body, barrier, outcomes):
    barrier.wait(timeout=15)
    try:
        report = _reports().SQLiteStatisticalReports(path).put(statistical_report_id(body), body)
        outcomes.put(("ok", report))
    except BaseException as error:
        outcomes.put(("error", repr(error)))
        raise


def test_sqlite_parallel_processes_converge(tmp_path):
    path = str(tmp_path / "reports.db")
    repository = SQLiteRunStore(path).statistical_reports
    context = mp.get_context("spawn")
    barrier, outcomes = context.Barrier(4), context.Queue()
    processes = [context.Process(target=_publish_in_process, args=(path, _body(), barrier, outcomes))
                 for _ in range(4)]
    for process in processes:
        process.start()
    try:
        results = [outcomes.get(timeout=25) for _ in processes]
        for process in processes:
            process.join(timeout=10)
        assert all(process.exitcode == 0 for process in processes)
        assert all(item == results[0] and item[0] == "ok" for item in results)
        assert repository.list() == [results[0][1]]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        outcomes.close()


def _migration():
    path = MIGRATIONS_DIR / "versions" / "0016_statistical_reports.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_empty_roundtrip_and_fixture_cleanup():
    module = _migration()
    assert module.revision == "0016_statistical_reports"
    assert module.down_revision == "0015_m7_platform_tables"
    assert isinstance(module.DOWN_STATEMENTS, tuple) and module.DOWN_STATEMENTS
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
                columns = inspect(connection).get_columns("statistical_reports")
                assert [column["name"] for column in columns] == ["report_id", "body", "published_at"]
                assert all(str(column["type"]) == "TEXT" for column in columns)
                module.downgrade()
                assert not inspect(connection).has_table("statistical_reports")
                module.upgrade()
                for _ in range(2):
                    for statement in module.DOWN_STATEMENTS:
                        connection.execute(text(statement))
    finally:
        engine.dispose()


def test_migration_populated_downgrade_preserves_exact_bytes():
    module = _migration()
    engine = create_engine("sqlite://")
    row = ("stat-report-corrupt", "{raw damaged evidence", "original timestamp")
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
                connection.execute(text("INSERT INTO statistical_reports VALUES (:id, :body, :time)"),
                                   dict(zip(("id", "body", "time"), row)))
                with pytest.raises(RuntimeError, match="statistical_reports"):
                    module.downgrade()
                assert tuple(connection.execute(text("SELECT * FROM statistical_reports")).one()) == row
    finally:
        engine.dispose()


def test_pg_downgrade_emits_lock_before_counts_and_drop(monkeypatch):
    """Offline SQL-boundary evidence only; live races live in the PG test file."""
    module = _migration()
    statements = []
    bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"),
                           get_isolation_level=lambda: "READ COMMITTED",
                           execute=lambda sql: statements.append(str(sql)))
    monkeypatch.setattr(module, "op", SimpleNamespace(get_bind=lambda: bind,
                                                     execute=lambda sql: statements.append(str(sql))))
    monkeypatch.setattr(module, "downgrade_blockers", lambda bind: statements.append("COUNT") or [])
    monkeypatch.setattr(module, "_maintenance_active", lambda bind:
                        statements.append("CHECK MAINTENANCE") or False, raising=False)
    module.downgrade()
    assert statements == [
        "SELECT pg_advisory_xact_lock(hashtext('motteavl:maintenance-meta'))",
        "CHECK MAINTENANCE", "LOCK TABLE statistical_reports IN ACCESS EXCLUSIVE MODE", "COUNT",
        "DROP TABLE IF EXISTS statistical_reports",
    ]


def test_pg_active_maintenance_refuses_before_table_lock_and_counts(monkeypatch):
    module = _migration()
    statements = []
    bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"),
                           get_isolation_level=lambda: "READ COMMITTED",
                           execute=lambda sql: statements.append(str(sql)))
    monkeypatch.setattr(module, "op", SimpleNamespace(get_bind=lambda: bind,
                                                     execute=lambda sql: statements.append(str(sql))))
    monkeypatch.setattr(module, "downgrade_blockers", lambda bind: statements.append("COUNT") or [])
    monkeypatch.setattr(module, "_maintenance_active", lambda bind:
                        statements.append("CHECK MAINTENANCE") or True, raising=False)
    with pytest.raises(RuntimeError, match="maintenance.*active"):
        module.downgrade()
    assert statements == [
        "SELECT pg_advisory_xact_lock(hashtext('motteavl:maintenance-meta'))", "CHECK MAINTENANCE",
    ]


def test_migration_active_flag_refuses_without_changing_table_or_metadata():
    module = _migration()
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE motte_meta (meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL)"
            ))
            connection.execute(text("INSERT INTO motte_meta VALUES ('maintenance', 'active')"))
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
                with pytest.raises(RuntimeError, match="maintenance.*active"):
                    module.downgrade()
            assert inspect(connection).has_table("statistical_reports")
            assert connection.execute(text("SELECT * FROM statistical_reports")).fetchall() == []
            assert tuple(connection.execute(text("SELECT * FROM motte_meta")).one()) == (
                "maintenance", "active",
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_pg_downgrade_rejects_stale_snapshot_isolation(isolation, monkeypatch):
    module = _migration()
    statements = []
    bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"),
                           get_isolation_level=lambda: isolation,
                           execute=lambda sql: statements.append(str(sql)))
    monkeypatch.setattr(module, "op", SimpleNamespace(get_bind=lambda: bind,
                                                     execute=lambda sql: statements.append(str(sql))))
    with pytest.raises(RuntimeError, match="READ COMMITTED"):
        module.downgrade()
    assert statements == []


def test_repository_module_import_keeps_pg_driver_optional():
    module_path = Path(__file__).parents[2] / "packages/storage/motte_storage/statistical_reports.py"
    result = subprocess.run([sys.executable, "-c", """
import importlib.abc, runpy, sys
class BlockPG(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'psycopg', 'sqlalchemy', 'alembic'}:
            raise AssertionError('new repository eagerly imported optional dependency: ' + fullname)
sys.meta_path.insert(0, BlockPG())
module = runpy.run_path(sys.argv[1])
assert module['MemoryStatisticalReports']
assert module['SQLiteStatisticalReports']
assert module['PgStatisticalReports']
""", str(module_path)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_sqlite_invalid_insert_rolls_back_the_whole_transaction(tmp_path):
    path = str(tmp_path / "reports.db")
    repository = SQLiteRunStore(path).statistical_reports
    with sqlite3.connect(path) as connection:
        connection.execute("""
            CREATE TRIGGER corrupt_insert AFTER INSERT ON statistical_reports
            BEGIN
              UPDATE statistical_reports SET published_at = 'damaged'
              WHERE report_id = NEW.report_id;
            END
        """)
    body = _body()
    with pytest.raises(_reports().StatisticalReportCorrupt):
        repository.put(statistical_report_id(body), body)
    assert repository.list() == []


@pytest.mark.parametrize('transaction', ['implicit', 'deferred', 'write', 'savepoint'])
def test_sqlite_downgrade_excludes_writers_after_counts(tmp_path, monkeypatch, transaction):
    path = tmp_path / 'downgrade-race.db'
    engine = create_engine('sqlite:///' + str(path))
    module = _migration()
    try:
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            connection.exec_driver_sql('CREATE TABLE prior_evidence (id TEXT PRIMARY KEY)')
        observed = []
        original = module.downgrade_blockers
        def insert_after_counts(bind):
            blockers = original(bind)
            assert blockers == []
            with sqlite3.connect(path, timeout=0.02) as writer:
                try:
                    writer.execute("INSERT INTO statistical_reports VALUES ('id', 'evidence', 'time')")
                    writer.commit()
                except sqlite3.OperationalError as error:
                    observed.append(str(error))
                else:
                    observed.append('committed provenance after empty count')
            return blockers
        monkeypatch.setattr(module, 'downgrade_blockers', insert_after_counts)
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            if transaction == 'deferred':
                connection.exec_driver_sql('BEGIN DEFERRED')
            elif transaction == 'write':
                connection.exec_driver_sql("INSERT INTO prior_evidence VALUES ('uncommitted caller write')")
            elif transaction == 'savepoint':
                connection.begin_nested()
            module.downgrade()
            assert observed == ['database is locked']
            assert connection.connection.driver_connection.in_transaction
        assert not inspect(engine).has_table('statistical_reports')
    finally:
        engine.dispose()


@pytest.mark.parametrize('transaction', ['implicit', 'deferred', 'write', 'savepoint'])
def test_sqlite_downgrade_preserves_caller_rollback(tmp_path, transaction):
    engine = create_engine('sqlite:///' + str(tmp_path / 'rollback.db'))
    module = _migration()
    try:
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            connection.exec_driver_sql('CREATE TABLE prior_evidence (id TEXT PRIMARY KEY)')
        with engine.connect() as connection, Operations.context(MigrationContext.configure(connection)):
            if transaction == 'deferred':
                connection.exec_driver_sql('BEGIN DEFERRED')
            elif transaction == 'write':
                connection.exec_driver_sql("INSERT INTO prior_evidence VALUES ('uncommitted caller write')")
            elif transaction == 'savepoint':
                connection.begin_nested()
            module.downgrade()
            connection.rollback()
        with engine.connect() as connection:
            assert inspect(connection).has_table('statistical_reports')
            assert connection.exec_driver_sql('SELECT * FROM prior_evidence').fetchall() == []
    finally:
        engine.dispose()


def test_sqlite_downgrade_retains_the_callers_savepoint(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'savepoint.db'))
    module = _migration()
    try:
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            connection.exec_driver_sql('CREATE TABLE prior_evidence (id TEXT PRIMARY KEY)')
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            connection.exec_driver_sql("INSERT INTO prior_evidence VALUES ('outer transaction')")
            savepoint = connection.begin_nested()
            module.downgrade()
            savepoint.rollback()
            assert inspect(connection).has_table('statistical_reports')
        with engine.connect() as connection:
            assert connection.exec_driver_sql('SELECT * FROM prior_evidence').fetchall() == [('outer transaction',)]
    finally:
        engine.dispose()
