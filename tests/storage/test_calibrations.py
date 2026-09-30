"""Synthetic persistence fixtures; no real human/model acceptance claims."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib.util
import sqlite3
from threading import Barrier

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from motte_contracts.hashing import canonical_json
from motte_eval.calibration import qualification_record
from motte_eval.calibration_records import (
    CalibrationExecution, CalibrationQualificationSource, CalibrationVersion,
)
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore
from motte_storage.scoring_jobs import scoring_jobs_for
from tests.evaluators.test_m8_calibration_records import (
    HASH, TIME, execution_fixture, import_fixture, report_fixture, reviewed_fixture,
)
from tests.storage.test_scoring_jobs import job_record


def lifecycle(store):
    assert getattr(store, "calibrations", None) is not None, "missing lifecycle repository"
    return store.calibrations


def prepare(store):
    repo = lifecycle(store)
    parent, child, records = reviewed_fixture()
    repo.put_version(parent)
    repo.put_review(records, child)
    execution = execution_fixture()
    return repo, execution, jobs_for(execution)


def jobs_for(execution):
    return [job_record(job_id=job_id, request_key=execution.request_key + '/' + job_id,
                       reserved_pass_id='pass-' + job_id, mode=execution.spec.mode,
                       owner={"kind": "calibration", "calibration_job_id": execution.execution_id})
            for job_id in execution.child_job_ids]


def qualification_for(report):
    return CalibrationQualificationSource.seal(dict(
        qualification=qualification_record(report.report),
        binding={**report.source.model_dump(), "report_id": report.report_id,
                 "report_sha256": report.content_sha256}, recorded_at=TIME,
    ))


class RepositoryContract:
    def test_immutable_replay_and_conflict(self, store):
        repo = lifecycle(store)
        version = import_fixture().to_version()
        saved = repo.put_version(version)
        assert repo.put_version(version) == saved
        version.pairs['sample-1'].candidates[0].evidence_allowlist.append('event:changed')
        fetched = repo.get_version(saved.reference)
        fetched.pairs['sample-1'].candidates[0].evidence_allowlist.append('event:tampered')
        assert repo.get_version(saved.reference) == saved
        assert repo.list_versions(saved.calibration.calibration_id) == [saved]
        altered = saved.model_dump(mode='json')
        altered['pairs']['sample-1']['candidates'][0]['content'] += ' changed'
        with pytest.raises(ValueError, match='immutable|conflict'):
            repo.put_version(CalibrationVersion.seal(altered))
        for name in ('delete', 'update', 'register'):
            assert not hasattr(repo, name)

    def test_unbounded_list_and_enumeration(self, store):
        from motte_eval.calibration import build_calibration_set
        from motte_eval.calibration_records import CalibrationImport
        repo = lifecycle(store)
        draft = import_fixture()
        versions = []
        for index in range(103):
            raw = draft.calibration.model_dump(exclude={'schema_version', 'content_sha256'})
            raw['version'] = str(index)
            raw['samples'] = draft.calibration.samples
            version = CalibrationImport(calibration=build_calibration_set(**raw), pairs=draft.pairs).to_version()
            versions.append(repo.put_version(version))
        assert repo.list_versions(draft.calibration.calibration_id) == sorted(
            versions, key=lambda version: version.calibration.version)
        assert len(list(repo.iter_records())) == 103

    def test_review_and_version_are_atomic(self, store):
        repo = lifecycle(store)
        parent, child, records = reviewed_fixture()
        with pytest.raises(ValueError, match='parent|source'):
            repo.put_review(records, child)
        assert repo.get_review(records[0].review_id) is None
        assert repo.get_version(child.reference) is None
        repo.put_version(parent)
        bad = records[0].model_copy(update={'after_sample_sha256': HASH})
        with pytest.raises(ValueError):
            repo.put_review([bad], child)
        assert repo.get_review(records[0].review_id) is None
        assert repo.get_version(child.reference) is None
        repo.put_review(records[0], child)
        assert repo.get_version(child.reference) == child
        assert repo.get_review(records[0].review_id) == records[0]
        repo.put_review(records, child)
        with pytest.raises(ValueError):
            repo.put_review([], child)

    def test_reviewed_version_cannot_bypass_source_chain(self, store):
        repo = lifecycle(store)
        parent, child, records = reviewed_fixture()
        repo.put_version(parent)
        with pytest.raises(ValueError, match='review'):
            repo.put_version(child)
        assert repo.get_version(child.reference) is None

    def test_group_submit_is_all_or_none(self, store):
        repo, execution, jobs = prepare(store)
        raw = execution.model_dump(mode='json')
        raw['child_job_ids'].append('child-2')
        raw['plan'].append({'call_id': 'call-2', 'sample_id': 'sample-1', 'child_job_id': 'child-2'})
        execution = CalibrationExecution.seal(raw)
        jobs = jobs_for(execution)
        # A legitimate pre-existing second child forces rollback after first insertion.
        scoring_jobs_for(store).submit(jobs[1])
        with pytest.raises(ValueError):
            repo.submit_execution(execution, jobs)
        assert repo.get_execution(execution.execution_id) is None
        assert scoring_jobs_for(store).get(jobs[0]['job_id']) is None
        assert scoring_jobs_for(store).get(jobs[1]['job_id']) is not None

    def test_submit_replay_and_fingerprint_conflict(self, store):
        repo, execution, jobs = prepare(store)
        first, created = repo.submit_execution(execution, jobs)
        assert created is True
        assert repo.get_execution(execution.execution_id) == first
        assert repo.get_execution_by_key(execution.request_key) == first
        assert repo.list_executions(execution.version.calibration.calibration_id) == [first]
        assert repo.submit_execution(execution, jobs) == (first, False)
        raw = execution.model_dump(mode='json')
        raw['request_fingerprint'] = 'sha256:' + 'b' * 64
        with pytest.raises(ValueError, match='fingerprint|conflict'):
            repo.submit_execution(CalibrationExecution.seal(raw), jobs)
        assert len(scoring_jobs_for(store).list_by_status()) == len(jobs)

    def test_concurrent_request_has_one_group(self, store):
        repo, execution, jobs = prepare(store)
        barrier = Barrier(6)
        def submit(_):
            barrier.wait(timeout=10)
            return repo.submit_execution(execution, jobs)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(submit, range(6)))
        assert sum(created for _, created in results) == 1
        assert all(item == execution for item, _ in results)
        assert len(scoring_jobs_for(store).list_by_status()) == len(jobs)

    def test_report_and_qualification_are_atomic(self, store):
        repo, execution, jobs = prepare(store)
        repo.submit_execution(execution, jobs)
        report, _ = report_fixture()
        qualification = qualification_for(report)
        raw = qualification.model_dump(mode='json')
        raw['binding']['report_sha256'] = 'sha256:' + 'b' * 64
        wrong = CalibrationQualificationSource.seal(raw)
        with pytest.raises(ValueError):
            repo.publish_report(report, wrong)
        assert repo.get_report(report.report_id) is None
        assert repo.get_qualification(wrong.binding.qualification_id) is None
        assert repo.publish_report(report, qualification) == report
        assert repo.publish_report(report, qualification) == report
        assert repo.get_qualification(qualification.binding.qualification_id) == qualification
        assert repo.list_reports(execution.execution_id) == [report]
        assert len(list(repo.iter_records())) == 6

    def test_child_owner_preserves_existing_sample_id_mapping(self, store):
        repo, execution, jobs = prepare(store)
        jobs[0]['owner']['sample_ids'] = {'sample-1': 'sample-1'}
        saved, created = repo.submit_execution(execution, jobs)
        assert created and saved == execution
        assert repo.get_execution(execution.execution_id) == execution
        assert scoring_jobs_for(store).get(jobs[0]['job_id'])['owner']['sample_ids'] == {'sample-1': 'sample-1'}

    def test_replay_returns_original_children_and_receipt(self, store):
        repo, execution, jobs = prepare(store)
        first, _ = repo.submit_execution(execution, jobs)
        raw = execution.model_dump(mode='json')
        raw['child_job_ids'] = ['retry-generated-child']
        raw['plan'][0]['child_job_id'] = 'retry-generated-child'
        raw['recorded_at'] = '2026-09-30T03:00:00Z'
        retry = CalibrationExecution.seal(raw)
        assert repo.submit_execution(retry, jobs_for(retry)) == (first, False)
        assert scoring_jobs_for(store).get('retry-generated-child') is None

    def test_payload_text_preserves_exact_numeric_identity(self, store):
        repo, execution, _ = prepare(store)
        for index, number in enumerate((-0.0, 0.0, 1.0, 1)):
            raw = execution.model_dump(mode='json')
            raw['request_key'] += '/' + str(index)
            raw['child_job_ids'] = ['numeric-child-' + str(index)]
            raw['plan'][0].update(child_job_id=raw['child_job_ids'][0], numeric_fixture=number)
            proposal = CalibrationExecution.seal(raw)
            repo.submit_execution(proposal, jobs_for(proposal))
            fetched = repo.get_execution(proposal.execution_id)
            assert canonical_json(fetched.plan) == canonical_json(proposal.plan)

    def test_report_rejects_missing_execution(self, store):
        report, _ = report_fixture()
        repo = lifecycle(store)
        with pytest.raises(ValueError, match='execution|source'):
            repo.publish_report(report, qualification_for(report))
        assert repo.get_report(report.report_id) is None

    def test_reference_closure_includes_calibration_jobs_without_run(self, store):
        from motte_storage.artifact_refs import referenced_artifact_hashes, referenced_run_ids
        repo, execution, jobs = prepare(store)
        jobs[0]['input_digests'] = {'artifact': {'artifact_id': 'fixture/input.json', 'sha256': HASH}}
        raw = execution.model_dump(mode='json')
        raw['plan'][0]['source_run_id'] = 'source-run'
        raw['plan'][0]['artifact_id'] = 'fixture/plan.json'
        execution = CalibrationExecution.seal(raw)
        repo.submit_execution(execution, jobs)
        assert store.runs.list() == []
        assert referenced_artifact_hashes(store)['fixture/input.json'] == HASH
        assert 'fixture/plan.json' in referenced_artifact_hashes(store)
        assert 'source-run' in referenced_run_ids(store)

    def test_missing_reference_enumerator_fails_closed(self, store, monkeypatch):
        from motte_storage.artifact_refs import referenced_artifact_hashes
        repo = lifecycle(store)
        monkeypatch.setattr(repo, 'iter_records', None)
        with pytest.raises((AttributeError, TypeError)):
            referenced_artifact_hashes(store)


class TestMemorySQLite(RepositoryContract):
    @pytest.fixture(params=['memory', 'sqlite'])
    def store(self, request, tmp_path):
        return InMemoryRunStore() if request.param == 'memory' else SQLiteRunStore(tmp_path / 'cal.db')


def migration():
    from motte_storage.migrations import MIGRATIONS_DIR
    spec = importlib.util.spec_from_file_location('calibration_migration', MIGRATIONS_DIR / 'versions/0017_judge_calibrations.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_preserves_0016_and_blocks_evidence_downgrade(tmp_path):
    assert (migration().revision, migration().down_revision) == ('0017_judge_calibrations', '0016_statistical_reports')
    engine = create_engine('sqlite:///' + str(tmp_path / 'migration.db'))
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        connection.execute(text('CREATE TABLE statistical_reports (report_id TEXT PRIMARY KEY, body TEXT, published_at TEXT)'))
        connection.execute(text("INSERT INTO statistical_reports VALUES ('prior', 'preserved', 'time')"))
        migration().upgrade()
        connection.execute(text("INSERT INTO judge_calibration_versions VALUES ('cal', '1', 'digest', 'damaged evidence')"))
        with pytest.raises(RuntimeError, match='judge_calibration_versions'):
            migration().downgrade()
        assert connection.execute(text('SELECT body FROM statistical_reports')).scalar() == 'preserved'


def test_maintenance_backup_and_reference_closure(tmp_path):
    from motte_storage.maintenance import begin_maintenance, consistent_backup, end_maintenance, restore_staging
    store = SQLiteRunStore(tmp_path / 'source.db')
    repo, execution, jobs = prepare(store)
    repo.submit_execution(execution, jobs)
    report, _ = report_fixture()
    repo.publish_report(report, qualification_for(report))
    lease = begin_maintenance(store)
    try:
        with pytest.raises(Exception, match='maintenance'):
            repo.put_version(import_fixture(calibration_id='blocked').to_version())
    finally:
        end_maintenance(store, owner=lease['owner'])
    root = tmp_path / 'artifacts'
    root.mkdir()
    consistent_backup(store, tmp_path / 'backup', artifacts_root=root)
    restored = restore_staging(tmp_path / 'backup', tmp_path / 'restore')
    recovered = SQLiteRunStore(restored['database'])
    assert list(recovered.calibrations.iter_records()) == list(repo.iter_records())


def corrupt_row(repo, kind, identity, column, value):
    """Explicitly emulate storage damage, not a public write interface."""
    from motte_storage.calibrations import MemoryCalibrations, _COLUMNS
    if isinstance(repo, MemoryCalibrations):
        key = identity if isinstance(identity, tuple) else (identity,)
        row = list(repo._rows[kind][key])
        row[(*_COLUMNS[kind], 'payload').index(column)] = value
        repo._rows[kind][key] = tuple(row)
        return
    if hasattr(repo, '_dsn'):
        import psycopg
        connection = psycopg.connect(repo._dsn)
        marker = '%s'
    else:
        connection = sqlite3.connect(repo._path)
        marker = '?'
    try:
        with connection:
            key = identity if isinstance(identity, tuple) else (identity,)
            where = ' AND '.join(col + '=' + marker for col in _COLUMNS[kind][:len(key)])
            connection.execute('UPDATE judge_calibration_' + kind + ' SET ' + column + '='
                               + marker + ' WHERE ' + where, (value, *key))
    finally:
        connection.close()


class CorruptionContract:
    @pytest.mark.parametrize('kind', ['versions', 'reviews', 'executions', 'reports', 'qualifications'])
    @pytest.mark.parametrize('damage', ['index', 'payload', 'canonical'])
    def test_corrupt_read_list_replay_fail_closed(self, store, kind, damage):
        from motte_storage.calibrations import CalibrationCorrupt
        repo, execution, jobs = prepare(store)
        repo.submit_execution(execution, jobs)
        report, _ = report_fixture()
        qualification = qualification_for(report)
        repo.publish_report(report, qualification)
        parent, child, reviews = reviewed_fixture()
        sources = {
            'versions': (parent, (parent.calibration.calibration_id, parent.calibration.version),
                         'content_sha256', lambda: repo.get_version(parent.reference),
                         lambda: repo.list_versions(parent.calibration.calibration_id),
                         lambda: repo.put_version(parent)),
            'reviews': (reviews[0], reviews[0].review_id, 'child_version',
                        lambda: repo.get_review(reviews[0].review_id), lambda: list(repo.iter_records()),
                        lambda: repo.put_review(reviews, child)),
            'executions': (execution, execution.execution_id, 'request_fingerprint',
                           lambda: repo.get_execution(execution.execution_id),
                           lambda: repo.list_executions(parent.calibration.calibration_id),
                           lambda: repo.submit_execution(execution, jobs)),
            'reports': (report, report.report_id, 'content_sha256',
                        lambda: repo.get_report(report.report_id),
                        lambda: repo.list_reports(execution.execution_id),
                        lambda: repo.publish_report(report, qualification)),
            'qualifications': (qualification, qualification.binding.qualification_id, 'content_sha256',
                               lambda: repo.get_qualification(qualification.binding.qualification_id),
                               lambda: list(repo.iter_records()),
                               lambda: repo.publish_report(report, qualification)),
        }
        record, identity, column, *reads = sources[kind]
        if damage == 'index':
            value = 'wrong-index'
        elif damage == 'payload':
            column, value = 'payload', '{damaged original bytes'
        else:
            column, value = 'payload', record.model_dump_json(indent=2)
        corrupt_row(repo, kind, identity, column, value)
        for read in reads:
            with pytest.raises(CalibrationCorrupt):
                read()

    def test_orphan_review_is_not_silently_enumerated(self, store):
        from motte_eval.calibration_records import HumanReviewRecord, calibration_review_id
        from motte_storage.calibrations import CalibrationCorrupt, _row
        repo, _, _ = prepare(store)
        parent, child, records = reviewed_fixture()
        changed = records[0].review.model_copy(update={'reason': 'different synthetic review'})
        orphan = HumanReviewRecord.model_validate({**records[0].model_dump(), 'review': changed,
                       'review_id': calibration_review_id(parent.reference, changed)})
        # Deliberately emulate corrupt direct storage, bypassing public source verification.
        with repo._transaction(write=True) as tx:
            repo._insert(tx, 'reviews', _row('reviews', orphan))
        with pytest.raises(CalibrationCorrupt):
            list(repo.iter_records())

    def test_bad_child_job_blocks_execution_replay(self, store):
        from motte_storage.calibrations import CalibrationCorrupt, MemoryCalibrations
        repo, execution, jobs = prepare(store)
        repo.submit_execution(execution, jobs)
        child_id = execution.child_job_ids[0]
        if isinstance(repo, MemoryCalibrations):
            store.scoring_jobs._rows[child_id]['owner_kind'] = 'subject'
        else:
            if hasattr(repo, '_dsn'):
                import psycopg
                connection = psycopg.connect(repo._dsn)
            else:
                connection = sqlite3.connect(repo._path)
            try:
                with connection:
                    connection.execute("UPDATE scoring_jobs SET owner_kind='subject'")
            finally:
                connection.close()
        for read in (lambda: repo.get_execution(execution.execution_id),
                     lambda: repo.submit_execution(execution, jobs), lambda: list(repo.iter_records())):
            with pytest.raises(CalibrationCorrupt):
                read()

    def test_evidence_allowlist_artifact_tokens_are_pinned(self, store):
        from motte_storage.artifact_refs import referenced_artifact_hashes
        parent = import_fixture().to_version()
        raw = parent.model_dump(mode='json')
        raw['pairs']['sample-1']['candidates'][0]['evidence_allowlist'] = ['artifact:fixture/evidence.json']
        lifecycle(store).put_version(CalibrationVersion.seal(raw))
        assert 'fixture/evidence.json' in referenced_artifact_hashes(store)


class TestMemorySQLiteCorruption(CorruptionContract):
    @pytest.fixture(params=['memory', 'sqlite'])
    def store(self, request, tmp_path):
        return InMemoryRunStore() if request.param == 'memory' else SQLiteRunStore(tmp_path / 'cal.db')


def test_outer_publication_guard_excludes_maintenance_before_validation(tmp_path):
    from motte_storage.calibrations import calibration_publication_guard
    from motte_storage.maintenance import begin_maintenance, end_maintenance
    from motte_storage.operation_locks import MaintenanceConflict
    store = SQLiteRunStore(tmp_path / 'guard.db')
    with calibration_publication_guard(store):
        with pytest.raises(MaintenanceConflict):
            begin_maintenance(SQLiteRunStore(tmp_path / 'guard.db'))
    lease = begin_maintenance(store)
    try:
        with pytest.raises(MaintenanceConflict):
            with calibration_publication_guard(store):
                pytest.fail('publication acquired maintenance exclusion')
    finally:
        end_maintenance(store, owner=lease['owner'])


class TransactionFaultContract:
    @pytest.mark.parametrize('operation', ['review', 'report'])
    def test_fault_after_second_insert_rolls_back_both_sources(self, store, monkeypatch, operation):
        repo = lifecycle(store)
        parent, child, reviews = reviewed_fixture()
        repo.put_version(parent)
        if operation == 'report':
            repo.put_review(reviews, child)
            execution = execution_fixture()
            repo.submit_execution(execution, jobs_for(execution))
        before = list(repo.iter_records())
        original = repo._insert
        target = 'versions' if operation == 'review' else 'qualifications'
        def fail_after_insert(tx, kind, row):
            original(tx, kind, row)
            if kind == target:
                raise RuntimeError('synthetic transaction crash')
        monkeypatch.setattr(repo, '_insert', fail_after_insert)
        with pytest.raises(RuntimeError, match='synthetic transaction crash'):
            if operation == 'review':
                repo.put_review(reviews, child)
            else:
                report, _ = report_fixture()
                repo.publish_report(report, qualification_for(report))
        assert list(repo.iter_records()) == before

    def test_multi_review_transition_requires_complete_ordered_batch(self, store):
        from motte_eval.calibration import build_calibration_set, sample_content_sha256
        from motte_eval.calibration_records import CalibrationImport, review_calibration_version
        from tests.evaluators.test_m8_calibration_records import pair_fixture, review_fixture, sample_fixture
        draft = import_fixture()
        sample2 = sample_fixture().model_copy(update={'sample_id': 'sample-2'})
        sample2 = sample2.model_copy(update={'content_sha256': sample_content_sha256(sample2)})
        raw = draft.calibration.model_dump(exclude={'schema_version', 'content_sha256'})
        raw['samples'] = [*draft.calibration.samples, sample2]
        parent = CalibrationImport(calibration=build_calibration_set(**raw),
                                   pairs={**draft.pairs, 'sample-2': pair_fixture()}).to_version()
        child, records = review_calibration_version(parent, new_version='2', reviews=[
            review_fixture(), review_fixture(sample_id='sample-2')], recorded_at=TIME)
        repo = lifecycle(store)
        repo.put_version(parent)
        for batch in (records[:1], records[::-1], [records[0], records[0]]):
            with pytest.raises(ValueError):
                repo.put_review(batch, child)
            assert repo.get_version(child.reference) is None
            assert repo.get_review(records[0].review_id) is None
        repo.put_review(records, child)
        assert repo.get_version(child.reference) == child


class TestMemorySQLiteTransactionFaults(TransactionFaultContract):
    @pytest.fixture(params=['memory', 'sqlite'])
    def store(self, request, tmp_path):
        return InMemoryRunStore() if request.param == 'memory' else SQLiteRunStore(tmp_path / 'cal.db')


def test_storage_only_import_and_construction_without_evaluator(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    code = '''
import importlib.abc, sys
class NoEvaluator(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.startswith(('motte_eval', 'motte_sdk')):
            raise AssertionError('eager dependency: ' + fullname)
sys.meta_path.insert(0, NoEvaluator())
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore
for store in (InMemoryRunStore(), SQLiteRunStore(sys.argv[1])):
    assert list(store.calibrations.iter_records()) == []
'''
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path / 'storage.db')],
                            env={**os.environ, 'PYTHONPATH': os.pathsep.join([
                                str(root / 'packages/storage'), str(root / 'packages/contracts')])},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('table,values', [
    ('versions', ('cal', '1', 'digest', 'raw')),
    ('reviews', ('review', 'cal', '1', '2', 'raw')),
    ('executions', ('exec', 'request', 'fingerprint', 'cal', '1', 'digest', 'raw')),
    ('reports', ('report', 'exec', 'digest', 'raw')),
    ('qualifications', ('qual', 'report', 'digest', 'raw')),
])
def test_every_provenance_table_blocks_downgrade(tmp_path, table, values):
    engine = create_engine('sqlite:///' + str(tmp_path / 'migration.db'))
    try:
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration().upgrade()
            connection.exec_driver_sql('INSERT INTO judge_calibration_' + table + ' VALUES ('
                                       + ','.join('?' for _ in values) + ')', values)
            with pytest.raises(RuntimeError, match='judge_calibration_' + table):
                migration().downgrade()
            assert connection.exec_driver_sql('SELECT * FROM judge_calibration_' + table).fetchone() == values
    finally:
        engine.dispose()


def test_all_calibration_tables_are_guarded_before_maintenance(tmp_path):
    from motte_storage.calibrations import TABLES
    from motte_storage.maintenance import begin_maintenance, end_maintenance
    store = SQLiteRunStore(tmp_path / 'guard.db')
    lease = begin_maintenance(store)
    try:
        with sqlite3.connect(tmp_path / 'guard.db') as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            for table in TABLES:
                for operation in ('INSERT', 'UPDATE', 'DELETE'):
                    assert f'motte_maintenance_{table}_{operation}' in names
    finally:
        end_maintenance(store, owner=lease['owner'])


def test_schema_fingerprint_covers_all_lifecycle_tables():
    from motte_storage.calibrations import TABLES
    from motte_storage.run_store import _SCHEMA
    with sqlite3.connect(':memory:') as connection:
        connection.executescript(_SCHEMA)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(TABLES).issubset(tables)


@pytest.mark.parametrize('transaction', ['implicit', 'deferred', 'write', 'savepoint'])
def test_sqlite_downgrade_excludes_writers_after_counts(tmp_path, monkeypatch, transaction):
    path = tmp_path / 'downgrade-race.db'
    engine = create_engine('sqlite:///' + str(path))
    module = migration()
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
                    writer.execute("INSERT INTO judge_calibration_versions VALUES ('cal', '1', 'digest', 'evidence')")
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
        assert not inspect(engine).has_table('judge_calibration_versions')
    finally:
        engine.dispose()


@pytest.mark.parametrize('transaction', ['implicit', 'deferred', 'write', 'savepoint'])
def test_sqlite_downgrade_preserves_caller_rollback(tmp_path, transaction):
    engine = create_engine('sqlite:///' + str(tmp_path / 'rollback.db'))
    module = migration()
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
            assert set(module._TABLE_COLUMNS).issubset(inspect(connection).get_table_names())
            assert connection.exec_driver_sql('SELECT * FROM prior_evidence').fetchall() == []
    finally:
        engine.dispose()


def publish_parentless_pass(store, *, location, digest=HASH):
    from tests.storage.test_scoring_jobs import pass_record, score_row
    repo, execution, children = prepare(store)
    repo.submit_execution(execution, children)
    child = children[0]
    jobs = scoring_jobs_for(store)
    claimed = jobs.claim(child['job_id'])
    record = {**pass_record(child['reserved_pass_id'], child['run_id']), 'job_id': child['job_id']}
    scores = [score_row()]
    target = record if location == 'pass' else scores[0]
    target['evidence'] = {'artifact_id': 'fixture/' + location + '-only.bin', 'sha256': digest,
                          'source_run_id': 'independent-source-run'}
    jobs.publish(child['job_id'], expected_revision=claimed['revision'], pass_record=record, scores=scores)
    assert store.runs.list() == []
    assert store.scoring_passes.get(child['reserved_pass_id']) is not None
    return child


class CalibrationEvidenceContract:
    @pytest.mark.parametrize('location', ['pass', 'score'])
    def test_parentless_child_pass_and_scores_are_independent_pins(self, store, location):
        from motte_storage.artifact_refs import referenced_artifact_hashes, referenced_run_ids
        child = publish_parentless_pass(store, location=location)
        # Explicit rollback exclusions cannot retire this independently owned evidence.
        refs = referenced_artifact_hashes(store, exclude_run_ids=[child['run_id']])
        assert refs['fixture/' + location + '-only.bin'] == HASH
        assert 'independent-source-run' in referenced_run_ids(store, exclude_run_ids=[child['run_id']])
        assert store.runs.list() == []

    @pytest.mark.parametrize('reader', ['invocations', 'scoring_passes', 'score_sets'])
    @pytest.mark.parametrize('failure', ['facade_missing', 'method_missing', 'not_callable', 'raises'])
    def test_parentless_calibration_required_readers_fail_closed(self, store, monkeypatch, reader, failure):
        from types import SimpleNamespace
        from motte_storage.artifact_refs import referenced_artifact_hashes
        from tests.storage.test_scoring_jobs import invocation_record
        child = publish_parentless_pass(store, location='pass')
        invocation = invocation_record(owner={**child['owner'], 'sample_id': 'sample-1'})
        invocation['job_id'] = child['job_id']
        invocation['request_summary'] = {'artifact_id': 'fixture/invocation-only.bin'}
        store.invocations.create(invocation)
        assert 'fixture/invocation-only.bin' in referenced_artifact_hashes(store)
        method = {'invocations': 'list_for_job', 'scoring_passes': 'get',
                  'score_sets': 'list_for_pass'}[reader]
        repository = getattr(store, reader)
        if failure == 'facade_missing':
            monkeypatch.setattr(store, reader, None)
        elif failure == 'method_missing':
            facade = SimpleNamespace(**{name: getattr(repository, name) for name in ('get', 'list_for_run')
                                        if name != method and hasattr(repository, name)})
            monkeypatch.setattr(store, reader, facade)
        elif failure == 'not_callable':
            monkeypatch.setattr(repository, method, None)
        else:
            def broken(*args, **kwargs):
                raise RuntimeError('synthetic required reader failure')
            monkeypatch.setattr(repository, method, broken)
        with pytest.raises((AttributeError, RuntimeError), match='required|synthetic'):
            referenced_artifact_hashes(store)


class TestMemorySQLiteCalibrationEvidence(CalibrationEvidenceContract):
    @pytest.fixture(params=['memory', 'sqlite'])
    def store(self, request, tmp_path):
        return InMemoryRunStore() if request.param == 'memory' else SQLiteRunStore(tmp_path / 'cal.db')


@pytest.mark.parametrize('location', ['pass', 'score'])
def test_calibration_pass_evidence_survives_gc_recheck_and_backup(tmp_path, location):
    from hashlib import sha256
    from motte_storage.gc import apply_gc, plan_gc
    from motte_storage.maintenance import consistent_backup, restore_staging
    store = SQLiteRunStore(tmp_path / 'pins.db')
    root = tmp_path / 'artifacts'
    path = root / 'fixture' / (location + '-only.bin')
    path.parent.mkdir(parents=True)
    path.write_bytes(b'synthetic parentless calibration evidence')
    planned_before_pin = plan_gc(store, root, artifact_ttl_days=0)
    assert [row['artifact_id'] for row in planned_before_pin.deletable] == [path.relative_to(root).as_posix()]
    publish_parentless_pass(store, location=location, digest='sha256:' + sha256(path.read_bytes()).hexdigest())
    result = apply_gc(store, root, planned_before_pin, confirm=True)
    assert result['deleted'] == 0
    assert path.is_file()
    manifest = consistent_backup(store, tmp_path / 'backup', artifacts_root=root)
    assert manifest['status'] == 'complete'
    restored = restore_staging(tmp_path / 'backup', tmp_path / 'restore')
    restored_store = SQLiteRunStore(restored['database'])
    from motte_storage.artifact_refs import referenced_artifact_hashes
    assert path.relative_to(root).as_posix() in referenced_artifact_hashes(restored_store)
    assert restored_store.runs.list() == []


@pytest.mark.parametrize('reader,method', [('invocations', 'list_for_job'),
                                          ('scoring_passes', 'get'), ('score_sets', 'list_for_pass')])
def test_missing_calibration_reader_aborts_gc_apply(tmp_path, monkeypatch, reader, method):
    from motte_storage.gc import apply_gc, plan_gc
    store = SQLiteRunStore(tmp_path / 'pins.db')
    root = tmp_path / 'artifacts'
    root.mkdir()
    path = root / 'unrelated-orphan.bin'
    path.write_bytes(b'synthetic orphan must survive an incomplete scan')
    plan = plan_gc(store, root, artifact_ttl_days=0)
    assert len(plan.deletable) == 1
    publish_parentless_pass(store, location='pass')
    monkeypatch.setattr(getattr(store, reader), method, None)
    with pytest.raises(AttributeError, match='required'):
        apply_gc(store, root, plan, confirm=True)
    assert path.is_file()


def test_sqlite_downgrade_retains_the_callers_savepoint(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'savepoint.db'))
    module = migration()
    try:
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            connection.exec_driver_sql('CREATE TABLE prior_evidence (id TEXT PRIMARY KEY)')
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            connection.exec_driver_sql("INSERT INTO prior_evidence VALUES ('outer transaction')")
            savepoint = connection.begin_nested()
            module.downgrade()
            savepoint.rollback()
            assert set(module._TABLE_COLUMNS).issubset(inspect(connection).get_table_names())
        with engine.connect() as connection:
            assert connection.exec_driver_sql('SELECT * FROM prior_evidence').fetchall() == [('outer transaction',)]
    finally:
        engine.dispose()
