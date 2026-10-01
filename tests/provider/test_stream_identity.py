"""Offline SSE identity policies share complete()'s explicit alias semantics."""
from copy import deepcopy

import pytest

from motte_provider.anthropic_messages import AnthropicMessagesProvider
from motte_provider.errors import ProviderHTTPError
from motte_provider.openai_compatible import OpenAICompatibleProvider
from motte_provider.openai_responses import OpenAIResponsesProvider
from tests.provider.test_streaming_contracts import (
    ANTHROPIC_STREAM,
    CHAT_STREAM,
    RESPONSES_STREAM,
    FakeSSEResponse,
    _request,
    _sse_lines,
    _transport_with,
)

PROVIDERS = [OpenAICompatibleProvider, OpenAIResponsesProvider, AnthropicMessagesProvider]
STRICT_POLICIES = ["require_reported", "require_match"]
MISSING = object()


def identity_event(cls, reported):
    if cls is OpenAICompatibleProvider:
        event = {"choices": []}
        container = event
    elif cls is OpenAIResponsesProvider:
        event = {"type": "response.created", "response": {}}
        container = event["response"]
    else:
        event = {"type": "message_start", "message": {}}
        container = event["message"]
    if reported is not MISSING:
        container["model"] = reported
    return event


def stream_events(cls, reported):
    fixtures = {
        OpenAICompatibleProvider: CHAT_STREAM,
        OpenAIResponsesProvider: RESPONSES_STREAM,
        AnthropicMessagesProvider: ANTHROPIC_STREAM,
    }
    events = deepcopy(fixtures[cls])
    first = identity_event(cls, reported)
    if cls is OpenAICompatibleProvider:
        if reported is not MISSING:
            events[0]["model"] = reported
    else:
        key = "response" if cls is OpenAIResponsesProvider else "message"
        events[0][key].update(first[key])
    return events


def provider_with(cls, events, **config):
    transport, response = _transport_with(_sse_lines(events))
    return cls(transport, "m1", **config), response


@pytest.mark.parametrize("cls", PROVIDERS)
@pytest.mark.parametrize("policy", STRICT_POLICIES)
@pytest.mark.parametrize("reported", [MISSING, None, "", "  ", 42, False, {"model": "m1"}])
def test_strict_stream_missing_identity_never_finishes(cls, policy, reported):
    provider, response = provider_with(cls, stream_events(cls, reported), identity_policy=policy)
    delivered = []
    with pytest.raises(ProviderHTTPError, match="did not report a model identity") as caught:
        delivered.extend(provider.stream(_request()))
    assert caught.value.error_class == "model_identity"
    assert delivered and all(event["type"] != "finish" for event in delivered)
    assert response.closed


@pytest.mark.parametrize("cls", PROVIDERS)
def test_require_match_rejects_mismatch_before_yielding_chunk(cls):
    provider, response = provider_with(
        cls, stream_events(cls, "other-model"), identity_policy="require_match",
    )
    delivered = []
    with pytest.raises(ProviderHTTPError, match="does not match") as caught:
        delivered.extend(provider.stream(_request()))
    assert caught.value.error_class == "model_identity"
    assert delivered == []
    assert response.closed


@pytest.mark.parametrize("cls", PROVIDERS)
@pytest.mark.parametrize("policy", STRICT_POLICIES)
@pytest.mark.parametrize("reported", ["m1", "vendor/m1-v2"])
def test_strict_stream_accepts_exact_and_explicit_versioned_alias(cls, policy, reported):
    aliases = {"vendor/m1-v2": "m1"}
    provider, response = provider_with(
        cls, stream_events(cls, reported), identity_policy=policy,
        identity_aliases=aliases, identity_alias_version="profile-v2",
    )
    aliases["vendor/m1-v2"] = "changed-outside-provider"
    events = list(provider.stream(_request()))
    assert [event["type"] for event in events].count("finish") == 1
    assert events[-1]["type"] == "finish"
    assert events[-1]["payload"]["usage_reported"] is True
    assert response.closed


@pytest.mark.parametrize("cls", PROVIDERS)
def test_require_reported_accepts_consistent_mismatch(cls):
    provider, response = provider_with(
        cls, stream_events(cls, "other-model"), identity_policy="require_reported",
    )
    assert list(provider.stream(_request()))[-1]["type"] == "finish"
    assert response.closed


@pytest.mark.parametrize("cls", PROVIDERS)
@pytest.mark.parametrize("policy", STRICT_POLICIES)
def test_strict_stream_rejects_changing_model_even_between_allowed_aliases(cls, policy):
    events = stream_events(cls, "m1")
    events.insert(3, identity_event(cls, "vendor/m1-v2"))
    provider, response = provider_with(
        cls, events, identity_policy=policy,
        identity_aliases={"vendor/m1-v2": "m1"}, identity_alias_version="profile-v2",
    )
    delivered = []
    with pytest.raises(ProviderHTTPError, match="changed during stream") as caught:
        delivered.extend(provider.stream(_request()))
    assert caught.value.error_class == "model_identity"
    assert delivered and all(event["type"] != "finish" for event in delivered)
    assert response.closed


@pytest.mark.parametrize("cls", PROVIDERS)
@pytest.mark.parametrize("policy", STRICT_POLICIES)
def test_strict_stream_accepts_repeated_identity(cls, policy):
    events = stream_events(cls, "m1")
    events.insert(3, identity_event(cls, "m1"))
    provider, _ = provider_with(cls, events, identity_policy=policy)
    assert list(provider.stream(_request()))[-1]["type"] == "finish"


