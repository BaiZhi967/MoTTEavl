import importlib
import json
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from tests.provider.test_openai_compatible import FakeResponse


@pytest.fixture(autouse=True)
def no_network_escape(monkeypatch):
    import socket
    import urllib.request
    def reject(*args, **kwargs):
        raise AssertionError('offline_feature_test_network_escape')
    monkeypatch.setattr(socket,'create_connection',reject)
    monkeypatch.setattr(socket.socket,'connect',reject)
    monkeypatch.setattr(socket.socket,'connect_ex',reject)
    monkeypatch.setattr(urllib.request.OpenerDirector,'open',reject)


def module():
    assert importlib.util.find_spec('scripts.opencode_go_feature_smoke') is not None
    return importlib.import_module('scripts.opencode_go_feature_smoke')


def request(**changes):
    body={'model':'space-bunny-free','max_tokens':512,'messages':[{'role':'user','content':'Fix Python'}]}
    body.update(changes)
    return Request('https://opencode.ai/zen/go/v1/chat/completions',method='POST',
                   data=json.dumps(body).encode(),headers={
                       'User-Agent':'MoTTEavl/0.1.0','x-opencode-session':'session-a',
                       'Authorization':'Bearer secret-never-log'})


def test_feature_guard_caps_sends_and_records_only_safe_metadata(tmp_path):
    mod=module()
    seen=[]
    ctx=mod.LiveContext(tmp_path,'fake-profile',{'small':1},opener=lambda req,**kw:
        seen.append(req) or FakeResponse({'model':'space-bunny-free','usage':{'prompt_tokens':12},
                                         'choices':[]}))
    ctx.start_feature('small')
    with ctx.send(request(),timeout=20) as response:
        response.read()
    with pytest.raises(ValueError,match='budget'):
        ctx.send(request(),timeout=20)
    assert len(seen)==1
    wire=ctx.requests[0]
    assert wire['user_agent_valid'] is True
    assert wire['reported_model_matches'] is True
    assert wire['usage']=={'prompt_tokens':12}
    assert wire['closed'] is True
    assert 'secret-never-log' not in json.dumps(ctx.requests)
    assert 'Authorization' not in json.dumps(ctx.requests)
    assert 'session-a' not in json.dumps(ctx.requests)


@pytest.mark.parametrize('changes',[{'model':'paid-model'},{'max_tokens':513},{'max_tokens':True}])
def test_guard_rejects_request_expansion_before_send(tmp_path,changes):
    mod=module()
    ctx=mod.LiveContext(tmp_path,'fake',{'small':1},opener=lambda *a,**kw:pytest.fail('sent'))
    ctx.start_feature('small')
    with pytest.raises(ValueError):
        ctx.send(request(**changes),timeout=20)
    assert ctx.requests==[]


def test_rate_limit_stops_following_features_without_retry(tmp_path):
    mod=module()
    seen=[]
    def opener(req,**kwargs):
        seen.append(req)
        raise HTTPError(req.full_url,429,'secret-never-log',{'Retry-After':'30'},None)
    ctx=mod.LiveContext(tmp_path,'fake',{'a':2,'b':2},opener=opener)
    ctx.start_feature('a')
    with pytest.raises(HTTPError):
        ctx.send(request(),timeout=20)
    with pytest.raises(ValueError,match='stopped'):
        ctx.start_feature('b')
    assert len(seen)==1
    assert ctx.requests[0]['http_status']==429
    assert ctx.requests[0]['retry_after_seconds']==30
    assert 'secret-never-log' not in json.dumps(ctx.requests)


def test_dry_plan_never_constructs_live_context(monkeypatch,capsys):
    mod=module()
    monkeypatch.setattr(mod,'LiveContext',lambda *a,**kw:pytest.fail('live context'))
    assert mod.main([])==0
    plan=json.loads(capsys.readouterr().out)
    assert plan['live'] is False
    assert plan['model']=='space-bunny-free'
    assert plan['max_http_requests']==sum(plan['feature_limits'].values())


def test_reported_identity_drift_stops_remaining_features(tmp_path):
    mod=module()
    ctx=mod.LiveContext(tmp_path,'fake',{'a':1,'b':1},opener=lambda *a,**kw:
        FakeResponse({'model':'other-model','choices':[]}))
    ctx.start_feature('a')
    with ctx.send(request(),timeout=20) as response:
        response.read()
    with pytest.raises(ValueError,match='stopped'):
        ctx.start_feature('b')


