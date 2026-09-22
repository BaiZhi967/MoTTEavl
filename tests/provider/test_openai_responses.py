"""openai_responses 适配器：请求构造、归一化、错误映射（离线 fixture）。"""
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from motte_contracts.messages import Message, ModelRequest
from motte_provider.capabilities import UnsupportedParameterError
from motte_provider.openai_responses import OpenAIResponsesProvider, validate_config
from motte_provider.pricing import parse_price_table
from motte_provider.transport import HTTPTransport

SECRET = "sk-openai-test-secret"
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


def make_provider(opener, *, parameters=None, price_table=None):
    transport = HTTPTransport("https://api.openai.test/v1", SECRET, opener=opener, sleep=lambda _: None)
    return OpenAIResponsesProvider(transport, "gpt-4o-mini", parameters=parameters, price_table=price_table)


def complete_with(provider, request=None):
    return provider.complete(request or ModelRequest(
        model="gpt-4o-mini", messages=[Message(role="user", content="hello")]
    ))


def test_request_body_uses_instructions_and_input():
    captured = []

    def opener(request, *, timeout):
        captured.append(json.loads(request.data))
        return FakeResponse(fixture("openai_responses_text"))

    envelope = complete_with(make_provider(opener), ModelRequest(
        model="gpt-4o-mini",
        messages=[Message(role="user", content="hello")],
        system="be terse",
        temperature=0.2,
    ))
    body = captured[0]
    assert body["model"] == "gpt-4o-mini"
    assert body["instructions"] == "be terse"
    assert body["input"] == [{"role": "user", "content": "hello"}]
    assert body["temperature"] == 0.2
    assert "messages" not in body
    assert envelope["provider"] == "openai_responses"
    assert envelope["requested_model"] == "gpt-4o-mini"
    assert envelope["reported_model"] == "gpt-4o-mini-2024-07-18"
    assert envelope["resolved_model_identity"] is None
    assert envelope["identity_policy_result"] == "mismatch"
    assert envelope["canonical"]["request"]["path"] == "/responses"


