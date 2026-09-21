"""One CaseAttempt owns one fresh Codex stdio/thread/turn and durable command loop."""
from __future__ import annotations

import os
import sys
from concurrent.futures import TimeoutError
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from time import monotonic
from uuid import uuid4

from motte_harness.codex_app_server import APPROVAL_METHODS, CodexAppServerTransport, RpcError, typed_id
from motte_harness.parsers.codex_app_server import PARSER_VERSION, AppServerParser
from motte_harness.process import process_identity
from motte_harness.supervisor import SupervisedLimits
from motte_storage.integrity import RunConflictError
from motte_storage.runtime_sessions import deadline
from motte_trace.redaction import redact_secrets

from .cli_runtime import CliRuntimeCaseExecutor, _backend_error
from .commands import request_hash_of, settle_session_commands, transition_command
from .native_config import prepare_native_environment, resolve_credential_env


class CodexAppServerCaseExecutor(CliRuntimeCaseExecutor):
    def __init__(self, run, *, transport_factory=CodexAppServerTransport):
        super().__init__(run, backend='codex-cli')
        self.backend = 'codex-app-server'
        self.transport_factory = transport_factory
        self.session = None
        self.transport = None
        self.parser = None
        self.pending = {}
        self.sent_approvals = {}
        self.cancel_future = None
        self.cancel_started = None
        self.sequence = 0
        self.resolutions = []
        self._credential_values = ()

    def _clean(self, value):
        if isinstance(value, str):
            for secret in self._credential_values:
                value = value.replace(secret, '[REDACTED-CREDENTIAL]')
            return value
        if isinstance(value, dict):
            return {self._clean(key): self._clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._clean(item) for item in value]
        return value

    def _parser_version(self):
        return PARSER_VERSION

    def _preflight(self):
        effective = super()._preflight()
        if effective['expected_version'] != '0.155.1':
            raise _backend_error('RUNTIME_VERSION_DRIFT', 'app-server requires pinned 0.155.1')
        if self.settings.get('sandbox', 'read-only') not in {'read-only', 'workspace-write'}:
            raise _backend_error('RUNTIME_PROFILE_INVALID', 'app-server requires read-only or workspace-write sandbox')
        if self.settings.get('approval_policy', 'on-request') not in {'on-request', 'untrusted'}:
            raise _backend_error('RUNTIME_PROFILE_INVALID', 'app-server requires user approval policy')
        return effective

    def _save(self, **changes):
        # Native IDs remain exact protocol identities. Refuse secret-bearing IDs
        # instead of redacting them into a different durable binding.
        for key in ('native_thread_id', 'active_turn_id'):
            if key in changes and redact_secrets(self._clean(changes[key])) != changes[key]:
                raise RuntimeError('native session identity contains credential material')
        changes = self._clean(changes)
        if 'effective_settings' in changes:
            changes['effective_settings'] = redact_secrets(changes['effective_settings'])
        # Claims can advance session revision; reload before the owner CAS.
        previous = self._service.store.runtime_sessions.get(self.session['session_id'])
        self.session = self._service.store.runtime_sessions.transition(previous['session_id'],
            expected_revision=previous['revision'], expected_state=previous['state'], changes=changes)

    def _audit(self, event_type, **payload):
        self._service.emit_run_event(self.run['id'], event_type, {
            'case_id': self.session['case_id'], 'session_id': self.session['session_id'],
            'parser_version': PARSER_VERSION, **redact_secrets(self._clean(payload))})

    def _wait_rpc(self, future):
        # Bounded startup; a lost initial turn response is never replayed.
        return future.result(timeout=min(10, self._effective['total_timeout']))

    def _approval(self, frame):
        if self._clean(frame) != frame:
            raise RuntimeError('native approval contains configured credential; refusing persistence')
        params = frame['params']
        if any(params.get(k) != expected for k, expected in (
            ('threadId', self.session['native_thread_id']), ('turnId', self.session['active_turn_id']),
        )) or not isinstance(params.get('itemId'), str) or type(params.get('startedAtMs')) is not int:
            raise RuntimeError('approval native binding mismatch')
        item = deepcopy(self.parser.items.get(params['itemId']))
        if frame['method'] == 'item/fileChange/requestApproval':
            if not item or item.get('type') != 'fileChange' or not item.get('changes'):
                raise RuntimeError('file approval has no original proposal')
            action = item
        else:
            if not isinstance(params.get('command'), str) or not params.get('cwd'):
                raise RuntimeError('command approval has no original action')
            action = {'command': params['command'], 'cwd': params['cwd'], 'item': item}
        binding = {k: self.session[k] for k in ('run_id', 'case_id', 'session_id', 'attempt_id', 'start_token')}
        request_hash = request_hash_of({**binding, 'native_request_id': frame['id'],
            'native_request_id_type': type(frame['id']).__name__, 'method': frame['method'],
            'params': params, 'action': action})
        approval_id = f'approval-{uuid4().hex}'
        approvals = deepcopy(self.session.get('pending_approvals', {}))
        if len(approvals) >= 128:
            raise RuntimeError('approval bound exceeded')
        summary = params.get('command') or '; '.join(str(c.get('path')) + ': ' + str(c.get('diff', '')) for c in item['changes'])
        approvals[approval_id] = {'approval_id': approval_id, 'request_hash': request_hash,
            'method': frame['method'], 'native_request_id': frame['id'], 'params': deepcopy(params),
            'item_id': params['itemId'], 'action': action, 'summary': str(summary)[:4000],
            'created_at': datetime.now(UTC).isoformat(),
            'expires_at': (datetime.now(UTC) + timedelta(seconds=min(120, self._effective['total_timeout']))).isoformat(),
            'state': 'pending'}
        self._save(pending_approvals=approvals)
        self._audit('runtime_approval_requested', approval_id=approval_id, request_hash=request_hash)

    def _event(self, frame):
        self.sequence += 1
        if self.parser.terminal:
            self._audit('runtime_late_event', source_method=frame['method'],
                source_seq=self.sequence, payload=frame)
            return
        self.parser.feed(frame)
        method = frame['method']
        self._audit('runtime_native_event', source_method=method, source_seq=self.sequence,
            sequence_origin='platform-observed', coverage='partial', payload=frame)
        if 'id' in frame:
            if method not in APPROVAL_METHODS:
                raise RuntimeError(f'unsupported server request: {method}')
            self._approval(frame)
        elif method == 'serverRequest/resolved':
            key = typed_id(frame['params'].get('requestId'))
            pending = self.sent_approvals.pop(key, None)
            if pending and not self.parser.terminal and self.cancel_started is None:
                self.resolutions.append((pending, monotonic(), {
                        'kind': 'request_resolved', 'server_request_id': frame['params']['requestId'],
                        'thread_id': self.session['native_thread_id'], 'turn_id': self.session['active_turn_id'],
                        'source_seq': self.sequence, 'decision_outcome': 'not_proven'}))
            approvals = deepcopy(self._service.store.runtime_sessions.get(self.session['session_id'])['pending_approvals'])
            matched = False
            for approval in approvals.values():
                if typed_id(approval['native_request_id']) == key:
                    approval['state'] = 'resolved'
                    matched = True
            if matched:
                self._save(pending_approvals=approvals)

    def _acks(self):
        for resolution in list(self.resolutions):
            command, observed_at, evidence = resolution
            if monotonic() - observed_at < .05:
                continue
            self.resolutions.remove(resolution)
            if not self.parser.terminal and self.cancel_started is None:
                transition_command(self._service, command, 'acknowledged',
                    acknowledged_at=datetime.now(UTC).isoformat(), ack_evidence=evidence)
        for command_id, (command, future, sent_at) in list(self.pending.items()):
            if not future.done() and monotonic() - sent_at < 10:
                continue
            self.pending.pop(command_id)
            status, evidence = 'delivery_unknown', None
            error = None
            try:
                if not future.done():
                    raise TimeoutError('RPC acknowledgement deadline exceeded')
                result = future.result()
                if command['type'] == 'user_message':
                    if result.get('turnId') != self.session['active_turn_id']:
                        raise RuntimeError('steer response turn identity mismatch')
                    semantic = 'message_accepted'
                else:
                    if result != {}:
                        raise RuntimeError('interrupt response shape mismatch')
                    semantic = 'interrupt_requested'
                status = 'acknowledged'
                evidence = {'kind': semantic, 'rpc_request_id': future.rpc_request_id,
                    'thread_id': self.session['native_thread_id'], 'turn_id': self.session['active_turn_id']}
            except RpcError as exc:
                status, error = 'failed', str(exc)
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
            changes = {'ack_evidence': evidence} if evidence else {'error': {'class': 'delivery', 'message': redact_secrets(self._clean(error))}}
            transition_command(self._service, command, status, **changes)

    def _interrupt(self, reason):
        if self.cancel_started is not None:
            return
        self.cancel_started = monotonic()
        self._audit('runtime_interrupt_requested', reason=reason)
        self.cancel_future = self.transport.request('turn/interrupt', {
            'threadId': self.session['native_thread_id'], 'turnId': self.session['active_turn_id']})

    def _commands(self):
        for command in self._service.store.commands.list_for_run(self.run['id']):
            if command['status'] != 'queued' or command.get('session_id') != self.session['session_id']:
                continue
            try:
                if datetime.now(UTC) >= deadline(command['expires_at']):
                    transition_command(self._service, command, 'expired')
                    continue
                self.session = self._service.store.runtime_sessions.get(self.session['session_id'])
                claimed = self._service.store.commands.claim(command['id'], expected_revision=command['revision'],
                    session_id=self.session['session_id'], expected_control_revision=self.session['control_revision'],
                    worker_token=self.session['worker_token'], now=datetime.now(UTC))
            except RunConflictError as exc:
                current = self._service.store.commands.get(command['id'])
                if current and current['status'] == 'queued':
                    transition_command(self._service, current, 'rejected', error={'class': 'command', 'message': redact_secrets(self._clean(str(exc)))})
                continue
            try:
                # Honor already-arrived terminal/resolution before a possible write.
                while (frame := self.transport.poll_event()) is not None:
                    self._event(frame)
                run = self._service.store.runs.get(self.run['id'])
                if self.parser.terminal or (run.get('cancellation') and claimed['type'] != 'interrupt'):
                    transition_command(self._service, claimed, 'rejected', error={'class': 'command', 'message': 'session stopping before write'})
                    continue
                if datetime.now(UTC) >= deadline(claimed['expires_at']):
                    transition_command(self._service, claimed, 'expired')
                    continue
                if claimed['type'] in {'approve', 'reject'}:
                    approval = self._service.store.runtime_sessions.get(self.session['session_id'])['pending_approvals'][claimed['payload']['approval_id']]
                    if approval['state'] != 'claimed' or datetime.now(UTC) >= deadline(approval['expires_at']):
                        transition_command(self._service, claimed, 'rejected', error={'class': 'command', 'message': 'approval cleared or expired before write'})
                        continue
                    original = approval['action'] if approval['method'] == 'item/fileChange/requestApproval' else approval['action'].get('item')
                    current_item = self.parser.items.get(approval['item_id'])
                    request_drift = current_item and approval['method'] == 'item/commandExecution/requestApproval' and any(
                        current_item.get(key) != approval['params'][key] for key in ('command', 'cwd'))
                    if request_drift or current_item and (current_item.get('status') in {'completed', 'failed', 'declined'} or (
                        original and any(original.get(key) != current_item.get(key) for key in ('command', 'cwd', 'changes'))
                    )):
                        transition_command(self._service, claimed, 'rejected', error={'class': 'command', 'message': 'original native proposal changed or completed'})
                        continue
                    self.transport.respond(approval['native_request_id'], {'decision': 'accept' if claimed['type'] == 'approve' else 'decline'})
                    self.sent_approvals[typed_id(approval['native_request_id'])] = claimed
                else:
                    if claimed['type'] == 'interrupt':
                        self._service.cancel(self.run['id'], 'interactive interrupt')
                        self._interrupt('command')
                        future = self.cancel_future
                    else:
                        future = self.transport.request('turn/steer', {'threadId': self.session['native_thread_id'],
                            'expectedTurnId': self.session['active_turn_id'],
                            'input': [{'type': 'text', 'text': claimed['content']}], 'clientUserMessageId': claimed['id']})
                    self.pending[claimed['id']] = claimed, future, monotonic()
            except Exception as exc:  # noqa: BLE001 - no replay across the write boundary
                transition_command(self._service, claimed, 'delivery_unknown', error={'class': 'delivery', 'message': redact_secrets(self._clean(str(exc)))})
                raise

    def _loop(self):
        while True:
            frame = self.transport.poll_event(.05)
            if frame is not None:
                self._event(frame)
                for _ in range(255):
                    frame = self.transport.poll_event()
                    if frame is None:
                        break
                    self._event(frame)
            self._acks()
            if self.parser.terminal:
                return
            if self.transport.closed.is_set() or self.transport.failure:
                raise self.transport.failure or RuntimeError('server disconnected before terminal')
            run = self._service.store.runs.get(self.run['id'])
            if run.get('cancellation'):
                self._interrupt('run_cancel')
            for approval in self.session.get('pending_approvals', {}).values():
                if approval['state'] in {'pending', 'claimed'} and datetime.now(UTC) >= deadline(approval['expires_at']):
                    self._interrupt('approval_deadline_deny_default')
            if self.cancel_started is not None:
                if monotonic() - self.cancel_started > 1:
                    return
            else:
                self._commands()

    def invoke(self, case_id):
        from .agent_tasks import selected_agent_cases_from_snapshot
        if self._service is None:
            raise _backend_error('RUNTIME_SERVICE_REQUIRED', 'interactive runtime requires durable RunService')
        self._effective = self._preflight()
        self._enforce_binary_version(self._effective['expected_version'])
        attempts = [a for a in self._service.store.attempts.list_open(self.run['id'])
            if a['case_id'] == case_id and a['status'] == 'dispatching']
        if len(attempts) != 1:
            raise _backend_error('RUNTIME_ATTEMPT_UNRESOLVED', 'expected exactly one dispatching attempt')
        self._attempt_id = attempts[0]['id']
        case = next(c for c in selected_agent_cases_from_snapshot(self.run) if c['case_id'] == case_id)
        workspace = self._workspace_for(case_id, self._effective['workspace'])
        native_home = None
        self.session = None
        try:
            workspace.materialize_fixture(case.get('fixture') or {})
            before = workspace.snapshot()
            binary = str(self.binary or 'codex')
            credentials = resolve_credential_env(self.backend, self.profile.get('credential_refs') or [])
            self._credential_values = tuple(credentials.values())
            native_home = prepare_native_environment(self.backend, [], workspace=workspace.root,
                binary=binary, settings=self.settings)
            native_home.evidence['credentials'] = [{'ref': ref, 'present': True} for ref in credentials]
            native_home.evidence['auth_channel'] = 'private-account-login-api-key' if credentials else 'none'
            env = native_home.env
            argv = [binary, 'app-server']
            if binary.lower().endswith('.py'):
                argv.insert(0, sys.executable)
            if binary.lower().endswith(('.cmd', '.bat')):
                raise _backend_error('RUNTIME_BINARY_INVALID', 'pin the actual Codex executable, not a shell wrapper')
            self.pending, self.sent_approvals = {}, {}
            self.resolutions = []
            self.cancel_started, self.cancel_future = None, None
            self.sequence, self.parser = 0, None
            self.session = self._service.store.runtime_sessions.create({
                'session_id': f'codex-session-{uuid4().hex}', 'run_id': self.run['id'], 'case_id': case_id,
                'attempt_id': self._attempt_id, 'operation_id': f'op-{uuid4().hex}',
                'start_token': uuid4().hex, 'worker_token': uuid4().hex, 'state': 'prepared',
                'config_hash': self.profile.get('config_hash'), 'parser_version': PARSER_VERSION,
                'native_config': native_home.evidence,
                'workspace': str(workspace.root), 'native_thread_id': None, 'active_turn_id': None})
            from motte_harness.session import (
                SESSION_STORE_ROOT, mark_spawned, mark_terminal, new_session_record,
                persist_session, session_path,
            )
            supplemental = new_session_record(run_id=self.run['id'], case_id=case_id,
                attempt_id=self._attempt_id, operation_id=self.session['operation_id'], backend=self.backend,
                argv=argv, workspace=str(workspace.root), config_hash=self.profile.get('config_hash'))
            supplemental['session_id'] = self.session['session_id']
            supplemental['start_token'] = self.session['start_token']
            record_path = session_path(os.environ.get('MOTTE_RUNTIME_SESSION_ROOT', str(SESSION_STORE_ROOT)),
                self.run['id'], case_id, self.session['session_id'])
            supplemental = persist_session(record_path, supplemental)
            self.transport = self.transport_factory(argv, cwd=str(workspace.root), env=env,
                limits=SupervisedLimits(total_timeout=self._effective['total_timeout'],
                    idle_timeout=self._effective['idle_timeout'], interrupt_grace=.2, drain_timeout=2))
        except Exception:
            cleanup = workspace.cleanup()
            if native_home is not None:
                native_home.close()
            if self.session is not None:
                self._save(state='terminal', cleanup={**cleanup, 'confirmed': True},
                    terminal_status='preparation_failed', terminal_at=datetime.now(UTC).isoformat())
            raise
        started, dispatched, error, outcome = monotonic(), False, None, None
        try:
            self.transport.start()
            self._save(state='starting', pid=self.transport.pid, identity=process_identity(self.transport.pid))
            supplemental = mark_spawned(supplemental, self.transport.pid, path=record_path)
            initialized = self._wait_rpc(self.transport.request('initialize', {
                'clientInfo': {'name': 'motteavl', 'version': '2'}}))
            if not all(isinstance(initialized.get(key), str) for key in ('userAgent', 'codexHome', 'platformFamily', 'platformOs')):
                raise RuntimeError('invalid initialize response')
            self.transport.notify('initialized')
            if credentials:
                authenticated = self._wait_rpc(self.transport.request('account/login/start', {
                    'type': 'apiKey', 'apiKey': credentials['CODEX_API_KEY']}))
                if authenticated != {'type': 'apiKey'}:
                    raise RuntimeError('unexpected native API-key login response')
            settings = {'model': self.settings['model'], 'cwd': str(workspace.root),
                'approvalPolicy': self.settings.get('approval_policy', 'on-request'),
                'approvalsReviewer': 'user', 'sandbox': self.settings.get('sandbox', 'read-only'),
                'ephemeral': True}
            thread = self._wait_rpc(self.transport.request('thread/start', settings))
            thread_id = (thread.get('thread') or {}).get('id')
            if not isinstance(thread_id, str) or not thread_id:
                raise RuntimeError('missing native thread identity')
            if thread.get('model') != settings['model'] or thread.get('approvalPolicy') != settings['approvalPolicy']:
                raise RuntimeError('effective native thread settings drift')
            sandbox = thread.get('sandbox') or {}
            sandbox_type = {'read-only': 'readOnly', 'workspace-write': 'workspaceWrite'}[settings['sandbox']]
            if (sandbox.get('type') != sandbox_type or sandbox.get('networkAccess', False) is not False
                or thread.get('approvalsReviewer') != 'user'
                or os.path.normcase(str(thread.get('cwd'))) != os.path.normcase(settings['cwd'])):
                raise RuntimeError('effective native sandbox/cwd/approval reviewer drift')
            self._save(native_thread_id=thread_id, effective_settings={
                'model': thread.get('model'), 'approvalPolicy': thread.get('approvalPolicy'),
                'sandbox': thread.get('sandbox'), 'instructionSources': thread.get('instructionSources'),
                'config_reproducibility': 'partial'})
            if self._service.store.runs.get(self.run['id']).get('cancellation'):
                raise RuntimeError('cancelled before initial turn dispatch')
            self._save(initial_operation_state='dispatch_intent')
            dispatched = True
            result = self._wait_rpc(self.transport.request('turn/start', {
                'threadId': thread_id, 'input': [{'type': 'text', 'text': case['input']}]}))
            turn = result.get('turn') or {}
            if not isinstance(turn.get('id'), str) or not turn['id'] or turn.get('status') not in {
                'inProgress', 'completed', 'failed', 'interrupted',
            }:
                raise RuntimeError('invalid initial turn reply')
            self._save(state='active', active_turn_id=turn['id'], initial_operation_state='accepted')
            self.parser = AppServerParser(thread_id, turn['id'])
            self._loop()
        except Exception as exc:  # noqa: BLE001 - preserve evidence and always clean before freeze
            error = exc
        finally:
            try:
                self._save(state='stopping')
                # Capture proved RPCs before closing. Persistence failure must not
                # bypass process-tree cleanup or leave approval input writable.
                self._acks()
                settle_session_commands(self._service, self.run['id'], self.session['session_id'], reason='session ended')
            except Exception as exc:  # noqa: BLE001
                error = error or exc
            finally:
                try:
                    if self.transport.pid is not None:
                        outcome = self.transport.close()
                except Exception as exc:  # noqa: BLE001
                    error = error or exc
        confirmed = outcome is not None and not outcome.residual_pids and 'unconfirmed' not in str(outcome.detail)
        if not confirmed:
            native_home.retain()
            self._save(cleanup={'confirmed': False, 'retained_workspace': str(workspace.root)})
            failure = _backend_error('RUNTIME_STOP_UNCONFIRMED', 'process cleanup unconfirmed; workspace retained')
            failure.quarantine = True
            raise failure
        parsed = self.parser.result() if self.parser else {'status': 'insufficient', 'usage': {'reported': False}}
        cancellation = self._service.store.runs.get(self.run['id']).get('cancellation')
        if cancellation:
            parsed['status'] = 'cancelled'
        elif error:
            parsed['status'] = 'insufficient'
            parsed['reason'] = str(error)
        process_view = {key: getattr(outcome, key) for key in (
            'status', 'exit_code', 'truncated', 'detail', 'residual_pids', 'duration_ms')}
        # Stdio servers stay alive after turn completion. A managed stop is normal
        # only with a validated terminal and confirmed process-tree cleanup.
        capture_process = dict(process_view)
        if self.parser and self.parser.terminal and outcome.detail == 'session_closed' and not outcome.truncated:
            capture_process.update(status='exited', exit_code=0)
        parsed = self._clean(parsed)
        parsed, refs = self._freeze_raw_evidence(parsed, self._clean(outcome.stdout), self._clean(outcome.stderr), case_id, [])
        observation = self._capture(workspace, case, parsed, capture_process, before,
            self.session['session_id'], self.session['operation_id'], refs)
        observation['coverage']['events_captured'] = self.sequence
        from motte_contracts.evaluation import observation_evidence_hash

        observation['evidence_hash'] = observation_evidence_hash(observation)
        cleanup = {**workspace.cleanup(), 'confirmed': True}
        native_home.close()
        approvals = deepcopy(self._service.store.runtime_sessions.get(self.session['session_id'])['pending_approvals'])
        for approval in approvals.values():
            if approval['state'] in {'pending', 'claimed'}:
                approval['state'] = 'abandoned'
        self._save(state='terminal', pending_approvals=approvals, cleanup=cleanup,
            terminal_at=datetime.now(UTC).isoformat(), terminal_status=parsed['status'])
        mark_terminal(supplemental, parsed['status'], path=record_path, cleanup=cleanup)
        envelope = {'agent': {'final_output': parsed.get('final_output'),
            'termination_reason': observation['termination']['reason'],
            'termination_detail': parsed.get('reason'), 'steps': 1,
            'tool_calls': len(parsed.get('tool_calls', [])), 'duration_ms': (monotonic() - started) * 1000,
            'runtime': {'backend': 'codex-app-server@2', 'session_id': self.session['session_id'],
                'operation_id': self.session['operation_id'], 'parser_version': PARSER_VERSION,
                'model_control': 'runner-configured', 'observed_model': None,
                'effective_model': self.settings['model'], 'process': process_view}},
            'observation': observation, 'events': [], 'artifacts_captured': len(observation['artifact_refs']),
            'capture_errors': [], 'cleanup': cleanup}
        if error:
            envelope['error'] = {'class': type(error).__name__, 'message': self._clean(str(error))}
        if error and dispatched and not cancellation and not (self.parser and self.parser.terminal):
            failure = _backend_error('RUNTIME_OPERATION_INDETERMINATE', self._clean(str(error)))
            failure.quarantine = True
            failure.evidence = envelope
            raise failure
        if error and not cancellation:
            failure = _backend_error('RUNTIME_PROTOCOL_FAILED', self._clean(str(error)))
            failure.evidence = envelope
            raise failure
        return envelope
