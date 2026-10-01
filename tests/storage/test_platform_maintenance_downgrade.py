"""The platform migration owns the runtime-installed maintenance SQL guards.

SQLite exercises the downgrade checks without a PostgreSQL service. Disposable
PostgreSQL tests prove trigger dependencies, transactional rollback, and the
Alembic version update after the metadata table disappears.
"""

from __future__ import annotations

import importlib.util
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from queue import Queue
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from motte_storage.migrations import MIGRATIONS_DIR


PLATFORM_REVISION = "0015_m7_platform_tables"


def _upgrade_platform(dsn):
    """This suite exercises 0015 itself even after later revisions exist."""
    from alembic import command
    from motte_storage.migrations import alembic_config, current

    command.upgrade(alembic_config(dsn), PLATFORM_REVISION)
    return current(dsn)


def _migration():
    path = MIGRATIONS_DIR / "versions" / "0015_m7_platform_tables.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def _platform_bind(*, active=False, evidence_table=None):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE motte_meta (meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL)"
            ))
            for table in ("motte_imports", "motte_import_mappings", "motte_gc_tombstones"):
                connection.execute(text(f"CREATE TABLE {table} (evidence TEXT)"))
            if active:
                connection.execute(text("INSERT INTO motte_meta VALUES ('maintenance', 'active')"))
            if evidence_table:
                connection.execute(text(f"INSERT INTO {evidence_table} VALUES ('keep')"))
            yield connection
    finally:
        engine.dispose()


def test_downgrade_refuses_active_or_abandoned_maintenance_without_removing_metadata():
    module = _migration()
    with _platform_bind(active=True) as connection:
        with Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError, match="maintenance.*active"):
                module.downgrade()
        assert connection.execute(text(
            "SELECT meta_value FROM motte_meta WHERE meta_key = 'maintenance'"
        )).scalar_one() == "active"
        assert inspect(connection).has_table("motte_gc_tombstones")


@pytest.mark.parametrize("table", [
    "motte_imports", "motte_import_mappings", "motte_gc_tombstones",
])
def test_downgrade_preserves_each_audit_table_before_guard_teardown(table):
    module = _migration()
    with _platform_bind(evidence_table=table) as connection:
        with Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError, match=table):
                module.downgrade()
        assert connection.execute(text(f"SELECT evidence FROM {table}")).scalar_one() == "keep"
        assert inspect(connection).has_table("motte_meta")


def test_downgrade_emits_guard_teardown_before_removing_its_metadata(monkeypatch):
    """An offline SQL-boundary check; actual PG execution is covered below."""
    module = _migration()
    statements = []
    with _platform_bind() as connection:
        monkeypatch.setattr(module, "op", SimpleNamespace(
            get_bind=lambda: connection, execute=lambda sql: statements.append(str(sql)),
        ))
        module.downgrade()
    function_drop = "DROP FUNCTION IF EXISTS public.motte_maintenance_guard() RESTRICT"
    assert function_drop in statements
    assert statements.index(function_drop) < statements.index("DROP TABLE IF EXISTS motte_meta")
    assert "DROP TRIGGER" in statements[0]
    assert "tgname = 'motte_maintenance_write'" in statements[0]
    assert "to_regprocedure('public.motte_maintenance_guard()')" in statements[0]


def test_postgres_downgrade_locks_audits_before_counts_and_guard_teardown(monkeypatch):
    """Check emitted SQL at the unavailable PostgreSQL connection boundary."""
    module = _migration()
    statements = []
    bind = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"),
        get_isolation_level=lambda: "READ COMMITTED",
        execute=lambda sql: statements.append(str(sql)),
    )
    monkeypatch.setattr(module, "op", SimpleNamespace(
        get_bind=lambda: bind, execute=lambda sql: statements.append(str(sql)),
    ))

    def check_blockers(_bind):
        statements.append("CHECK AUDIT COUNTS")
        return []

    monkeypatch.setattr(module, "downgrade_blockers", check_blockers)
    monkeypatch.setattr(module, "_maintenance_active", lambda _bind: (
        statements.append("CHECK MAINTENANCE") or False
    ), raising=False)
    module.downgrade()
    assert statements[:4] == [
        "SELECT pg_advisory_xact_lock(hashtext('motteavl:maintenance-meta'))",
        "CHECK MAINTENANCE",
        "LOCK TABLE motte_imports, motte_import_mappings, motte_gc_tombstones IN SHARE MODE",
        "CHECK AUDIT COUNTS",
    ]
    assert "DROP TRIGGER" in statements[4]


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_postgres_downgrade_rejects_pre_lock_snapshots(isolation, monkeypatch):
    module = _migration()
    statements = []
    bind = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"),
        get_isolation_level=lambda: isolation,
        execute=lambda sql: statements.append(str(sql)),
    )
    monkeypatch.setattr(module, "op", SimpleNamespace(
        get_bind=lambda: bind, execute=lambda sql: statements.append(str(sql)),
    ))
    monkeypatch.setattr(module, "downgrade_blockers", lambda _bind: [])
    with pytest.raises(RuntimeError, match="READ COMMITTED"):
        module.downgrade()
    assert statements == [], "unsupported isolation must fail before teardown or lock acquisition"


