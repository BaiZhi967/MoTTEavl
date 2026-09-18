"""HTTP adapter model identity: reported names never inherit request configuration."""
import io
import json
from urllib.error import HTTPError

import pytest

from motte_contracts.model import IdentityResult
from motte_provider.base import ProviderCallError
from motte_provider.config import build_case_provider, validate_provider_config
from motte_provider.registry import adapter_for

KINDS = ("openai_compatible", "openai_responses", "anthropic_messages")
MISSING = object()
SECRET = "identity-secret-123"


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def response_body(kind, reported):
    if kind == "openai_compatible":
        body = {
            "id": "resp-1", "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        }
    elif kind == "openai_responses":
        body = {
            "id": "resp-1", "status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "answer"}]}],
            "usage": {"input_tokens": 2, "output_tokens": 3},
        }
    else:
        body = {
            "id": "resp-1", "stop_reason": "end_turn", "content": [{"type": "text", "text": "answer"}],
            "usage": {"input_tokens": 2, "output_tokens": 3},
        }
    if reported is not MISSING:
        body["model"] = reported
    body["metadata"] = {"api_key": SECRET}
    return body


def make_provider(kind, reported, **config_overrides):
    config = {"kind": kind, "base_url": "https://model.example.test/v1", "model": "model-a",
              **config_overrides}
    driven = build_case_provider(config, {"case-1": {"prompt": "hello"}}, api_key="")
    provider = driven.provider
    provider.transport._opener = lambda request, *, timeout: FakeResponse(response_body(kind, reported))
    return driven


def assert_identity_contract(envelope):
    identity = {field: envelope[field] for field in IdentityResult.model_fields}
    assert IdentityResult.model_validate(identity).model_dump(mode="json") == identity


@pytest.mark.parametrize("kind", KINDS)
def test_exact_identity_is_reported_by_every_http_adapter(kind):
    provider = make_provider(kind, "model-a")
    envelope = provider.invoke("case-1")
    assert_identity_contract(envelope)
    assert envelope["model"] == envelope["requested_model"] == "model-a"
    assert envelope["reported_model"] == envelope["resolved_model_identity"] == "model-a"
    assert envelope["identity_policy_result"] == "exact_match"
    assert envelope["identity_policy"] == "report_only"
    assert envelope["policy_passed"] is True
    assert envelope["identity_evidence"] == {
        "source": "provider_response", "reported_value": "model-a", "path": "$.model",
        "alias_map_version": None, "matched_alias": None, "policy": "report_only", "details": {},
    }
    assert envelope["implementation_version"] == adapter_for(kind).implementation_version == "1"


@pytest.mark.parametrize("kind", KINDS)
def test_versioned_explicit_alias_allows_strict_match(kind):
    aliases = {"gateway/model-a-2026": "model-a"}
    provider = make_provider(kind, "gateway/model-a-2026", identity_policy="require_match",
                             identity_aliases=aliases, identity_alias_version="model-profile-v3")
    aliases["gateway/model-a-2026"] = "other"  # Resolved config must not change mid-run.
    envelope = provider.invoke("case-1")
    assert_identity_contract(envelope)
    assert envelope["reported_model"] == "gateway/model-a-2026"
    assert envelope["resolved_model_identity"] == "model-a"
    assert envelope["identity_policy_result"] == "alias_match"
    assert envelope["policy_passed"] is True
    assert envelope["identity_evidence"]["matched_alias"] == "gateway/model-a-2026"
    assert envelope["identity_evidence"]["alias_map_version"] == "model-profile-v3"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("reported", [MISSING, None, ""])
def test_missing_model_is_null_never_request_model(kind, reported):
    envelope = make_provider(kind, reported).invoke("case-1")
    assert_identity_contract(envelope)
    assert envelope["model"] == envelope["requested_model"] == "model-a"
    assert envelope["reported_model"] is None
    assert envelope["resolved_model_identity"] is None
    assert envelope["identity_policy_result"] == "unreported"
    assert envelope["policy_passed"] is True
    assert envelope["identity_evidence"]["source"] is None
    assert envelope["identity_evidence"]["reported_value"] is None


@pytest.mark.parametrize("policy", ["require_reported", "require_match"])
def test_strict_unreported_keeps_redacted_response_usage_and_cost(policy):
    provider = make_provider("openai_compatible", MISSING, identity_policy=policy,
                             price_table={"version": "prices-v1", "input_per_million": 1,
                                          "output_per_million": 2})
    with pytest.raises(ProviderCallError) as caught:
        provider.invoke("case-1")
    evidence = caught.value.evidence
    assert_identity_contract(evidence)
    assert caught.value.error_class == evidence["error"]["class"] == "model_identity"
    assert evidence["metering"]["error_class"] == "model_identity"
    assert evidence["identity_policy_result"] == "unreported"
    assert evidence["policy_passed"] is False
    assert evidence["reported_model"] is None
    assert evidence["response_id"] == "resp-1"
    assert evidence["content"] == "answer"
    assert evidence["usage"]["prompt_tokens"] == 2
    assert evidence["cost"]["price_table_version"] == "prices-v1"
    assert evidence["canonical"]["response"]["body"]["metadata"]["api_key"] == "[REDACTED]"
    assert SECRET not in json.dumps(evidence)
    assert provider.provider.calls == [evidence]


def test_mismatch_is_observed_without_blocking_report_only_or_require_reported():
    for policy in ("report_only", "require_reported"):
        envelope = make_provider("openai_responses", "other", identity_policy=policy).invoke("case-1")
        assert envelope["reported_model"] == "other"
        assert envelope["resolved_model_identity"] is None
        assert envelope["identity_policy_result"] == "mismatch"
        assert envelope["policy_passed"] is True
        assert "error" not in envelope


@pytest.mark.parametrize("kind", KINDS)
def test_require_match_rejects_mismatch_with_full_evidence(kind):
    provider = make_provider(kind, "other", identity_policy="require_match")
    with pytest.raises(ProviderCallError) as caught:
        provider.invoke("case-1")
    evidence = caught.value.evidence
    assert_identity_contract(evidence)
    assert evidence["identity_policy_result"] == "mismatch"
    assert evidence["requested_model"] == "model-a"
    assert evidence["reported_model"] == "other"
    assert evidence["resolved_model_identity"] is None
    assert evidence["policy_passed"] is False
    assert evidence["usage"]["prompt_tokens"] == 2
    assert evidence["canonical"]["response"]["body"]["metadata"]["api_key"] == "[REDACTED]"
    assert SECRET not in json.dumps(evidence)
    assert provider.provider.calls == [evidence]


def test_namespace_is_not_trimmed_without_explicit_alias():
    provider = make_provider("openai_compatible", "tenant/model-a", identity_policy="require_match")
    with pytest.raises(ProviderCallError) as caught:
        provider.invoke("case-1")
    assert caught.value.evidence["reported_model"] == "tenant/model-a"
    assert caught.value.evidence["resolved_model_identity"] is None
    assert caught.value.evidence["identity_policy_result"] == "mismatch"
    assert make_provider("openai_compatible", "tenant/model-a", identity_policy="require_match",
                         identity_aliases={"tenant/model-a": "model-a"},
                         identity_alias_version="v1").invoke("case-1")["identity_policy_result"] == "alias_match"


def test_http_failure_is_not_evaluated_and_never_claims_reported_identity():
    provider = make_provider("openai_compatible", "model-a", identity_policy="require_match")

    def unauthorized(request, *, timeout):
        body = io.BytesIO(json.dumps({"error": {"message": "not authorized", "api_key": SECRET}}).encode())
        raise HTTPError(request.full_url, 401, "unauthorized", {}, body)

    provider.provider.transport._opener = unauthorized
    with pytest.raises(ProviderCallError) as caught:
        provider.invoke("case-1")
    evidence = caught.value.evidence
    assert_identity_contract(evidence)
    assert evidence["error"]["class"] == "auth"
    assert evidence["identity_policy_result"] == "not_evaluated"
    assert evidence["policy_passed"] is None
    assert evidence["requested_model"] == "model-a"
    assert evidence["reported_model"] is None
    assert evidence["resolved_model_identity"] is None
    assert evidence["canonical"]["response"]["body"]["error"]["api_key"] == "[REDACTED]"
    assert SECRET not in json.dumps(evidence)


@pytest.mark.parametrize("invalid", [
    {"identity_policy": "strict"},
    {"identity_aliases": {"other": "model-a"}},
    {"identity_aliases": {"other": "model-a"}, "identity_alias_version": ""},
    {"identity_aliases": {"other": ""}, "identity_alias_version": "v1"},
    {"identity_aliases": ["other"], "identity_alias_version": "v1"},
    {"identity_aliases": {1: "model-a"}, "identity_alias_version": "v1"},
])
def test_identity_config_is_rejected_before_network(invalid):
    config = {"kind": "openai_compatible", "base_url": "https://model.example.test", "model": "model-a", **invalid}
    with pytest.raises(ValueError, match="identity_"):
        validate_provider_config(config)
    with pytest.raises(ValueError, match="identity_"):
        build_case_provider(config, api_key="")
