from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore


def client_with_resources():
    store = InMemoryResourceStore()
    return TestClient(create_app(InMemoryRunStore(), resource_store=store)), store


def test_provider_crud_and_credential_rejection():
    client, store = client_with_resources()
    created = client.post(
        "/api/v1/providers",
        json={"name": "local-vllm", "kind": "openai_compatible", "base_url": "http://localhost:8001/v1", "api_key_env": "LOCAL_VLLM_KEY"},
    )
    assert created.status_code == 201
    assert client.get("/api/v1/providers/local-vllm").json()["base_url"] == "http://localhost:8001/v1"
    assert client.get("/api/v1/providers").json()["total"] == 1

    leaked = client.post("/api/v1/providers", json={"name": "bad", "kind": "openai_compatible", "base_url": "http://x", "api_key": "sk-123"})
    assert leaked.status_code == 422
    assert leaked.json()["error"]["code"] == "CREDENTIALS_REJECTED"
    assert len(store.providers.list()) == 1

    invalid = client.post("/api/v1/providers", json={"name": "no-url", "kind": "openai_compatible"})
    assert invalid.status_code == 422
    assert client.delete("/api/v1/providers/local-vllm").status_code == 200
    assert client.get("/api/v1/providers/local-vllm").status_code == 404


def test_model_crud_validates_contract():
    client, _ = client_with_resources()
    invalid = client.post("/api/v1/models", json={"id": "m", "provider": "p"})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "CONTRACT_INVALID"

    orphan = client.post(
        "/api/v1/models",
        json={"id": "qwen-7b", "provider": "local-vllm", "capabilities": {"text": True}},
    )
    assert orphan.status_code == 422
    assert orphan.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    client.post("/api/v1/providers", json={
        "name": "local-vllm", "kind": "openai_compatible", "base_url": "http://localhost:8001/v1",
    })
    created = client.post(
        "/api/v1/models",
        json={"id": "qwen-7b", "provider": "local-vllm", "capabilities": {"text": True}},
    )
    assert created.status_code == 201
    assert client.get("/api/v1/models/qwen-7b").json()["provider"] == "local-vllm"


def test_versioned_scenario_and_dataset_routes():
    client, _ = client_with_resources()
    created = client.post("/api/v1/scenarios", json={"name": "json_extract", "version": "3", "cases": []})
    assert created.status_code == 201
    assert client.get("/api/v1/scenarios/json_extract/3").json()["version"] == "3"
    assert client.get("/api/v1/scenarios/json_extract/4").status_code == 404
    missing = client.post("/api/v1/datasets", json={"name": "d"})
    assert missing.status_code == 422
    assert client.delete("/api/v1/scenarios/json_extract/3").json() == {"deleted": "json_extract@3"}


def test_agent_and_skill_catalogs():
    client, _ = client_with_resources()
    agents = client.get("/api/v1/agents").json()
    assert {agent["id"] for agent in agents["items"]} >= {"builtin-react", "pi"}

    registered = client.post(
        "/api/v1/skills",
        json={"name": "json-path", "version": "1.0.0", "entrypoint": "skills/jsonpath/main.py", "permissions": ["fs:read"]},
    )
    assert registered.status_code == 201
    listed = client.get("/api/v1/skills").json()
    assert listed["total"] == 1 and listed["items"][0]["name"] == "json-path"


def test_harnesses_catalog_reports_installation():
    client, _ = client_with_resources()
    payload = client.get("/api/v1/harnesses").json()
    names = {item["name"] for item in payload["items"]}
    assert names == {"claude-cli", "codex-cli"}
    for item in payload["items"]:
        assert "installed" in item and "runnable" in item


def test_run_listing_filter_and_report(tmp_path):
    from motte_storage.run_store import SQLiteRunStore

    store = SQLiteRunStore(tmp_path / "api.db")
    client = TestClient(create_app(store, resource_store=InMemoryResourceStore()))
    run = client.post("/api/v1/runs", json={"scenario_version": "replay@1", "case_ids": ["case-1"]}).json()
    client.post(f"/api/v1/runs/{run['id']}/replay", json={"cases": {"case-1": {"output": {"n": 1}, "expected": {"n": 1}}}})

    listed = client.get("/api/v1/runs").json()
    assert listed["total"] == 1
    assert client.get("/api/v1/runs?status=queued").json()["total"] == 0
    assert client.get("/api/v1/runs?status=completed").json()["total"] == 1

    report = client.get(f"/api/v1/runs/{run['id']}/report").json()
    assert report["run_id"] == run["id"]
    assert report["summary"] == {"cases": 1, "scored": 1, "passed": 1, "failed": 0, "pass_rate": 1.0}
    assert report["cost"]["total"] is None  # replay 无成本
    assert client.get("/api/v1/runs/run-404/report").status_code == 404
