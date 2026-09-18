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


def test_provider_kind_catalog_lists_user_facing_kinds():
    client, _ = client_with_resources()
    payload = client.get("/api/v1/provider_kinds").json()
    by_kind = {item["kind"]: item for item in payload["items"]}
    assert set(by_kind) == {"openai_compatible", "anthropic_messages", "openai_responses"}
    assert by_kind["anthropic_messages"]["default_base_url"] == "https://api.anthropic.com/v1"
    assert by_kind["anthropic_messages"]["default_key_env"] == "ANTHROPIC_API_KEY"
    assert by_kind["openai_responses"]["default_base_url"] == "https://api.openai.com/v1"
    assert by_kind["openai_compatible"]["default_base_url"] is None
    assert by_kind["openai_compatible"]["label"] == "OpenAI 兼容"


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


def test_server_managed_resource_fields_cannot_bypass_lifecycle():
    client, store = client_with_resources()
    forged_provider = client.post("/api/v1/providers", json={
        "name": "forged", "kind": "openai_compatible",
        "base_url": "http://localhost:8001/v1", "generation": 99,
    })
    assert forged_provider.status_code == 422
    assert forged_provider.json()["error"]["code"] == "SERVER_MANAGED_FIELD"
    hidden_version = client.post("/api/v1/scenarios", json={
        "name": "hidden", "version": "1", "cases": [], "_deleted": True,
    })
    assert hidden_version.status_code == 422
    assert hidden_version.json()["error"]["code"] == "SERVER_MANAGED_FIELD"
    assert store.scenarios.get("hidden", "1") is None

    client.post("/api/v1/providers", json={
        "name": "local-vllm", "kind": "openai_compatible",
        "base_url": "http://localhost:8001/v1",
    })
    forged_model = client.post("/api/v1/models", json={
        "id": "forged", "provider": "local-vllm", "capabilities": {"text": True},
        "lifecycle": "published", "published_at": "2026-09-18T00:00:00Z",
    })
    assert forged_model.status_code == 422
    assert forged_model.json()["error"]["code"] == "SERVER_MANAGED_FIELD"
    assert store.models.get("forged") is None

    client.post("/api/v1/models", json={
        "id": "draft", "provider": "local-vllm", "capabilities": {"text": True},
    })
    forged_update = client.put("/api/v1/models/draft", json={
        "lifecycle": "published", "published_at": "2026-09-18T00:00:00Z",
    })
    assert forged_update.status_code == 422
    assert store.models.get("draft")["lifecycle"] == "draft"


