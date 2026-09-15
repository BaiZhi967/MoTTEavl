import json
from urllib.error import HTTPError

import pytest

from motte_contracts.messages import Message, ModelRequest
from motte_provider.capabilities import UnsupportedParameterError
from motte_provider.config import build_case_provider, validate_provider_config
from motte_provider.openai_compatible import CaseDrivenProvider, OpenAICompatibleProvider, ProviderCallError
from motte_provider.pricing import cost_detail, estimate_cost, parse_price_table
from motte_provider.transport import HTTPTransport

SECRET = "sk-test-secret-123"


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def chat_body(content="hi", prompt_tokens=2, completion_tokens=3):
    return {
        "id": "chatcmpl-1",
        "model": "test-model",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def make_provider(opener, *, parameters=None, price_table=None):
    state = {"now": 0.0}

    def clock():
        state["now"] += 0.15
        return state["now"]

    transport = HTTPTransport(
        "https://api.example.test/v1",
        SECRET,
        opener=opener,
        sleep=lambda _: None,
        clock=clock,
    )
    return OpenAICompatibleProvider(transport, "test-model", parameters=parameters, price_table=price_table)


def test_complete_returns_metered_envelope_with_redacted_canonical():
    opener = lambda request, *, timeout: FakeResponse(chat_body())  # noqa: E731
    provider = make_provider(opener)
    envelope = provider.complete(ModelRequest(model="test-model", messages=[Message(role="user", content="ping")]))
    assert envelope["content"] == "hi"
    assert envelope["usage"] == {"prompt_tokens": 2, "completion_tokens": 3}
    assert envelope["metering"] == {"latency_ms": 150.0, "attempts": 1, "retry_count": 0, "error_class": None}
    assert envelope["cost"] is None
    assert envelope["canonical"]["request"]["path"] == "/chat/completions"
    assert envelope["canonical"]["request"]["body"]["model"] == "test-model"
    assert envelope["canonical"]["response"]["status"] == 200
    assert provider.calls == [envelope]
    dumped = json.dumps(envelope)
    assert SECRET not in dumped
    assert "Authorization" not in dumped


def test_request_body_merges_provider_and_request_level_parameters():
    captured = []

    def opener(request, *, timeout):
        captured.append(json.loads(request.data))
        return FakeResponse(chat_body())

    provider = make_provider(opener, parameters={"temperature": 0.2})
    provider.complete(
        ModelRequest(
            model="test-model",
            messages=[Message(role="user", content="ping")],
            temperature=0.5,
            max_output_tokens=100,
            system="be brief",
        )
    )
    body = captured[0]
    assert body["temperature"] == 0.5
    assert body["max_tokens"] == 100
    assert body["messages"][0] == {"role": "system", "content": "be brief"}
    assert body["messages"][1] == {"role": "user", "content": "ping"}


def test_unsupported_parameter_fails_before_any_paid_call():
    calls = []

    def opener(request, *, timeout):
        calls.append(request)
        return FakeResponse(chat_body())

    provider = make_provider(opener)
    with pytest.raises(UnsupportedParameterError):
        provider.complete(
            ModelRequest(model="test-model", messages=[Message(role="user", content="x")], temperature=5.0)
        )
    with pytest.raises(UnsupportedParameterError):
        OpenAICompatibleProvider(
            HTTPTransport("https://api.example.test", opener=opener), "m", parameters={"logit_bias": 0}
        )
    assert calls == []


def test_429_retry_records_retry_count():
    attempts = []

    def opener(request, *, timeout):
        attempts.append(1)
        if len(attempts) == 1:
            raise HTTPError(request.full_url, 429, "busy", {"Retry-After": "0"}, None)
        return FakeResponse(chat_body())

    envelope = make_provider(opener).complete(
        ModelRequest(model="test-model", messages=[Message(role="user", content="x")])
    )
    assert envelope["metering"]["attempts"] == 2
    assert envelope["metering"]["retry_count"] == 1
    assert "error" not in envelope


def test_auth_failure_is_classified_and_carries_evidence():
    def opener(request, *, timeout):
        raise HTTPError(request.full_url, 401, "unauthorized", {}, None)

    provider = make_provider(opener)
    with pytest.raises(ProviderCallError) as excinfo:
        provider.complete(ModelRequest(model="test-model", messages=[Message(role="user", content="x")]))
    error = excinfo.value
    assert error.error_class == "auth"
    evidence = error.evidence
    assert evidence["error"]["class"] == "auth"
    assert evidence["canonical"]["request"]["body"]["model"] == "test-model"
    assert evidence["canonical"]["response"] is None
    assert SECRET not in json.dumps(evidence)


def test_cost_uses_price_table_version_snapshot():
    table = parse_price_table(
        {"version": "2026-09-15", "input_per_million": 1.0, "output_per_million": 2.0}
    )
    envelope = make_provider(lambda r, *, timeout: FakeResponse(chat_body()), price_table=table).complete(
        ModelRequest(model="test-model", messages=[Message(role="user", content="x")])
    )
    assert envelope["cost"]["total"] == 0.000008
    assert envelope["cost"]["price_table_version"] == "2026-09-15"


def test_unknown_prices_stay_null():
    unknown = parse_price_table({"version": "v-empty"})
    assert unknown.input_per_million is None
    assert estimate_cost(unknown, {"prompt_tokens": 10, "completion_tokens": 10}) is None
    assert cost_detail(unknown, {"prompt_tokens": 10}) is None
    assert cost_detail(None, {"prompt_tokens": 10}) is None


def test_nested_secrets_in_request_body_are_redacted():
    secret_payload = [{"type": "text", "text": "x", "api_key": SECRET, "authorization": SECRET}]

    opener = lambda request, *, timeout: FakeResponse(chat_body())  # noqa: E731
    provider = make_provider(opener)
    envelope = provider.complete(
        ModelRequest(model="test-model", messages=[Message(role="user", content=secret_payload)])
    )
    body = envelope["canonical"]["request"]["body"]["messages"][0]["content"]
    assert body[0]["api_key"] == "[REDACTED]"
    assert body[0]["authorization"] == "[REDACTED]"
    assert SECRET not in json.dumps(envelope)


def test_case_driven_provider_maps_cases_to_requests():
    captured = []

    def opener(request, *, timeout):
        captured.append(json.loads(request.data))
        return FakeResponse(chat_body(content="42"))

    cases = {
        "case-1": {"prompt": "1+1=?", "system": "calculator", "expected": "42"},
    }
    driven = CaseDrivenProvider(make_provider(opener), cases)
    envelope = driven.invoke("case-1")
    assert envelope["content"] == "42"
    assert driven.expected_for("case-1") == "42"
    body = captured[0]
    assert body["messages"] == [
        {"role": "system", "content": "calculator"},
        {"role": "user", "content": "1+1=?"},
    ]
    with pytest.raises(KeyError):
        driven.invoke("missing-case")


def test_validate_provider_config_requires_shape_and_strict_params():
    with pytest.raises(ValueError, match="kind"):
        validate_provider_config({"kind": "anthropic", "base_url": "x", "model": "m"})
    with pytest.raises(ValueError, match="base_url"):
        validate_provider_config({"kind": "openai_compatible", "model": "m"})
    with pytest.raises(UnsupportedParameterError):
        validate_provider_config(
            {"kind": "openai_compatible", "base_url": "https://x", "model": "m", "parameters": {"foo": 1}}
        )
    validate_provider_config(
        {
            "kind": "openai_compatible",
            "base_url": "https://x",
            "model": "m",
            "parameters": {"temperature": 0.3},
            "price_table": {"version": "v1", "input_per_million": 1, "output_per_million": 2},
        }
    )


def test_build_case_provider_reads_api_key_from_env(monkeypatch):
    monkeypatch.setenv("MY_PROVIDER_KEY", SECRET)
    created = {}

    class FakeTransport:
        def __init__(self, base_url, api_key, **kwargs):
            created["api_key"] = api_key
            created["kwargs"] = kwargs

    import motte_provider.config as config_module

    monkeypatch.setattr(config_module, "HTTPTransport", FakeTransport)
    provider = build_case_provider(
        {
            "kind": "openai_compatible",
            "base_url": "https://x",
            "model": "m",
            "api_key_env": "MY_PROVIDER_KEY",
            "backoff_initial_ms": 750,
            "backoff_max_ms": 5000,
        },
        {},
    )
    assert created["api_key"] == SECRET
    assert created["kwargs"] == {"timeout": 30.0, "max_retries": 2, "backoff_initial": 0.75, "backoff_max": 5.0}
    assert isinstance(provider, CaseDrivenProvider)
