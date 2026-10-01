"""Synthetic archive closure, repeated retention and snapshot verification."""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import pytest

from motte_storage import trace_archives as archives
from motte_storage import trace_retention as retention
from motte_storage import trace_retention_models as models
from motte_storage.artifacts import ArtifactStore
from motte_storage.maintenance import consistent_backup, restore_staging, platform_for, RESTORE_GUARD_KEY
from tests.storage import test_trace_retention_transactions as common

durable_store = common.durable_store
CONFIG, NOW = common.CONFIG, common.NOW


def seed(store, root, monkeypatch):
    root.mkdir(exist_ok=True)
    artifacts = ArtifactStore(root)
    named = artifacts.put_bytes('named.bin', b'named evidence')
    hashed = artifacts.put_bytes('hash-only.bin', b'hash-only evidence')
    monkeypatch.setattr(models, 'utc_now', lambda: NOW - timedelta(days=20))
    for run in ('a', 'target', 'event-target', 'pass-target'):
        store.runs.create({'id': run, 'status': 'completed'})
    store.scoring_passes.append({'id': 'saved-pass', 'run_id': 'pass-target'}, [])
    store.events.append({'run_id': 'a', 'nested': {
        'source_run_id': 'target', 'source_pass_id': 'saved-pass',
        'coupled': {'run_id': 'pass-target', 'scoring_pass_id': 'saved-pass'},
        'evidence': {'kind': 'event', 'run_id': 'event-target', 'locator': '2'},
        'artifact': {'id': named.id, 'sha256': named.sha256},
        'artifacts': [{'sha256': hashed.sha256}]}})
    for run in ('a', 'target', 'event-target', 'pass-target'):
        while len(store.events.list_for_run(run)) < 4:
            store.events.append({'run_id': run, 'type': 'plain'})
    monkeypatch.setattr(models, 'utc_now', lambda: NOW)
    plan = retention.plan_trace_retention(store, config=CONFIG)
    result = retention.apply_trace_retention(store, root, plan, config=CONFIG, confirm=True)
    return artifacts, plan, result, named, hashed


@pytest.fixture
def archived(durable_store, tmp_path, monkeypatch):
    root = tmp_path / 'artifacts'
    values = seed(durable_store, root, monkeypatch)
    return durable_store, root, *values


def test_archived_cross_run_pass_event_pins_survive_trim_and_restart(archived):
    store, root, artifacts, plan, result, named, hashed = archived
    reopened = common.child_store(common.address_of(store))
    protection = retention.collect_trace_protection(reopened)
    assert {'target', 'pass-target'} <= protection.run_ids
    assert 'a' not in protection.run_ids
    assert protection.event_seqs['event-target'] == {2}
    receipt = next(row for row in reopened.trace_archives.list() if row.prefix.run_id == 'a')
    assert receipt.reference_document is not None
    archives.verify_trace_archives(reopened, artifacts)
    assert retention.apply_trace_retention(reopened, root, plan, config=CONFIG, confirm=True).trimmed_events == 0


def test_repeated_retention_begins_after_verified_receipts(archived, monkeypatch):
    store, root, artifacts, _, original, *_ = archived
    monkeypatch.setattr(models, 'utc_now', lambda: NOW - timedelta(days=10))
    store.events.append({'run_id': 'a'})
    store.events.append({'run_id': 'a'})
    monkeypatch.setattr(models, 'utc_now', lambda: NOW)
    plan = retention.plan_trace_retention(store, config=CONFIG)
    prefix = next(row for row in plan.prefixes if row.run_id == 'a')
    assert (prefix.first_seq, prefix.last_seq, prefix.keep_seq) == (4, 5, 6)
    result = retention.apply_trace_retention(store, root, plan, config=CONFIG, confirm=True)
    assert result.trimmed_events == 2
    assert [row['seq'] for row in store.events.list_for_run('a')] == [6]
    assert len(store.trace_archives.list()) == len(original.receipts) + 1
    archives.verify_trace_archives(store, artifacts)