def test_request_body_flattens_tools_and_maps_tool_choice():
    captured = []

    def opener(request, *, timeout):
        captured.append(json.loads(request.data))
        return FakeResponse(fixture("openai_responses_function_call"))

    complete_with(make_provider(opener), ModelRequest(
        model="gpt-4o-mini",
        messages=[Message(role="user", content="weather in SF?")],
        tools=[CANONICAL_TOOL],
        tool_choice={"type": "function", "function": {"name": "get_weather"}},
        max_output_tokens=256,
    ))
    body = captured[0]
    assert body["tools"] == [{
        "type": "function",
        "name": "get_weather",
        "description": "query weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    }]
    assert body["tool_choice"] == {"type": "function", "name": "get_weather"}
    assert body["max_output_tokens"] == 256


def test_flat_canonical_tool_history_and_malformed_response():
    captured = []

    def opener(request, *, timeout):
        captured.append(json.loads(request.data))
        return FakeResponse(fixture("openai_responses_text"))

    complete_with(make_provider(opener), ModelRequest(
        model="gpt-4o-mini",
        messages=[
            Message(role="assistant", content="", tool_calls=[
                {"id": "call_1", "name": "get_weather", "arguments": '{"city":"SF"}'},
            ]),
            Message(role="tool", content="sunny", tool_call_id="call_1"),
        ],
    ))
    assert captured[0]["input"] == [
        {"type": "function_call", "call_id": "call_1",
         "name": "get_weather", "arguments": '{"city":"SF"}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "sunny"},
    ]
    from motte_provider.base import ProviderCallError

    invalid = make_provider(lambda request, *, timeout: FakeResponse({"output": {}}))
    with pytest.raises(ProviderCallError) as failure:
        complete_with(invalid)
    assert failure.value.error_class == "protocol"
    assert failure.value.evidence["canonical"]["response"]["body"] == {"output": {}}


def test_seed_and_stop_are_strictly_rejected():
    with pytest.raises(UnsupportedParameterError):
        OpenAIResponsesProvider(
            HTTPTransport("https://api.openai.test/v1"), "gpt-4o-mini", parameters={"seed": 1},
        )
    with pytest.raises(UnsupportedParameterError):
        validate_config({"kind": "openai_responses", "base_url": "https://x", "model": "m", "parameters": {"stop": ["\n"]}})


def test_text_response_normalizes_reasoning_and_cache_usage():
    envelope = complete_with(make_provider(lambda request, *, timeout: FakeResponse(fixture("openai_responses_text"))))
    assert envelope["content"] == "Hi there"
    assert envelope["finish_reason"] == "stop"
    assert envelope["usage"] == {"prompt_tokens": 92, "completion_tokens": 17, "total_tokens": 109}
    assert envelope["usage_details"] == {"cached_tokens": 64, "reasoning_tokens": 90}
    assert envelope["response_id"] == "resp_67ccf18e2a1c8e70b1e8f9a7e0d1f2a3"
    assert envelope["tool_calls"] == []


def test_function_call_response_normalizes_tool_calls():
    envelope = complete_with(make_provider(lambda request, *, timeout: FakeResponse(fixture("openai_responses_function_call"))))
    assert envelope["content"] == ""
    assert envelope["finish_reason"] == "stop"
    assert envelope["tool_calls"] == [{
        "id": "call_abc123",
        "name": "get_weather",
        "arguments": "{\"city\": \"San Francisco\"}",
    }]


def test_incomplete_response_maps_to_length():
    envelope = complete_with(make_provider(lambda request, *, timeout: FakeResponse(fixture("openai_responses_incomplete"))))
    assert envelope["content"] == "Truncated ans"
    assert envelope["finish_reason"] == "length"  # incomplete + max_output_tokens


def test_tool_round_trip_messages_flatten_to_items():
    captured = []

    def opener(request, *, timeout):
        captured.append(json.loads(request.data))
        return FakeResponse(fixture("openai_responses_function_call"))

    complete_with(make_provider(opener), ModelRequest(
        model="gpt-4o-mini",
        messages=[
            Message(role="user", content="weather in SF?"),
            Message(role="assistant", content="", tool_calls=[{
                "id": "call_abc123", "type": "function",
                "function": {"name": "get_weather", "arguments": "{\"city\": \"SF\"}"},
            }]),
            Message(role="tool", content="18C", tool_call_id="call_abc123"),
        ],
    ))
    items = captured[0]["input"]
    assert items[0] == {"role": "user", "content": "weather in SF?"}
    assert items[1] == {"type": "function_call", "call_id": "call_abc123", "name": "get_weather", "arguments": "{\"city\": \"SF\"}"}
    assert items[2] == {"type": "function_call_output", "call_id": "call_abc123", "output": "18C"}


def test_error_body_message_is_carried_into_evidence():
    def opener(request, *, timeout):
        error = HTTPError(request.full_url, 429, "Too Many Requests", {}, None)
        error.read = lambda: json.dumps({
            "error": {"message": "Rate limit reached", "type": "rate_limit_exceeded", "code": 429},
        }).encode()
        raise error

    from motte_provider.base import ProviderCallError

    with pytest.raises(ProviderCallError) as caught:
        complete_with(make_provider(opener))
    evidence = caught.value.evidence
    assert evidence["error"]["class"] == "rate_limit"
    assert "Rate limit reached" in evidence["error"]["message"]
    assert "rate_limit_exceeded" in evidence["error"]["message"]
    assert SECRET not in json.dumps(evidence)


def test_price_snapshot_applies_to_responses_usage():
    table = parse_price_table({"version": "resp-2026-09", "input_per_million": 0.15, "output_per_million": 0.6})
    envelope = complete_with(
        make_provider(lambda request, *, timeout: FakeResponse(fixture("openai_responses_text")), price_table=table)
    )
    assert envelope["cost"]["total"] == round((92 * 0.15 + 17 * 0.6) / 1_000_000, 8)
    assert envelope["cost"]["price_table_version"] == "resp-2026-09"
