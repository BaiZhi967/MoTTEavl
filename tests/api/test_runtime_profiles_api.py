"""M4 review R20/R23：runtime_profiles 公共管理与发布幂等的 API 测试。"""
from __future__ import annotations

import time
import pytest

from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_storage.run_store import InMemoryRunStore


def _client():
    application = create_app(store=InMemoryRunStore())
    return TestClient(application), application


def test_runtime_profiles_list_is_404_free_and_publish_roundtrip():
    client, _ = _client()
    listed = client.get("/api/v1/runtime_profiles")
    assert listed.status_code == 200
    assert listed.json()["total"] == 0

    # 未发布 runtime → 具名拒绝。
    rejected = client.post("/api/v1/runtime_profiles", json={
        "name": "p1", "version": "1", "runtime": "pi-agent@9",
        "native_settings": {"model": "m"},
    })
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "RUNTIME_NOT_FOUND"

    # 先发布 runtime，再发布 profile → 列表可见。
    assert client.post("/api/v1/runtimes/publish").status_code == 200
    published = client.post("/api/v1/runtime_profiles", json={
        "name": "p1", "version": "1", "runtime": "pi-agent@1",
        "native_settings": {"model": "scripted-1"},
    })
    assert published.status_code == 201
    assert published.json()["name"] == "p1"
    listed = client.get("/api/v1/runtime_profiles")
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["runtime"] == "pi-agent@1"

    # 秘密原文形态的 credential_refs 在契约层拒绝。
    bad = client.post("/api/v1/runtime_profiles", json={
        "name": "p2", "version": "1", "runtime": "pi-agent@1",
        "native_settings": {"model": "m"},
        "credential_refs": ["sk-live-secret-value"],
    })
    assert bad.status_code == 422


def test_runtime_publish_is_idempotent_across_time():
    """R23：重复发布复用既有记录（同 definition），不再 409。"""
    client, _ = _client()
    first = client.post("/api/v1/runtimes/publish")
    assert first.status_code == 200
    time.sleep(0.02)
    second = client.post("/api/v1/runtimes/publish")
    assert second.status_code == 200
    # 两次都成功；幂等由存储返回的既有记录保证。
    names_first = set(first.json()["published"])
    names_second = set(second.json()["published"])
    assert names_first & names_second >= {"pi-agent", "claude-cli", "codex-cli"}


@pytest.mark.parametrize("secret_key", ["api_key", "access_token", "refresh_token", "client_secret", "apiKey"])
def test_profile_publish_rejects_nested_plaintext_and_unknown_native_fields(secret_key):
    client, _ = _client()
    client.post("/api/v1/runtimes/publish")
    payload = {"name": "secure", "version": "1", "runtime": "pi-agent@1",
               "native_settings": {"model": "m", "provider": {secret_key: "ordinary-secret"}}}
    rejected = client.post("/api/v1/runtime_profiles", json=payload)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "CREDENTIALS_REJECTED"
    assert "ordinary-secret" not in rejected.text
    payload["native_settings"] = {"model": "m", "unknown_option": True}
    assert client.post("/api/v1/runtime_profiles", json=payload).status_code == 422
    assert client.get("/api/v1/runtime_profiles").json()["total"] == 0


def test_profile_publish_preserves_timestamp_and_rejects_changed_content():
    client, _ = _client()
    client.post("/api/v1/runtimes/publish")
    payload = {"name": "stable", "version": "1", "runtime": "pi-agent@1",
               "native_settings": {"model": "m"}}
    first = client.post("/api/v1/runtime_profiles", json=payload)
    second = client.post("/api/v1/runtime_profiles", json=payload)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    payload["native_settings"] = {"model": "changed"}
    assert client.post("/api/v1/runtime_profiles", json=payload).status_code == 409


def test_expanded_profile_is_secret_checked_and_static_configuration_fails_before_queue():
    from motte_contracts.agent_tasks import normalize_agent_tasks_dataset, scenario_for_agent_tasks

    client, application = _client()
    client.post("/api/v1/runtimes/publish")
    resources = application.state.resource_store
    dataset = normalize_agent_tasks_dataset({
        "name": "preflight", "version": "1", "suite": "agent-tasks",
        "cases": [{"case_id": "c1", "input": "write a file", "fixture": {}, "expected": {}}],
    })
    resources.datasets.put(dataset)
    scenario = resources.scenarios.put(scenario_for_agent_tasks(dataset))
    profile = {"name": "legacy", "version": "1", "runtime": "pi-agent@1",
               "published_at": "2026-09-21T00:00:00Z",
               "native_settings": {"model": "m", "provider": {"api_key": "ordinary-secret"}}}
    resources.runtime_profiles.put(profile)
    request = {"scenario_version": f"{scenario['name']}@{scenario['version']}",
               "manifest": {"runtime": "pi-agent@1", "runtime_profile": "legacy@1"}}
    rejected = client.post("/api/v1/runs", json=request)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "CREDENTIALS_REJECTED"
    request["manifest"]["runtime_profile"] = {
        "runtime": "pi-agent@1", "native_settings": {"model": "m"},
    }
    rejected = client.post("/api/v1/runs", json=request)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "RUNTIME_MODEL_CONFIG_REQUIRED"
    assert client.get("/api/v1/runs").json()["total"] == 0