@pytest.mark.parametrize('feature',['nonstream','stream_text','stream_tools','stream_cancel',
                                    'multi_turn_tools','legacy_json'])
def test_core_witnesses_exercise_real_adapter_with_fake_http(tmp_path,monkeypatch,feature):
    mod=module()
    from motte_provider import config
    from tests.provider.test_streaming_contracts import FakeSSEResponse, _sse_lines
    monkeypatch.setattr(config,'resolve_api_key',lambda *a,**kw:'fake-key')
    count=[0]
    def response(req,**kwargs):
        body=json.loads(req.data)
        count[0]+=1
        stage=count[0]
        tool=None
        content='return a + b'
        if feature=='stream_tools' and stage==1 or feature=='multi_turn_tools' and stage==1:
            tool={'id':'read','type':'function','function':{'name':'read_file',
                'arguments':'{"path":"solution.py"}'}}
        if feature=='multi_turn_tools' and stage==3:
            tool={'id':'write','type':'function','function':{'name':'write_file',
                'arguments':json.dumps({'path':'solution.py','content':'def add(a,b):\n    return a+b\n'})}}
        if feature=='legacy_json':
            if stage==1:
                content=json.dumps({'action':'tool','tool':'read_file','input':{'path':'solution.py'}})
            elif stage==2:
                content=json.dumps({'action':'tool','tool':'write_file','input':{
                    'path':'solution.py','content':'def add(a,b):\n    return a+b\n'}})
            else:
                content=json.dumps({'action':'final','answer':'fixed'})
        message={'tool_calls':[tool]} if tool else {'content':content}
        if body.get('stream'):
            delta={'tool_calls':[{'index':0,**tool}]} if tool else {'content':content}
            return FakeSSEResponse(_sse_lines([
                {'model':'space-bunny-free','choices':[{'delta':delta}]},
                {'model':'space-bunny-free','choices':[{'delta':{},'finish_reason':'tool_calls' if tool else 'stop'}]},
            ]))
        return FakeResponse({'model':'space-bunny-free','choices':[{'message':message,
            'finish_reason':'tool_calls' if tool else 'stop'}],
            'usage':{'prompt_tokens':5,'completion_tokens':3}})
    ctx=mod.LiveContext(tmp_path,'fake',{feature:mod.FEATURE_LIMITS[feature]},opener=response)
    ctx.start_feature(feature)
    with ctx.guarded_transport():
        result=mod.checks()[feature](ctx)
    assert result
    assert 1<=count[0]<=mod.FEATURE_LIMITS[feature]
    assert all(r['reported_model_matches'] for r in ctx.requests)
    assert all(r['closed'] for r in ctx.requests)


def test_saved_profile_never_falls_back_to_unrelated_env(tmp_path,monkeypatch):
    mod=module()
    from motte_provider import config
    calls=[]
    def resolve(profile=None,*,env_name=None):
        calls.append((profile,env_name))
        return None
    monkeypatch.setattr(config,'resolve_api_key',resolve)
    monkeypatch.setenv('OPENAI_API_KEY','unrelated-key')
    ctx=mod.LiveContext(tmp_path,'selected',{'nonstream':1},opener=lambda *a,**kw:pytest.fail('sent'))
    ctx.start_feature('nonstream')
    with ctx.guarded_transport(),pytest.raises(ValueError,match='profile'):
        ctx.provider()
    assert calls==[('selected',None)]


def test_feature_deadline_stops_following_dispatch(tmp_path):
    mod=module()
    import threading
    release=threading.Event()
    ctx=mod.LiveContext(tmp_path,'fake',{'a':1,'b':1},opener=lambda *a,**kw:pytest.fail('sent'))
    ctx.start_feature('a')
    try:
        with pytest.raises(mod.FeatureTimeout):
            mod.run_with_deadline(ctx,lambda _:release.wait(2),timeout=0.01)
        assert ctx.stopped is True
        with pytest.raises(ValueError,match='stopped'):
            ctx.start_feature('b')
    finally:
        release.set()
        ctx.worker.join(2)


def test_usage_sanitizer_omits_provider_strings_and_nested_fields():
    mod=module()
    assert mod.numeric_usage({'prompt_tokens':4,'completion_tokens':'private-key',
                              'extra':{'secret':'private-key'},'total_tokens':True})=={'prompt_tokens':4}