def _install_then_release(dsn):
    from motte_storage.maintenance import begin_maintenance, end_maintenance
    from motte_storage.postgres import create_postgres_run_store

    store = create_postgres_run_store(dsn)
    lease = begin_maintenance(store)
    end_maintenance(store, owner=lease["owner"])
    return store


def test_postgres_maintenance_downgrade_cleans_owned_guards_and_reinstalls(isolated_pg_database):
    import psycopg

    from motte_storage.migrations import current, downgrade, revision_ids, upgrade

    dsn = isolated_pg_database
    head = PLATFORM_REVISION
    _upgrade_platform(dsn)
    _install_then_release(dsn)
    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'alembic_version'::regclass "
            "AND tgname = 'motte_maintenance_write'"
        ).fetchone()[0] == 1
        connection.execute("""
            CREATE FUNCTION public.keep_unrelated_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$
        """)
        connection.execute(
            "CREATE TRIGGER keep_unrelated_guard BEFORE UPDATE ON runs "
            "FOR EACH STATEMENT EXECUTE FUNCTION public.keep_unrelated_guard()"
        )
    assert downgrade(dsn) == "0014_experiments_and_gates"
    assert current(dsn) == "0014_experiments_and_gates"
    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT to_regprocedure('public.motte_maintenance_guard()')"
        ).fetchone()[0] is None
        assert connection.execute("SELECT to_regclass('public.motte_meta')").fetchone()[0] is None
        assert connection.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'motte_maintenance_write'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'keep_unrelated_guard'"
        ).fetchone()[0] == 1
        connection.execute("UPDATE runs SET revision = revision WHERE false")
    assert _upgrade_platform(dsn) == head
    _install_then_release(dsn)
    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'alembic_version'::regclass "
            "AND tgname = 'motte_maintenance_write'"
        ).fetchone()[0] == 1
    # Exercise every remaining Alembic version mutation with no dangling guard.
    assert downgrade(dsn, steps=revision_ids().index(PLATFORM_REVISION) + 1) is None
    assert _upgrade_platform(dsn) == head


def test_postgres_abandoned_maintenance_blocks_downgrade_and_writes(isolated_pg_database):
    import psycopg

    from motte_storage.migrations import current, downgrade, revision_ids, upgrade

    dsn = isolated_pg_database
    head = PLATFORM_REVISION
    _upgrade_platform(dsn)
    _install_then_release(dsn)
    # Match crash recovery state: persisted active flag, no live process locks.
    with psycopg.connect(dsn) as connection:
        connection.execute("INSERT INTO motte_meta VALUES ('maintenance', 'active')")
    with pytest.raises(RuntimeError, match="maintenance.*active"):
        downgrade(dsn)
    assert current(dsn) == head
    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT meta_value FROM motte_meta WHERE meta_key = 'maintenance'"
        ).fetchone()[0] == "active"
        with pytest.raises(psycopg.errors.RaiseException, match="maintenance mode active"):
            connection.execute("UPDATE runs SET revision = revision WHERE false")
        connection.rollback()


def test_postgres_downgrade_preserves_unexpected_guard_dependency(isolated_pg_database):
    import psycopg
    from sqlalchemy.exc import InternalError

    from motte_storage.migrations import current, downgrade, revision_ids, upgrade

    dsn = isolated_pg_database
    _upgrade_platform(dsn)
    _install_then_release(dsn)
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "CREATE TRIGGER foreign_guard BEFORE UPDATE ON runs "
            "FOR EACH STATEMENT EXECUTE FUNCTION public.motte_maintenance_guard()"
        )
    with pytest.raises(InternalError) as failure:
        downgrade(dsn)
    assert isinstance(failure.value.orig, psycopg.errors.DependentObjectsStillExist)
    assert current(dsn) == PLATFORM_REVISION
    with psycopg.connect(dsn) as connection:
        assert connection.execute("SELECT to_regclass('public.motte_meta')").fetchone()[0]
        assert connection.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'runs'::regclass "
            "AND tgname IN ('foreign_guard', 'motte_maintenance_write')"
        ).fetchone()[0] == 2


_AUDIT_INSERTS = [
    ("motte_imports", "INSERT INTO motte_imports(import_id, manifest_sha256, status, payload) "
     "VALUES ('audit-race', 'sha256:fixed', 'completed', '{}'::jsonb)"),
    ("motte_import_mappings", "INSERT INTO motte_import_mappings(mapping_key, import_id, status, payload) "
     "VALUES ('audit-race', 'audit-race', 'completed', '{}'::jsonb)"),
    ("motte_gc_tombstones", "INSERT INTO motte_gc_tombstones(gc_run_id, artifact_id, payload) "
     "VALUES ('audit-race', 'fixed-evidence', '{}'::jsonb)"),
]


