"""Transactional interactive sessions and command dispatch, shared by all stores.

Lock order is Run -> session -> command. A committed dispatch intent can never
be returned to queued: the process write is deliberately outside the transaction.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .integrity import COMMAND_TRANSITIONS, RunConflictError, advance_record, new_record, validate_event

SESSION_IDENTITIES = frozenset({
    'session_id', 'run_id', 'case_id', 'attempt_id', 'operation_id', 'start_token',
    'worker_token', 'config_hash', 'parser_version', 'created_at', 'revision',
    'control_revision',
})
COMMAND_IDENTITIES = frozenset({
    'session_id', 'expected_session_revision', 'dedupe_key', 'intent_hash', 'request_hash',
    'payload', 'type', 'content', 'actor', 'expires_at', 'created_at', 'intervention',
})


def deadline(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise RunConflictError('invalid command/approval deadline') from error
    if parsed.tzinfo is None:
        raise RunConflictError('deadline must have a timezone')
    return parsed


class Transactions:
    def __init__(self, store: Any, original: Any) -> None:
        self.store, self.original = store, original
        self.path = getattr(original, '_path', None)
        self.dsn = getattr(original, '_dsn', None)
        self.sessions: dict[str, dict] = {}

    @contextmanager
    def open(self):
        if self.path is None and self.dsn is None:
            with self.original._lock:
                yield Transaction(self, None)
            return
        if self.dsn:
            from .postgres import _connect
            with _connect(self.dsn) as connection:
                yield Transaction(self, connection)
        else:
            from .run_store import _connect
            connection = _connect(self.path)
            try:
                with connection:
                    connection.execute('BEGIN IMMEDIATE')
                    yield Transaction(self, connection)
            finally:
                connection.close()


class Transaction:
    def __init__(self, owner: Transactions, connection: Any) -> None:
        self.owner, self.connection = owner, connection

    def sql(self, query: str, args=()):
        return self.connection.execute(query.replace('?', '%s') if self.owner.dsn else query, args)

    def payload(self, record):
        if self.owner.dsn:
            from psycopg.types.json import Json
            return Json(record)
        return json.dumps(record, sort_keys=True)

    def rows(self, table):
        if table == 'runtime_sessions':
            return self.owner.sessions
        if table == 'runs':
            return self.owner.store.runs._runs
        return self.owner.original._rows

    def get(self, table: str, identity: str):
        if self.connection is None:
            return deepcopy(self.rows(table).get(identity))
        key = 'session_id' if table == 'runtime_sessions' else 'id'
        suffix = ' FOR UPDATE' if self.owner.dsn else ''
        row = self.sql(f'SELECT payload FROM {table} WHERE {key} = ?{suffix}', (identity,)).fetchone()
        if not row:
            return None
        return deepcopy(row[0]) if isinstance(row[0], dict) else json.loads(row[0])

    def list(self, table: str, run_id: str):
        if self.connection is None:
            return deepcopy([r for r in self.rows(table).values() if r['run_id'] == run_id])
        rows = self.sql(f'SELECT payload FROM {table} WHERE run_id = ?', (run_id,)).fetchall()
        return [deepcopy(r[0]) if isinstance(r[0], dict) else json.loads(r[0]) for r in rows]

    def put(self, table: str, record: dict, *, create=False):
        key = 'session_id' if table == 'runtime_sessions' else 'id'
        if self.connection is None:
            if create and record[key] in self.rows(table):
                raise RunConflictError(f'{table} already exists')
            self.rows(table)[record[key]] = deepcopy(record)
            return
        state = 'state' if table == 'runtime_sessions' else 'status'
        if create:
            self.sql(f'INSERT INTO {table} ({key},run_id,{state},revision,payload) VALUES (?,?,?,?,?)',
                (record[key], record['run_id'], record[state], record['revision'], self.payload(record)))
        else:
            self.sql(f'UPDATE {table} SET {state}=?,revision=?,payload=? WHERE {key}=?',
                (record[state], record['revision'], self.payload(record), record[key]))

    def audit(self, event, run_id):
        pending = validate_event(event, run_id)
        if pending is None:
            return
        if self.connection is None:
            self.owner.store.events.append(pending)
        elif self.owner.dsn:
            from .postgres import _append_event
            with self.connection.cursor() as cursor:
                _append_event(cursor, pending)
        else:
            from .run_store import _append_event
            _append_event(self.connection, pending)


class RuntimeSessions:
    def __init__(self, transactions: Transactions):
        self.transactions = transactions

    def create(self, record):
        stored = deepcopy(record)
        for key in ('session_id', 'run_id', 'case_id', 'attempt_id', 'operation_id', 'start_token', 'worker_token'):
            if not isinstance(stored.get(key), str) or not stored[key]:
                raise ValueError(f'session requires {key}')
        stored.setdefault('revision', 1)
        stored.setdefault('control_revision', 1)
        stored.setdefault('state', 'prepared')
        stored.setdefault('pending_approvals', {})
        stored.setdefault('created_at', datetime.now(UTC).isoformat())
        if stored['revision'] != 1:
            raise ValueError('new session must have revision 1')
        with self.transactions.open() as tx:
            if tx.get('runs', stored['run_id']) is None:
                raise RunConflictError('session run missing')
            if tx.get('runtime_sessions', stored['session_id']) is not None:
                raise RunConflictError('session already exists')
            tx.put('runtime_sessions', stored, create=True)
        return stored

    def get(self, session_id):
        with self.transactions.open() as tx:
            return tx.get('runtime_sessions', session_id)

    def list_for_run(self, run_id):
        with self.transactions.open() as tx:
            return tx.list('runtime_sessions', run_id)

    def transition(self, session_id, *, expected_revision, expected_state, changes):
        if SESSION_IDENTITIES.intersection(changes):
            raise ValueError('session identity is immutable')
        with self.transactions.open() as tx:
            previous = tx.get('runtime_sessions', session_id)
            if previous is None or previous['revision'] != expected_revision or previous['state'] != expected_state:
                raise RunConflictError('session revision/state changed')
            if previous['state'] in {'terminal', 'abandoned'}:
                raise RunConflictError('session is closed')
            if changes.get('state', previous['state']) not in {'prepared', 'starting', 'active', 'stopping', 'terminal', 'abandoned'}:
                raise ValueError('unknown runtime session state')
            for key in ('native_thread_id', 'active_turn_id'):
                if previous.get(key) and key in changes and changes[key] != previous[key]:
                    raise ValueError('native session identity is immutable once bound')
            if 'pending_approvals' in changes:
                mutable = {'state', 'claimed_command_id', 'resolution_evidence'}
                for key, approval in previous.get('pending_approvals', {}).items():
                    incoming = changes['pending_approvals'].get(key)
                    if incoming is None or {
                        k: v for k, v in approval.items() if k not in mutable
                    } != {k: v for k, v in incoming.items() if k not in mutable}:
                        raise ValueError('approval identity/action is immutable')
                    if approval.get('state') in {'resolved', 'expired', 'abandoned'} and incoming.get('state') != approval['state']:
                        raise ValueError('closed approval cannot reopen')
            stored = {**previous, **deepcopy(changes), 'revision': expected_revision + 1}
            if any(k in changes and changes[k] != previous.get(k) for k in (
                'state', 'native_thread_id', 'active_turn_id', 'pending_approvals',
            )):
                stored['control_revision'] = previous['control_revision'] + 1
            tx.put('runtime_sessions', stored)
        return stored


def check_binding(run, session, command, now):
    if not run or run.get('status') != 'running' or (
        run.get('cancellation') and command.get('type') != 'interrupt'
    ):
        raise RunConflictError('run is not active or is cancelling')
    if not session or session.get('state') != 'active':
        raise RunConflictError('session is not active')
    if any(command.get(key) != session.get(key) for key in ('run_id', 'case_id', 'session_id')):
        raise RunConflictError('command session binding mismatch')
    if command.get('expected_session_revision') != session.get('control_revision'):
        raise RunConflictError('session control revision mismatch')
    if now >= deadline(command.get('expires_at')):
        raise RunConflictError('command expired')
    if command.get('type') in {'approve', 'reject'}:
        payload = command.get('payload') or {}
        approval = session.get('pending_approvals', {}).get(payload.get('approval_id'))
        if not approval or approval.get('state') != 'pending':
            raise RunConflictError('approval is not pending')
        if payload.get('request_hash') != approval.get('request_hash'):
            raise RunConflictError('approval request hash mismatch')
        if now >= deadline(approval.get('expires_at')):
            raise RunConflictError('approval expired')


class InteractiveCommands:
    def __init__(self, transactions: Transactions):
        self.transactions = transactions

    def create(self, record):
        # Low-level legacy import interface. The public path uses create_or_get.
        return self.transactions.original.create(record)

    def get(self, command_id):
        with self.transactions.open() as tx:
            return tx.get('run_commands', command_id)

    def list_for_run(self, run_id):
        with self.transactions.open() as tx:
            return tx.list('run_commands', run_id)

    list = list_for_run

    def create_or_get(self, record, *, event=None):
        stored = new_record(record, 'command', 'queued')
        if not stored.get('dedupe_key') or not stored.get('intent_hash'):
            raise ValueError('command requires dedupe key and canonical intent hash')
        pending = validate_event(event, stored['run_id'])
        with self.transactions.open() as tx:
            run = tx.get('runs', stored['run_id'])
            # Run row serializes same-key inserts across PostgreSQL connections.
            for existing in tx.list('run_commands', stored['run_id']):
                if existing.get('dedupe_key') == stored['dedupe_key']:
                    if existing.get('intent_hash') != stored['intent_hash']:
                        raise RunConflictError('dedupe key has a different command intent')
                    return existing, False
            session = tx.get('runtime_sessions', stored.get('session_id'))
            check_binding(run, session, stored, datetime.now(UTC))
            if tx.get('run_commands', stored['id']) is not None:
                raise RunConflictError('command already exists')
            tx.put('run_commands', stored, create=True)
            tx.audit(pending, stored['run_id'])
        return stored, True

    def claim(self, command_id, *, expected_revision, session_id,
              expected_control_revision, worker_token, now, event=None):
        reference = self.get(command_id)
        if not reference:
            raise RunConflictError('command missing')
        with self.transactions.open() as tx:
            run = tx.get('runs', reference['run_id'])
            session = tx.get('runtime_sessions', session_id)
            command = tx.get('run_commands', command_id)
            check_binding(run, session, command, now)
            if session['worker_token'] != worker_token or session['control_revision'] != expected_control_revision:
                raise RunConflictError('session owner/revision changed')
            stored = advance_record(command, expected_revision=expected_revision,
                expected_status='queued', status='delivered', changes={
                    'delivered_at': now.isoformat(), 'execution_token': uuid4().hex,
                    'worker_token': worker_token,
                }, transitions=COMMAND_TRANSITIONS)
            pending = validate_event(event or {'type': 'command_delivered', 'command_id': command_id}, command['run_id'])
            if command['type'] in {'approve', 'reject'}:
                approval = session['pending_approvals'][command['payload']['approval_id']]
                approval.update(state='claimed', claimed_command_id=command_id)
                session['revision'] += 1
                # Reservation does not invalidate other pending action identities.
                tx.put('runtime_sessions', session)
            tx.put('run_commands', stored)
            tx.audit(pending, command['run_id'])
        return stored

    def transition(self, command_id, *, expected_revision, expected_status, status, changes=None, event=None):
        if COMMAND_IDENTITIES.intersection(changes or {}):
            raise ValueError('command intent/binding is immutable')
        reference = self.get(command_id)
        if not reference:
            raise RunConflictError('command missing')
        with self.transactions.open() as tx:
            tx.get('runs', reference['run_id'])
            previous = tx.get('run_commands', command_id)
            stored = advance_record(previous, expected_revision=expected_revision,
                expected_status=expected_status, status=status, changes=changes,
                transitions=COMMAND_TRANSITIONS)
            pending = validate_event(event or {'type': f'command_{status}', 'command_id': command_id}, stored['run_id'])
            tx.put('run_commands', stored)
            tx.audit(pending, stored['run_id'])
        return stored


def attach_interactive_repositories(store):
    transactions = Transactions(store, store.commands)
    store.runtime_sessions = RuntimeSessions(transactions)
    store.commands = InteractiveCommands(transactions)
