"""Immutable calibration sources with atomic review, job-group and report writes.

Evaluator contracts are loaded only when calibration data is used. Constructing
or importing a storage-only installation does not require motte-eval or the SDK.
Canonical payload TEXT is deliberately identical on SQLite and PostgreSQL.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from copy import deepcopy
import json
from typing import Any, TYPE_CHECKING

from motte_contracts.hashing import canonical_json

if TYPE_CHECKING:
    from motte_eval.calibration_records import (
        CalibrationExecution, CalibrationQualificationSource, CalibrationRef,
        CalibrationReportRecord, CalibrationVersion, HumanReviewRecord,
    )


class CalibrationConflict(ValueError):
    """An immutable identity or request key was reused for different content."""


class CalibrationCorrupt(ValueError):
    """Stored data or its indexed identity no longer proves its source chain."""


# Columns other than payload must match exactly on every read, including lists.
_COLUMNS = {
    'versions': ('calibration_id', 'version', 'content_sha256'),
    'reviews': ('review_id', 'calibration_id', 'parent_version', 'child_version'),
    'executions': ('execution_id', 'request_key', 'request_fingerprint', 'calibration_id',
                   'version', 'content_sha256'),
    'reports': ('report_id', 'execution_id', 'content_sha256'),
    'qualifications': ('qualification_id', 'report_id', 'content_sha256'),
}
_KEYS = {'versions': 2, 'reviews': 1, 'executions': 1, 'reports': 1, 'qualifications': 1}
_MODELS = {'versions': 'CalibrationVersion', 'reviews': 'HumanReviewRecord',
           'executions': 'CalibrationExecution', 'reports': 'CalibrationReportRecord',
           'qualifications': 'CalibrationQualificationSource'}
TABLES = tuple('judge_calibration_' + name for name in _COLUMNS)
_SCHEMA = '\n'.join(
    'CREATE TABLE IF NOT EXISTS judge_calibration_' + kind + ' ('
    + ', '.join(column + ' TEXT NOT NULL' for column in columns)
    + ', payload TEXT NOT NULL, PRIMARY KEY (' + ', '.join(columns[:_KEYS[kind]]) + ')'
    + (', UNIQUE (request_key)' if kind == 'executions' else '') + ');'
    for kind, columns in _COLUMNS.items()
)


def _model(kind, value):
    from motte_eval import calibration_records

    cls = getattr(calibration_records, _MODELS[kind])
    return cls.model_validate(value.model_dump(mode='json') if hasattr(value, 'model_dump') else value)


def _indices(kind, record):
    if kind == 'versions':
        return (record.calibration.calibration_id, record.calibration.version, record.content_sha256)
    if kind == 'reviews':
        return (record.review_id, record.parent_ref.calibration_id,
                record.parent_ref.version, record.child_ref.version)
    if kind == 'executions':
        return (record.execution_id, record.request_key, record.request_fingerprint,
                record.version.calibration.calibration_id, record.version.calibration.version,
                record.content_sha256)
    if kind == 'reports':
        return (record.report_id, record.execution_id, record.content_sha256)
    return (record.binding.qualification_id, record.binding.report_id, record.content_sha256)


def _row(kind, record):
    record = _model(kind, record)
    return (*_indices(kind, record), canonical_json(record.model_dump(mode='json')))


def _decode(kind, row, *, key=None):
    try:
        record = _model(kind, json.loads(row[-1]))
        if tuple(row[:-1]) != _indices(kind, record):
            raise ValueError('stored index disagrees with envelope')
        if key is not None and tuple(row[:_KEYS[kind]]) != tuple(key):
            raise ValueError('stored identity disagrees with repository key')
        if canonical_json(record.model_dump(mode='json')) != row[-1]:
            raise ValueError('stored payload is not canonical TEXT')
        return record
    except (ValueError, TypeError, KeyError, RecursionError) as error:
        raise CalibrationCorrupt('corrupt calibration ' + kind) from error


def _identity(kind, record):
    if kind == 'versions':
        return canonical_json(record.model_dump(mode='json'))
    if kind == 'reviews':
        return canonical_json(record.model_dump(mode='json', exclude={'recorded_at'}))
    return canonical_json(record.identity_payload())


@contextmanager
def calibration_publication_guard(store):
    """Hold from evidence validation through pin publication, excluding GC/rollback.

    Services reconstructing ledgers should use this outer guard as well as the
    repository's own transaction. The same maintenance advisory/file lock is
    held in shared mode; maintenance takes it exclusively before any deletion.
    """
    from .maintenance import maintenance_status
    from .operation_locks import MaintenanceConflict, file_lock

    dsn = getattr(store, 'dsn', None)
    path = getattr(getattr(store, 'runs', None), '_path', None)
    if dsn:
        from .postgres import _connect

        with _connect(dsn) as connection:
            acquired = connection.execute(
                "SELECT pg_try_advisory_xact_lock_shared(hashtext(%s))",
                ('motteavl:maintenance',),
            ).fetchone()[0]
            if not acquired or maintenance_status(store)['active']:
                raise MaintenanceConflict('maintenance blocks calibration publication')
            yield
    elif path:
        from pathlib import Path

        with file_lock(str(Path(path).resolve()) + '.maintenance.lock',
                       label='maintenance', shared=True):
            if maintenance_status(store)['active']:
                raise MaintenanceConflict('maintenance blocks calibration publication')
            yield
    else:
        with store.runs._lock:
            yield


class _Calibrations:
    def _bind(self, store):
        self._store = store

    def _get(self, tx, kind, key):
        rows = self._select(tx, kind, dict(zip(_COLUMNS[kind][:_KEYS[kind]], key, strict=True)))
        return _decode(kind, rows[0], key=key) if rows else None

    def _put(self, tx, kind, record):
        proposed = _row(kind, record)
        key = proposed[:_KEYS[kind]]
        existing = self._get(tx, kind, key)
        if existing is None:
            self._insert(tx, kind, proposed)
            existing = self._get(tx, kind, key)
            if existing is None:
                raise CalibrationCorrupt('inserted calibration source is missing')
        if _identity(kind, existing) != _identity(kind, record):
            raise CalibrationConflict('immutable calibration identity conflict: ' + str(key))
        return existing

    def _version(self, tx, ref, seen=None):
        from motte_eval.calibration_records import CalibrationRef, verify_review_chain

        ref = CalibrationRef.model_validate(ref.model_dump() if hasattr(ref, 'model_dump') else ref)
        key = (ref.calibration_id, ref.version)
        record = self._get(tx, 'versions', key)
        if record is None:
            return None
        if record.reference != ref:
            raise CalibrationCorrupt('version digest disagrees with exact source reference')
        seen = set() if seen is None else set(seen)
        if key in seen:
            raise CalibrationCorrupt('cyclic calibration review chain')
        seen.add(key)
        if record.parent_ref is not None:
            parent = self._version(tx, record.parent_ref, seen)
            if parent is None:
                raise CalibrationCorrupt('review parent source is missing')
            new_ids = record.review_ids[len(parent.review_ids):]
            reviews = [self._get(tx, 'reviews', (review_id,)) for review_id in new_ids]
            if any(item is None for item in reviews):
                raise CalibrationCorrupt('review source is missing')
            try:
                verify_review_chain(parent, record, reviews)
            except ValueError as error:
                raise CalibrationCorrupt('invalid stored calibration review chain') from error
        return record

    def put_version(self, version: CalibrationVersion) -> CalibrationVersion:
        version = _model('versions', version)
        with self._transaction(write=True) as tx:
            if version.parent_ref is not None:
                existing = self._version(tx, version.reference)
                if existing is None:
                    raise ValueError('reviewed versions require atomic put_review')
            return self._put(tx, 'versions', version)

    def get_version(self, ref: CalibrationRef) -> CalibrationVersion | None:
        with self._transaction() as tx:
            return self._version(tx, ref)

    def list_versions(self, calibration_id: str) -> list[CalibrationVersion]:
        with self._transaction() as tx:
            return [self._version(tx, _decode('versions', row).reference)
                    for row in self._select(tx, 'versions', {'calibration_id': calibration_id})]

    def put_review(self, review: HumanReviewRecord | list[HumanReviewRecord],
                   child_version: CalibrationVersion) -> None:
        from motte_eval.calibration_records import verify_review_chain

        reviews = [_model('reviews', value) for value in (review if isinstance(review, list) else [review])]
        child = _model('versions', child_version)
        if not reviews or child.parent_ref is None:
            raise ValueError('complete review transition is required')
        with self._transaction(write=True) as tx:
            parent = self._version(tx, child.parent_ref)
            if parent is None:
                raise ValueError('review parent source is missing')
            verify_review_chain(parent, child, reviews)
            for record in reviews:
                self._put(tx, 'reviews', record)
            self._put(tx, 'versions', child)

    def get_review(self, review_id: str) -> HumanReviewRecord | None:
        with self._transaction() as tx:
            record = self._get(tx, 'reviews', (review_id,))
            if record is not None:
                child = self._version(tx, record.child_ref)
                if child is None or review_id not in child.review_ids:
                    raise CalibrationCorrupt('review child source is missing')
            return record

    def _execution(self, tx, execution_id):
        record = self._get(tx, 'executions', (execution_id,))
        if record is not None:
            version = self._version(tx, record.version.reference)
            if version is None or version != record.version:
                raise CalibrationCorrupt('execution version source is missing or inconsistent')
            self._verify_children(tx, record)
        return record

    def get_execution(self, execution_id: str) -> CalibrationExecution | None:
        with self._transaction() as tx:
            return self._execution(tx, execution_id)

    def get_execution_by_key(self, request_key: str) -> CalibrationExecution | None:
        with self._transaction() as tx:
            rows = self._select(tx, 'executions', {'request_key': request_key})
            if not rows:
                return None
            record = _decode('executions', rows[0])
            if record.request_key != request_key:
                raise CalibrationCorrupt('execution request index mismatch')
            return self._execution(tx, record.execution_id)

    def list_executions(self, calibration_id: str) -> list[CalibrationExecution]:
        with self._transaction() as tx:
            return [self._execution(tx, _decode('executions', row).execution_id)
                    for row in self._select(tx, 'executions', {'calibration_id': calibration_id})]

    def _verify_children(self, tx, execution):
        from .scoring_jobs import validate_scoring_job

        for job_id in execution.child_job_ids:
            job = self._job(tx, job_id)
            if job is not None:
                try:
                    job = validate_scoring_job(job)
                except ValueError as error:
                    raise CalibrationCorrupt('corrupt execution child job') from error
            if (job is None or job.get('job_id') != job_id
                    or job['owner'].get('kind') != 'calibration'
                    or job['owner'].get('calibration_job_id') != execution.execution_id):
                raise CalibrationCorrupt('execution child job is missing or has wrong ownership')

    def submit_execution(self, execution: CalibrationExecution,
                         jobs: list[dict]) -> tuple[CalibrationExecution, bool]:
        from .scoring_jobs import validate_scoring_job

        execution = _model('executions', execution)
        with self._transaction(write=True) as tx:
            existing = self._select(tx, 'executions', {'request_key': execution.request_key})
            if existing:
                first = _decode('executions', existing[0])
                first = self._execution(tx, first.execution_id)
                if first.request_fingerprint != execution.request_fingerprint:
                    raise CalibrationConflict('request fingerprint conflict')
                return first, False
            version = self._version(tx, execution.version.reference)
            if version is None or version != execution.version:
                raise ValueError('execution version source is missing or inconsistent')
            children = [validate_scoring_job(job) for job in jobs]
            if [job['job_id'] for job in children] != execution.child_job_ids:
                raise ValueError('child jobs must exactly match the frozen execution IDs')
            for child in children:
                if (child['owner'].get('kind') != 'calibration'
                        or child['owner'].get('calibration_job_id') != execution.execution_id):
                    raise ValueError('child job requires calibration execution ownership')
                if child['status'] != 'queued' or child['revision'] != 1 or child['calls']:
                    raise ValueError('new child jobs must be undispatched queued jobs')
                self._insert_job(tx, child)
            record = self._put(tx, 'executions', execution)
            self._verify_children(tx, record)
            return record, True

    def _report(self, tx, report_id):
        record = self._get(tx, 'reports', (report_id,))
        if record is not None:
            execution = self._execution(tx, record.execution_id)
            if execution is None:
                raise CalibrationCorrupt('report execution source is missing')
            try:
                record.verify_execution(execution)
            except ValueError as error:
                raise CalibrationCorrupt('report execution source disagrees') from error
        return record

    def publish_report(self, report: CalibrationReportRecord,
                       qualification: CalibrationQualificationSource | None) -> CalibrationReportRecord:
        report = _model('reports', report)
        qualification = _model('qualifications', qualification) if qualification is not None else None
        with self._transaction(write=True) as tx:
            execution = self._execution(tx, report.execution_id)
            if execution is None:
                raise ValueError('report execution source is missing')
            report.verify_execution(execution)
            if qualification is not None:
                qualification.verify_report(report)
            record = self._put(tx, 'reports', report)
            if qualification is not None:
                self._put(tx, 'qualifications', qualification)
            return record

    def get_report(self, report_id: str) -> CalibrationReportRecord | None:
        with self._transaction() as tx:
            return self._report(tx, report_id)

    def list_reports(self, execution_id: str) -> list[CalibrationReportRecord]:
        with self._transaction() as tx:
            return [self._report(tx, _decode('reports', row).report_id)
                    for row in self._select(tx, 'reports', {'execution_id': execution_id})]

    def _qualification(self, tx, qualification_id):
        record = self._get(tx, 'qualifications', (qualification_id,))
        if record is not None:
            report = self._report(tx, record.binding.report_id)
            if report is None:
                raise CalibrationCorrupt('qualification report source is missing')
            try:
                record.verify_report(report)
            except ValueError as error:
                raise CalibrationCorrupt('qualification report source disagrees') from error
        return record

    def get_qualification(self, qualification_id: str) -> CalibrationQualificationSource | None:
        with self._transaction() as tx:
            return self._qualification(tx, qualification_id)

    def iter_records(self):
        """Detached, unbounded full evidence enumeration; read failures propagate."""
        with self._transaction() as tx:
            records = []
            for kind in _COLUMNS:
                for row in self._select(tx, kind, {}):
                    record = _decode(kind, row)
                    if kind == 'versions':
                        self._version(tx, record.reference)
                    elif kind == 'reviews':
                        child = self._version(tx, record.child_ref)
                        if child is None or record.review_id not in child.review_ids:
                            raise CalibrationCorrupt('review child source is missing')
                    elif kind == 'executions':
                        self._execution(tx, record.execution_id)
                    elif kind == 'reports':
                        self._report(tx, record.report_id)
                    else:
                        self._qualification(tx, record.binding.qualification_id)
                    records.append(record.model_dump(mode='json'))
            return iter(records)


class MemoryCalibrations(_Calibrations):
    def __init__(self, lock):
        self._lock = lock
        self._rows = {kind: {} for kind in _COLUMNS}

    @contextmanager
    def _transaction(self, write=False):
        with self._lock:
            tx = (deepcopy(self._rows), deepcopy(self._store.scoring_jobs._rows)) if write else (
                self._rows, self._store.scoring_jobs._rows)
            yield tx
            if write:
                self._rows = tx[0]
                self._store.scoring_jobs._rows = tx[1]

    def _select(self, tx, kind, filters):
        found = []
        for key, row in sorted(tx[0][kind].items()):
            _decode(kind, row, key=key)
            if all(row[_COLUMNS[kind].index(name)] == value for name, value in filters.items()):
                found.append(row)
        return found

    def _insert(self, tx, kind, row):
        tx[0][kind][row[:_KEYS[kind]]] = row

    def _job(self, tx, job_id):
        return tx[1].get(job_id)

    def _insert_job(self, tx, job):
        if (job['job_id'] in tx[1]
                or any(value['request_key'] == job['request_key'] for value in tx[1].values())):
            raise CalibrationConflict('child scoring job already exists')
        tx[1][job['job_id']] = deepcopy(job)


class SQLiteCalibrations(_Calibrations):
    def __init__(self, path):
        self._path = str(path)
        self._placeholder = '?'

    @contextmanager
    def _transaction(self, write=False):
        from contextlib import nullcontext
        import sqlite3

        with calibration_publication_guard(self._store) if write else nullcontext():
            with closing(sqlite3.connect(self._path, isolation_level=None, timeout=10)) as connection:
                with connection:
                    connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                    yield connection

    def _select(self, tx, kind, filters):
        columns = (*_COLUMNS[kind], 'payload')
        statement = 'SELECT ' + ', '.join(columns) + ' FROM judge_calibration_' + kind
        if filters:
            statement += ' WHERE ' + ' AND '.join(name + ' = ' + self._placeholder for name in filters)
        statement += ' ORDER BY ' + ', '.join(_COLUMNS[kind][:_KEYS[kind]])
        return tx.execute(statement, tuple(filters.values())).fetchall()

    def _insert(self, tx, kind, row):
        columns = (*_COLUMNS[kind], 'payload')
        statement = ('INSERT INTO judge_calibration_' + kind + ' (' + ', '.join(columns)
                     + ') VALUES (' + ', '.join([self._placeholder] * len(columns)) + ')')
        tx.execute(statement, row)

    def _job(self, tx, job_id):
        row = tx.execute('SELECT job_id, request_key, fingerprint, owner_kind, owner_ref, run_id, '
                         'status, revision, reserved_pass_id, payload FROM scoring_jobs WHERE job_id = '
                         + self._placeholder, (job_id,)).fetchone()
        if row is None:
            return None
        from .scoring_jobs import validate_scoring_job

        try:
            raw = row[-1] if isinstance(row[-1], dict) else json.loads(row[-1])
            record = validate_scoring_job(raw)
            names = ('job_id', 'request_key', 'fingerprint', 'owner_kind', 'owner_ref', 'run_id',
                     'status', 'revision', 'reserved_pass_id')
            if tuple(record[name] for name in names) != tuple(row[:-1]) or record['job_id'] != job_id:
                raise ValueError('job index mismatch')
            return record
        except (ValueError, TypeError, KeyError) as error:
            raise CalibrationCorrupt('corrupt execution child job') from error

    def _insert_job(self, tx, job):
        from .scoring_jobs import ScoringJobConflict, insert_scoring_job_in_transaction

        try:
            insert_scoring_job_in_transaction(tx, job, placeholder=self._placeholder)
        except ScoringJobConflict as error:
            raise CalibrationConflict(str(error)) from error
        except Exception as error:
            # An independent normal ScoringJob insert may win a PG unique index
            # after our precheck. The enclosing group transaction still rolls back.
            if getattr(error, 'sqlstate', None) == '23505':
                raise CalibrationConflict('child scoring job already exists') from error
            raise