def test_stream_identity_missing_is_not_counted_as_verified(tmp_path):
    mod=module()
    ctx=mod.LiveContext(tmp_path,'fake',{'stream_text':1})
    ctx.requests=[{'feature':'stream_text','stream':True,'closed':True,'http_status':200}]
    with pytest.raises(ValueError,match='identity'):
        mod.verify_completed_requests(ctx,0,'stream_text')
    assert ctx.stopped


def test_waiting_for_response_headers_is_still_active_and_blocks_next_feature(tmp_path):
    mod=module()
    import threading
    entered=threading.Event()
    release=threading.Event()
    def opener(req,**kwargs):
        entered.set()
        release.wait(2)
        return FakeResponse({'model':'space-bunny-free'})
    ctx=mod.LiveContext(tmp_path,'fake',{'a':1,'b':1},opener=opener)
    ctx.start_feature('a')
    def call():
        with ctx.send(request(),timeout=20) as response:
            response.read()
    thread=threading.Thread(target=call,daemon=True)
    thread.start()
    assert entered.wait(1)
    try:
        assert 'http_status' not in ctx.requests[0]
        with pytest.raises(mod.FeatureTimeout):
            mod.verify_completed_requests(ctx,0,'a')
        assert ctx.stopped
    finally:
        release.set()
        thread.join(2)


def test_persisted_scenario_session_drift_fails_live_witness(tmp_path):
    mod=module()
    ctx=mod.LiveContext(tmp_path,'fake',{'scenario_multiturn':2})
    ctx.requests=[{'feature':'scenario_multiturn','active':False,'session_sha256':value}
                  for value in ('first','second')]
    with pytest.raises(ValueError,match='session'):
        mod.verify_completed_requests(ctx,0,'scenario_multiturn')


def test_only_judge_features_have_a_bounded_larger_output_cap(tmp_path):
    mod=module()
    ctx=mod.LiveContext(tmp_path,'fake',{'nonstream':1,'judge_pairwise':2},opener=lambda *a,**kw:
        FakeResponse({'model':'space-bunny-free','choices':[{'finish_reason':'length'}]}))
    ctx.start_feature('nonstream')
    with pytest.raises(ValueError):
        ctx.send(request(max_tokens=1024),timeout=20)
    ctx.start_feature('judge_pairwise')
    assert ctx.provider_config()['max_output_tokens']==1024
    with ctx.send(request(max_tokens=1024),timeout=20) as response:
        response.read()
    assert ctx.requests[-1]['finish_reason']=='length'
    with pytest.raises(ValueError):
        ctx.send(request(max_tokens=1025),timeout=20)


def test_failure_report_only_accepts_typed_allowlisted_judge_diagnostics():
    mod=module()
    from scripts.opencode_go_judge_checks import JudgeWitnessFailure
    error=JudgeWitnessFailure('judge_output_not_scored',{
        'parse_failure_count':2,'completion_tokens':'secret','secret':'secret','billed_calls':True})
    assert mod.safe_failure_fields(error)=={'error_code':'judge_output_not_scored',
        'diagnostics':{'parse_failure_count':2}}
    assert mod.safe_failure_fields(RuntimeError('secret'))=={}


def test_judge_diagnostic_timeout_is_sixty_but_other_features_stay_twenty(tmp_path):
    mod=module()
    timeouts=[]
    ctx=mod.LiveContext(tmp_path,'fake',{'nonstream':1,'judge_pairwise':2},opener=lambda req,**kw:
        timeouts.append(kw['timeout']) or FakeResponse({'model':'space-bunny-free'}))
    ctx.start_feature('nonstream')
    with ctx.send(request(),timeout=99):
        pass
    ctx.start_feature('judge_pairwise')
    assert ctx.provider_config()['timeout']==60
    with ctx.send(request(max_tokens=1024),timeout=99):
        pass
    assert timeouts[0]<=20 and 59<timeouts[1]<=60


def test_absolute_feature_deadline_includes_setup_and_last_read_time(tmp_path):
    mod=module()
    clock=[0.0]
    ctx=mod.LiveContext(tmp_path,'fake',{'nonstream':1},clock=lambda:clock[0])
    ctx.start_feature('nonstream')
    def finishes_late(_):
        clock[0]=21.0
        return {'ok':True}
    with pytest.raises(mod.FeatureTimeout):
        mod.run_with_deadline(ctx,finishes_late,timeout=20)
    assert ctx.stopped
