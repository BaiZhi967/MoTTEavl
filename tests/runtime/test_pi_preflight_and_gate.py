"""M4 review 批 2/3 单元：Pi 执行器预检、版本门与预算终态映射。"""
from __future__ import annotations

import pytest

from motte_sdk.pi_runtime import PiRuntimeCaseExecutor


def _run(backend_overrides=None):
    run = {
        "id": "run-1",
        "manifest": {
            "runtime": "pi-agent@1",
            "runtime_profile": {
                "runtime": "pi-agent@1",
                "native_settings": {
                    "model": "scripted-1",
                    "script": [[{"type": "text", "text": "ok"}]],
                },
            },
            "runtime_snapshot": {
                "name": "pi-agent", "version": "1", "lifecycle": "published",
                "content_hash": "sha256:" + "0" * 64,
                "model_control": "runner-configured",
                "tool_enforcement": "bridge-sandbox",
                "tools": ["read_file", "write_file", "list_files"],
                "upstream_version": "@mariozechner/pi-agent-core@0.73.1",
                "config_schema": {"properties": {"model": {"type": "string"}}},
            },
        },
    }
    if backend_overrides:
        for section, values in backend_overrides.items():
            run["manifest"].setdefault(section, {}).update(values)
    return run


def test_preflight_compiles_budgets_and_tools():
    run = _run({
        "runtime_profile": {
            "budgets": {"max_steps": 3, "max_tool_calls": 2, "total_timeout": 12.5},
        },
    })
    effective = PiRuntimeCaseExecutor(run)._preflight()
    assert effective["tools"] == ["read_file", "write_file", "list_files"]
    assert effective["max_steps"] == 3
    assert effective["max_tool_calls"] == 2
    assert effective["total_timeout"] == 12.5
    assert effective["expected_sdk"] == "0.73.1"


def test_preflight_intersects_declared_tools():
    run = _run({
        "runtime_snapshot": {"tools": ["read_file"]},
    })
    effective = PiRuntimeCaseExecutor(run)._preflight()
    assert effective["tools"] == ["read_file"]


def test_preflight_rejects_credential_refs_by_name():
    from motte_sdk.execution_backends import ExecutionBackendError

    run = _run({
        "runtime_profile": {"credential_refs": ["anthropic-console"]},
    })
    with pytest.raises(ExecutionBackendError) as raised:
        PiRuntimeCaseExecutor(run).run_build_gate()
    assert raised.value.code == "RUNTIME_CREDENTIALS_UNRESOLVED"


def test_preflight_rejects_script_and_provider_together():
    from motte_sdk.execution_backends import ExecutionBackendError

    run = _run({
        "runtime_profile": {
            "native_settings": {
                "model": "m",
                "script": [[{"type": "text", "text": "ok"}]],
                "provider": {"api": "openai-completions", "base_url": "http://x"},
            },
        },
    })
    with pytest.raises(ExecutionBackendError) as raised:
        PiRuntimeCaseExecutor(run)._preflight()
    assert raised.value.code == "RUNTIME_MODEL_CONFIG_REQUIRED"


def test_preflight_accepts_provider_transport_without_script():
    run = _run({
        "runtime_profile": {
            "native_settings": {
                "model": "fake-model",
                "provider": {
                    "api": "openai-completions",
                    "base_url": "http://127.0.0.1:9/v1",
                    "api_key_env": "FAKE_PI_KEY",
                },
            },
        },
    })
    effective = PiRuntimeCaseExecutor(run)._preflight()
    assert effective is not None  # http 传输通过预检


def test_sdk_version_gate_fails_closed_on_drift():
    from motte_sdk.execution_backends import ExecutionBackendError

    class _FakeSession:
        sdk_version = "0.72.0"
        closed = False

        def close(self):
            self.closed = True

    executor = PiRuntimeCaseExecutor(_run())
    fake = _FakeSession()
    with pytest.raises(ExecutionBackendError) as raised:
        executor._enforce_sdk_version(fake, "0.73.1")
    assert raised.value.code == "RUNTIME_VERSION_DRIFT"
    assert fake.closed is True


def test_termination_reason_mapping_budget_and_timeout():
    assert PiRuntimeCaseExecutor._termination_reason(
        {"budget_stop_reason": "max_steps"},
    ) == "max_steps"
    assert PiRuntimeCaseExecutor._termination_reason(
        {"budget_stop_reason": "max_tool_calls"},
    ) == "max_tool_calls"
    assert PiRuntimeCaseExecutor._termination_reason(
        {"interrupted": True, "status": "completed"},
    ) == "cancelled"
    assert PiRuntimeCaseExecutor._termination_reason({"status": "timeout"}) == "wall_time"
    assert PiRuntimeCaseExecutor._termination_reason({"status": "completed"}) == "final_answer"
    assert PiRuntimeCaseExecutor._termination_reason({"status": "error"}) == "error"