def test_archive_references_ignore_rollback_exclusions(archived):
    from motte_storage.artifact_refs import referenced_artifact_hashes, referenced_run_ids, referenced_pass_ids
    store, _, _, _, result, named, hashed = archived
    hashes = set()
    refs = referenced_artifact_hashes(store, exclude_run_ids={'a'}, hashes=hashes)
    assert {named.id, *(receipt.artifact_id for receipt in result.receipts)} <= set(refs)
    assert hashed.sha256 in hashes
    assert {'a', 'target', 'pass-target', 'event-target'} <= referenced_run_ids(store, exclude_run_ids={'a'})
    assert 'saved-pass' in referenced_pass_ids(store, exclude_run_ids={'a'})


@pytest.mark.parametrize('failure', ['missing', 'bytes', 'document', 'missing_pass', 'mismatched_pass', 'truncated'])
def test_archive_reference_read_failure_is_fatal(archived, monkeypatch, failure):
    store, root, artifacts, _, result, *_ = archived
    receipt = next(row for row in result.receipts if row.prefix.run_id == 'a')
    if failure == 'missing':
        (root / receipt.artifact_id).unlink()
    elif failure == 'bytes':
        (root / receipt.artifact_id).write_bytes(b'corrupt fixture')
    elif failure == 'document':
        payload = receipt.model_dump(mode='json')
        payload['reference_document']['run_ids'] = []
        common.sql(store, 'UPDATE trace_archive_receipts SET payload=? WHERE archive_id=?',
                   (json.dumps(payload), receipt.archive_id))
    elif failure == 'truncated':
        monkeypatch.setattr(store.trace_archives, 'list', lambda: [])
    else:
        monkeypatch.setattr(store.scoring_passes, 'get', lambda _: None if failure == 'missing_pass'
                            else {'id': 'saved-pass', 'run_id': 'wrong'})
    with pytest.raises((ValueError, FileNotFoundError)):
        archives.verify_trace_archives(store, artifacts)


def test_legacy_absent_document_remains_conservative(archived):
    store, _, artifacts, _, result, *_ = archived
    receipt = result.receipts[0]
    payload = receipt.model_dump(mode='json')
    payload.pop('reference_document', None)
    common.sql(store, 'UPDATE trace_archive_receipts SET payload=? WHERE archive_id=?',
               (json.dumps(payload), receipt.archive_id))
    assert store.trace_archives.get(receipt.archive_id).reference_document is None
    with pytest.raises(ValueError, match='legacy|reference'):
        retention.plan_trace_retention(store, config=CONFIG)
    with pytest.raises(ValueError, match='legacy|reference'):
        archives.verify_trace_archives(store, artifacts)


def test_reference_document_is_strict_detached_and_uses_shared_ownership_rules():
    from motte_storage.trace_references import extract_reference_document, resolve_reference_document
    record = {'run_id': 'a', 'scoring_pass_id': 'own-pass', 'nested': {
        'source_run_id': 'a', 'evidence': {'kind': 'event', 'run_id': 'b', 'locator': '2'},
        'run_id': 'b', 'scoring_pass_id': 'coupled-pass'}}
    document = extract_reference_document([(record, 'a')])
    assert 'a' in document.run_ids  # Nested independent self-reference survives.
    snapshot = document.model_dump(mode='json')
    readback = dict(document)
    readback['event_seqs'].clear()
    assert document.model_dump(mode='json') == snapshot
    resolve_reference_document(document, lambda pid: {'id': pid, 'run_id': 'a' if pid == 'own-pass' else 'b'})
    with pytest.raises(ValueError, match='coupled'):
        resolve_reference_document(document, lambda pid: {'id': pid, 'run_id': 'a'})
    with pytest.raises(ValueError):
        type(document).model_validate({**snapshot, 'schema_version': True})


