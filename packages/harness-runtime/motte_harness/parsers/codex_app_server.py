"""Codex 0.155.1 v2 notifications (not the exec JSONL protocol)."""
from __future__ import annotations

from copy import deepcopy

PARSER_VERSION = 'codex-appserver-v2-0.155.1'


class AppServerParser:
    def __init__(self, thread_id, turn_id):
        self.thread_id, self.turn_id = thread_id, turn_id
        self.items = {}
        self.terminal = None
        self.unknown = []
        self.usage = {'reported': False}
        self.seq = 0
        self.open_items = set()

    def feed(self, frame):
        self.seq += 1
        method, params = frame['method'], frame.get('params', {})
        if not isinstance(params, dict):
            raise RuntimeError('invalid native event parameters')
        bound_turn_methods = {
            'item/started', 'item/completed', 'item/agentMessage/delta', 'thread/tokenUsage/updated',
            'item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
        }
        bound_thread_methods = bound_turn_methods | {'turn/started', 'turn/completed', 'serverRequest/resolved'}
        if (method in bound_thread_methods and params.get('threadId') != self.thread_id
            or 'threadId' in params and params['threadId'] != self.thread_id):
            raise RuntimeError('native thread identity mismatch')
        if (method in bound_turn_methods and params.get('turnId') != self.turn_id
            or 'turnId' in params and params['turnId'] != self.turn_id):
            raise RuntimeError('native turn identity mismatch')
        if method == 'thread/started' and (params.get('thread') or {}).get('id') != self.thread_id:
            raise RuntimeError('native nested thread identity mismatch')
        if method in {'turn/started', 'turn/completed'}:
            turn = params.get('turn')
            if not isinstance(turn, dict) or turn.get('id') != self.turn_id:
                raise RuntimeError('native nested turn identity mismatch')
            if turn.get('status') not in {'inProgress', 'completed', 'failed', 'interrupted'}:
                raise RuntimeError('invalid native turn status')
        if method in {'item/started', 'item/completed'}:
            item = params.get('item')
            if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                raise RuntimeError('invalid native item')
            self.items[item['id']] = deepcopy(item)
            if method == 'item/started':
                self.open_items.add(item['id'])
            else:
                self.open_items.discard(item['id'])
        elif method == 'turn/completed':
            turn = params.get('turn') or {}
            if turn.get('id') != self.turn_id or turn.get('status') not in {'completed', 'failed', 'interrupted'}:
                raise RuntimeError('invalid terminal identity/status')
            if self.terminal:
                raise RuntimeError('duplicate terminal')
            self.terminal = deepcopy(turn)
        elif method == 'thread/tokenUsage/updated':
            total = (params.get('tokenUsage') or {}).get('total') or {}
            if all(type(total.get(k)) is int and total[k] >= 0 for k in ('inputTokens', 'outputTokens')):
                self.usage = {'reported': True, 'input_tokens': total['inputTokens'], 'output_tokens': total['outputTokens']}
        elif method not in {
            'thread/started', 'turn/started', 'item/agentMessage/delta', 'serverRequest/resolved',
            'item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
        }:
            if len(self.unknown) < 128:
                self.unknown.append(method)

    def result(self):
        terminal = self.terminal or {}
        status = {'completed': 'final', 'failed': 'error', 'interrupted': 'cancelled'}.get(terminal.get('status'), 'insufficient')
        incomplete = self.open_items or any(item.get('status') == 'inProgress' for item in self.items.values())
        if status == 'final' and incomplete:
            status = 'insufficient'
        tools = []
        for item in self.items.values():
            if item.get('type') in {'commandExecution', 'fileChange'}:
                tools.append({'call_id': item['id'], 'name': item['type'],
                    'status': item.get('status'), 'native_item': deepcopy(item)})
        return {'status': status, 'final_output': '\n'.join(
            str(item.get('text') or '') for item in self.items.values() if item.get('type') == 'agentMessage'),
            'usage': self.usage, 'model': None, 'cost_usd': None, 'coverage': 'partial',
            'tool_trajectory': 'partial', 'tool_calls': tools, 'unknown_events': self.unknown,
            'reason': 'native items unfinished at terminal' if incomplete else str(terminal.get('error') or '') or None}
