"""Owner-bound retention, using synthetic disposable stores only."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from motte_contracts.identity import canonical_sha256
from motte_storage import trace_retention as retention
from motte_storage import trace_retention_models as models
from motte_storage.artifacts import ArtifactStore
from motte_storage.maintenance import BackupUnsupported, maintenance_status
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore

NOW = datetime(2026, 9, 30, tzinfo=UTC)
CONFIG = models.TraceRetentionConfig(enabled=True, retention_days=7)


@pytest.fixture(params=['sqlite', 'postgres'])
def durable_store(request, tmp_path):
    if request.param == 'sqlite':
        return SQLiteRunStore(tmp_path / 'runs.db')
    from motte_storage.postgres import create_postgres_run_store
    return create_postgres_run_store(request.getfixturevalue('isolated_pg_database'), migrate=True)


@pytest.fixture
def seeded(durable_store, tmp_path, monkeypatch):
    root = tmp_path / 'artifacts'
    root.mkdir()
    # Provision the durable root and its parent before modeling archive creation.
    for path in (root, root.parent):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    monkeypatch.setattr(models, 'utc_now', lambda: NOW - timedelta(days=20))
    for run_id in ('a', 'b'):
        durable_store.runs.create({'id': run_id, 'status': 'completed'})
        for seq in range(4):
            durable_store.events.append({'run_id': run_id, 'type': 'evidence', 'value': seq})
    monkeypatch.setattr(models, 'utc_now', lambda: NOW)
    return durable_store, root, retention.plan_trace_retention(durable_store, config=CONFIG)


def apply(store, root, plan, **kwargs):
    return retention.apply_trace_retention(store, root, plan, config=kwargs.pop('config', CONFIG),
                                          confirm=kwargs.pop('confirm', True), **kwargs)


def seqs(store):
    return {run: [row['seq'] for row in store.events.list_for_run(run)] for run in ('a', 'b')}


def sql(store, statement, params=()):
    if hasattr(store, 'dsn'):
        from psycopg import connect
        with connect(store.dsn) as connection:
            connection.execute(statement.replace('?', '%s'), params)
    else:
        with sqlite3.connect(store.events._path) as connection:
            connection.execute(statement, params)


def test_readers_are_eager_read_only_and_memory_apply_unsupported(tmp_path):
    store = InMemoryRunStore()
    assert store.trace_archives.list() == []
    assert store.trace_archives.get('missing') is None
    assert store.trace_archives.list_for_run('missing') == []
    assert not any(hasattr(store.trace_archives, name) for name in ('put', 'update', 'delete'))
    plan = retention.plan_trace_retention(store, config=CONFIG)
    with pytest.raises(BackupUnsupported):
        apply(store, tmp_path, plan)


def test_receipt_and_trim_are_atomic_and_exact_reapply(seeded):
    store, root, plan = seeded
    result = apply(store, root, plan)
    assert result.trimmed_events == 6
    assert seqs(store) == {'a': [4], 'b': [4]}
    assert len(result.receipts) == 2
    assert store.trace_archives.list() == result.receipts
    for receipt in result.receipts:
        assert receipt.archive_id == 'trace-archive-' + receipt.sha256.removeprefix('sha256:')
        assert store.trace_archives.get(receipt.archive_id) == receipt
        assert store.trace_archives.list_for_run(receipt.prefix.run_id) == [receipt]
        assert receipt.prefix.event_count == 3
        from motte_storage.trace_archives import verify_trace_archive
        verify_trace_archive(ArtifactStore(root).read_bytes(receipt.artifact_id), receipt)
    replay = apply(store, root, plan)
    assert replay.receipts == result.receipts and replay.trimmed_events == 0
    assert not maintenance_status(store)['active']
    assert store.events.append({'run_id': 'a'})['seq'] == 5
    following = retention.plan_trace_retention(store, config=CONFIG)
    assert [(row.run_id, row.first_seq, row.last_seq, row.keep_seq) for row in following.prefixes] == [('a', 4, 4, 5)]


@pytest.mark.parametrize('change', ['confirm', 'disabled', 'config', 'hash', 'future',
    'revision', 'status', 'payload', 'time', 'max_seq', 'pin', 'reader', 'store'])
def test_apply_rechecks_every_plan_precondition(seeded, monkeypatch, tmp_path, change):
    store, root, plan = seeded
    kwargs = {}
    if change == 'confirm':
        kwargs['confirm'] = False
    elif change == 'disabled':
        kwargs['config'] = models.TraceRetentionConfig()
    elif change == 'config':
        kwargs['config'] = models.TraceRetentionConfig(enabled=True, retention_days=8)
    elif change == 'hash':
        plan = plan.model_copy(update={'plan_id': 'sha256:' + '0' * 64})
    elif change == 'future':
        body = plan.model_dump(mode='json', exclude={'plan_id'})
        body['cutoff'] = NOW.isoformat().replace('+00:00', 'Z')
        plan = models.TraceRetentionPlan(**body, plan_id=canonical_sha256(body))
    elif change in {'status', 'revision'}:
        run = store.runs.get('a')
        store.runs.update({**run, 'status': 'running' if change == 'status' else 'completed'},
                          expected_revision=run['revision'], expected_status=run['status'])
    elif change == 'payload':
        payload = json.dumps({'run_id': 'a', 'seq': 1, 'changed': True})
        sql(store, 'UPDATE trace_events SET payload=? WHERE run_id=? AND seq=1', (payload, 'a'))
    elif change == 'time':
        sql(store, 'UPDATE trace_events SET stored_at=? WHERE run_id=? AND seq=1',
            (NOW.isoformat(), 'a'))
    elif change == 'max_seq':
        store.events.append({'run_id': 'a'})
    elif change == 'pin':
        store.runs.create({'id': 'pin', 'status': 'completed', 'source_run_id': 'a'})
    elif change == 'reader':
        def unreadable(**kwargs):
            raise RuntimeError('unreadable reference')
        monkeypatch.setattr(store.statistical_reports, 'list', unreadable)
    else:
        store = SQLiteRunStore(tmp_path / 'different.db')
    before = seqs(store)
    with pytest.raises((ValueError, RuntimeError)):
        apply(store, root, plan, **kwargs)
    assert seqs(store) == before
    assert store.trace_archives.list() == []
    assert not list(root.rglob('*.json'))
    assert not maintenance_status(store)['active']


def test_elapsed_time_uses_saved_cutoff(seeded, monkeypatch):
    store, root, _ = seeded
    sql(store, 'UPDATE trace_events SET stored_at=? WHERE seq=2', ((NOW - timedelta(days=1)).isoformat(),))
    plan = retention.plan_trace_retention(store, config=CONFIG)
    assert [prefix.last_seq for prefix in plan.prefixes] == [1, 1]
    monkeypatch.setattr(models, 'utc_now', lambda: NOW + timedelta(days=40))
    assert apply(store, root, plan).trimmed_events == 2
    assert seqs(store) == {'a': [2, 3, 4], 'b': [2, 3, 4]}


@pytest.mark.parametrize('failure', ['mkdir_trace-archives', 'mkdir_trace-archives/sha256',
    'fsync_file', 'fsync_sha256_dir', 'fsync_trace_archives_dir', 'fsync_artifact_root', None])
def test_first_archive_power_loss_through_real_apply(seeded, monkeypatch, failure):
    from tests.storage.test_trace_archives import DurableNamespace
    store, root, plan = seeded
    namespace = DurableNamespace(root, monkeypatch, fail=failure)
    if failure:
        with pytest.raises(OSError, match='injected'):
            apply(store, root, plan)
        assert seqs(store) == {'a': [1, 2, 3, 4], 'b': [1, 2, 3, 4]}
        assert store.trace_archives.list() == []
    else:
        result = apply(store, root, plan)
        for receipt in result.receipts:
            assert namespace.after_power_loss(receipt.artifact_id) == ArtifactStore(root).read_bytes(receipt.artifact_id)
        assert seqs(store) == {'a': [4], 'b': [4]}


def test_replay_refuses_corrupt_archive_and_partial_receipts(seeded):
    store, root, plan = seeded
    result = apply(store, root, plan)
    receipt = result.receipts[0]
    path = root / receipt.artifact_id
    original = path.read_bytes()
    path.write_bytes(b'corrupted fixture')
    with pytest.raises(ValueError):
        apply(store, root, plan)
    path.write_bytes(original)
    sql(store, 'DELETE FROM trace_archive_receipts WHERE archive_id=?', (receipt.archive_id,))
    with pytest.raises(ValueError):
        apply(store, root, plan)
    assert seqs(store) == {'a': [4], 'b': [4]}


class ObservedConnection:
    """Test-only connection observer; no product callback or SQL escape hatch."""
    def __init__(self, connection, observe):
        object.__setattr__(self, 'connection', connection)
        object.__setattr__(self, 'observe', observe)

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def __setattr__(self, name, value):
        setattr(self.connection, name, value)

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)

    def execute(self, statement, *args, **kwargs):
        self.observe('before', str(statement), self.connection)
        result = self.connection.execute(statement, *args, **kwargs)
        self.observe('after', str(statement), self.connection)
        return result

    def commit(self):
        self.observe('before', 'COMMIT', self.connection)
        self.connection.commit()
        self.observe('after', 'COMMIT', self.connection)


def observe_connections(monkeypatch, store, observe):
    if hasattr(store, 'dsn'):
        from motte_storage import postgres
        original = postgres._connect
        monkeypatch.setattr(postgres, '_connect', lambda *a, **k: ObservedConnection(original(*a, **k), observe))
    else:
        original = sqlite3.connect
        monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: ObservedConnection(original(*a, **k), observe))


@pytest.mark.parametrize('boundary', ['before_receipt', 'after_receipt', 'after_delete', 'before_commit', 'after_commit'])
def test_receipt_and_trim_rollback_at_every_sql_boundary(seeded, monkeypatch, boundary):
    store, root, plan = seeded
    armed = False
    fired = False
    def observe(when, statement, connection):
        nonlocal armed, fired
        insert = statement.startswith('INSERT INTO trace_archive_receipts(')
        delete = statement.startswith('DELETE FROM trace_events WHERE')
        if insert:
            armed = True
        if not fired and ((boundary == 'before_receipt' and insert and when == 'before') or
                (boundary == 'after_receipt' and insert and when == 'after') or
                (boundary == 'after_delete' and delete and when == 'after') or
                (armed and statement == 'COMMIT' and boundary == when + '_commit')):
            fired = True
            raise RuntimeError('injected ' + boundary)
    with monkeypatch.context() as patch:
        observe_connections(patch, store, observe)
        with pytest.raises(RuntimeError, match='injected'):
            apply(store, root, plan)
    assert fired
    if boundary == 'after_commit':
        assert seqs(store) == {'a': [4], 'b': [4]}
        assert len(store.trace_archives.list()) == 2
        assert apply(store, root, plan).trimmed_events == 0
    else:
        assert seqs(store) == {'a': [1, 2, 3, 4], 'b': [1, 2, 3, 4]}
        assert store.trace_archives.list() == []
        assert apply(store, root, plan).trimmed_events == 6
    assert not maintenance_status(store)['active']


def test_receipt_reader_detects_index_payload_tampering_and_detaches(seeded):
    store, root, plan = seeded
    result = apply(store, root, plan)
    receipt = store.trace_archives.get(result.receipts[0].archive_id)
    receipt.prefix.model_dump()['event_count'] = 99
    dict(receipt)['artifact_refs']['fake'] = None
    assert store.trace_archives.get(receipt.archive_id) == result.receipts[0]
    sql(store, 'UPDATE trace_archive_receipts SET first_seq=2 WHERE archive_id=?', (receipt.archive_id,))
    with pytest.raises(ValueError, match='identity'):
        store.trace_archives.list()


def test_truncated_receipt_reader_cannot_hide_archive_history(seeded, monkeypatch):
    store, root, plan = seeded
    apply(store, root, plan)
    monkeypatch.setattr(store.trace_archives, 'list', lambda: [])
    with pytest.raises(ValueError, match='coverage|receipt'):
        retention.plan_trace_retention(store, config=CONFIG)


def test_commit_rejects_wrong_owner_and_incomplete_receipts(seeded):
    from motte_storage import maintenance
    from motte_storage.operation_locks import MaintenanceConflict
    store, root, plan = seeded
    with pytest.raises(MaintenanceConflict):
        maintenance.commit_trace_retention(store, owner='not-an-owner', plan=plan, receipts=[])
    lease = maintenance.begin_maintenance(store, reason='trace_retention', artifacts_root=root)
    try:
        with pytest.raises(ValueError, match='complete exact plan'):
            maintenance.commit_trace_retention(store, owner=lease['owner'], plan=plan, receipts=[])
    finally:
        maintenance.end_maintenance(store, owner=lease['owner'])
    assert seqs(store) == {'a': [1, 2, 3, 4], 'b': [1, 2, 3, 4]}


def child_store(address):
    if address.startswith('postgresql:'):
        from motte_storage.postgres import create_postgres_run_store
        return create_postgres_run_store(address)
    return SQLiteRunStore(address)


def child_apply(address, root, plan_json, pipe, mode):
    """Spawned process with no inherited live capabilities. Synthetic targets only."""
    import os
    from unittest.mock import patch
    store = child_store(address)
    plan = models.TraceRetentionPlan.model_validate_json(plan_json)
    pipe.send('ready')
    assert pipe.recv() == 'go'
    original = ArtifactStore.put_trace_archive
    once = False
    def publication(artifacts, data, **kwargs):
        nonlocal once
        result = original(artifacts, data, **kwargs)
        if not once and mode in {'pause', 'archive_crash'}:
            once = True
            pipe.send('archived')
            if mode == 'archive_crash':
                os._exit(71)
            assert pipe.recv() == 'resume'
        return result
    try:
        with patch.object(ArtifactStore, 'put_trace_archive', publication):
            result = apply(store, root, plan)
        pipe.send(('applied', result.trimmed_events))
    except Exception as error:
        pipe.send(('conflict', type(error).__name__))
    finally:
        pipe.close()


def address_of(store):
    return store.dsn if hasattr(store, 'dsn') else store.runs._path


def test_two_processes_cannot_trim_twice(seeded):
    import multiprocessing
    store, root, plan = seeded
    context = multiprocessing.get_context('spawn')
    children = []
    for mode in ('pause', 'normal'):
        parent, child = context.Pipe()
        process = context.Process(target=child_apply, args=(address_of(store), root, plan.model_dump_json(), child, mode))
        process.start()
        child.close()
        children.append((process, parent))
    try:
        for process, pipe in children:
            assert pipe.poll(20) and pipe.recv() == 'ready'
        children[0][1].send('go')
        assert children[0][1].poll(20) and children[0][1].recv() == 'archived'
        children[1][1].send('go')
        assert children[1][1].poll(20)
        second = children[1][1].recv()
        children[0][1].send('resume')
        assert children[0][1].poll(20)
        first = children[0][1].recv()
        assert sorted([first[0], second[0]]) == ['applied', 'conflict']
        assert first == ('applied', 6)
        assert seqs(store) == {'a': [4], 'b': [4]}
        assert len(store.trace_archives.list()) == 2
    finally:
        for process, pipe in children:
            process.join(20)
            if process.is_alive():
                process.kill()
                process.join()
            pipe.close()
        assert all(process.exitcode == 0 for process, _ in children)


def child_crash_sql(address, root, plan_json, boundary):
    import os
    from unittest.mock import patch
    store = child_store(address)
    plan = models.TraceRetentionPlan.model_validate_json(plan_json)
    armed = False
    def observe(when, statement, connection):
        nonlocal armed
        insert = statement.startswith('INSERT INTO trace_archive_receipts(')
        delete = statement.startswith('DELETE FROM trace_events WHERE')
        if insert:
            armed = True
        if ((boundary == 'before_receipt' and insert and when == 'before') or
                (boundary == 'after_receipt' and insert and when == 'after') or
                (boundary == 'after_delete' and delete and when == 'after') or
                (armed and statement == 'COMMIT' and boundary == when + '_commit')):
            os._exit(72)
    if hasattr(store, 'dsn'):
        from motte_storage import postgres
        original = postgres._connect
        target = patch.object(postgres, '_connect', lambda *a, **k: ObservedConnection(original(*a, **k), observe))
    else:
        original = sqlite3.connect
        target = patch.object(sqlite3, 'connect', lambda *a, **k: ObservedConnection(original(*a, **k), observe))
    with target:
        apply(store, root, plan)


@pytest.mark.parametrize('boundary', ['archive', 'before_receipt', 'after_receipt', 'after_delete', 'before_commit', 'after_commit'])
def test_crash_boundaries_preserve_evidence(seeded, boundary):
    import multiprocessing
    from motte_storage import maintenance
    from motte_storage.operation_locks import MaintenanceConflict
    store, root, plan = seeded
    context = multiprocessing.get_context('spawn')
    if boundary == 'archive':
        parent, child = context.Pipe()
        process = context.Process(target=child_apply, args=(address_of(store), root, plan.model_dump_json(), child, 'archive_crash'))
        process.start()
        child.close()
        assert parent.poll(20) and parent.recv() == 'ready'
        parent.send('go')
        assert parent.poll(20) and parent.recv() == 'archived'
        parent.close()
    else:
        process = context.Process(target=child_crash_sql, args=(address_of(store), root, plan.model_dump_json(), boundary))
        process.start()
    process.join(25)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail('crash child did not reach the declared boundary')
    assert process.exitcode == (71 if boundary == 'archive' else 72)
    assert seqs(store) == ({'a': [4], 'b': [4]} if boundary == 'after_commit' else
                           {'a': [1, 2, 3, 4], 'b': [1, 2, 3, 4]})
    receipts = store.trace_archives.list()
    assert len(receipts) == (2 if boundary == 'after_commit' else 0)
    status = maintenance_status(store)
    if boundary != 'after_commit':
        assert status['active']
        with pytest.raises(MaintenanceConflict):
            apply(store, root, plan)
        # Explicit recovery of the abandoned owner, never reason-only recovery.
        maintenance.end_maintenance(store, owner=maintenance.platform_for(store).meta.get(maintenance.MAINTENANCE_OWNER),
                                    reason='trace_retention')
    else:
        assert not status['active']
    assert apply(store, root, plan).trimmed_events == (0 if receipts else 6)


def child_writes(address, root, pipe, repeatable):
    """Attempt direct ordinary writes from another process, including old PG snapshots."""
    if address.startswith('postgresql:'):
        from psycopg import connect
        connection = connect(address, autocommit=True)
        connection.execute("SET lock_timeout = '250ms'")
        connection.autocommit = False
        if repeatable:
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            connection.execute('SELECT count(*) FROM runs').fetchone()
    else:
        connection = sqlite3.connect(address, timeout=0.25)
    pipe.send('ready')
    assert pipe.recv() == 'go'
    outcomes = []
    # These statement attempts exercise the raw DB barrier, even if a caller
    # bypasses repository-level state validation. No product rows are fabricated.
    tables = ['trace_events', 'runs', 'score_sets', 'baseline_snapshots', 'm6_baselines',
              'gate_results', 'statistical_reports', 'trace_archive_receipts']
    for table in tables:
        try:
            connection.execute('INSERT INTO ' + table + ' DEFAULT VALUES')
            connection.commit()
            outcomes.append((table, 'unexpected success'))
        except Exception as error:
            outcomes.append((table, str(error)))
            connection.rollback()
    try:
        ArtifactStore(root).put_bytes('ordinary.bin', b'ordinary concurrent write')
        outcomes.append(('artifact', 'unexpected success'))
    except Exception as error:
        outcomes.append(('artifact', str(error)))
    connection.close()
    pipe.send(outcomes)
    pipe.close()


@pytest.mark.parametrize('pause_at', ['archive', 'trim'])
def test_all_writers_remain_barred_through_archive_and_trim(seeded, monkeypatch, pause_at):
    import multiprocessing
    store, root, plan = seeded
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=child_writes, args=(address_of(store), root, child, True))
    process.start()
    child.close()
    assert parent.poll(20) and parent.recv() == 'ready'
    once = False
    def check():
        nonlocal once
        if once:
            return
        once = True
        parent.send('go')
        assert parent.poll(15)
        outcomes = parent.recv()
        assert len(outcomes) == 9
        for table, result in outcomes:
            assert any(marker in result for marker in ['maintenance mode active', 'lock timeout',
                                                       'database is locked', 'held by another operation']), (table, result)
    original = ArtifactStore.put_trace_archive
    def publication(artifacts, data, **kwargs):
        result = original(artifacts, data, **kwargs)
        if pause_at == 'archive':
            check()
        return result
    def observe(when, statement, connection):
        if pause_at == 'trim' and when == 'after' and statement.startswith('DELETE FROM trace_events WHERE'):
            check()
    try:
        monkeypatch.setattr(ArtifactStore, 'put_trace_archive', publication)
        observe_connections(monkeypatch, store, observe)
        assert apply(store, root, plan).trimmed_events == 6
        assert once and not (root / 'ordinary.bin').exists()
    finally:
        process.join(20)
        if process.is_alive():
            process.kill()
            process.join()
        parent.close()
        assert process.exitcode == 0


def test_all_archives_are_durable_and_verified_before_first_receipt_sql(seeded, monkeypatch):
    from tests.storage.test_trace_archives import DurableNamespace
    store, root, plan = seeded
    namespace = DurableNamespace(root, monkeypatch)
    verified = set()
    import motte_storage.trace_archives as archives
    real_verify = archives.verify_trace_archive
    def verify(data, receipt):
        real_verify(data, receipt)
        assert namespace.after_power_loss(receipt.artifact_id) == data
        verified.add(receipt.prefix.run_id)
    monkeypatch.setattr(archives, 'verify_trace_archive', verify)
    def observe(when, statement, connection):
        if when == 'before' and statement.startswith('INSERT INTO trace_archive_receipts('):
            assert verified == {'a', 'b'}
            namespace.operations.append('db_receipt')
        if when == 'before' and statement.startswith('DELETE FROM trace_events WHERE'):
            namespace.operations.append('db_trim')
    observe_connections(monkeypatch, store, observe)
    assert apply(store, root, plan).trimmed_events == 6
    assert namespace.operations.index('fsync_artifact_root') < namespace.operations.index('db_receipt')
    assert namespace.operations.index('db_receipt') < namespace.operations.index('db_trim')


def test_later_archive_failure_commits_no_earlier_candidate(seeded, monkeypatch):
    store, root, plan = seeded
    original = ArtifactStore.put_trace_archive
    seen = set()
    def fail_second(artifacts, data, **kwargs):
        run_id = json.loads(data)['run_id']
        seen.add(run_id)
        if run_id == 'b':
            raise OSError('second archive unavailable')
        return original(artifacts, data, **kwargs)
    monkeypatch.setattr(ArtifactStore, 'put_trace_archive', fail_second)
    with pytest.raises(OSError, match='second archive'):
        apply(store, root, plan)
    assert seen == {'a', 'b'}
    assert store.trace_archives.list() == []
    assert seqs(store) == {'a': [1, 2, 3, 4], 'b': [1, 2, 3, 4]}


def test_downgrade_refuses_nonempty_receipts(seeded):
    store, root, plan = seeded
    result = apply(store, root, plan)
    # Remove only synthetic fixture timestamps so the receipt-specific guard is
    # what prevents downgrade, rather than Task 1's existing timestamp guard.
    sql(store, 'UPDATE trace_events SET stored_at=NULL')
    if hasattr(store, 'dsn'):
        from alembic import command
        from motte_storage.migrations import alembic_config, current
        with pytest.raises(RuntimeError, match='receipts must be preserved'):
            command.downgrade(alembic_config(store.dsn), '0017_judge_calibrations')
        assert current(store.dsn) == '0018_trace_retention'
    else:
        import importlib.util
        from sqlalchemy import create_engine
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from motte_storage.migrations import MIGRATIONS_DIR
        spec = importlib.util.spec_from_file_location('trace_receipt_migration', MIGRATIONS_DIR / 'versions/0018_trace_retention.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        engine = create_engine('sqlite:///' + store.runs._path)
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match='receipts must be preserved'):
                    module.downgrade()
        engine.dispose()
    assert store.trace_archives.list() == result.receipts
    assert seqs(store) == {'a': [4], 'b': [4]}


@pytest.mark.parametrize('invalid', ['pending', 'id', 'indexed_key'])
def test_memory_receipt_reader_matches_durable_validation(invalid):
    from threading import RLock
    from motte_storage.trace_archives import MemoryTraceArchives
    from tests.storage.test_trace_archives import evidence, receipt_for, archives
    prefix, rows = evidence()
    data = archives().build_trace_archive(prefix, rows)
    receipt = receipt_for(data, prefix, archive_id='trace-archive-' + hashlib.sha256(data).hexdigest(),
                          committed_at=NOW)
    if invalid == 'pending':
        receipt = receipt.model_copy(update={'committed_at': None})
    elif invalid == 'id':
        receipt = receipt.model_copy(update={'archive_id': 'forged'})
    repo = MemoryTraceArchives(RLock())
    repo._rows['wrong-key' if invalid == 'indexed_key' else receipt.archive_id] = receipt
    with pytest.raises(ValueError):
        repo.list()


def test_replay_requires_committed_complete_same_plan_receipts(seeded):
    store, root, plan = seeded
    result = apply(store, root, plan)
    receipt = result.receipts[0]
    payload = receipt.model_dump(mode='json')
    payload['committed_at'] = None
    sql(store, 'UPDATE trace_archive_receipts SET payload=? WHERE archive_id=?', (json.dumps(payload), receipt.archive_id))
    with pytest.raises(ValueError):
        apply(store, root, plan)
    assert seqs(store) == {'a': [4], 'b': [4]}


def test_result_zero_count_still_rejects_uncommitted_or_mismatched_receipts(seeded):
    store, root, plan = seeded
    result = apply(store, root, plan)
    for bad in (result.receipts[0].model_copy(update={'committed_at': None}),
                result.receipts[0].model_copy(update={'plan_id': 'sha256:' + 'f' * 64})):
        with pytest.raises(ValueError):
            models.TraceRetentionResult(plan_id=plan.plan_id, trimmed_events=0, receipts=[bad])
    with pytest.raises(ValueError):
        models.TraceRetentionResult(plan_id=plan.plan_id, trimmed_events=1, receipts=result.receipts)


def guard_definitions(connection, pg):
    if pg:
        return connection.execute("""SELECT c.relname, pg_get_triggerdef(t.oid) FROM pg_trigger t
            JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND t.tgname='motte_maintenance_write' ORDER BY c.relname""").fetchall()
    return connection.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'motte_maintenance_%' ORDER BY name").fetchall()


def test_commit_restores_identical_guards_and_clears_only_owner_in_same_commit(seeded, monkeypatch):
    from motte_storage import maintenance
    from contextlib import closing
    store, root, plan = seeded
    captured = []
    original = maintenance._install_write_barrier
    def install(connection, placeholder):
        original(connection, placeholder)
        captured.extend(guard_definitions(connection, placeholder == '%s'))
    monkeypatch.setattr(maintenance, '_install_write_barrier', install)
    commits = []
    mutating = False
    def observe(when, statement, connection):
        nonlocal mutating
        if statement.startswith('INSERT INTO trace_archive_receipts('):
            mutating = True
        if mutating and statement == 'COMMIT' and when == 'before':
            assert guard_definitions(connection, hasattr(store, 'dsn')) == captured
            assert connection.execute("SELECT meta_value FROM motte_meta WHERE meta_key='maintenance_owner'").fetchone() is None
            commits.append(True)
    observe_connections(monkeypatch, store, observe)
    assert apply(store, root, plan).trimmed_events == 6
    assert commits == [True]
    if hasattr(store, 'dsn'):
        from psycopg import connect
        context = connect(store.dsn)
    else:
        context = closing(sqlite3.connect(store.runs._path))
    with context as connection:
        assert guard_definitions(connection, hasattr(store, 'dsn')) == captured
    assert not maintenance_status(store)['active']


def test_changed_reference_on_commit_recheck_leaves_only_orphan_archives(seeded, monkeypatch):
    store, root, plan = seeded
    original = store.statistical_reports.list
    calls = 0
    def unreadable(**kwargs):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise RuntimeError('commit reference reader failed')
        return original(**kwargs)
    monkeypatch.setattr(store.statistical_reports, 'list', unreadable)
    with pytest.raises(RuntimeError, match='commit reference'):
        apply(store, root, plan)
    assert seqs(store) == {'a': [1, 2, 3, 4], 'b': [1, 2, 3, 4]}
    assert store.trace_archives.list() == []
    assert len(list(root.rglob('*.json'))) == 2


def test_disabled_apply_is_named_and_does_not_install_maintenance(tmp_path):
    store = SQLiteRunStore(tmp_path / 'runs.db')
    config = models.TraceRetentionConfig()
    plan = retention.plan_trace_retention(store, config=config)
    with pytest.raises(models.TraceRetentionDisabled):
        apply(store, tmp_path, plan, config=config)
    with sqlite3.connect(store.runs._path) as connection:
        assert not connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'motte_maintenance_%'").fetchall()


def test_corrupt_earlier_archive_before_commit_prevents_every_trim(seeded, monkeypatch):
    store, root, plan = seeded
    original = ArtifactStore.put_trace_archive
    first = None
    def corrupt(artifacts, data, **kwargs):
        nonlocal first
        result = original(artifacts, data, **kwargs)
        if json.loads(data)['run_id'] == 'a':
            first = root / result.id
        else:
            first.write_bytes(b'corrupted fixture before commit')
        return result
    monkeypatch.setattr(ArtifactStore, 'put_trace_archive', corrupt)
    with pytest.raises(ValueError):
        apply(store, root, plan)
    assert store.trace_archives.list() == []
    assert seqs(store) == {'a': [1, 2, 3, 4], 'b': [1, 2, 3, 4]}


@pytest.mark.parametrize('mismatch', ['pid', 'store', 'reason', 'root'])
def test_commit_live_owner_scope_is_mandatory(seeded, tmp_path, monkeypatch, mismatch):
    from motte_storage import maintenance
    from motte_storage.operation_locks import MaintenanceConflict
    store, root, plan = seeded
    lease = maintenance.begin_maintenance(store, reason='backup' if mismatch == 'reason' else 'trace_retention',
                                         artifacts_root=None if mismatch == 'root' else root)
    token = lease['owner']
    try:
        with monkeypatch.context() as patch:
            if mismatch == 'pid':
                held = maintenance._ACTIVE_MAINTENANCE[token]
                patch.setitem(maintenance._ACTIVE_MAINTENANCE, token, (*held[:3], -1, held[4]))
            target = SQLiteRunStore(tmp_path / 'wrong.db') if mismatch == 'store' else store
            with pytest.raises(MaintenanceConflict):
                maintenance.commit_trace_retention(target, owner=token, plan=plan, receipts=[])
    finally:
        maintenance.end_maintenance(store, owner=token)
    assert seqs(store) == {'a': [1, 2, 3, 4], 'b': [1, 2, 3, 4]}
    assert store.trace_archives.list() == []
    assert not list(root.rglob('*.json'))
