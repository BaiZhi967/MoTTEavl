"""M4 review R22：操作手册示例必须能过公共解析（schema 与实现对齐）。

手册里的 manifest 示例经 prepare_run 实测——schema 漂移（手册说可用、
schema 拒绝）会让文档变成不可执行承诺。
"""
from __future__ import annotations

import pytest

from motte_contracts.agent_tasks import (
    normalize_agent_tasks_dataset,
    scenario_for_agent_tasks,
)
from motte_sdk.resolve import prepare_run
from motte_sdk.runtime_backends import publish_canonical_runtime_versions
from motte_storage.resource_store import InMemoryResourceStore


@pytest.fixture()
def resources():
    store = InMemoryResourceStore()
    publish_canonical_runtime_versions(store)
    dataset = normalize_agent_tasks_dataset({
        "name": "doc-examples", "version": "1", "suite": "agent-tasks",
        "cases": [{"case_id": "c1", "input": "write answer.txt"}],
    })
    store.datasets.put(dataset)
    store.scenarios.put(scenario_for_agent_tasks(dataset))
    return store


def test_cli_doc_manifests_resolve(resources):
    """docs/operations/cli-harnesses.md 的 Claude/Codex 示例。"""
    claude_manifest = {
        "runtime": "claude-cli@1",
        "runtime_profile": {
            "runtime": "claude-cli@1",
            "native_settings": {
                "model": "claude-sonnet-4-5", "max_turns": 8,
                "permission_mode": "acceptEdits", "binary": "claude",
            },
        },
        "runtime_accept_unenforced_tools": True,
    }
    codex_manifest = {
        "runtime": "codex-cli@1",
        "runtime_profile": {
            "runtime": "codex-cli@1",
            "native_settings": {
                "model": "gpt-5-codex", "sandbox": "workspace-write",
                "codex_config": {"c_sandbox_mode": "workspace-write"},
            },
        },
        "runtime_accept_unenforced_tools": True,
    }
    for manifest in (claude_manifest, codex_manifest):
        resolved, _ = prepare_run(
            "doc-examples@1", manifest, [], resources=resources,
        )
        assert resolved["execution"]["backend_id"] == manifest["runtime"].split("@")[0]
        assert resolved["runtime_snapshot"]["upstream_version"]


def test_pi_doc_manifest_resolves_with_provider_transport(resources):
    """docs/operations/pi.md：scripted 与 http（provider）两种传输示例。"""
    scripted = {
        "runtime": "pi-agent@1",
        "runtime_profile": {
            "runtime": "pi-agent@1",
            "native_settings": {
                "model": "scripted-1",
                "script": [[{"type": "text", "text": "ok"}]],
                "max_steps": 8,
            },
        },
    }
    resolved, _ = prepare_run("doc-examples@1", scripted, [], resources=resources)
    assert resolved["execution"]["backend_id"] == "pi-agent"

    http_manifest = {
        "runtime": "pi-agent@1",
        "runtime_profile": {
            "runtime": "pi-agent@1",
            "native_settings": {
                "model": "gpt-4o-mini",
                "provider": {
                    "api": "openai-completions",
                    "base_url": "https://api.example.com/v1",
                    "api_key_env": "MOTTE_PI_MODEL_KEY",
                },
            },
        },
    }
    resolved_http, _ = prepare_run(
        "doc-examples@1", http_manifest, [], resources=resources,
    )
    assert resolved_http["execution"]["backend_id"] == "pi-agent"
