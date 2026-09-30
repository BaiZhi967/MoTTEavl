"""Real PostgreSQL repository and migration tests, only in disposable databases."""
from concurrent.futures import ThreadPoolExecutor
import time

import pytest
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

from motte_contracts.hashing import canonical_json
from motte_contracts.statistical_reports import statistical_report_id
from motte_storage.migrations import _psycopg_url, alembic_config, current
from motte_storage.postgres import create_postgres_run_store
from tests.storage.test_statistical_reports import _RepositoryContract, _body, _migration


PARENT = "0015_m7_platform_tables"
REVISION = "0016_statistical_reports"


def _upgrade(dsn):
    command.upgrade(alembic_config(dsn), REVISION)


class TestPostgresReports(_RepositoryContract):
    @pytest.fixture
    def repository(self, isolated_pg_database):
        dsn = isolated_pg_database
        _upgrade(dsn)
        store = create_postgres_run_store(dsn)
        assert getattr(store, "statistical_reports", None) is not None
        return store.statistical_reports


def test_pg_migration_empty_roundtrip_preserves_prior_rows(isolated_pg_database):
    import psycopg

    dsn = isolated_pg_database
    command.upgrade(alembic_config(dsn), PARENT)
    with psycopg.connect(dsn) as connection:
        connection.execute("INSERT INTO runs(id, payload, revision) VALUES ('prior-run', '{}'::jsonb, 1)")
    _upgrade(dsn)
    assert current(dsn) == REVISION
    with psycopg.connect(dsn) as connection:
        assert connection.execute("SELECT id, payload, revision FROM runs").fetchall() == [("prior-run", {}, 1)]
        assert connection.execute("SELECT * FROM statistical_reports").fetchall() == []
    command.downgrade(alembic_config(dsn), PARENT)
    assert current(dsn) == PARENT
    with psycopg.connect(dsn) as connection:
        assert connection.execute("SELECT to_regclass('public.statistical_reports')").fetchone()[0] is None
    _upgrade(dsn)
    assert current(dsn) == REVISION
    with psycopg.connect(dsn) as connection:
        assert connection.execute("SELECT id FROM runs").fetchall() == [("prior-run",)]


def test_pg_reopen_preserves_canonical_text_and_timestamp(isolated_pg_database):
    import psycopg

    dsn = isolated_pg_database
    _upgrade(dsn)
    body = _body()
    body["result"]["diagnostic"] = {"negative_zero": -0.0, "float": 1.0, "text": "保留"}
    first = create_postgres_run_store(dsn).statistical_reports.put(statistical_report_id(body), body)
    with psycopg.connect(dsn) as connection:
        assert connection.execute("SELECT body, pg_typeof(body)::text FROM statistical_reports").fetchone() == (canonical_json(body), "text")
    reopened = create_postgres_run_store(dsn).statistical_reports
    assert reopened.get(first["report_id"]) == reopened.put(first["report_id"], body) == first


def test_pg_populated_downgrade_refuses_and_preserves_bytes(isolated_pg_database):
    import psycopg

    dsn = isolated_pg_database
    _upgrade(dsn)
    row = ("stat-report-damaged", "{original damaged bytes -0.0 1.0", "original timestamp")
    with psycopg.connect(dsn) as connection:
        connection.execute("INSERT INTO statistical_reports VALUES (%s, %s, %s)", row)
    with pytest.raises(RuntimeError, match="statistical_reports"):
        command.downgrade(alembic_config(dsn), PARENT)
    assert current(dsn) == REVISION
    with psycopg.connect(dsn) as connection:
        assert connection.execute("SELECT * FROM statistical_reports").fetchall() == [row]


def _wait_for_peer_lock(execute, future):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        execute("SELECT pg_stat_clear_snapshot()")
        count = execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "AND pg_backend_pid() = ANY(pg_blocking_pids(pid))"
        ).fetchone()[0]
        if count:
            return
        assert not future.done(), "competitor finished without waiting for the table lock"
        time.sleep(0.01)
    raise AssertionError("competitor never waited for the table lock")


def test_pg_downgrade_observes_inflight_report_commit(isolated_pg_database):
    """A commit arriving while downgrade waits must be counted before DROP."""
    import psycopg

    dsn = isolated_pg_database
    _upgrade(dsn)
    row = ("committing-report", "keep these bytes", "original timestamp")
    with ThreadPoolExecutor(max_workers=1) as executor:
        with psycopg.connect(dsn) as writer:
            writer.execute("INSERT INTO statistical_reports VALUES (%s, %s, %s)", row)
            future = executor.submit(command.downgrade, alembic_config(dsn), PARENT)
            try:
                _wait_for_peer_lock(writer.execute, future)
                writer.commit()
                with pytest.raises(RuntimeError, match="statistical_reports"):
                    future.result(timeout=10)
            finally:
                writer.rollback()
    assert current(dsn) == REVISION
    with psycopg.connect(dsn) as connection:
        assert connection.execute("SELECT * FROM statistical_reports").fetchall() == [row]


def test_pg_publication_cannot_commit_between_empty_count_and_drop(isolated_pg_database, monkeypatch):
    """After the empty check, a publication waits until DROP commits and fails."""
    import psycopg

    dsn = isolated_pg_database
    _upgrade(dsn)
    module = _migration()
    original_blockers = module.downgrade_blockers
    repository = create_postgres_run_store(dsn).statistical_reports
    engine = create_engine(_psycopg_url(dsn))
    pending = []
    body = _body()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            def publish_after_counts(bind):
                blockers = original_blockers(bind)
                assert blockers == []
                future = executor.submit(repository.put, statistical_report_id(body), body)
                pending.append(future)
                _wait_for_peer_lock(lambda sql: bind.execute(text(sql)), future)
                return blockers

            monkeypatch.setattr(module, "downgrade_blockers", publish_after_counts)
            with engine.begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    module.downgrade()
            with pytest.raises(psycopg.errors.UndefinedTable):
                pending[0].result(timeout=10)
    finally:
        engine.dispose()


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_pg_downgrade_rejects_nondefault_isolation(isolated_pg_database, isolation):
    dsn = isolated_pg_database
    _upgrade(dsn)
    engine = create_engine(_psycopg_url(dsn), isolation_level=isolation)
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match="READ COMMITTED"):
                    _migration().downgrade()
            assert connection.execute(text("SELECT to_regclass('public.statistical_reports')")).scalar_one()
    finally:
        engine.dispose()


