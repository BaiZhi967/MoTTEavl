"""M5-T01/T05 完成门：可执行 backend 注册前必须明确 unavailable。

契约、条件、Fixture 与引擎已实现，但把它们接进既有 CaseAttempt/Observation/
ScoringPass 纵向链路的 ScenarioCaseExecutor 尚未交付。本用例锁定这个事实：
创建期就具名拒绝，绝不静默改选 replay/direct-llm，也绝不让 Run 在分派后才失败。
"""
from __future__ import annotations

import pytest

from motte_sdk.execution_backends import (
    ExecutionBackendError,
    backend_for,
    resolve_execution,
)
from motte_sdk.scenario_backend import (
    SCENARIO_BACKEND_AVAILABLE,
    SCENARIO_BACKEND_ID,
    SCENARIO_BACKEND_VERSION,
    target_kind_of,
    validate_scenario_manifest,
)
from motte_scenario.targets import (
    TargetCapabilities,
    TargetCapabilityError,
    register_target_adapter,
    unregister_target_adapter,
)


@pytest.fixture
def builtin_adapter():
    """注册一个真实的测试用 Target adapter，验证能力交集与身份解析。"""
    adapter = register_target_adapter(
        TargetAdapter(
            kind="builtin-agent",
            capabilities=lambda _manifest: TargetCapabilities(
                kind="builtin-agent", multi_turn=True,
                tool_modes=("real", "mock", "replay", "deny"),
                tools=("orders.get", "orders.cancel"), interrupt=True,
                skill_injection=True,
                evidence=("events", "invocations", "artifacts", "usage"),
            ),
            open_session=lambda _manifest: None,
        ),
        replace=True,
    )
    yield adapter
    unregister_target_adapter("builtin-agent")


from motte_scenario.targets import TargetAdapter  # noqa: E402


def workflow_manifest(**overrides):
    payload = {
        "workflow": "order-cancel-confirmed@1",
        "workflow_snapshot": {
            "ref": "order-cancel-confirmed@1",
            "content_hash": "sha256:" + "1" * 64,
            "schema_version": 1,
            "step_ids": ["request"],
            "steps": [{"step_id": "request", "kind": "send_message", "message": "hi"}],
            "limits": {"max_total_steps": 20, "max_turns": 4, "wall_time_sec": 30},
            "failure_policy": "stop_case",
        },
        "target_snapshot": {"multi_turn": True, "min_turns": 1, "tool_modes": ["real"],
                            "required_tools": [], "interrupt": False,
                            "skill_injection": False, "evidence": ["events"]},
        "fixture_snapshot": {
            "order-state@1": {"fixture_id": "order-state", "version": "1", "kind": "json",
                              "content_hash": "sha256:" + "2" * 64},
        },
        "agent": "builtin-agent@1",
    }
    payload.update(overrides)
    return payload


def test_scenario_backend_identity_is_separate_from_the_target_runtime():
    assert target_kind_of({"runtime": "pi-agent@1"}) == "pi-agent"
    assert target_kind_of({"agent": "builtin-agent@1"}) == "builtin-agent"
    with pytest.raises(ExecutionBackendError, match="both"):
        target_kind_of({"runtime": "pi-agent@1", "agent": "builtin-agent@1"})
    with pytest.raises(ExecutionBackendError, match="require a target"):
        target_kind_of({})


def test_scenario_runs_cannot_override_the_outer_backend():
    manifest = workflow_manifest(execution={"backend_id": "replay", "backend_version": "1"})
    with pytest.raises(ExecutionBackendError) as error:
        resolve_execution("scenario@1", manifest, scenario={"mode": "scenario"})
    assert error.value.code == "EXECUTION_BACKEND_CONFLICT"


