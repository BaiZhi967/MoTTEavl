"""Actual isolated PostgreSQL maintenance connection identity proof."""
import pytest

from motte_storage import maintenance
from tests.storage import test_trace_retention_transactions as common

apply = common.apply
observe_connections = common.observe_connections
durable_store = common.durable_store
retention_seed = common.seeded


def test_pg_uses_lock_owning_connection(retention_seed, monkeypatch):
    store, root, plan = retention_seed
    observed, writes = [], []
    original = maintenance._hold_postgres_writes
    def hold(connection, **kwargs):
        original(connection, **kwargs)
        if connection is not None:
            observed.append(connection.info.backend_pid)
    monkeypatch.setattr(maintenance, '_hold_postgres_writes', hold)
    def observe(when, statement, connection):
        if when == 'before' and (statement.startswith('INSERT INTO trace_archive_receipts(')
                                 or statement.startswith('DELETE FROM trace_events WHERE')):
            writes.append(connection.info.backend_pid if hasattr(store, 'dsn') else id(connection))
    observe_connections(monkeypatch, store, observe)
    result = apply(store, root, plan)
    assert result.trimmed_events == 6 and len(writes) == 4
    if hasattr(store, 'dsn'):
        assert len(observed) == 1 and set(writes) == set(observed)
    else:
        assert len(set(writes)) == 1


def test_pg_refuses_a_released_lock_transaction(isolated_pg_database, tmp_path, monkeypatch):
    import pytest
    from motte_storage.postgres import create_postgres_run_store
    from motte_storage.operation_locks import MaintenanceConflict
    from motte_storage import trace_retention as retention
    from tests.storage.test_trace_retention_transactions import CONFIG
    store = create_postgres_run_store(isolated_pg_database, migrate=True)
    root = tmp_path / 'artifacts'
    root.mkdir()
    plan = retention.plan_trace_retention(store, config=CONFIG)
    original = maintenance._hold_postgres_writes
    def released(connection, **kwargs):
        original(connection, **kwargs)
        connection.commit()  # Synthetic corrupted internal lease; no live SHARE locks.
    monkeypatch.setattr(maintenance, '_hold_postgres_writes', released)
    with pytest.raises(MaintenanceConflict, match='transaction|lock'):
        apply(store, root, plan)
    assert not maintenance.maintenance_status(store)['active']


@pytest.mark.parametrize('durable_store', ['postgres'], indirect=True)
def test_pg_owner_metadata_cannot_change_between_validation_and_clear(retention_seed, monkeypatch):
    from psycopg import connect, errors
    store, root, plan = retention_seed
    observed = False
    def observe(when, statement, connection):
        nonlocal observed
        if not observed and when == 'after' and statement.startswith('DELETE FROM trace_events WHERE'):
            observed = True
            with connect(store.dsn, autocommit=True) as outsider:
                outsider.execute("SET lock_timeout='200ms'")
                with pytest.raises(errors.LockNotAvailable):
                    outsider.execute("UPDATE motte_meta SET meta_value='foreign-owner' WHERE meta_key='maintenance_owner'")
    observe_connections(monkeypatch, store, observe)
    assert apply(store, root, plan).trimmed_events == 6
    assert observed


@pytest.mark.parametrize('durable_store', ['postgres'], indirect=True)
def test_pg_existing_metadata_writer_finishes_without_lock_inversion(retention_seed, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Timer
    from time import monotonic
    from motte_storage.artifacts import ArtifactStore
    store, root, plan = retention_seed
    ready, release = Event(), Event()
    started = False
    waited = []
    worker = None
    original = ArtifactStore.put_trace_archive
    def metadata_writer():
        with maintenance._metadata_transaction(store) as (connection, placeholder):
            # Existing supported metadata ordering: advisory -> row write, and
            # ordinary read of SHARE-locked business data is still permitted.
            connection.execute("UPDATE motte_meta SET meta_value=meta_value WHERE meta_key='maintenance_reason'")
            assert connection.execute('SELECT count(*) FROM runs').fetchone()[0] == 2
            ready.set()
            assert release.wait(4), 'metadata writer did not receive bounded release'
    with ThreadPoolExecutor(max_workers=1) as pool:
        def publication(artifacts, data, **kwargs):
            nonlocal started, worker
            result = original(artifacts, data, **kwargs)
            if not started:
                started = True
                worker = pool.submit(metadata_writer)
                assert ready.wait(3)
            return result
        timer = None
        before = None
        def observe(when, statement, connection):
            nonlocal timer, before
            if statement == "SELECT pg_advisory_xact_lock(hashtext('motteavl:maintenance-meta'))":
                if when == 'before':
                    before = monotonic()
                    timer = Timer(0.2, release.set)
                    timer.start()
                else:
                    waited.append(monotonic() - before)
        monkeypatch.setattr(ArtifactStore, 'put_trace_archive', publication)
        observe_connections(monkeypatch, store, observe)
        try:
            assert apply(store, root, plan).trimmed_events == 6
            worker.result(timeout=3)
            assert len(waited) == 1 and 0.1 <= waited[0] < 3
        finally:
            release.set()
            if timer:
                timer.join(3)
    assert not maintenance.maintenance_status(store)['active']
