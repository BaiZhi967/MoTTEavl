"""Profile -> snapshot -> real adapter -> captured HTTP request, never live."""
import json

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_contracts.messages import ModelRequest
from motte_provider.config import build_case_provider
from motte_sdk.resolve import ManifestResolutionError, resolve_manifest
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore
from tests.provider.test_openai_compatible import FakeResponse


@pytest.fixture
def resources():
    store = InMemoryResourceStore()
    store.providers.put({"name": "p", "kind": "openai_compatible", "base_url": "https://invalid.test"})
    store.models.put({"id": "m", "provider": "p", "capabilities": {}, "max_output_tokens": 128,
                      "parameters": {"max_output_tokens": 32}})
    return store


@pytest.mark.parametrize("kind,key", [("openai_compatible", "max_tokens"),
                                       ("anthropic_messages", "max_tokens"),
                                       ("openai_responses", "max_output_tokens")])
@pytest.mark.parametrize("canonical,expected", [(128, 128), (None, 32)])
def test_output_limit_end_to_end(resources, kind, key, canonical, expected):
    resources.providers.put({"name": "p", "kind": kind, "base_url": "https://invalid.test"})
    resources.models.put({**resources.models.get("m"), "max_output_tokens": canonical})
    snapshot = resolve_manifest({"model": "m"}, resources)["provider"]
    assert snapshot["max_output_tokens"] == expected
    assert snapshot["parameters"]["max_output_tokens"] == expected
    provider = build_case_provider(snapshot, api_key="").provider
    captured = []

    def opener(request, **kwargs):
        captured.append(json.loads(request.data))
        return FakeResponse({})

    provider.transport._opener = opener
    provider.complete(ModelRequest(model="m", messages=[]))
    assert captured[-1][key] == expected
    provider.complete(ModelRequest(model="m", messages=[], max_output_tokens=16))
    assert captured[-1][key] == 16
    with pytest.raises(ValueError, match="ceiling"):
        provider.complete(ModelRequest(model="m", messages=[], max_output_tokens=expected + 1))
    assert len(captured) == 2
    with pytest.raises(ManifestResolutionError, match="ceiling"):
        resolve_manifest({"model": "m", "parameters": {"max_output_tokens": expected + 1}}, resources)


@pytest.mark.parametrize("kind,expression,key,expected", [
    ("openai_compatible", '{"reasoning_effort": reasoningLevel}', "reasoning_effort", "high"),
    ("openai_responses", '{"reasoning": {"effort": reasoningLevel}}', "reasoning", {"effort": "high"}),
    ("anthropic_messages", 'reasoningLevel == "high" ? {"thinking": {"type": "enabled", "budget_tokens": 1024}} : {}', "thinking", {"type": "enabled", "budget_tokens": 1024}),
])
def test_reasoning_snapshot_and_final_http_body(resources, kind, expression, key, expected):
    resources.providers.put({"name": "p", "kind": kind, "base_url": "https://invalid.test"})
    reasoning = {"supported": True, "levels": ["low", "high"], "default_level": "low", "control": expression}
    resources.models.put({**resources.models.get("m"), "reasoning": reasoning})
    snapshot = resolve_manifest({"model": "m", "reasoning_level": "high"}, resources)["provider"]
    assert snapshot["reasoning"] == reasoning
    assert snapshot["reasoning_level"] == "high"
    reasoning["control"] = '{"messages": []}'
    assert snapshot["reasoning"]["control"] == expression
    provider = build_case_provider(snapshot, api_key="").provider
    captured = []

    def opener(request, **kwargs):
        captured.append(json.loads(request.data))
        return FakeResponse({})

    provider.transport._opener = opener
    provider.complete(ModelRequest(model="m", messages=[]))
    assert captured[-1][key] == expected
    assert captured[-1]["model"] == "m"