def test_pg_invalid_insert_rolls_back_the_whole_transaction(isolated_pg_database):
    import psycopg
    from motte_storage.statistical_reports import StatisticalReportCorrupt

    dsn = isolated_pg_database
    _upgrade(dsn)
    repository = create_postgres_run_store(dsn).statistical_reports
    with psycopg.connect(dsn) as connection:
        connection.execute("""
            CREATE FUNCTION corrupt_report_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN NEW.published_at := 'damaged'; RETURN NEW; END $$
        """)
        connection.execute("""
            CREATE TRIGGER corrupt_insert BEFORE INSERT ON statistical_reports
            FOR EACH ROW EXECUTE FUNCTION corrupt_report_insert()
        """)
    body = _body()
    with pytest.raises(StatisticalReportCorrupt):
        repository.put(statistical_report_id(body), body)
    assert repository.list() == []


def test_pg_live_maintenance_refuses_downgrade_before_table_lock(isolated_pg_database):
    from motte_storage.maintenance import begin_maintenance, end_maintenance

    dsn = isolated_pg_database
    _upgrade(dsn)
    store = create_postgres_run_store(dsn)
    engine = create_engine(_psycopg_url(dsn), connect_args={"options": "-c lock_timeout=1000"})
    lease = begin_maintenance(store)
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match="maintenance.*active"):
                    _migration().downgrade()
            assert connection.execute(text("SELECT to_regclass('public.statistical_reports')")).scalar_one()
            assert connection.execute(text(
                "SELECT meta_value FROM motte_meta WHERE meta_key = 'maintenance'"
            )).scalar_one() == "active"
        assert current(dsn) == REVISION
    finally:
        end_maintenance(store, owner=lease["owner"])
        engine.dispose()
    # Releasing the owner permits a real revision downgrade without bypasses.
    command.downgrade(alembic_config(dsn), PARENT)
    assert current(dsn) == PARENT


def test_pg_abandoned_maintenance_preserves_reports_table_and_guards(isolated_pg_database):
    import psycopg
    from motte_storage.maintenance import begin_maintenance, end_maintenance

    dsn = isolated_pg_database
    _upgrade(dsn)
    store = create_postgres_run_store(dsn)
    lease = begin_maintenance(store)
    end_maintenance(store, owner=lease["owner"])
    # Crash shape: persistent active flag and installed guards, no live locks.
    with psycopg.connect(dsn) as connection:
        connection.execute("INSERT INTO motte_meta VALUES ('maintenance', 'active')")
    with pytest.raises(RuntimeError, match="maintenance.*active"):
        command.downgrade(alembic_config(dsn), PARENT)
    assert current(dsn) == REVISION
    with psycopg.connect(dsn) as connection:
        assert connection.execute("SELECT * FROM statistical_reports").fetchall() == []
        assert connection.execute(
            "SELECT meta_value FROM motte_meta WHERE meta_key = 'maintenance'"
        ).fetchone()[0] == "active"
        assert connection.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'statistical_reports'::regclass "
            "AND tgname = 'motte_maintenance_write'"
        ).fetchone()[0] == 1
    body = _body()
    with pytest.raises(psycopg.errors.RaiseException, match="maintenance mode active"):
        store.statistical_reports.put(statistical_report_id(body), body)


def test_pg_active_downgrade_refuses_before_queued_report_writer(isolated_pg_database):
    import psycopg
    from queue import Queue
    from motte_storage.maintenance import begin_maintenance, end_maintenance

    dsn = isolated_pg_database
    _upgrade(dsn)
    store = create_postgres_run_store(dsn)
    engine = create_engine(_psycopg_url(dsn), connect_args={"options": "-c lock_timeout=1000"})
    pids = Queue()
    body = _body()
    report_id = statistical_report_id(body)

    def insert_report():
        with psycopg.connect(dsn) as connection:
            pids.put(connection.info.backend_pid)
            connection.execute("INSERT INTO statistical_reports VALUES (%s, %s, %s)",
                               (report_id, canonical_json(body), "2026-09-30T00:00:00Z"))

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            lease = begin_maintenance(store)
            try:
                future = executor.submit(insert_report)
                writer_pid = pids.get(timeout=10)
                with engine.begin() as connection:
                    deadline = time.monotonic() + 10
                    while True:
                        waiting = connection.execute(text(
                            "SELECT cardinality(pg_blocking_pids(:writer_pid))"
                        ), {"writer_pid": writer_pid}).scalar_one()
                        if waiting:
                            break
                        assert not future.done(), "report writer was not blocked by maintenance"
                        assert time.monotonic() < deadline, "writer never entered the lock queue"
                        time.sleep(0.01)
                    with Operations.context(MigrationContext.configure(connection)):
                        with pytest.raises(RuntimeError, match="maintenance.*active"):
                            _migration().downgrade()
            finally:
                end_maintenance(store, owner=lease["owner"])
            future.result(timeout=10)
        assert current(dsn) == REVISION
        assert store.statistical_reports.get(report_id)["body"] == body
    finally:
        engine.dispose()
