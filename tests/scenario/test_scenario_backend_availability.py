"""M5-T01/T05/R6 完成门：scenario backend 的实际可用性与具名拒绝。

R6 之前这里锁定的是"公开执行关闭"这一事实（创建期 EXECUTION_BACKEND_UNAVAILABLE）。
公共纵向链路交付后（真实证明见 tests/integration/test_scenario_run_backend.py：
公共 API 创建 → 持久 queued Run → 普通 WorkerLoop 领取 → 报告与评分），本文件
改为验证**实际可用性**：

* scenario@1 已注册且 available=True，resolve_execution 解析出外层 backend 身份；
* 目标能力不满足（未注册 / 多轮 / tool_modes / required_tools / evidence）仍在
  创建期具名拒绝，绝不静默改选 replay/direct-llm；
* 显式 install_scenario_backend(available=False) 仍然可以关闭新执行；
* fixture 需求在创建期与执行期使用同一个判断（见 executor.workflow_requires_fixture）。
"""
from __future__ import annotations

import pytest

from motte_scenario.targets import (
    TargetAdapter,
    TargetCapabilities,
    register_target_adapter,
    unregister_target_adapter,
)
from motte_sdk.execution_backends import (
    ExecutionBackendError,
    backend_for,
    resolve_execution,
)
from motte_sdk.scenario_backend import (
    SCENARIO_BACKEND_AVAILABLE,
    SCENARIO_BACKEND_ID,
    SCENARIO_BACKEND_VERSION,
    install_scenario_backend,
    target_kind_of,
    validate_scenario_manifest,
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
    # teardown 必须**恢复生产 adapter**，而不是简单注销：直接
    # unregister_target_adapter("builtin-agent") 会把生产注册删掉并留给后续
    # 测试，导致同会话内其它测试（例如三臂对照）创建 Run 时
    # SCENARIO_TARGET_UNSUPPORTED。该泄漏由 tests/scenario +
    # tests/integration/test_skill_ablation.py 的同会话组合暴露。
    from motte_sdk.scenario_target import install_builtin_target_adapter

    install_builtin_target_adapter()


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
        "agent_config": {"mode": "legacy-json", "budget": {"max_steps": 4}},
        "provider": {"kind": "openai-compatible", "model": "test-model"},
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


def test_scenario_backend_is_actually_available(builtin_adapter):
    """R6：注册事实与实际可用性一致，解析出外层 backend 身份。"""
    assert SCENARIO_BACKEND_AVAILABLE is True
    spec = backend_for(SCENARIO_BACKEND_ID, SCENARIO_BACKEND_VERSION)
    assert spec.available is True
    assert spec.execution_mode == "sample"
    assert spec.capabilities["multi_turn"] is True
    # 带副作用、不可安全重放：崩溃恢复不得自动重跑（M5-G05）。
    assert spec.capabilities["safe_to_repeat"] is False

    manifest = workflow_manifest()
    validate_scenario_manifest(manifest)
    resolved = resolve_execution("scenario@1", manifest, scenario={"mode": "scenario"})
    assert resolved["execution"]["backend_id"] == SCENARIO_BACKEND_ID
    assert resolved["execution"]["backend_version"] == SCENARIO_BACKEND_VERSION
    # 内层目标身份单独保留，不与外层 backend 混作一个身份。
    assert resolved["agent"] == "builtin-agent@1"
    assert resolved["workflow"] == "order-cancel-confirmed@1"


def test_execution_can_still_be_explicitly_disabled(builtin_adapter):
    """显式关闭新执行：创建期具名拒绝，绝不静默改选 replay/direct-llm。"""
    try:
        install_scenario_backend(available=False)
        with pytest.raises(ExecutionBackendError) as error:
            backend_for(SCENARIO_BACKEND_ID, SCENARIO_BACKEND_VERSION)
        assert error.value.code == "EXECUTION_BACKEND_UNAVAILABLE"
        with pytest.raises(ExecutionBackendError) as resolved:
            resolve_execution("scenario@1", workflow_manifest(), scenario={"mode": "scenario"})
        assert resolved.value.code == "EXECUTION_BACKEND_UNAVAILABLE"
    finally:
        install_scenario_backend(available=SCENARIO_BACKEND_AVAILABLE)
    assert backend_for(SCENARIO_BACKEND_ID, SCENARIO_BACKEND_VERSION).available is True


def test_missing_or_empty_snapshots_are_refused_at_creation(builtin_adapter):
    with pytest.raises(ExecutionBackendError, match="workflow_snapshot"):
        validate_scenario_manifest(workflow_manifest(workflow_snapshot=None))
    with pytest.raises(ExecutionBackendError, match="target_snapshot"):
        validate_scenario_manifest(workflow_manifest(target_snapshot=None))
    with pytest.raises(ExecutionBackendError, match="no steps"):
        validate_scenario_manifest(workflow_manifest(workflow_snapshot={
            "ref": "x@1", "content_hash": "sha256:" + "1" * 64, "steps": [],
        }))
    # 没有声明 fixture_refs 的流程不要求固定 Fixture（纯消息 + 断言的场景）。
    validate_scenario_manifest(workflow_manifest(fixture_snapshot=None))
    # 一旦 Workflow 声明了 fixture_refs，缺失/空的固定 Fixture 就必须拒绝。
    declared = workflow_manifest(fixture_snapshot=None)
    declared["workflow_snapshot"] = {
        **declared["workflow_snapshot"],
        "fixture_refs": [{"fixture_id": "order-state", "version": 1, "kind": "json"}],
    }
    with pytest.raises(ExecutionBackendError, match="pinned fixture"):
        validate_scenario_manifest(declared)
    with pytest.raises(ExecutionBackendError, match="pinned fixture"):
        validate_scenario_manifest({
            **declared, "fixture_snapshot": {},
        })


def test_fixture_requirement_agrees_between_creation_and_execution(builtin_adapter):
    """声明 fixture 工具步骤却没有固定 Fixture：创建期就必须具名拒绝。"""
    needs_fixture = workflow_manifest(fixture_snapshot=None)
    needs_fixture["workflow_snapshot"] = {
        **needs_fixture["workflow_snapshot"],
        "steps": [
            {"step_id": "call", "kind": "invoke_fixture_tool", "tool": "orders.cancel",
             "arguments": {"order_id": "order-1"}},
        ],
    }
    with pytest.raises(ExecutionBackendError) as error:
        validate_scenario_manifest(needs_fixture)
    assert error.value.code == "SCENARIO_FIXTURE_REQUIRED"
    # 同一个判断来源（executor.workflow_requires_fixture）也把事件步骤算进去。
    from motte_scenario.executor import workflow_requires_fixture

    assert workflow_requires_fixture(needs_fixture["workflow_snapshot"]) is True
    assert workflow_requires_fixture({
        "steps": [{"step_id": "greet", "kind": "send_message", "message": "hi"}],
    }) is False
    assert workflow_requires_fixture({
        "steps": [{"step_id": "loop", "kind": "loop", "max_iterations": 2, "body": [
            {"step_id": "inner", "kind": "trigger_fixture_event", "event": "tick"},
        ]}],
    }) is True


def test_capability_intersection_refuses_targets_that_cannot_serve_the_workflow(
    builtin_adapter,
):
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