def test_sqlite_backup_restore_validates_archive_receipts(tmp_path, monkeypatch):
    from motte_storage.run_store import SQLiteRunStore
    store = SQLiteRunStore(tmp_path / 'source.db')
    root = tmp_path / 'artifacts'
    _, _, result, named, hashed = seed(store, root, monkeypatch)
    manifest = consistent_backup(store, tmp_path / 'backup', artifacts_root=root)
    assert manifest['counts']['trace_archive_receipts'] == len(result.receipts)
    files = {item['path'] for item in manifest['artifacts']['files']}
    assert {named.id, hashed.id, *(r.artifact_id for r in result.receipts)} <= files
    restored = restore_staging(tmp_path / 'backup', tmp_path / 'restored')
    target = SQLiteRunStore(restored['database'])
    assert target.trace_archives.list() == result.receipts
    assert [row['seq'] for row in target.events.list_for_run('a')] == [4]
    assert platform_for(target).meta.get(RESTORE_GUARD_KEY)
    archives.verify_trace_archives(target, ArtifactStore(tmp_path / 'restored/artifacts'))


@pytest.mark.parametrize('backend', ['memory', 'sqlite', 'postgres'])
def test_gc_plan_missing_root_without_receipts_never_creates_parents(tmp_path, request, backend):
    from motte_storage.gc import plan_gc
    from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore
    if backend == 'memory':
        store = InMemoryRunStore()
    elif backend == 'sqlite':
        store = SQLiteRunStore(tmp_path / 'empty.db')
    else:
        from motte_storage.postgres import create_postgres_run_store
        store = create_postgres_run_store(request.getfixturevalue('isolated_pg_database'), migrate=True)
    root = tmp_path / 'missing-parent' / 'artifacts'
    assert store.trace_archives.list() == []
    assert not root.parent.exists()

    plan = plan_gc(store, root)

    assert not root.exists()
    assert not root.parent.exists()
    assert plan.warnings == [f'artifact root missing: {root}']
    assert plan.dry_run and not plan.deletable and not plan.protected
    assert store.trace_archives.list() == []


def test_gc_plan_required_missing_archives_never_creates_parents(archived, tmp_path):
    from motte_storage.gc import plan_gc
    store, _, _, _, result, *_ = archived
    root = tmp_path / 'missing-parent' / 'missing-artifacts'
    assert result.receipts and not root.parent.exists()

    with pytest.raises(FileNotFoundError):
        plan_gc(store, root)

    assert not root.exists()
    assert not root.parent.exists()
    assert store.trace_archives.list() == result.receipts


def test_gc_plan_archive_reader_rejects_out_of_root_symlink(archived, tmp_path):
    from motte_storage.gc import plan_gc
    store, source, _, _, _, *_ = archived
    root = tmp_path / 'alias-root'
    root.mkdir()
    (root / 'trace-archives').symlink_to(source / 'trace-archives', target_is_directory=True)

    with pytest.raises(ValueError, match='artifact path escapes root'):
        plan_gc(store, root)


def test_gc_archive_reader_rejects_root_replaced_after_construction(tmp_path):
    from motte_storage.gc import _ReadOnlyArtifactReader
    root, outside = tmp_path / 'root', tmp_path / 'outside'
    root.mkdir()
    outside.mkdir()
    (outside / 'evidence.bin').write_bytes(b'outside evidence')
    reader = _ReadOnlyArtifactReader(root)
    root.rename(tmp_path / 'original-root')
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match='artifact path escapes root'):
        reader.read_bytes('evidence.bin')