def test_workflow_run_resolves_to_the_scenario_backend_identity(builtin_adapter):
    """可用时解析出 scenario@1；不可用时创建期就拒绝（不能静默改选）。"""
    manifest = workflow_manifest()
    validate_scenario_manifest(manifest)
    if SCENARIO_BACKEND_AVAILABLE:
        resolved = resolve_execution("scenario@1", manifest, scenario={"mode": "scenario"})
        assert resolved["execution"]["backend_id"] == SCENARIO_BACKEND_ID
        assert resolved["execution"]["backend_version"] == SCENARIO_BACKEND_VERSION
        # 内层目标身份单独保留，不与外层 backend 混作一个身份。
        assert resolved["agent"] == "builtin-agent@1"
        assert resolved["workflow"] == "order-cancel-confirmed@1"
    else:
        with pytest.raises(ExecutionBackendError) as error:
            resolve_execution("scenario@1", manifest, scenario={"mode": "scenario"})
        assert error.value.code == "EXECUTION_BACKEND_UNAVAILABLE"


def test_missing_or_empty_snapshots_are_refused_at_creation(builtin_adapter):
    with pytest.raises(ExecutionBackendError, match="workflow_snapshot"):
        validate_scenario_manifest(workflow_manifest(workflow_snapshot=None))
    with pytest.raises(ExecutionBackendError, match="target_snapshot"):
        validate_scenario_manifest(workflow_manifest(target_snapshot=None))
    with pytest.raises(ExecutionBackendError, match="no steps"):
        validate_scenario_manifest(workflow_manifest(workflow_snapshot={
            "ref": "x@1", "content_hash": "sha256:" + "1" * 64, "steps": [],
        }))
    with pytest.raises(ExecutionBackendError, match="pinned fixture"):
        validate_scenario_manifest(workflow_manifest(fixture_snapshot={}))


def test_execution_is_unavailable_until_the_vertical_chain_is_delivered(builtin_adapter):
    """M5-T05 未交付：公开执行必须明确拒绝，不能静默改选其他 backend。"""
    if not SCENARIO_BACKEND_AVAILABLE:
        with pytest.raises(ExecutionBackendError) as error:
            backend_for(SCENARIO_BACKEND_ID, SCENARIO_BACKEND_VERSION)
        assert error.value.code == "EXECUTION_BACKEND_UNAVAILABLE"
        with pytest.raises(ExecutionBackendError):
            resolve_execution("scenario@1", workflow_manifest(), scenario={"mode": "scenario"})
    else:  # pragma: no cover - 仅在 T05 交付后走到
        spec = backend_for(SCENARIO_BACKEND_ID, SCENARIO_BACKEND_VERSION)
        assert spec.available is True


def test_capability_intersection_refuses_targets_that_cannot_serve_the_workflow(builtin_adapter):
    manifest = workflow_manifest(target_snapshot={
        "multi_turn": True, "min_turns": 2, "required_tools": ["orders.refund"],
        "tool_modes": ["real"], "interrupt": False, "skill_injection": False,
        "evidence": ["events"],
    })
    with pytest.raises(ExecutionBackendError) as error:
        validate_scenario_manifest(manifest)
    assert error.value.code == "SCENARIO_TARGET_TOOL_UNSUPPORTED"

    # 单轮 CLI 适配器不能被 workflow 当成多轮业务会话。
    register_target_adapter(TargetAdapter(
        kind="builtin-agent",
        capabilities=lambda _m: TargetCapabilities(
            kind="builtin-agent", multi_turn=False, tool_modes=("real",),
        ),
        open_session=lambda _m: None,
    ), replace=True)
    try:
        with pytest.raises(ExecutionBackendError) as single:
            validate_scenario_manifest(workflow_manifest())
        assert single.value.code == "SCENARIO_TARGET_MULTI_TURN_UNSUPPORTED"
    finally:
        unregister_target_adapter("builtin-agent")


def test_unregistered_target_fails_explicitly_without_falling_back(builtin_adapter):
    unregister_target_adapter("builtin-agent")
    with pytest.raises(ExecutionBackendError) as error:
        validate_scenario_manifest(workflow_manifest())
    assert error.value.code == "SCENARIO_TARGET_UNSUPPORTED"