def test_provider_update_edits_connection_and_toggles_enabled():
    client, _ = client_with_resources()
    client.post("/api/v1/providers", json={
        "name": "local-vllm", "kind": "openai_compatible", "base_url": "http://localhost:8001/v1",
    })
    updated = client.put(
        "/api/v1/providers/local-vllm",
        json={"base_url": "http://gateway:9000/v1", "credentials": "gateway-profile", "enabled": False},
    )
    assert updated.status_code == 200
    record = client.get("/api/v1/providers/local-vllm").json()
    assert record["base_url"] == "http://gateway:9000/v1"
    assert record["credentials"] == "gateway-profile"
    assert record["enabled"] is False
    assert client.put("/api/v1/providers/local-vllm", json={"enabled": True}).json()["enabled"] is True

    renamed = client.put("/api/v1/providers/local-vllm", json={"name": "other"})
    assert renamed.status_code == 422
    assert client.put("/api/v1/providers/missing", json={"enabled": True}).status_code == 404
    leaked = client.put("/api/v1/providers/local-vllm", json={"api_key": "sk-123"})
    assert leaked.status_code == 422
    assert leaked.json()["error"]["code"] == "CREDENTIALS_REJECTED"
    invalid = client.put(
        "/api/v1/providers/local-vllm",
        json={"kind": "anthropic_messages", "base_url": ""},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "PROVIDER_CONFIG_INVALID"


def test_model_update_merges_fields_and_toggles_enabled():
    client, _ = client_with_resources()
    client.post("/api/v1/providers", json={
        "name": "local-vllm", "kind": "openai_compatible", "base_url": "http://localhost:8001/v1",
    })
    client.post(
        "/api/v1/models",
        json={"id": "qwen-7b", "provider": "local-vllm", "capabilities": {"text": True}, "parameters": {"temperature": 0.2}},
    )
    updated = client.put(
        "/api/v1/models/qwen-7b",
        json={"parameters": {"temperature": 0.9, "top_p": 0.8}, "enabled": False},
    )
    assert updated.status_code == 200
    record = client.get("/api/v1/models/qwen-7b").json()
    assert record["parameters"] == {"temperature": 0.9, "top_p": 0.8}
    assert record["enabled"] is False

    moved = client.put("/api/v1/models/qwen-7b", json={"provider": "other"})
    assert moved.status_code == 422
    unknown_field = client.put("/api/v1/models/qwen-7b", json={"unknown_field": 1})
    assert unknown_field.status_code == 422
    assert client.put("/api/v1/models/missing", json={"enabled": True}).status_code == 404


def test_slash_in_model_id_and_provider_name_is_addressable():
    """带路径分隔符的资源键（Qwen/Qwen2.5-7B 这类 id）必须整键路由，不能被路径切分。"""
    client, store = client_with_resources()
    client.post("/api/v1/providers", json={
        "name": "gateway/cn", "kind": "openai_compatible", "base_url": "http://localhost:8001/v1",
    })
    created = client.post(
        "/api/v1/models",
        json={"id": "Qwen/Qwen2.5-7B-Instruct", "provider": "gateway/cn", "capabilities": {"text": True}},
    )
    assert created.status_code == 201

    model_path = "/api/v1/models/Qwen/Qwen2.5-7B-Instruct"
    assert client.get(model_path).json()["provider"] == "gateway/cn"
    assert client.put(model_path, json={"enabled": False}).json()["enabled"] is False
    assert client.put(model_path, json={"enabled": True}).json()["enabled"] is True
    assert client.put("/api/v1/providers/gateway/cn", json={"enabled": False}).json()["enabled"] is False
    assert client.put(model_path, json={"enabled": True}).status_code == 200

    assert client.delete(model_path).json()["deprecated"] == "Qwen/Qwen2.5-7B-Instruct"
    assert store.models.get("Qwen/Qwen2.5-7B-Instruct")["lifecycle"] == "deprecated"
    assert client.delete("/api/v1/providers/gateway/cn").status_code == 200


def test_run_rejects_disabled_provider_and_model():
    client, _ = client_with_resources()
    client.post("/api/v1/providers", json={
        "name": "local-vllm", "kind": "openai_compatible", "base_url": "http://localhost:8001/v1",
    })
    client.post(
        "/api/v1/models",
        json={"id": "qwen-7b", "provider": "local-vllm", "capabilities": {"text": True}},
    )
    client.put("/api/v1/providers/local-vllm", json={"enabled": False})
    blocked_provider = client.post(
        "/api/v1/runs",
        json={"scenario_version": "direct-llm@1", "case_ids": ["case-1"], "manifest": {"provider": "local-vllm"}},
    )
    assert blocked_provider.status_code == 422
    assert blocked_provider.json()["error"]["code"] == "PROVIDER_DISABLED"

    client.put("/api/v1/providers/local-vllm", json={"enabled": True})
    client.put("/api/v1/models/qwen-7b", json={"enabled": False})
    blocked_model = client.post(
        "/api/v1/runs",
        json={"scenario_version": "direct-llm@1", "case_ids": ["case-1"], "manifest": {"model": "qwen-7b"}},
    )
    assert blocked_model.status_code == 422
    assert blocked_model.json()["error"]["code"] == "MODEL_DISABLED"


def test_versioned_scenario_and_dataset_routes():
    client, _ = client_with_resources()
    created = client.post("/api/v1/scenarios", json={"name": "json_extract", "version": "3", "cases": []})
    assert created.status_code == 201
    assert client.get("/api/v1/scenarios/json_extract/3").json()["version"] == "3"
    assert client.get("/api/v1/scenarios/json_extract/4").status_code == 404
    missing = client.post("/api/v1/datasets", json={"name": "d"})
    assert missing.status_code == 422
    immutable = client.delete("/api/v1/scenarios/json_extract/3")
    assert immutable.status_code == 409
    assert immutable.json()["error"]["code"] == "PUBLISHED_RESOURCE_IMMUTABLE"
    assert client.get("/api/v1/scenarios/json_extract/3").status_code == 200


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
    from apps.worker.motte_worker.reporting import WorkerReporter
    from apps.worker.motte_worker.runtime import WorkerLoop

    WorkerLoop(
        client.app.state.run_service, reporter=WorkerReporter(enabled=False)
    ).claim_and_execute(run["id"])

    listed = client.get("/api/v1/runs").json()
    assert listed["total"] == 1
    assert client.get("/api/v1/runs?status=queued").json()["total"] == 0
    assert client.get("/api/v1/runs?status=completed").json()["total"] == 1

    report = client.get(f"/api/v1/runs/{run['id']}/report").json()
    assert report["run_id"] == run["id"]
    assert report["summary"] == {"cases": 1, "scored": 1, "passed": 1, "failed": 0, "pass_rate": 1.0}
    assert report["cost"]["total"] is None  # replay 无成本
    assert client.get("/api/v1/runs/run-404/report").status_code == 404