def test_gc_plan_rejects_root_replaced_after_reader_construction(archived, tmp_path, monkeypatch):
    from motte_storage.gc import plan_gc
    store, root, _, _, result, *_ = archived
    original = archives.checked_trace_receipts
    switched = False

    def checked_with_root_switch(candidate_store):
        nonlocal switched
        if not switched:
            switched = True
            outside = tmp_path / 'outside-root'
            root.rename(outside)
            root.symlink_to(outside, target_is_directory=True)
        return original(candidate_store)

    monkeypatch.setattr(archives, 'checked_trace_receipts', checked_with_root_switch)
    with pytest.raises(ValueError, match='artifact path escapes root'):
        plan_gc(store, root)
    assert switched
    assert store.trace_archives.list() == result.receipts


@pytest.mark.parametrize('operation', ['plan', 'apply'])
def test_gc_rechecks_canonical_archive_closure_before_any_deletion(archived, operation):
    from motte_storage.gc import plan_gc, apply_gc
    store, root, artifacts, _, result, named, hashed = archived
    plan = plan_gc(store, root, artifact_ttl_days=1)
    receipt = next(row for row in result.receipts if row.prefix.run_id == 'a')
    payload = receipt.model_dump(mode='json')
    payload.update(artifact_refs={}, artifact_hashes=[])
    common.sql(store, 'UPDATE trace_archive_receipts SET payload=? WHERE archive_id=?',
               (json.dumps(payload), receipt.archive_id))
    with pytest.raises(ValueError, match='archive|evidence'):
        if operation == 'plan':
            plan_gc(store, root, artifact_ttl_days=1)
        else:
            apply_gc(store, root, plan, confirm=True)
    assert artifacts.read_bytes(named.id) == b'named evidence'
    assert artifacts.read_bytes(hashed.id) == b'hash-only evidence'


def test_trimmed_evidence_survives_gc_permanently(archived):
    import os
    from motte_storage.gc import plan_gc, apply_gc
    store, root, artifacts, _, result, named, hashed = archived
    for path in root.rglob('*'):
        if path.is_file():
            os.utime(path, (1, 1))
    plan = plan_gc(store, root, artifact_ttl_days=1)
    protected = {named.id, hashed.id, *(r.artifact_id for r in result.receipts)}
    assert protected.isdisjoint({row['artifact_id'] for row in plan.deletable})
    assert apply_gc(store, root, plan, confirm=True)['deleted'] == 0
    assert all((root / identifier).is_file() for identifier in protected)


@pytest.mark.parametrize('corruption', ['overlap', 'gap', 'max', 'live_overlap', 'missing_run'])
def test_archive_receipt_ranges_and_live_bounds_fail_backup(archived, tmp_path, corruption):
    from motte_storage.maintenance import BackupIncomplete, consistent_backup_postgres
    store, root, _, _, result, *_ = archived
    receipt = next(row for row in result.receipts if row.prefix.run_id == 'a')
    if corruption in {'overlap', 'gap'}:
        body = receipt.model_dump(mode='json')
        body.update(archive_id='trace-archive-' + '1' * 64, sha256='sha256:' + '1' * 64,
                    artifact_id='trace-archives/sha256/' + '1' * 64 + '.json')
        first = 2 if corruption == 'overlap' else 5
        body['prefix'].update(first_seq=first, last_seq=first, event_count=1, keep_seq=6)
        common.sql(store, 'INSERT INTO trace_archive_receipts VALUES (?,?,?,?,?,?)',
                   (body['archive_id'], body['plan_id'], 'a', first, first, json.dumps(body)))
    elif corruption == 'max':
        body = receipt.model_dump(mode='json')
        body['prefix']['keep_seq'] = 100
        common.sql(store, 'UPDATE trace_archive_receipts SET payload=? WHERE archive_id=?',
                   (json.dumps(body), receipt.archive_id))
    elif corruption == 'live_overlap':
        common.sql(store, 'INSERT INTO trace_events(run_id,seq,payload,stored_at) VALUES (?,?,?,?)',
                   ('a', 3, json.dumps({'run_id': 'a', 'seq': 3}), NOW.isoformat()))
    else:
        common.sql(store, 'DELETE FROM runs WHERE id=?', ('a',))
    with pytest.raises(BackupIncomplete, match='range|gap|overlap|bounds|owning Run'):
        if hasattr(store, 'dsn'):
            consistent_backup_postgres(store.dsn, tmp_path / 'bad-backup', artifacts_root=root, store=store)
        else:
            consistent_backup(store, tmp_path / 'bad-backup', artifacts_root=root)


