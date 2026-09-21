import json
import sys

import pytest

from motte_harness.codex_app_server import CodexAppServerTransport
from motte_harness.supervisor import SupervisedLimits
from motte_harness.parsers.codex_app_server import AppServerParser


@pytest.mark.parametrize('frame', [
    {'method': 'item/completed', 'params': {'item': {'id': 'a', 'type': 'agentMessage', 'text': 'forged'}}},
    {'method': 'item/started', 'params': {'threadId': 't', 'item': {'id': 'a'}}},
    {'method': 'turn/completed', 'params': {'turn': {'id': 'u', 'status': 'completed'}}},
    {'method': 'turn/started', 'params': {'threadId': 't', 'turn': {'id': 'other', 'status': 'inProgress'}}},
    {'method': 'thread/started', 'params': {'thread': {'id': 'other'}}},
    {'method': 'serverRequest/resolved', 'params': {'requestId': 1}},
    {'method': 'item/agentMessage/delta', 'params': {'threadId': 't', 'delta': 'forged'}},
])
def test_known_native_events_require_actual_bound_identity(frame):
    with pytest.raises(RuntimeError, match='identity'):
        AppServerParser('t', 'u').feed(frame)


def test_terminal_does_not_promote_unfinished_native_tool_to_final():
    parser = AppServerParser('t', 'u')
    parser.feed({'method': 'item/started', 'params': {'threadId': 't', 'turnId': 'u',
        'item': {'id': 'i', 'type': 'commandExecution', 'status': 'inProgress'}}})
    parser.feed({'method': 'turn/completed', 'params': {'threadId': 't',
        'turn': {'id': 'u', 'status': 'completed'}}})
    assert parser.result()['status'] == 'insufficient'


def test_single_reader_routes_interleaved_typed_ids_and_native_approval(tmp_path):
    script = tmp_path / 'server.py'
    script.write_text('''import json,sys
def emit(x): print(json.dumps(x),flush=True)
first=json.loads(input())
emit({'id':first['id'],'method':'item/commandExecution/requestApproval','params':{'threadId':'t','turnId':'u','itemId':'i','startedAtMs':1,'command':'echo ok','cwd':'.'}})
second=json.loads(input())
emit({'method':'item/agentMessage/delta','params':{'threadId':'t','turnId':'u','itemId':'a','delta':'ok'}})
emit({'id':second['id'],'result':{'turnId':'u'}})
emit({'id':first['id'],'result':{'turnId':'u'}})
reply=json.loads(input())
assert reply == {'id':first['id'],'result':{'decision':'decline'}}
emit({'method':'serverRequest/resolved','params':{'threadId':'t','requestId':first['id']}})
''', encoding='utf-8')
    transport = CodexAppServerTransport([sys.executable, str(script)], cwd=str(tmp_path),
        env={}, limits=SupervisedLimits(total_timeout=5, idle_timeout=3, interrupt_grace=.1))
    transport.start()
    try:
        first = transport.request('turn/steer', {}, request_id=1)
        second = transport.request('turn/steer', {}, request_id='1')
        assert second.result(timeout=3) == {'turnId': 'u'}
        assert first.result(timeout=3) == {'turnId': 'u'}
        approval = transport.poll_event(1)
        assert approval['method'] == 'item/commandExecution/requestApproval'
        assert type(approval['id']) is int
        transport.respond(approval['id'], {'decision': 'decline'})
        assert transport.poll_event(1)['method'] == 'item/agentMessage/delta'
        assert transport.poll_event(1)['method'] == 'serverRequest/resolved'
        outcome = transport.wait_closed(3)
        assert outcome.exit_code == 0
        assert outcome.residual_pids == []
    finally:
        transport.close()


def test_malformed_reply_fails_pending_future_without_retry(tmp_path):
    script = tmp_path / 'invalid.py'
    script.write_text("input(); print('{bad json', flush=True)", encoding='utf-8')
    transport = CodexAppServerTransport([sys.executable, str(script)], cwd=str(tmp_path), env={},
        limits=SupervisedLimits(total_timeout=3, idle_timeout=1, interrupt_grace=.1))
    transport.start()
    try:
        future = transport.request('initialize', {})
        import pytest
        with pytest.raises(RuntimeError, match='JSON'):
            future.result(timeout=2)
    finally:
        transport.close()
