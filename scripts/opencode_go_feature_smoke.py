"""Bounded real coding-feature verification. Dry by default; never a full benchmark.

All model requests pass through a pinned, counted HTTP boundary. Reports contain
only synthetic-check results, session hashes, numeric usage and protocol facts.
Existing credentials are resolved by the normal provider factory, never printed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from urllib.error import HTTPError
from uuid import uuid4

from motte_agent.budget import ExecutionBudget
from motte_agent.builtin_react import BuiltinReActRuntime
from motte_agent.native_tools import TOOL_DECLARATIONS
from motte_contracts.messages import Message, ModelRequest
from motte_provider import transport as transport_module
from motte_provider import config as provider_config_module
from motte_provider.config import build_case_provider
from motte_provider.opencode_go import GO_BASE_URL, SPACE_BUNNY_MODEL, USER_AGENT

FEATURE_LIMITS = {
    'nonstream': 1, 'stream_text': 1, 'stream_tools': 2, 'stream_cancel': 1,
    'multi_turn_tools': 8, 'legacy_json': 6,
    'api_model_test': 1, 'queued_direct': 1, 'native_agent_queue': 6,
    'scenario_multiturn': 4,
    'judge_single': 1, 'judge_pairwise': 2, 'judge_calibration': 2,
    'direct_v2': 1, 'experiment_cell': 1, 'skill_ablation': 6,
}
FEATURE_OUTPUT_CAPS = {name:(4096 if name.startswith('judge_') else 512)
                       for name in FEATURE_LIMITS}
FEATURE_SOCKET_TIMEOUTS = {name:(120 if name.startswith('judge_') else 20)
                           for name in FEATURE_LIMITS}
FEATURE_DEADLINE_CAPS = {name:(240 if name.startswith('judge_') else 120)
                         for name in FEATURE_LIMITS}
CORE_FEATURES = ('nonstream','stream_text','stream_tools','stream_cancel',
                 'multi_turn_tools','legacy_json')


class FeatureTimeout(TimeoutError):
    error_class = 'timeout'


def numeric_usage(value):
    if not isinstance(value, dict):
        return None
    return {k:v for k,v in value.items() if k in
            ('prompt_tokens','completion_tokens','total_tokens') and type(v) is int and v >= 0}


def safe_finish_reason(value):
    return value if value in ('stop','length','tool_calls','content_filter','error') else 'other'


def run_with_deadline(ctx, function, *, timeout):
    box = {}
    def run():
        try:
            box['result'] = function(ctx)
        except BaseException as error:
            box['error'] = error
    ctx.worker = threading.Thread(target=run, daemon=True, name='go-feature-witness')
    ctx.worker.start()
    ctx.worker.join(max(0,min(timeout,min(ctx.deadline,ctx.feature_deadline)-ctx._clock())))
    if ctx.worker.is_alive() or ctx._clock() >= min(ctx.deadline,ctx.feature_deadline):
        ctx.stopped = True
        raise FeatureTimeout('absolute feature deadline reached')
    if 'error' in box:
        raise box['error']
    return box.get('result')


def verify_completed_requests(ctx, start, feature):
    rows = ctx.requests[start:]
    if feature == 'skill_ablation' and len({r.get('session_sha256') for r in rows}) != 3:
        ctx.stopped = True
        raise ValueError('three skill arms must use independent conversation sessions')
    if feature in ('multi_turn_tools','native_agent_queue','scenario_multiturn') and (
            not rows or len({r.get('session_sha256') for r in rows}) != 1):
        ctx.stopped = True
        raise ValueError('conversation session identity changed within one feature')
    if any(r.get('reported_model_matches') is False for r in rows):
        ctx.stopped = True
        raise ValueError('reported model identity mismatch')
    if feature != 'stream_cancel' and any(r.get('stream') and
            r.get('reported_model_matches') is not True for r in rows):
        ctx.stopped = True
        raise ValueError('stream model identity is unverified')
    if any(r.get('active') for r in rows):
        ctx.stopped = True
        raise FeatureTimeout('request outcome still in flight; later features stopped')


def safe_failure_fields(error):
    try:
        from scripts.opencode_go_judge_checks import (
            JudgeWitnessFailure, SAFE_FAILURE_CODES, SAFE_DIAGNOSTIC_KEYS)
    except ModuleNotFoundError:
        return {}
    if not isinstance(error,JudgeWitnessFailure) or error.code not in SAFE_FAILURE_CODES:
        return {}
    return {'error_code':error.code,'diagnostics':{
        key:value for key,value in error.diagnostics.items()
        if key in SAFE_DIAGNOSTIC_KEYS and type(value) is int and value >= 0}}


def now():
    return datetime.now(UTC).isoformat()


class ResponseEvidence:
    """Observe only safe protocol fields; never retain response text or headers."""
    def __init__(self, response, record, on_identity_mismatch=None):
        self.response = response
        self.record = record
        self.on_identity_mismatch = on_identity_mismatch
        self.status = getattr(response, 'status', 200)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def observe(self, data):
        if not isinstance(data, dict):
            return
        model = data.get('model')
        if isinstance(model, str):
            matches = model == SPACE_BUNNY_MODEL
            if not matches and self.on_identity_mismatch:
                self.on_identity_mismatch()
            self.record['reported_model_matches'] = (
                self.record.get('reported_model_matches', True) and matches)
        choices = data.get('choices')
        if isinstance(choices,list) and choices and isinstance(choices[0],dict):
            finish = choices[0].get('finish_reason')
            if finish is not None:
                self.record['finish_reason'] = safe_finish_reason(finish)
        usage = data.get('usage')
        if isinstance(usage, dict):
            self.record['usage'] = numeric_usage(usage)

    def read(self):
        data = self.response.read()
        try:
            self.observe(json.loads(data))
        except (ValueError, TypeError):
            pass
        return data

    def __iter__(self):
        for line in self.response:
            if isinstance(line, bytes) and line.startswith(b'data:'):
                try:
                    self.observe(json.loads(line[5:].strip()))
                except (ValueError, TypeError):
                    pass
            yield line

    def close(self):
        close = getattr(self.response, 'close', None)
        if close:
            close()
        self.record['closed'] = True
        self.record['active'] = False


class LiveContext:
    def __init__(self, root, profile, feature_limits, *, opener=None, clock=time.monotonic):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.profile = profile
        self.limits = dict(feature_limits)
        self.requests = []
        self.feature = None
        self.stopped = False
        self._clock = clock
        self.deadline = clock() + 900
        self.feature_deadline = self.deadline
        self._opener = opener or transport_module._bounded_urlopen
        self._lock = threading.Lock()
        self.worker = None

    def start_feature(self, feature):
        if self.stopped:
            raise ValueError('live feature run stopped after upstream safety error')
        if feature not in self.limits:
            raise ValueError('unknown feature')
        self.feature = feature
        self.feature_deadline = min(self.deadline,self._clock()+min(
            FEATURE_DEADLINE_CAPS.get(feature,120),self.limits[feature]*FEATURE_SOCKET_TIMEOUTS.get(feature,20)))

    def provider_config(self):
        return {'kind':'openai_compatible','base_url':GO_BASE_URL,'model':SPACE_BUNNY_MODEL,
                'credentials':self.profile,'timeout':FEATURE_SOCKET_TIMEOUTS.get(self.feature,20),
                'max_retries':0,
                'follow_redirects':False,'max_output_tokens':FEATURE_OUTPUT_CAPS.get(self.feature,512),
                'identity_policy':'require_match'}

    def provider(self):
        return build_case_provider(self.provider_config()).provider

    def request(self, prompt, *, session=None, **kwargs):
        return ModelRequest(model=SPACE_BUNNY_MODEL,system='You are a coding assistant.',
            messages=[Message(role='user',content=prompt)],
            metadata={'session_id':session or uuid4().hex},max_output_tokens=512,**kwargs)

    def send(self, request, *, timeout):
        body = json.loads(request.data)
        session = request.get_header('X-opencode-session')
        ua = request.get_header('User-agent')
        if (request.full_url != GO_BASE_URL+'/chat/completions'
                or request.get_method() != 'POST' or body.get('model') != SPACE_BUNNY_MODEL
                or type(body.get('max_tokens')) is not int or not 1 <= body['max_tokens'] <=
                    FEATURE_OUTPUT_CAPS.get(self.feature,512)
                or not isinstance(session,str) or not session or ua != USER_AGENT):
            raise ValueError('pinned endpoint/model/output/client/session contract rejected')
        with self._lock:
            count = sum(r['feature'] == self.feature for r in self.requests)
            if self.feature not in self.limits or count >= self.limits[self.feature]:
                raise ValueError('per-feature request budget exhausted')
            if len(self.requests) >= sum(self.limits.values()):
                raise ValueError('total request budget exhausted')
            remaining = min(self.deadline,self.feature_deadline)-self._clock()
            if self.stopped or remaining <= 0:
                raise ValueError('live run stopped or time budget exhausted')
            record = {'feature':self.feature,'ordinal':len(self.requests)+1,'sent_at':now(),
                      'stream':body.get('stream') is True,'max_tokens':body['max_tokens'],
                      'session_sha256':hashlib.sha256(session.encode()).hexdigest(),
                      'user_agent_valid':ua == USER_AGENT,'closed':False,'active':True}
            self.requests.append(record)
        try:
            response = self._opener(request,timeout=min(float(timeout),FEATURE_SOCKET_TIMEOUTS.get(self.feature,20),remaining))
            record['http_status'] = getattr(response,'status',200)
            return ResponseEvidence(response,record,lambda: setattr(self,'stopped',True))
        except HTTPError as error:
            record['active'] = False
            record['http_status'] = error.code
            retry = (error.headers or {}).get('Retry-After')
            if isinstance(retry,str) and retry.isdigit():
                record['retry_after_seconds'] = int(retry)
            if error.code in (401,403,429):
                self.stopped = True
            raise
        except (OSError, TimeoutError):
            record['active'] = False
            record['network_error'] = True
            self.stopped = True
            raise

    @contextmanager
    def guarded_transport(self):
        original_resolver = provider_config_module.resolve_api_key
        def selected_profile_only(profile=None, *, env_name=None):
            if profile != self.profile:
                raise ValueError('unselected credential profile refused')
            key = original_resolver(self.profile, env_name=None)
            if not key:
                raise ValueError('selected credential profile is missing')
            return key
        provider_config_module.resolve_api_key = selected_profile_only
        original_safe = transport_module._safe_urlopen
        original_bounded = transport_module._bounded_urlopen
        transport_module._safe_urlopen = self.send
        transport_module._bounded_urlopen = self.send
        try:
            yield
        finally:
            # A timed-out daemon may still unwind. Keep deny-on-stopped guards in
            # this dedicated process until it exits, never reopen the network path.
            if (self.worker is None or not self.worker.is_alive()) and not any(
                    r.get('active') for r in self.requests):
                transport_module._safe_urlopen = original_safe
                transport_module._bounded_urlopen = original_bounded
                provider_config_module.resolve_api_key = original_resolver


def check_nonstream(ctx):
    result = ctx.provider().complete(ctx.request(
        'Fix Python def add(a,b): return a-b. Reply only with the corrected return statement.'))
    assert 'a+b' in result['content'].replace(' ','')
    assert result['policy_passed'] is True
    return {'identity_match':True,'finish_reason':safe_finish_reason(result['finish_reason']),
            'usage':numeric_usage(result.get('usage'))}


def check_stream_text(ctx):
    events = list(ctx.provider().stream(ctx.request(
        'Fix Python def add(a,b): return a-b. Reply with the corrected return statement.')))
    text = ''.join(e.get('delta','') for e in events if e['type']=='text_delta')
    assert 'a+b' in text.replace(' ','')
    assert sum(e['type']=='finish' for e in events)==1
    assert ctx.requests[-1]['closed']
    return {'text_deltas':sum(e['type']=='text_delta' for e in events),
            'single_terminal':True,'usage':numeric_usage(events[-1]['payload'].get('usage'))}


def check_stream_tools(ctx):
    session = uuid4().hex
    provider = ctx.provider()
    first = ctx.request('Read solution.py with read_file to inspect a Python addition bug.',
        session=session,tools=[TOOL_DECLARATIONS['read_file']],
        tool_choice={'type':'function','function':{'name':'read_file'}})
    events = list(provider.stream(first))
    calls = events[-1]['payload']['tool_calls']
    assert len(calls)==1 and calls[0]['name']=='read_file'
    assert json.loads(calls[0]['arguments'])['path']=='solution.py'
    call = {k:calls[0][k] for k in ('id','name','arguments')}
    second = first.model_copy(update={'messages':[
        *first.messages,Message(role='assistant',content='',tool_calls=[call]),
        Message(role='tool',content='def add(a,b):\n    return a-b\n',tool_call_id=call['id']),
        Message(role='user',content='Reply only with the corrected return statement.')],
        'tool_choice':'none'})
    done = list(provider.stream(second))
    answer = ''.join(e.get('delta','') for e in done if e['type']=='text_delta')
    assert 'a+b' in answer.replace(' ','')
    assert ctx.requests[-1]['session_sha256']==ctx.requests[-2]['session_sha256']
    return {'tool_delta_events':sum(e['type']=='tool_delta' for e in events),
            'tool_result_history_accepted':True,'session_stable':True,'terminal_count':1}


def check_stream_cancel(ctx):
    stream = ctx.provider().stream(ctx.request(
        'Explain this Python function line by line and propose tests: '
        'def unique(items): return list(dict.fromkeys(items)).'))
    seen = None
    try:
        for event in stream:
            if event['type'] in ('text_delta','tool_delta'):
                seen = event
                break
    finally:
        stream.close()
    if seen is None:
        raise ValueError('stream completed before a cancellable delta; inconclusive')
    assert ctx.requests[-1]['closed']
    return {'first_event':seen['type'],'client_stream_closed':True,
            'server_side_cancellation':'not observable'}


def check_multi_turn_tools(ctx):
    files = {'solution.py':'def add(a,b):\n    return a-b\n'}
    actions = []
    def read(args):
        assert args['path']=='solution.py'
        actions.append('read_file')
        return files['solution.py']
    def write(args):
        assert args['path']=='solution.py' and len(args['content'])<=8192
        actions.append('write_file')
        files['solution.py']=args['content']
        return 'updated'
    runtime = BuiltinReActRuntime(ctx.provider().complete,{'read_file':read,'write_file':write},
        model=SPACE_BUNNY_MODEL,mode='native-tool',budget=ExecutionBudget(
            max_steps=8,max_tool_calls=6,per_call_timeout_sec=20,max_output_tokens=512))
    runtime.begin()
    first = runtime.send('Inspect solution.py and explain its bug. Do not edit yet.')
    assert first['termination_reason']=='final_answer' and 'write_file' not in actions
    second = runtime.send('Now fix solution.py to return a + b. Preserve the function signature.')
    runtime.close()
    from motte_cli.opencode_smoke import _same_code
    assert second['termination_reason']=='final_answer'
    assert _same_code(files['solution.py'],'def add(a,b):\n    return a+b\n')
    rows = [r for r in ctx.requests if r['feature']==ctx.feature]
    assert len({r['session_sha256'] for r in rows})==1
    return {'turns':2,'tools':actions,'session_stable':True,'code_correct':True}


def check_legacy_json(ctx):
    files={'solution.py':'def add(a,b):\n    return a-b\n'}
    actions=[]
    def read(args):
        assert args['path']=='solution.py'
        actions.append('read_file')
        return files['solution.py']
    def write(args):
        assert args['path']=='solution.py' and len(args['content'])<=8192
        actions.append('write_file')
        files['solution.py']=args['content']
        return 'updated'
    runtime=BuiltinReActRuntime(ctx.provider().complete,{'read_file':read,'write_file':write},
        model=SPACE_BUNNY_MODEL,mode='legacy-json',budget=ExecutionBudget(
            max_steps=6,max_tool_calls=5,per_call_timeout_sec=20,max_output_tokens=512))
    result=runtime.run_agent('Use read_file with input {"path":"solution.py"}, then write_file '
        'with input {"path":"solution.py","content":"full corrected code"} to fix add(a,b) '
        'to return a + b. Preserve the function signature. Then give the final answer.')
    from motte_cli.opencode_smoke import _same_code
    assert result['termination_reason']=='final_answer'
    assert _same_code(files['solution.py'],'def add(a,b):\n    return a+b\n')
    assert 'read_file' in actions and 'write_file' in actions
    return {'action_protocol':'legacy-json','tools':actions,'code_correct':True}


def checks(names=None):
    available={name:globals()['check_'+name] for name in CORE_FEATURES}
    if names is not None and all(name in CORE_FEATURES for name in names):
        return available
    try:
        from scripts.opencode_go_integration_checks import (
            check_api_model,check_queued_direct,check_native_agent,check_scenario_multiturn)
        available.update(api_model_test=check_api_model,queued_direct=check_queued_direct,
                         native_agent_queue=check_native_agent,scenario_multiturn=check_scenario_multiturn)
    except ModuleNotFoundError:
        pass
    try:
        from scripts.opencode_go_judge_checks import (
            check_judge_single,check_judge_pairwise,check_judge_calibration)
        available.update(judge_single=check_judge_single,judge_pairwise=check_judge_pairwise,
                         judge_calibration=check_judge_calibration)
    except ModuleNotFoundError:
        pass
    try:
        from scripts.opencode_go_suite_checks import (
            check_direct_v2,check_experiment_cell,check_skill_ablation)
        available.update(direct_v2=check_direct_v2,experiment_cell=check_experiment_cell,
                         skill_ablation=check_skill_ablation)
    except ModuleNotFoundError:
        pass
    return available


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--confirm-free',action='store_true')
    parser.add_argument('--credentials',default='opencode-go')
    parser.add_argument('--features',nargs='+',choices=tuple(FEATURE_LIMITS),default=list(CORE_FEATURES))
    parser.add_argument('--output',type=Path)
    parser.add_argument('--prior-http-requests',type=int,default=36)
    args=parser.parse_args(argv)
    limits={name:FEATURE_LIMITS[name] for name in dict.fromkeys(args.features)}
    report={'live':args.live,'model':SPACE_BUNNY_MODEL,'base_url':GO_BASE_URL,
            'feature_limits':limits,'max_http_requests':sum(limits.values()),'concurrency':1,
            'retries':0,'output_token_limits':{name:FEATURE_OUTPUT_CAPS[name] for name in limits},
            'socket_timeout_sec':{name:FEATURE_SOCKET_TIMEOUTS[name] for name in limits},
            'feature_timeout_sec':{name:min(FEATURE_DEADLINE_CAPS[name],cap*FEATURE_SOCKET_TIMEOUTS[name])
                                   for name,cap in limits.items()},
            'prior_http_requests':args.prior_http_requests,'features':[],'requests':[]}
    if not args.live:
        print(json.dumps(report,indent=2))
        return 0
    if not args.confirm_free or args.output is None:
        print('Live execution requires --confirm-free and --output; no requests sent.')
        return 2
    report['source_sha']=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    report['started_at']=now()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    def save():
        args.output.write_text(json.dumps(report,indent=2)+'\n')
    save()
    with tempfile.TemporaryDirectory(prefix='motte-go-features-') as root:
        ctx=LiveContext(root,args.credentials,limits)
        with ctx.guarded_transport():
            for name in limits:
                start=len(ctx.requests)
                try:
                    ctx.start_feature(name)
                    function=checks((name,)).get(name)
                    if function is None:
                        entry={'name':name,'status':'blocked','reason':'witness_not_implemented'}
                    else:
                        evidence=run_with_deadline(ctx,function,timeout=min(FEATURE_DEADLINE_CAPS[name],limits[name]*FEATURE_SOCKET_TIMEOUTS[name]))
                        verify_completed_requests(ctx,start,name)
                        if name in ('multi_turn_tools','native_agent_queue','scenario_multiturn'):
                            evidence['session_stable'] = True
                        if name == 'skill_ablation':
                            evidence['arm_sessions_distinct'] = True
                        entry={'name':name,'status':'passed','evidence':evidence}
                except Exception as error:
                    entry={'name':name,'status':'failed','error_type':type(error).__name__,
                           'error_class':getattr(error,'error_class',None),
                           **safe_failure_fields(error)}
                    if getattr(error,'error_class',None) in ('auth','rate_limit','network','timeout'):
                        ctx.stopped=True
                pending = [r for r in ctx.requests[start:] if r.get('active')]
                if pending:
                    ctx.stopped=True
                    entry['in_flight_outcome']='unknown; no subsequent requests dispatched'
                entry['http_requests']=len(ctx.requests)-start
                report['features'].append(entry)
                report['requests']=ctx.requests
                report['http_requests']=len(ctx.requests)
                report['cumulative_http_requests']=args.prior_http_requests+len(ctx.requests)
                save()
                if ctx.stopped:
                    report['stopped_after_safety_error']=True
                    break
    report['finished_at']=now()
    report['status']='passed' if len(report['features'])==len(limits) and all(
        f['status']=='passed' for f in report['features']) else 'incomplete_or_failed'
    save()
    print(json.dumps({'status':report['status'],'http_requests':report.get('http_requests',0),
                      'features':report['features']},indent=2))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