@pytest.mark.parametrize('change', ['absent', 'bool', 'float', 'extra'])
def test_reference_document_requires_strict_version(change):
    from motte_storage.trace_references import extract_reference_document
    doc = extract_reference_document([]).model_dump(mode='json')
    if change == 'absent':
        doc.pop('schema_version')
    elif change == 'extra':
        doc['trusted'] = True
    else:
        doc['schema_version'] = True if change == 'bool' else 1.0
    with pytest.raises(ValueError):
        models.TraceReferenceDocument.model_validate(doc)


def test_rollback_rejects_tampered_archive_before_deleting_any_artifact(tmp_path, monkeypatch):
    from motte_sdk.migration import rollback_import
    from motte_storage.run_store import SQLiteRunStore
    store = SQLiteRunStore(tmp_path / 'source.db')
    root = tmp_path / 'artifacts'
    _, _, result, *_ = seed(store, root, monkeypatch)
    ledger = platform_for(store).imports
    ledger.begin_import({'import_id': 'fixture', 'status': 'applied', 'manifest_sha256': 'sha256:' + 'f' * 64})
    receipt = next(row for row in result.receipts if row.prefix.run_id == 'a')
    payload = receipt.model_dump(mode='json')
    payload.update(artifact_refs={}, artifact_hashes=[])
    common.sql(store, 'UPDATE trace_archive_receipts SET payload=? WHERE archive_id=?',
               (json.dumps(payload), receipt.archive_id))
    with pytest.raises(ValueError, match='archive|evidence'):
        rollback_import(store, root, 'fixture', operator='fixture', confirm=True)


def test_existing_live_gap_is_not_mislabeled_as_archived_or_crossed(durable_store, tmp_path, monkeypatch):
    store = durable_store
    root = tmp_path / 'artifacts'
    root.mkdir()
    monkeypatch.setattr(models, 'utc_now', lambda: NOW - timedelta(days=20))
    store.runs.create({'id': 'gap', 'status': 'completed'})
    for _ in range(4):
        store.events.append({'run_id': 'gap'})
    common.sql(store, 'DELETE FROM trace_events WHERE run_id=? AND seq=3', ('gap',))
    monkeypatch.setattr(models, 'utc_now', lambda: NOW)
    plan = retention.plan_trace_retention(store, config=CONFIG)
    assert [(p.first_seq, p.last_seq) for p in plan.prefixes] == [(1, 2)]
    retention.apply_trace_retention(store, root, plan, config=CONFIG, confirm=True)
    archives.verify_trace_archives(store, ArtifactStore(root))
    assert retention.plan_trace_retention(store, config=CONFIG).prefixes == []