def _wait_for_blocked_peer(execute, future):
    """Observe the lock wait, rather than guessing from a fixed sleep."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        execute("SELECT pg_stat_clear_snapshot()")
        waiting = execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "AND pg_backend_pid() = ANY(pg_blocking_pids(pid))"
        ).fetchone()[0]
        if waiting:
            return
        assert not future.done(), "competing operation completed without the required audit lock"
        time.sleep(0.01)
    raise AssertionError("competing operation never waited on the audit table lock")


@pytest.mark.parametrize("table, insert", _AUDIT_INSERTS)
def test_postgres_downgrade_observes_inflight_audit_commit(isolated_pg_database, table, insert):
    """A writer committing while downgrade waits must stop that downgrade."""
    import psycopg

    from motte_storage.migrations import current, downgrade, revision_ids, upgrade

    dsn = isolated_pg_database
    _upgrade_platform(dsn)
    _install_then_release(dsn)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with psycopg.connect(dsn) as writer:
            writer.execute(insert)
            future = executor.submit(downgrade, dsn)
            try:
                _wait_for_blocked_peer(writer.execute, future)
                writer.commit()
                with pytest.raises(RuntimeError, match=table):
                    future.result(timeout=10)
            finally:
                writer.rollback()
    assert current(dsn) == PLATFORM_REVISION
    with psycopg.connect(dsn) as connection:
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1


def test_postgres_audit_insert_cannot_commit_after_downgrade_counts(isolated_pg_database, monkeypatch):
    """An ordinary writer started after zero-count checks waits through commit."""
    import psycopg

    from motte_storage.migrations import _psycopg_url, upgrade

    dsn = isolated_pg_database
    _upgrade_platform(dsn)
    _install_then_release(dsn)
    module = _migration()
    original_blockers = module.downgrade_blockers
    engine = create_engine(_psycopg_url(dsn))
    writer_started = []

    def insert_audit():
        with psycopg.connect(dsn) as connection:
            connection.execute(_AUDIT_INSERTS[2][1])

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            def write_after_counts(bind):
                blockers = original_blockers(bind)
                assert blockers == []
                future = executor.submit(insert_audit)
                writer_started.append(future)
                _wait_for_blocked_peer(lambda sql: bind.execute(text(sql)), future)
                return blockers

            monkeypatch.setattr(module, "downgrade_blockers", write_after_counts)
            with engine.begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    module.downgrade()
            with pytest.raises(psycopg.errors.UndefinedTable):
                writer_started[0].result(timeout=10)
    finally:
        engine.dispose()


def test_postgres_active_downgrade_refuses_before_queued_audit_writer(isolated_pg_database):
    """A queued writer must not make downgrade hold the owner-release lock."""
    import psycopg

    from motte_storage.maintenance import begin_maintenance, end_maintenance
    from motte_storage.migrations import _psycopg_url, upgrade

    dsn = isolated_pg_database
    _upgrade_platform(dsn)
    store = _install_then_release(dsn)
    engine = create_engine(_psycopg_url(dsn), connect_args={"options": "-c lock_timeout=1000"})
    pids = Queue()

    def insert_audit():
        with psycopg.connect(dsn) as connection:
            pids.put(connection.info.backend_pid)
            connection.execute(_AUDIT_INSERTS[2][1])

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            lease = begin_maintenance(store)
            try:
                future = executor.submit(insert_audit)
                writer_pid = pids.get(timeout=10)
                with engine.begin() as connection:
                    deadline = time.monotonic() + 10
                    while True:
                        waiting = connection.execute(text(
                            "SELECT cardinality(pg_blocking_pids(:writer_pid))"
                        ), {"writer_pid": writer_pid}).scalar_one()
                        if waiting:
                            break
                        assert not future.done(), "audit writer was not blocked by live maintenance"
                        assert time.monotonic() < deadline, "audit writer never entered the lock queue"
                        time.sleep(0.01)
                    with Operations.context(MigrationContext.configure(connection)):
                        with pytest.raises(RuntimeError, match="maintenance.*active"):
                            _migration().downgrade()
            finally:
                end_maintenance(store, owner=lease["owner"])
            future.result(timeout=10)
    finally:
        engine.dispose()


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_postgres_nondefault_isolation_preserves_metadata(isolated_pg_database, isolation):
    from motte_storage.migrations import _psycopg_url, upgrade

    dsn = isolated_pg_database
    _upgrade_platform(dsn)
    engine = create_engine(_psycopg_url(dsn), isolation_level=isolation)
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match="READ COMMITTED"):
                    _migration().downgrade()
            assert connection.execute(text("SELECT to_regclass('public.motte_meta')")).scalar_one()
    finally:
        engine.dispose()
