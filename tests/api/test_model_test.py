from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_provider.base import ProviderCallError
from motte_provider.registry import AdapterSpec, register, unregister
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore

FAKE_KIND = "fake_smoke_test"


class _FakeProvider:
    """模拟 HTTP adapter：只实现测试端点用到的构造与 complete。"""

    def __init__(self, transport, model, parameters=None, price_table=None, **kwargs):
        self.model = model

    def complete(self, request):
        if self.model == "boom":
            raise ProviderCallError(
                {
                    "provider": FAKE_KIND,
                    "model": self.model,
                    "metering": {"latency_ms": 12.5, "attempts": 2, "retry_count": 1},
                    "error": {"class": "auth", "message": "invalid api key"},
                }
            )
        return {
            "provider": FAKE_KIND,
            "model": self.model,
            "metering": {"latency_ms": 240.0, "attempts": 1, "retry_count": 0},
            "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
        }


def setup_function():
    register(AdapterSpec(
        kind=FAKE_KIND,
        validate=lambda config: None,
        build=lambda config, manifest: None,
        smoke_supported=True,
        connection_required_fields=("base_url",),
        provider_cls=_FakeProvider,
    ))


def teardown_function():
    unregister(FAKE_KIND)


def client_with_store():
    return TestClient(create_app(InMemoryRunStore(), resource_store=InMemoryResourceStore()))


def _seed(client, *, model_id="fake-model", model=None, kind=FAKE_KIND):
    client.post("/api/v1/providers", json={"name": "fake", "kind": kind, "base_url": "http://fake.local/v1"})
    client.post("/api/v1/models", json={
        "id": model_id,
        "provider": "fake",
        "model": model,
        "capabilities": {"text": True},
    })


def test_model_test_success_returns_sanitized_report():
    client = client_with_store()
    _seed(client)
    report = client.post("/api/v1/models/fake-model/test", json={}).json()
    assert report["ok"] is True
    assert report["provider"] == FAKE_KIND
    assert report["model"] == "fake-model"
    assert report["latency_ms"] == 240.0
    assert report["usage"]["total_tokens"] == 11
    assert "error" not in report or report["error"] is None
    assert "api_key" not in str(report)


def test_model_test_failure_maps_error_class():
    client = client_with_store()
    _seed(client, model_id="boom-model", model="boom")
    report = client.post("/api/v1/models/boom-model/test", json={}).json()
    assert report["ok"] is False
    assert report["error"]["class"] == "auth"
    assert report["retry_count"] == 1


def test_model_test_uses_api_model_name():
    client = client_with_store()
    _seed(client, model_id="alias", model="real-name")
    report = client.post("/api/v1/models/alias/test", json={}).json()
    assert report["model"] == "real-name"


def test_model_test_missing_resources():
    client = client_with_store()
    assert client.post("/api/v1/models/ghost/test", json={}).status_code == 404

    client.post("/api/v1/providers", json={"name": "replay-p", "kind": "replay"})
    client.post("/api/v1/models", json={"id": "r", "provider": "replay-p", "capabilities": {"text": True}})
    rejected = client.post("/api/v1/models/r/test", json={})
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "PROVIDER_TEST_UNSUPPORTED"


def test_model_update_overwrites_profile():
    client = client_with_store()
    _seed(client)
    updated = client.post("/api/v1/models", json={
        "id": "fake-model",
        "provider": "fake",
        "capabilities": {"text": True},
        "parameters": {"temperature": 0.2, "top_p": 0.9, "max_output_tokens": 4096},
        "context_window": 131072,
    })
    assert updated.status_code == 201
    stored = client.get("/api/v1/models/fake-model").json()
    assert stored["parameters"]["temperature"] == 0.2
    assert stored["context_window"] == 131072
