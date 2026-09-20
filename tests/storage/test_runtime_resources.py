"""M4-T01：runtime 版本与 profile 资源的三存储不可变语义。

反例先行：覆写已发布版本、删除版本资源、异内容同键都必须失败；
InMemory 与 SQLite 语义一致，PG 迁移单独登记。旧 Run（无 runtime 字段）
与空 runtime 目录共存。
"""
from __future__ import annotations

import json

import pytest

from motte_storage.resource_store import (
    InMemoryResourceStore,
    ResourceConflictError,
    SQLiteResourceStore,
)


def _runtime_version_record(name="pi-agent", version="1"):
    return {
        "name": name,
        "version": version,
        "definition": {
            "kind": "pi-bridge",
            "transport": "bridge-stdio-jsonl",
            "upstream_version": "@mariozechner/pi-agent-core@0.73.1",
            "adapter_version": "pi-bridge@1",
            "parser_version": "pi-jsonl-v2",
            "config_schema": {
                "properties": {"model": {"type": "string"}},
                "required": ["model"],
            },
            "supported_modes": ["batch"],
            "interactive": False,
            "model_control": "runner-configured",
            "tool_control": {
                "enforcement": "bridge-sandbox",
                "enforcement_owner": "platform",
                "tools": ["read_file", "write_file", "list_files"],
            },
            "evidence_capabilities": {"session_events": True},
        },
        "lifecycle": "published",
        "published_at": "2026-09-20T00:00:00Z",
    }


def _runtime_profile_record(name="pi-default", version="1"):
    return {
        "name": name,
        "version": version,
        "runtime": "pi-agent@1",
        "native_settings": {"model": "scripted-1"},
        "workspace": {"source": "per-case-temp"},
        "credential_refs": [],
        "published_at": "2026-09-20T00:00:00Z",
    }


def _stores(tmp_path):
    return [
        ("in-memory", InMemoryResourceStore()),
        ("sqlite", SQLiteResourceStore(tmp_path / "resources.db")),
    ]


class TestRuntimeVersionResources:
    @pytest.mark.parametrize("store_kind", ["in-memory", "sqlite"])
    def test_publish_is_idempotent_and_immutable(self, store_kind, tmp_path):
        for kind, store in _stores(tmp_path):
            if kind != store_kind:
                continue
            record = _runtime_version_record()
            first = store.runtimes.put(record)
            assert first["lifecycle"] == "published"
            # 同内容重放幂等
            again = store.runtimes.put(_runtime_version_record())
            assert again == first
            # 异内容同键拒绝
            conflict = json.loads(json.dumps(record))
            conflict["definition"]["upstream_version"] = "@mariozechner/pi-agent-core@0.0.1"
            with pytest.raises(ResourceConflictError):
                store.runtimes.put(conflict)
            # 版本资源不可删除（与 dataset 版本一致：显式冲突）
            with pytest.raises(ResourceConflictError):
                store.runtimes.delete("pi-agent", "1")
            assert store.runtimes.get("pi-agent", "1") is not None

    @pytest.mark.parametrize("store_kind", ["in-memory", "sqlite"])
    def test_invalid_runtime_records_rejected(self, store_kind, tmp_path):
        for kind, store in _stores(tmp_path):
            if kind != store_kind:
                continue
            bad = _runtime_version_record()
            bad["definition"]["kind"] = "random-agent"
            with pytest.raises(ValueError):
                store.runtimes.put(bad)
            bad = _runtime_version_record()
            bad["lifecycle"] = "draft"
            with pytest.raises(ValueError):
                store.runtimes.put(bad)

    @pytest.mark.parametrize("store_kind", ["in-memory", "sqlite"])
    def test_runtime_profiles_are_versioned_resources(self, store_kind, tmp_path):
        for kind, store in _stores(tmp_path):
            if kind != store_kind:
                continue
            record = _runtime_profile_record()
            store.runtime_profiles.put(record)
            assert store.runtime_profiles.get("pi-default", "1")["runtime"] == "pi-agent@1"
            conflict = _runtime_profile_record()
            conflict["native_settings"]["model"] = "scripted-2"
            with pytest.raises(ResourceConflictError):
                store.runtime_profiles.put(conflict)
            with pytest.raises(ResourceConflictError):
                store.runtime_profiles.delete("pi-default", "1")

    def test_empty_runtime_catalog_and_legacy_runs_coexist(self, tmp_path):
        store = SQLiteResourceStore(tmp_path / "resources.db")
        assert store.runtimes.list() == []
        assert store.runtime_profiles.list() == []
        # 旧资源路径不受影响
        store.models.put({
            "id": "legacy-model", "provider": "p", "capabilities": {},
        })
        assert store.models.get("legacy-model") is not None
