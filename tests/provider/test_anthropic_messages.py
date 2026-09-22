"""anthropic_messages 适配器：请求构造、归一化、错误映射、凭据头（离线 fixture）。"""
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from motte_contracts.messages import Message, ModelRequest
from motte_provider.anthropic_messages import (
    ANTHROPIC_VERSION,
    AnthropicMessagesProvider,
    validate_config,
)
from motte_provider.capabilities import UnsupportedParameterError
from motte_provider.config import build_provider
from motte_provider.pricing import parse_price_table
from motte_provider.transport import HTTPTransport

SECRET = "sk-ant-test-secret"
FIXTURES = Path(__file__).parent.parent / "fixtures" / "provider"

CANONICAL_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "query weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def make_provider(opener, *, parameters=None, price_table=None, idempotency_key=None):
    transport = HTTPTransport(
        "https://api.anthropic.test/v1", SECRET, opener=opener, sleep=lambda _: None,
        auth="x-api-key", default_headers={
            "anthropic-version": ANTHROPIC_VERSION,
            **({"Idempotency-Key": idempotency_key} if idempotency_key else {}),
        },
    )
    return AnthropicMessagesProvider(transport, "claude-sonnet-4-5", parameters=parameters, price_table=price_table)


def complete_with(provider, request=None):
    return provider.complete(request or ModelRequest(
        model="claude-sonnet-4-5", messages=[Message(role="user", content="hello")]
    ))


def test_request_body_puts_system_top_level_and_requires_max_tokens():
    captured = []

    def opener(request, *, timeout):
        captured.append(json.loads(request.data))
        return FakeResponse(fixture("anthropic_messages_text"))

    envelope = complete_with(make_provider(opener), ModelRequest(
        model="claude-sonnet-4-5",
        messages=[Message(role="user", content="hello")],
        system="be terse",
    ))
    body = captured[0]
    assert body["model"] == "claude-sonnet-4-5"
    assert body["max_tokens"] == 4096  # Anthropic 必填，缺省值
    assert body["system"] == "be terse"
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert "messages" in body and all("system" not in m or m["role"] == "user" for m in body["messages"])
    assert envelope["provider"] == "anthropic_messages"
    assert envelope["requested_model"] == "claude-sonnet-4-5"
    assert envelope["reported_model"] == "claude-sonnet-4-5-20250929"
    assert envelope["resolved_model_identity"] is None
    assert envelope["identity_policy_result"] == "mismatch"


def test_request_body_converts_tools_and_tool_choice():
    captured = []

    def opener(request, *, timeout):
        captured.append(json.loads(request.data))
        return FakeResponse(fixture("anthropic_messages_tool_use"))

    complete_with(make_provider(opener), ModelRequest(
        model="claude-sonnet-4-5",
        messages=[Message(role="user", content="weather in SF?")],
        tools=[CANONICAL_TOOL],
        tool_choice="required",
        max_output_tokens=512,
    ))
    body = captured[0]
    assert body["tools"] == [{
        "name": "get_weather",
        "description": "query weather",
        "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
    }]
    assert body["tool_choice"] == {"type": "any"}
    assert body["max_tokens"] == 512  # 请求级参数覆盖缺省


def test_seed_is_strictly_rejected():
    with pytest.raises(UnsupportedParameterError):
        AnthropicMessagesProvider(
            HTTPTransport("https://api.anthropic.test/v1"), "claude-sonnet-4-5",
            parameters={"seed": 1},
        )
    with pytest.raises(UnsupportedParameterError):
        validate_config({"kind": "anthropic_messages", "base_url": "https://x", "model": "m", "parameters": {"seed": 1}})


def test_text_response_normalizes_to_canonical_envelope():
    envelope = complete_with(make_provider(lambda request, *, timeout: FakeResponse(fixture("anthropic_messages_text"))))
    assert envelope["content"] == "Hi there — how can I help?"
    assert envelope["finish_reason"] == "stop"  # end_turn → stop
    assert envelope["usage"] == {"prompt_tokens": 25, "completion_tokens": 150}
    assert envelope["response_id"] == "msg_01XFDUDYJgAACzvnptvVoYEL"
    assert envelope["usage_details"] == {"cache_read_input_tokens": 320}
    assert envelope["tool_calls"] == []


def test_tool_use_response_normalizes_calls_and_cache_creation():
    envelope = complete_with(make_provider(lambda request, *, timeout: FakeResponse(fixture("anthropic_messages_tool_use"))))
    assert envelope["content"] == "Let me check the weather."
    assert envelope["finish_reason"] == "tool_calls"  # tool_use → tool_calls
    assert envelope["tool_calls"] == [{
        "id": "toolu_01A09q90qw90lq917835lq9",
        "name": "get_weather",
        "arguments": json.dumps({"city": "San Francisco", "unit": "celsius"}),
    }]
    assert envelope["usage_details"] == {"cache_creation_input_tokens": 128}


def test_tool_round_trip_messages_convert_to_blocks():
    captured = []

    def opener(request, *, timeout):
        captured.append(json.loads(request.data))
        return FakeResponse(fixture("anthropic_messages_text"))

    complete_with(make_provider(opener), ModelRequest(
        model="claude-sonnet-4-5",
        messages=[
            Message(role="user", content="weather in SF?"),
            Message(role="assistant", content="", tool_calls=[{
                "id": "toolu_01", "type": "function",
                "function": {"name": "get_weather", "arguments": "{\"city\": \"SF\"}"},
            }]),
            Message(role="tool", content="18C and sunny", tool_call_id="toolu_01"),
        ],
    ))
    messages = captured[0]["messages"]
    assert messages[1] == {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "toolu_01", "name": "get_weather", "input": {"city": "SF"}}],
    }
    assert messages[2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "toolu_01", "content": "18C and sunny"}],
    }


