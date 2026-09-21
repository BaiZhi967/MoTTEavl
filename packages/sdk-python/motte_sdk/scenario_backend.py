"""scenario@1 执行后端：用 Workflow 步骤驱动一个 Target（M5-T05）。

身份分层（M5 执行计划第 2 节第 1 条）：

* 外层 backend 身份固定为 scenario@1，写进 manifest.execution；
* 内层 target 身份来自 manifest.runtime（例如 pi-agent@1）或内置 Agent，
  单独冻结在 manifest.runtime_snapshot / agent_config 里；
* 二者不得混作同一 backend，也不得借 execution 字段整体放松 M4 的身份校验。

流程步骤**不是**子 Run，也**不是**伪 Trial：一个 CaseAttempt 装配一个
FixtureInstance + 一个 TargetSession + 一个引擎实例，共用既有 service attach
的事件、调用日志、取消与 Artifact 通道。
"""
from __future__ import annotations

from typing import Any

from .execution_backends import (
    ExecutionBackendError,
    ExecutionBackendSpec,
    ExecutionHandle,
    register_backend,
)

SCENARIO_BACKEND_ID = "scenario"
SCENARIO_BACKEND_VERSION = "1"

#: 场景 backend 的能力声明：逐 Case 调用；带副作用，重复执行不安全。
SCENARIO_CAPABILITIES: dict[str, bool] = {
    "interactive": False,
    "safe_to_repeat": False,
    "multi_turn": True,
}


def target_kind_of(manifest: dict[str, Any]) -> str:
    """解析目标身份；实现只有一份（motte_scenario.target_identity）。"""
    from motte_scenario.target_identity import TargetIdentityError, target_kind_of as _kind

    try:
        return _kind(manifest)
    except TargetIdentityError as error:
        raise ExecutionBackendError(error.code, str(error)) from error


def validate_scenario_manifest(manifest: dict[str, Any]) -> None:
    """创建/分派前的场景配置校验：快照齐备、目标能力满足要求、fixture 有 hash。"""
    snapshot = manifest.get("workflow_snapshot")
    if not isinstance(snapshot, dict) or not snapshot:
        raise ExecutionBackendError(
            "SCENARIO_WORKFLOW_REQUIRED",
            "scenario runs require a resolved manifest.workflow_snapshot",
        )
    target = manifest.get("target_snapshot")
    if not isinstance(target, dict) or not target:
        raise ExecutionBackendError(
            "SCENARIO_TARGET_REQUIREMENTS_REQUIRED",
            "scenario runs require manifest.target_snapshot from the workflow contract",
        )
    steps = snapshot.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ExecutionBackendError(
            "SCENARIO_WORKFLOW_INVALID", "workflow snapshot carries no steps"
        )
    # 先校验冻结输入的形状，再校验目标能力：配置错误应当先报出来，能力不足
    # 是另一类错误（错误码不同，客户端可以分别处理）。
    fixtures = manifest.get("fixture_snapshot")
    if not isinstance(fixtures, dict) or not fixtures:
        raise ExecutionBackendError(
            "SCENARIO_FIXTURE_REQUIRED",
            "scenario runs require pinned fixture snapshots",
        )
    for key, record in fixtures.items():
        if not isinstance(record, dict) or not record.get("content_hash"):
            raise ExecutionBackendError(
                "SCENARIO_FIXTURE_INVALID",
                f"fixture snapshot {key} is missing a pinned content hash",
            )
    kind = target_kind_of(manifest)
    from motte_scenario.targets import (
        TargetCapabilityError,
        require_capabilities,
        target_capabilities,
    )

    try:
        capabilities = target_capabilities(kind, manifest)
        require_capabilities(target, capabilities)
    except TargetCapabilityError as error:
        raise ExecutionBackendError(error.code, str(error)) from error
    if kind == "builtin-agent":
        _validate_builtin_agent_target(manifest)


def _validate_builtin_agent_target(manifest: dict[str, Any]) -> None:
    """内置 Agent 目标的最小配置：模式受支持、模型路径已解析。"""
    from motte_contracts.agent_tasks import AGENT_MODES

    config = manifest.get("agent_config")
    if not isinstance(config, dict):
        raise ExecutionBackendError(
            "SCENARIO_TARGET_CONFIG_REQUIRED",
            "builtin-agent scenario runs require manifest.agent_config",
        )
    mode = config.get("mode")
    if mode not in AGENT_MODES:
        raise ExecutionBackendError(
            "SCENARIO_TARGET_MODE_UNSUPPORTED",
            "agent mode must be one of " + ", ".join(AGENT_MODES) + f"; got {mode!r}",
        )
    if not isinstance(manifest.get("provider"), dict):
        raise ExecutionBackendError(
            "SCENARIO_TARGET_PROVIDER_REQUIRED",
            "builtin-agent scenario runs require a resolved provider snapshot",
        )


def _build_scenario(run: dict[str, Any]) -> ExecutionHandle:
    from motte_scenario.executor import ScenarioCaseExecutor

    executor = ScenarioCaseExecutor(run)

    def attach(service: Any, run_id: str) -> None:
        executor.bind_service(service)

    return ExecutionHandle(
        backend_id=SCENARIO_BACKEND_ID,
        backend_version=SCENARIO_BACKEND_VERSION,
        invoke=executor.invoke,
        capabilities=dict(SCENARIO_CAPABILITIES),
        attach=attach,
    )


def install_scenario_backend(*, available: bool | None = None) -> ExecutionBackendSpec:
    """注册/更新 scenario@1。

    available=False 时创建期即明确 unavailable，不静默改选其他 backend
    （M5-T01 完成门：可执行 backend 注册前公开执行必须明确不可用）。
    默认沿用本模块的 SCENARIO_BACKEND_AVAILABLE 事实开关。
    """
    if available is None:
        available = SCENARIO_BACKEND_AVAILABLE
    return register_backend(
        ExecutionBackendSpec(
            id=SCENARIO_BACKEND_ID,
            version=SCENARIO_BACKEND_VERSION,
            validate=validate_scenario_manifest,
            build=_build_scenario,
            capabilities=dict(SCENARIO_CAPABILITIES),
            available=available,
            execution_mode="sample",
        ),
        replace=True,
    )


#: M5 执行状态开关。
#:
#: 契约、编译、条件、Fixture 生命周期与有界引擎（含受控进程目标）都已实现
#: 并有测试；但把引擎接进既有 CaseAttempt/Observation/ScoringPass 纵向链路的
#: ScenarioCaseExecutor **尚未交付**（M5-T05 未完成）。在它落地之前公开执行
#: 必须明确 unavailable：创建期就返回 EXECUTION_BACKEND_UNAVAILABLE，而不是
#: 让 Run 进入分派后才失败，也绝不静默改选 replay/direct-llm。
SCENARIO_BACKEND_AVAILABLE = False

# 内置 Agent 目标 adapter 与 backend 一起安装：目标不可用时创建期就拒绝。
try:
    from .scenario_target import install_builtin_target_adapter as _install_builtin_target

    _install_builtin_target()
except ImportError:  # pragma: no cover - motte-agent 缺失时保持目标不可用
    pass

install_scenario_backend(available=SCENARIO_BACKEND_AVAILABLE)
