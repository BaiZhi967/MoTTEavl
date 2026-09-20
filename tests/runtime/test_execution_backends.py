from copy import deepcopy

import pytest

from motte_sdk.execution_backends import (
    ExecutionBackendError,
    ExecutionBackendSpec,
    ExecutionHandle,
    backend_for,
    build_execution_handle,
    legacy_execution,
    register_backend,
    registered_backends,
    resolve_execution,
    unregister_backend,
)


def test_builtin_backend_registry_is_explicit_and_versioned():
    assert [(spec.id, spec.version) for spec in registered_backends()] == [
        ("builtin-agent", "1"), ("claude-cli", "1"), ("codex-app-server", "1"),
        ("codex-cli", "1"), ("direct-llm", "1"),
        ("external-benchmark", "1"),
        ("pi-agent", "1"),
        ("replay", "1"),
    ]
    assert backend_for("replay", "1").capabilities["safe_to_repeat"] is True


def test_replay_provider_resource_uses_manifest_fixture_for_direct_backend():
    from motte_provider.config import build_provider

    fixture = {"case-a": {"output": {"ok": True}, "expected": {"ok": True}}}
    provider = build_provider({"kind": "replay"}, {"replay_fixture": fixture})
    assert provider.invoke("case-a") == {"ok": True}


def test_direct_backend_preserves_top_level_replay_fixture_only_for_replay_provider():
    manifest = resolve_execution(
        "direct-llm@1",
        {
            "provider": {"kind": "replay"},
            "replay_fixture": {"case-a": {"output": "ok"}},
        },
    )
    handle = build_execution_handle({"manifest": manifest})
    assert handle.invoke("case-a") == "ok"


def test_resolve_execution_pins_direct_provider_backend():
    manifest = resolve_execution(
        "direct-llm@1",
        {"provider": {"kind": "replay", "fixture": {}}},
        scenario={"mode": "direct-llm"},
    )
    assert manifest["execution"] == {
        "backend_id": "direct-llm",
        "backend_version": "1",
        "capabilities": {"interactive": False, "safe_to_repeat": False},
        "execution_mode": "sample",
    }