def test_sqlite_restore_verifies_archive_document_beyond_outer_hashes(tmp_path, monkeypatch):
    from motte_storage.run_store import SQLiteRunStore
    from motte_storage import maintenance
    import sqlite3
    from contextlib import closing
    store = SQLiteRunStore(tmp_path / 'source.db')
    root = tmp_path / 'artifacts'
    seed(store, root, monkeypatch)
    backup = tmp_path / 'backup'
    manifest = consistent_backup(store, backup, artifacts_root=root)
    db = backup / manifest['database']['snapshot']
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("DELETE FROM motte_meta WHERE meta_key='maintenance'")
        archive_id, raw = connection.execute("SELECT archive_id,payload FROM trace_archive_receipts WHERE run_id='a'").fetchone()
        payload = json.loads(raw)
        payload['reference_document']['run_ids'] = []
        connection.execute('UPDATE trace_archive_receipts SET payload=? WHERE archive_id=?', (json.dumps(payload), archive_id))
    manifest['database']['sha256'] = hashlib.sha256(db.read_bytes()).hexdigest()
    manifest['database']['bytes'] = db.stat().st_size
    manifest['manifest_sha256'] = maintenance._manifest_content_sha256(manifest)
    next(backup.glob('manifest-*.json')).write_text(json.dumps(manifest))
    original = maintenance._validate_restored_reference_closure
    checked = []
    def validate(staging_store, artifacts_root):
        assert platform_for(staging_store).meta.get(RESTORE_GUARD_KEY)
        checked.append(True)
        return original(staging_store, artifacts_root)
    monkeypatch.setattr(maintenance, '_validate_restored_reference_closure', validate)
    with pytest.raises(maintenance.RestoreIncomplete, match='reference document'):
        restore_staging(backup, tmp_path / 'staging')
    assert checked == [True] and not (tmp_path / 'staging').exists()


@pytest.mark.parametrize('count', [2.0, True, None])
def test_sqlite_restore_rejects_noninteger_archive_counts(tmp_path, monkeypatch, count):
    from motte_storage.run_store import SQLiteRunStore
    from motte_storage import maintenance
    store = SQLiteRunStore(tmp_path / 'source.db')
    root = tmp_path / 'artifacts'
    seed(store, root, monkeypatch)
    backup = tmp_path / 'backup'
    manifest = consistent_backup(store, backup, artifacts_root=root)
    manifest['counts']['trace_archive_receipts'] = count
    manifest['manifest_sha256'] = maintenance._manifest_content_sha256(manifest)
    next(backup.glob('manifest-*.json')).write_text(json.dumps(manifest))
    with pytest.raises(maintenance.RestoreIncomplete):
        restore_staging(backup, tmp_path / 'staging')


@pytest.mark.parametrize('corruption', ['count', 'event_digest', 'time', 'bytes', 'sequence'])
def test_backup_rejects_receipt_payload_mismatch(archived, tmp_path, corruption):
    from motte_storage.maintenance import BackupIncomplete, consistent_backup_postgres
    store, root, _, _, result, *_ = archived
    receipt = next(row for row in result.receipts if row.prefix.run_id == 'a')
    payload = receipt.model_dump(mode='json')
    if corruption == 'count':
        payload['prefix']['event_count'] += 1
    elif corruption == 'event_digest':
        payload['prefix']['events_sha256'] = 'sha256:' + '0' * 64
    elif corruption == 'time':
        payload['cutoff'] = (NOW - timedelta(days=20)).isoformat()
    elif corruption == 'sequence':
        payload['prefix']['first_seq'] = 2
        payload['prefix']['event_count'] = 2
    else:
        payload['bytes'] += 1
    common.sql(store, 'UPDATE trace_archive_receipts SET payload=? WHERE archive_id=?',
               (json.dumps(payload), receipt.archive_id))
    with pytest.raises((ValueError, BackupIncomplete)):
        if hasattr(store, 'dsn'):
            consistent_backup_postgres(store.dsn, tmp_path / 'bad-backup', artifacts_root=root, store=store)
        else:
            consistent_backup(store, tmp_path / 'bad-backup', artifacts_root=root)


def test_missing_or_truncated_receipt_reader_aborts_all_reference_consumers(archived, monkeypatch):
    from motte_storage.artifact_refs import referenced_artifact_hashes, referenced_run_ids, referenced_pass_ids
    store, _, _, _, _, *_ = archived
    monkeypatch.setattr(store.trace_archives, 'list', lambda: [])
    for read in (referenced_artifact_hashes, referenced_run_ids, referenced_pass_ids):
        with pytest.raises(ValueError, match='coverage'):
            read(store)
