"""OpenCode Go wire requirements: real conversation IDs, truthful UA, no secrets."""
import json
from urllib.error import HTTPError

import pytest

from motte_agent.builtin_react import BuiltinReActRuntime
from motte_contracts.messages import Message, ModelRequest
from motte_provider.openai_compatible import CaseDrivenProvider, OpenAICompatibleProvider
from motte_provider.transport import HTTPTransport
from tests.provider.test_openai_compatible import FakeResponse, chat_body
from tests.provider.test_streaming_contracts import FakeSSEResponse, _sse_lines

BASE = "https://opencode.ai/zen/go/v1"


def request(session=None):
    return ModelRequest(model="space-bunny-free", messages=[Message(role="user", content="Fix a bug")],
                        metadata={"session_id": session} if session is not None else {})


def test_go_headers_follow_each_conversation_without_leaking_into_body():
    seen = []
    transport = HTTPTransport(BASE, "test-secret", opener=lambda req, **kw:
                              seen.append(req) or FakeResponse(chat_body()))
    provider = OpenAICompatibleProvider(transport, "space-bunny-free")
    for session in ("session-a", "session-a", "session-b"):
        provider.complete(request(session))
    assert [r.get_header("X-opencode-session") for r in seen] == [
        "session-a", "session-a", "session-b"]
    assert all(r.get_header("User-agent") == "MoTTEavl/0.1.0" for r in seen)
    assert all("metadata" not in json.loads(r.data) for r in seen)
    assert "test-secret" not in json.dumps(provider.calls)


@pytest.mark.parametrize("session", [None, "", "bad\r\nheader", 42])
def test_go_missing_or_invalid_session_fails_before_network(session):
    seen = []
    provider = OpenAICompatibleProvider(HTTPTransport(BASE, opener=lambda *a, **k: seen.append(a)),
                                        "space-bunny-free")
    with pytest.raises(ValueError, match="session_id"):
        provider.complete(request(session))
    assert seen == []


def test_go_retry_reuses_session_header():
    seen = []
    def opener(req, **kwargs):
        seen.append(req)
        if len(seen) == 1:
            raise HTTPError(req.full_url, 503, "busy", {}, None)
        return FakeResponse(chat_body())
    transport = HTTPTransport(BASE, opener=opener, sleep=lambda _: None, max_retries=1,
                              default_headers={"Idempotency-Key": "test-attempt"})
    OpenAICompatibleProvider(transport, "space-bunny-free").complete(request("session-a"))
    assert len(seen) == 2
    assert {r.get_header("X-opencode-session") for r in seen} == {"session-a"}


def test_go_stream_has_same_conversation_headers():
    seen = []
    response = FakeSSEResponse(_sse_lines([
        {"choices": [{"delta": {"content": "fixed"}, "finish_reason": "stop"}]}]))
    transport = HTTPTransport(BASE, opener=lambda req, **kw: seen.append(req) or response)
    provider = OpenAICompatibleProvider(transport, "space-bunny-free")
    assert list(provider.stream(request("stream-session")))[-1]["type"] == "finish"
    assert seen[0].get_header("X-opencode-session") == "stream-session"
    assert seen[0].get_header("User-agent") == "MoTTEavl/0.1.0"


def test_builtin_session_survives_tool_turns_and_changes_between_cases():
    requests = []
    outputs = iter([
        {"tool_calls": [{"id": "c1", "name": "read_file", "arguments": '{"path":"a.py"}'}]},
        {"content": "fixed"}, {"content": "another turn"}, {"content": "new case"},
    ])
    def complete(req):
        requests.append(req)
        return next(outputs)
    runtime = BuiltinReActRuntime(complete, {"read_file": lambda arg: "a = 1"}, mode="native-tool")
    session = runtime.begin()
    runtime.send("Inspect a.py")
    runtime.send("Explain the fix")
    runtime.close()
    runtime.run_agent("New independent coding case")
    assert [r.metadata.get("session_id") for r in requests[:3]] == [session.session_id] * 3
    assert requests[3].metadata.get("session_id") != session.session_id
    assert requests[3].metadata.get("session_id")
    assert requests[1].messages[-1].tool_call_id == "c1"


def test_case_invocations_get_independent_sessions():
    seen = []
    transport = HTTPTransport(BASE, opener=lambda req, **kw:
                              seen.append(req) or FakeResponse(chat_body()))
    cases = CaseDrivenProvider(OpenAICompatibleProvider(transport, "space-bunny-free"),
                               {"a": {"prompt": "Fix a"}, "b": {"prompt": "Fix b"}})
    cases.invoke("a")
    cases.invoke("b")
    sessions = [r.get_header("X-opencode-session") for r in seen]
    assert all(sessions) and sessions[0] != sessions[1]


def test_existing_single_call_smoke_supplies_session_metadata():
    from motte_cli.smoke import run_live_smoke
    seen = []
    transport = HTTPTransport(BASE, opener=lambda req, **kw:
                              seen.append(req) or FakeResponse(chat_body()))
    run_live_smoke("openai_compatible", "space-bunny-free", base_url=BASE,
                   api_key=None, prompt="Explain the Python bug", transport=transport)
    assert seen[0].get_header("X-opencode-session")


def test_unrelated_provider_does_not_receive_opencode_session_header():
    seen = []
    transport = HTTPTransport("https://other.invalid/v1", opener=lambda req, **kw:
                              seen.append(req) or FakeResponse(chat_body()))
    OpenAICompatibleProvider(transport, "test-model").complete(request("private-session"))
    assert seen[0].get_header("X-opencode-session") is None
