"""Durable session-bound commands: receipt, dispatch intent and evidence are distinct."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from motte_contracts.run import RunCommandStatus
from motte_storage.integrity import RunConflictError
from motte_storage.runtime_sessions import deadline

INTERVENTION_TYPES = frozenset({'user_message', 'approve', 'reject', 'interrupt'})


class CommandError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def request_hash_of(payload):
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return 'sha256:' + hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def submit_command(service: Any, run_id: str, *, kind='user_message', content=None,
                   payload=None, case_id=None, session_id=None, dedupe_key=None,
                   expires_at=None, expected_session_revision=None, actor='anonymous-local'):
    if kind not in INTERVENTION_TYPES:
        raise CommandError('COMMAND_INVALID', 'unsupported command kind')
    if not all(isinstance(x, str) and 0 < len(x) <= 256 for x in (case_id, session_id, dedupe_key)):
        raise CommandError('COMMAND_BINDING_REQUIRED', 'case, session and dedupe key are required')
    if type(expected_session_revision) is not int or expected_session_revision < 1:
        raise CommandError('COMMAND_BINDING_REQUIRED', 'expected session revision is required')
    payload = dict(payload or {})
    if kind == 'user_message':
        if not isinstance(content, str) or not content.strip() or len(content) > 16000 or payload:
            raise CommandError('COMMAND_INVALID', 'message requires bounded text and empty payload')
    elif content is not None:
        raise CommandError('COMMAND_INVALID', 'only messages carry content')
    if kind in {'approve', 'reject'}:
        if set(payload) != {'approval_id', 'request_hash'} or not all(
            isinstance(v, str) and 0 < len(v) <= 256 for v in payload.values()
        ):
            raise CommandError('COMMAND_INVALID', 'approval identity and request hash required')
    elif payload:
        raise CommandError('COMMAND_INVALID', 'unexpected command payload')
    now = datetime.now(UTC)
    expires = expires_at or now + timedelta(minutes=5)
    if expires.tzinfo is None or expires > now + timedelta(minutes=10):
        raise CommandError('COMMAND_INVALID', 'command deadline must be timezone-aware and bounded')
    intent = dict(run_id=run_id, type=kind, content=content, payload=payload, case_id=case_id,
        session_id=session_id, dedupe_key=dedupe_key, expected_session_revision=expected_session_revision,
        actor=actor)
    record = {**intent, 'id': f'cmd-{uuid4().hex}', 'status': 'queued',
        'expires_at': expires.isoformat(), 'created_at': now.isoformat(), 'intervention': True,
        'intent_hash': request_hash_of(intent), 'request_hash': payload.get('request_hash')}
    try:
        command, _ = service.store.commands.create_or_get(record, event={
            'type': 'command_submitted', 'command_id': record['id'], 'kind': kind,
            'intent_hash': record['intent_hash'], 'actor': actor, 'intervention': True})
    except RunConflictError as error:
        raise CommandError('COMMAND_CONFLICT', str(error)) from error
    return command


def transition_command(service, command, status, **changes):
    return service.store.commands.transition(command['id'], expected_revision=command['revision'],
        expected_status=command['status'], status=status, changes=changes)


def settle_session_commands(service, run_id, session_id, *, reason):
    for command in service.store.commands.list_for_run(run_id):
        if command.get('session_id') != session_id or command['status'] not in {'queued', 'delivered'}:
            continue
        status = 'delivery_unknown' if command['status'] == 'delivered' else 'rejected'
        if status == 'rejected':
            try:
                if datetime.now(UTC) >= deadline(command.get('expires_at')):
                    status = 'expired'
            except RunConflictError:
                pass
        try:
            transition_command(service, command, status, error={'class': 'delivery', 'message': reason})
        except RunConflictError:
            pass


def recover_delivered_without_ack(service, run_id):
    recovered = []
    for command in service.store.commands.list_for_run(run_id):
        if command['status'] == 'delivered':
            transition_command(service, command, 'delivery_unknown',
                error={'class': 'delivery', 'message': 'worker restarted before durable ack'})
            recovered.append(command['id'])
    return recovered


def recover_interactive_sessions(service, run_id):
    recover_delivered_without_ack(service, run_id)
    sessions = service.store.runtime_sessions
    for session in sessions.list_for_run(run_id):
        if session['state'] not in {'terminal', 'abandoned'}:
            approvals = session.get('pending_approvals', {})
            for approval in approvals.values():
                if approval['state'] in {'pending', 'claimed'}:
                    approval['state'] = 'abandoned'
            sessions.transition(session['session_id'], expected_revision=session['revision'],
                expected_state=session['state'], changes={'state': 'abandoned',
                    'pending_approvals': approvals, 'terminal_at': datetime.now(UTC).isoformat()})
        settle_session_commands(service, run_id, session['session_id'], reason='worker ownership lost; no replay')


def intervention_summary(service, run_id):
    rows = [c for c in service.store.commands.list_for_run(run_id) if c.get('intervention')]
    entries = []
    for command in rows:
        status = command['status']
        effect = ('effective' if status == 'acknowledged' else
                  'possible' if status in {'delivered', 'delivery_unknown'} else 'attempted')
        entries.append({'kind': command['type'], 'intent_hash': command.get('intent_hash'),
            'request_hash': command.get('request_hash'), 'actor': command.get('actor'),
            'case_id': command.get('case_id'), 'status': status, 'effect': effect,
            'ack_kind': (command.get('ack_evidence') or {}).get('kind')})
    entries.sort(key=lambda x: json.dumps(x, sort_keys=True))
    effective = [entry for entry in entries if entry['effect'] != 'attempted']
    return {'count': len(entries), 'kinds': sorted({c['type'] for c in rows}),
        'command_ids': [c['id'] for c in rows], 'entries': entries,
        'evidence_hash': request_hash_of(entries), 'condition_hash': request_hash_of(effective),
        'possible': any(e['effect'] == 'possible' for e in entries)}


def public_sessions(service, run_id):
    from motte_trace.redaction import redact_secrets
    result = []
    for session in service.store.runtime_sessions.list_for_run(run_id):
        view = {key: session.get(key) for key in (
            'session_id', 'run_id', 'case_id', 'attempt_id', 'state', 'revision',
            'control_revision', 'native_thread_id', 'active_turn_id', 'created_at', 'terminal_at',
        )}
        view['pending_approvals'] = [
            {key: approval.get(key) for key in (
                'approval_id', 'method', 'item_id', 'summary', 'request_hash', 'expires_at', 'state',
            )} for approval in session.get('pending_approvals', {}).values()
            if approval['state'] in {'pending', 'claimed'}
        ]
        result.append(redact_secrets(view))
    return result