@pytest.mark.parametrize("cls", PROVIDERS)
def test_report_only_preserves_events_for_missing_mismatched_and_changing_models(cls):
    baseline, _ = provider_with(cls, stream_events(cls, MISSING))
    expected = list(baseline.stream(_request()))
    for reported in (MISSING, None, "", "other-model"):
        provider, response = provider_with(cls, stream_events(cls, reported))
        assert list(provider.stream(_request())) == expected
        assert response.closed
    # report_only also permits inconsistent reported names without changing deltas.
    events = stream_events(cls, "m1")
    events.insert(3, identity_event(cls, "other-model"))
    provider, _ = provider_with(cls, events)
    actual = list(provider.stream(_request()))
    expected[-1]["payload"]["chunks"] += 1
    assert actual == expected


@pytest.mark.parametrize("cls", [OpenAIResponsesProvider, AnthropicMessagesProvider])
def test_nested_protocols_do_not_accept_top_level_model_as_identity(cls):
    events = stream_events(cls, MISSING)
    events[0]["model"] = "m1"
    provider, _ = provider_with(cls, events, identity_policy="require_match")
    with pytest.raises(ProviderHTTPError, match="did not report a model identity"):
        list(provider.stream(_request()))


@pytest.mark.parametrize("policy", STRICT_POLICIES)
def test_responses_identity_may_first_arrive_with_completed_event(policy):
    events = stream_events(OpenAIResponsesProvider, MISSING)
    events[-1]["response"]["model"] = "m1"
    provider, _ = provider_with(OpenAIResponsesProvider, events, identity_policy=policy)
    assert list(provider.stream(_request()))[-1]["type"] == "finish"


@pytest.mark.parametrize("cls", PROVIDERS)
def test_identity_failure_message_does_not_echo_untrusted_values(cls):
    secret = "untrusted-secret-do-not-echo"
    provider, _ = provider_with(cls, stream_events(cls, secret), identity_policy="require_match")
    with pytest.raises(ProviderHTTPError) as caught:
        list(provider.stream(_request()))
    assert caught.value.error_class == "model_identity"
    assert secret not in str(caught.value)


@pytest.mark.parametrize("cls", PROVIDERS)
def test_interleaved_strict_streams_do_not_share_reported_identity(cls):
    provider, _ = provider_with(cls, [], identity_policy="require_reported")
    responses = iter([
        FakeSSEResponse(_sse_lines(stream_events(cls, "first-model"))),
        FakeSSEResponse(_sse_lines(stream_events(cls, MISSING))),
    ])
    provider.transport._opener = lambda request, *, timeout: next(responses)
    first = provider.stream(_request())
    second = provider.stream(_request())
    assert next(first)["type"] != "finish"
    with pytest.raises(ProviderHTTPError, match="did not report a model identity"):
        list(second)
    assert list(first)[-1]["type"] == "finish"


@pytest.mark.parametrize("failure", ["identity", "protocol"])
def test_failure_closes_transport_iterator_even_if_another_reference_retains_it(failure):
    events = (stream_events(OpenAICompatibleProvider, "other-model")
              if failure == "identity" else [{"unknown": True}])
    provider, response = provider_with(OpenAICompatibleProvider, events, identity_policy="require_match")
    original = provider.transport.post_sse
    retained = []

    def post_sse(*args, **kwargs):
        iterator = original(*args, **kwargs)
        retained.append(iterator)
        return iterator

    provider.transport.post_sse = post_sse
    with pytest.raises(ProviderHTTPError):
        list(provider.stream(_request()))
    assert response.closed
    assert list(retained[0]) == []


def test_chat_changed_identity_does_not_yield_its_text_tool_or_usage():
    events = stream_events(OpenAICompatibleProvider, "m1")
    bad_chunk = {
        "model": "other-model", "usage": {"prompt_tokens": 999},
        "choices": [{"delta": {
            "content": "must-not-be-delivered",
            "tool_calls": [{"index": 0, "function": {"name": "unsafe", "arguments": "{}"}}],
        }, "finish_reason": "tool_calls"}],
    }
    events.insert(1, bad_chunk)
    provider, response = provider_with(
        OpenAICompatibleProvider, events, identity_policy="require_reported",
    )
    delivered = []
    with pytest.raises(ProviderHTTPError, match="changed during stream"):
        delivered.extend(provider.stream(_request()))
    assert delivered == [{"type": "text_delta", "delta": "Hel", "payload": {}}]
    assert response.closed


def test_responses_changed_identity_on_terminal_frame_cannot_finish():
    events = stream_events(OpenAIResponsesProvider, "m1")
    events[-1]["response"]["model"] = "other-model"
    provider, response = provider_with(
        OpenAIResponsesProvider, events, identity_policy="require_reported",
    )
    delivered = []
    with pytest.raises(ProviderHTTPError, match="changed during stream"):
        delivered.extend(provider.stream(_request()))
    assert delivered and all(event["type"] not in {"usage", "finish"} for event in delivered)
    assert response.closed


def test_anthropic_model_in_a_nonidentity_event_does_not_count():
    events = stream_events(AnthropicMessagesProvider, MISSING)
    events[1]["message"] = {"model": "m1"}
    provider, _ = provider_with(AnthropicMessagesProvider, events, identity_policy="require_reported")
    with pytest.raises(ProviderHTTPError, match="did not report a model identity"):
        list(provider.stream(_request()))


@pytest.mark.parametrize("cls", PROVIDERS)
def test_strict_identity_checks_the_wire_model_not_the_model_request_hint(cls):
    provider, _ = provider_with(cls, stream_events(cls, "m1"), identity_policy="require_match")
    request = _request().model_copy(update={"model": "different-request-hint"})
    assert list(provider.stream(request))[-1]["type"] == "finish"