def test_direct_backend_projects_only_execution_inputs_to_provider_adapters():
    from motte_provider.registry import (
        AdapterSpec,
        register as register_provider_adapter,
        unregister as unregister_provider_adapter,
    )

    kind = "__capturing_execution_manifest__"
    observed = {}

    class CapturingProvider:
        def __init__(self, config, manifest):
            observed["config"] = deepcopy(config)
            observed["build_manifest"] = deepcopy(manifest)
            self.manifest = manifest

        def invoke(self, case_id):
            observed["invoke_manifest"] = deepcopy(self.manifest)
            return {"content": self.manifest["cases"][case_id]["prompt"]}

    register_provider_adapter(
        AdapterSpec(
            kind=kind,
            implementation_version="1",
            validate=lambda config: None,
            build=lambda config, manifest: CapturingProvider(config, manifest),
        )
    )
    try:
        tools = [{
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "safe tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        manifest = {
            "provider": {
                "kind": kind,
                "parameters": {"temperature": 0.2, "max_output_tokens": 32},
                "max_retries": 1,
            },
            "cases": {"c1": {"case_id": "c1", "prompt": "safe prompt"}},
            "tools": tools,
            "parameters": {"temperature": 0.9},
            "execution": {"backend_id": "direct-llm", "backend_version": "1"},
            "benchmark_snapshot": {
                "selected_cases": [{
                    "case_id": "c1",
                    "input": "safe prompt",
                    "expected": "GOLD-EXPECTED",
                    "metadata": {"scorer": "GOLD-SCORER"},
                }],
                "dataset": {"provenance": {"source": "GOLD-PROVENANCE"}},
            },
            "benchmark_provenance": {"source": "GOLD-PROVENANCE"},
            "evaluation": {"scorer_id": "GOLD-SCORER"},
        }
        handle = build_execution_handle({"manifest": manifest})
        assert handle.invoke("c1") == {"content": "safe prompt"}
        expected_projection = {
            "cases": manifest["cases"],
            "tools": tools,
            "parameters": manifest["parameters"],
        }
        assert observed["build_manifest"] == expected_projection
        assert observed["invoke_manifest"] == expected_projection
        assert observed["config"] == manifest["provider"]
        assert observed["build_manifest"] is not manifest
        serialized = repr((observed["build_manifest"], observed["invoke_manifest"]))
        assert "GOLD-EXPECTED" not in serialized
        assert "GOLD-SCORER" not in serialized
        assert "GOLD-PROVENANCE" not in serialized
        assert "benchmark_snapshot" not in observed["build_manifest"]
        assert "benchmark_provenance" not in observed["build_manifest"]
        assert "evaluation" not in observed["build_manifest"]
        assert observed["build_manifest"]["parameters"] == manifest["parameters"]
    finally:
        unregister_provider_adapter(kind)


def test_direct_backend_rejects_fixture_on_non_replay_provider():
    with pytest.raises(ExecutionBackendError) as raised:
        resolve_execution(
            "direct-llm@1",
            {
                "provider": {
                    "kind": "openai_compatible",
                    "base_url": "https://example.test/v1",
                    "fixture": {"case-a": {"output": "not-used"}},
                }
            },
        )
    assert raised.value.code == "EXECUTION_BACKEND_CONFLICT"


def test_resolve_execution_pins_replay_backend_and_builds_handle():
    manifest = resolve_execution(
        "replay@1",
        {"provider": {"kind": "replay", "fixture": {"c1": {"output": "ok"}}}},
    )
    handle = build_execution_handle({"manifest": manifest})
    assert handle.backend_id == "replay"
    assert handle.invoke("c1") == "ok"


def test_replay_backend_rejects_conflicting_persisted_fixtures():
    manifest = resolve_execution("replay@1", {
        "provider": {"kind": "replay", "fixture": {"a": {"output": 1}}},
    })
    manifest["replay_fixture"] = {"a": {"output": 2}}
    with pytest.raises(ExecutionBackendError, match="must match"):
        build_execution_handle({"manifest": manifest})


def test_runtime_fields_never_fall_back_to_plain_provider():
    with pytest.raises(ExecutionBackendError) as raised:
        resolve_execution(
            "agent@1",
            {"agent": "builtin-react", "provider": {"kind": "replay", "fixture": {}}},
        )
    assert raised.value.code == "EXECUTION_BACKEND_UNSUPPORTED"


def test_explicit_direct_backend_cannot_ignore_agent_fields():
    with pytest.raises(ExecutionBackendError) as raised:
        resolve_execution(
            "agent@1",
            {
                "agent": "builtin-react",
                "provider": {"kind": "replay", "fixture": {}},
                "execution": {"backend_id": "direct-llm", "backend_version": "1"},
            },
        )
    assert raised.value.code == "EXECUTION_BACKEND_UNSUPPORTED"


def test_external_backend_resolves_but_fails_closed_without_adapter():
    """M2-T07：后端可用性翻转；adapter 未注册时在 build（分派）层拒绝。"""
    resolved = resolve_execution(
        "external@1",
        {
            "external_benchmark": {"adapter_id": "future", "adapter_version": "1",
                                   "runner_version": "r", "dataset_revision": "rev",
                                   "environment_digest": "d",
                                   "profile": {"benchmark_id": "b", "benchmark_version": "1"},
                                   "runner_config": {"cases": [{"case_id": "c1", "subject": "s"}]}},
            "execution": {"backend_id": "external-benchmark", "backend_version": "1"},
        },
    )
    assert resolved["execution"]["execution_mode"] == "job"
    with pytest.raises(ExecutionBackendError) as raised:
        build_execution_handle({"manifest": resolved, "case_ids": ["c1"]})
    assert raised.value.code == "ADAPTER_UNKNOWN"


def test_unknown_explicit_backend_fails_closed():
    with pytest.raises(ExecutionBackendError) as raised:
        resolve_execution(
            "custom@1",
            {"execution": {"backend_id": "missing", "backend_version": "9"}},
        )
    assert raised.value.code == "EXECUTION_BACKEND_UNSUPPORTED"


def test_legacy_projection_does_not_mutate_historical_manifest():
    run = {
        "scenario_version": "direct-llm@1",
        "manifest": {"provider": {"kind": "replay", "fixture": {}}},
    }
    projected = legacy_execution(run)
    assert projected["execution"]["backend_id"] == "direct-llm"
    assert "execution" not in run["manifest"]


def test_registry_extension_requires_only_a_spec():
    backend_id = "tiny-test"
    spec = ExecutionBackendSpec(
        id=backend_id,
        version="1",
        validate=lambda manifest: None,
        build=lambda run: ExecutionHandle(backend_id, "1", lambda case_id: case_id),
        capabilities={"interactive": False, "safe_to_repeat": True},
    )
    register_backend(spec)
    try:
        manifest = resolve_execution(
            "tiny@1",
            {"execution": {"backend_id": backend_id, "backend_version": "1"}},
        )
        handle = build_execution_handle({"manifest": manifest})
        assert handle.invoke("case-1") == "case-1"
    finally:
        unregister_backend(backend_id, "1")