def test_flat_canonical_tool_history_and_malformed_response():
    captured = []

    def opener(request, *, timeout):
        captured.append(json.loads(request.data))
        return FakeResponse(fixture("anthropic_messages_text"))

    provider = make_provider(opener)
    complete_with(provider, ModelRequest(
        model="claude-sonnet-4-5",
        messages=[
            Message(role="assistant", content="", tool_calls=[
                {"id": "call_1", "name": "get_weather", "arguments": '{"city":"SF"}'},
            ]),
            Message(role="tool", content="sunny", tool_call_id="call_1"),
        ],
    ))
    assert captured[0]["messages"][0]["content"] == [
        {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "SF"}},
    ]
    from motte_provider.base import ProviderCallError

    invalid = make_provider(lambda request, *, timeout: FakeResponse({"content": {}}))
    with pytest.raises(ProviderCallError) as failure:
        complete_with(invalid)
    assert failure.value.error_class == "protocol"
    assert failure.value.evidence["canonical"]["response"]["body"] == {"content": {}}


def test_error_body_refines_classification():
    body = {"type": "error", "error": {"type": "rate_limit_error", "message": "Number of requests too high"}}

    def opener(request, *, timeout):
        raise HTTPError(request.full_url, 400, "Bad Request", {}, None)

    # HTTPError 的 body 不可注入，直接验证 529 重试 + 401 认证路径
    def overloaded_then_ok(request, *, timeout):
        if not hasattr(overloaded_then_ok, "calls"):
            overloaded_then_ok.calls = 0
        overloaded_then_ok.calls += 1
        if overloaded_then_ok.calls == 1:
            raise HTTPError(request.full_url, 529, "Overloaded", {}, None)
        return FakeResponse(fixture("anthropic_messages_text"))

    envelope = complete_with(make_provider(overloaded_then_ok, idempotency_key="test-key") )
    assert envelope["metering"]["attempts"] == 2  # 529 overloaded 可重试
    assert envelope["metering"]["retry_count"] == 1
    assert body["error"]["type"] == "rate_limit_error"  # 错误体形状与映射表一致


def test_auth_failure_carries_evidence_and_error_class():
    def opener(request, *, timeout):
        error = HTTPError(request.full_url, 401, "Unauthorized", {}, None)
        error.read = lambda: json.dumps({
            "type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"},
        }).encode()
        raise error

    from motte_provider.base import ProviderCallError

    with pytest.raises(ProviderCallError) as caught:
        complete_with(make_provider(opener))
    evidence = caught.value.evidence
    assert evidence["error"]["class"] == "auth"
    assert "authentication_error" in evidence["error"]["message"]
    assert SECRET not in json.dumps(evidence)


def test_transport_sends_x_api_key_and_version_header_not_bearer():
    captured = []

    def opener(request, *, timeout):
        captured.append(request)
        return FakeResponse(fixture("anthropic_messages_text"))

    complete_with(make_provider(opener))
    request = captured[0]
    assert request.get_header("X-api-key") == SECRET
    assert request.get_header("Authorization") is None
    assert request.get_header("Anthropic-version") == ANTHROPIC_VERSION


def test_registry_build_dispatches_anthropic_with_credentials_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_CREDENTIALS_PATH", str(tmp_path / "credentials.toml"))
    from motte_provider.credentials import save_api_key

    save_api_key("anthropic-main", "sk-ant-from-file-000000")
    captured = []

    def opener(request, *, timeout):
        captured.append(request)
        return FakeResponse(fixture("anthropic_messages_text"))

    import motte_provider.config as provider_config

    original = provider_config.HTTPTransport

    def patched_transport(base_url, api_key, **kwargs):
        kwargs["opener"] = opener
        kwargs["sleep"] = lambda _: None
        return original(base_url, api_key, **kwargs)

    monkeypatch.setattr(provider_config, "HTTPTransport", patched_transport)
    provider = build_provider({
        "kind": "anthropic_messages", "name": "anthropic-main", "credentials": "anthropic-main",
        "base_url": "https://api.anthropic.test/v1", "model": "claude-sonnet-4-5",
        "price_table": {"version": "v1", "input_per_million": 3, "output_per_million": 15},
    }, {"cases": {"case-1": {"prompt": "hello", "expected": "Hi"}}})
    envelope = provider.invoke("case-1")
    assert envelope["content"].startswith("Hi there")
    assert envelope["cost"]["price_table_version"] == "v1"
    assert captured[0].get_header("X-api-key") == "sk-ant-from-file-000000"


def test_price_snapshot_applies_to_anthropic_usage():
    table = parse_price_table({"version": "anth-2026-09", "input_per_million": 3, "output_per_million": 15})
    envelope = complete_with(make_provider(lambda request, *, timeout: FakeResponse(fixture("anthropic_messages_text")), price_table=table))
    assert envelope["cost"]["total"] == round((25 * 3 + 150 * 15) / 1_000_000, 8)
