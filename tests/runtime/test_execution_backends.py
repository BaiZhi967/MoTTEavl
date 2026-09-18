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
        ("direct-llm", "1"),
        ("external-benchmark", "1"),
        ("replay", "1"),
    ]
    assert backend_for("replay", "1").capabilities["safe_to_repeat"] is True


def test_replay_provider_resource_uses_manifest_fixture_for_direct_backend():
    from motte_provider.config import build_provider

    fixture = {"case-a": {"output": {"ok": True}, "expected": {"ok": True}}}
    provider = build_provider({"kind": "replay"}, {"replay_fixture": fixture})
    assert provider.invoke("case-a") == {"ok": True}


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
    }


def test_resolve_execution_pins_replay_backend_and_builds_handle():
    manifest = resolve_execution(
        "replay@1",
        {"provider": {"kind": "replay", "fixture": {"c1": {"output": "ok"}}}},
    )
    handle = build_execution_handle({"manifest": manifest})
    assert handle.backend_id == "replay"
    assert handle.invoke("c1") == "ok"


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


def test_registered_but_unavailable_backend_fails_at_resolution():
    with pytest.raises(ExecutionBackendError) as raised:
        resolve_execution(
            "external@1",
            {
                "external_benchmark": {"adapter_id": "future"},
                "execution": {"backend_id": "external-benchmark", "backend_version": "1"},
            },
        )
    assert raised.value.code == "EXECUTION_BACKEND_UNAVAILABLE"


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
