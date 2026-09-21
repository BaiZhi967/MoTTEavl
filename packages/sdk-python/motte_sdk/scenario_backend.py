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
    """解析目标身份：runtime 名字，或内置 Agent 的 builtin-agent。

    两种声明同时出现是配置冲突（不允许猜）；都没有时明确失败。
    """
    runtime = manifest.get("runtime")
    agent = manifest.get("agent")
    if runtime and agent:
        raise ExecutionBackendError(
            "SCENARIO_TARGET_AMBIGUOUS",
            "a scenario run declares both manifest.runtime and manifest.agent",
        )
    if runtime:
        name = str(runtime).partition("@")[0]
        if not name:
            raise ExecutionBackendError(
                "SCENARIO_TARGET_INVALID", f"runtime reference is not name@version: {runtime!r}"
            )
        return name
    if agent:
        return str(agent).partition("@")[0]
    raise ExecutionBackendError(
        "SCENARIO_TARGET_REQUIRED",
        "scenario runs require a target: manifest.runtime or manifest.agent",
    )


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


def _build_scenario(run: dict[str, Any]) -> ExecutionHandle:
    from motte_scenario.engine import ScenarioCaseExecutor

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


def install_scenario_backend(*, available: bool) -> ExecutionBackendSpec:
    """注册/更新 scenario@1。

    available=False 时创建期即明确 unavailable，不静默改选其他 backend
    （M5-T01 完成门：可执行 backend 注册前公开执行必须明确不可用）。
    """
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


install_scenario_backend(available=True)
