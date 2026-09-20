"""M4 review R20/R23：runtime_profiles 公共管理与发布幂等的 API 测试。"""
from __future__ import annotations

import time

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