@pytest.mark.parametrize("control", [
    'not CEL ???', '{"messages": []}', '{"headers": {"authorization": "x"}}',
    '{"max_tokens": 9999}', 'reasoningLevel', '[1].map(x, x)',
    'reasoningLevel == "high" ? {"model": "other"} : {}',
    '{"value": unknown}', '{"x": 1 / 0}',
])
def test_invalid_cel_api_and_runtime_fail_before_network(resources, control):
    client = TestClient(create_app(InMemoryRunStore(), resources))
    reasoning = {"supported": True, "levels": ["low", "high"], "control": control}
    response = client.put("/api/v1/models/m", json={"reasoning": reasoning})
    assert response.status_code == 422, response.text
    assert "reasoning" in response.text
    provider = build_case_provider(resolve_manifest({"model": "m"}, resources)["provider"], api_key="").provider
    provider.reasoning = reasoning
    provider.reasoning_level = "low"
    provider.transport._opener = lambda *a, **kw: pytest.fail("network must not be reached")
    with pytest.raises(ValueError):
        provider.complete(ModelRequest(model="m", messages=[]))


def test_api_roundtrip_and_legacy_metadata(resources):
    client = TestClient(create_app(InMemoryRunStore(), resources))
    response = client.post("/api/v1/models", json={"id": "old", "provider": "p", "capabilities": {"custom": 1},
                          "reasoning": {"supported": True, "control": "reasoning_effort"}})
    assert response.status_code == 201
    saved = client.get("/api/v1/models/old").json()
    assert saved["input_modalities"] == ["text"]
    assert saved["capabilities"] == {"custom": 1, "structured_output": False, "native_search": False, "system_messages": False}
    assert saved["reasoning"]["default_level"] is None
    assert resolve_manifest(
        {"model": "old"}, resources, allow_draft_model=True
    )["provider"]["reasoning"]["control"] == "reasoning_effort"
    assert client.put("/api/v1/models/old", json={"input_modalities": ["image"]}).status_code == 422
    assert client.put("/api/v1/models/old", json={"capabilities": {"native_search": "yes"}}).status_code == 422
    assert client.put("/api/v1/models/old", json={"max_output_tokens": None, "parameters": {"max_output_tokens": None}}).status_code == 200


@pytest.mark.parametrize("kind,key", [("openai_compatible", "max_tokens"), ("anthropic_messages", "max_tokens"), ("openai_responses", "max_output_tokens")])
def test_model_test_honors_profile_and_reasoning_override(resources, monkeypatch, kind, key):
    from motte_provider.transport import HTTPTransport
    import motte_provider.config as config_module

    captured = []

    def opener(request, **kwargs):
        captured.append(json.loads(request.data))
        return FakeResponse({})

    monkeypatch.setattr(config_module, "HTTPTransport", lambda *a, **kw: HTTPTransport(*a, **kw, opener=opener))
    resources.providers.put({"name": "p", "kind": kind, "base_url": "https://invalid.test"})
    resources.models.put({**resources.models.get("m"), "reasoning": {"supported": True,
                         "levels": ["low", "high"], "default_level": "low", "control": '{"effort": reasoningLevel}'}})
    client = TestClient(create_app(InMemoryRunStore(), resources))
    response = client.post("/api/v1/models/m/test", json={"reasoning_level": "high"})
    assert response.status_code == 200, response.text
    assert captured[-1][key] == 16
    assert captured[-1]["effort"] == "high"
    assert client.post("/api/v1/models/m/test", json={}).status_code == 200
    assert captured[-1]["effort"] == "low"
    assert client.post("/api/v1/models/m/test", json={"reasoning_level": "unsupported"}).status_code == 422
    assert len(captured) == 2


@pytest.mark.parametrize("field", ["max_output_tokens", "context_window"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "32"])
def test_api_positive_integer_limits(resources, field, value):
    client = TestClient(create_app(InMemoryRunStore(), resources))
    response = client.put("/api/v1/models/m", json={field: value})
    assert response.status_code == 422, response.text
    response = client.post("/api/v1/models", json={"id": "n", "provider": "p", "capabilities": {}, field: value})
    assert response.status_code == 422, response.text
