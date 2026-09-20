"""M4 runtime 执行后端：Pi / Claude CLI / Codex CLI 的共享装配与校验。

batch 首版注册 ``pi-agent@1``、``claude-cli@1``、``codex-cli@1``
（execution_mode=sample，逐 Case 独立 session/workspace）；交互用独立的
``codex-app-server@1``（M4-T10）。本模块只负责 manifest 校验与 backend
注册的公共部分——真实执行链路在各自任务中接线，不在这里伪造支持。

校验规则（M4-G01/G13/G14）：

- ``manifest.runtime`` 必须是 ``name@version`` 且与 backend 身份一致；
  快照必须是已发布 runtime 版本（不可变资源）。
- model_control 三分支：platform-controlled 必须解析发布模型并携带
  provider 快照；runner-configured 必须带通过 config_schema allowlist 的
  原生模型设置；externally-managed 必须声明凭据引用（名称，不是秘密）。
- 工具权限取交集：manifest 里的任何声明都不能扩大 runtime 定义之外
  的工具；强制边界为 not-enforced 时必须显式确认才允许运行。
"""
from __future__ import annotations

from typing import Any, Callable

from motte_contracts.runtime import (
    RuntimeProfile,
    RuntimeVersion,
    validate_runtime_settings,
)
from pydantic import ValidationError

from .execution_backends import (
    ExecutionBackendError,
    ExecutionBackendSpec,
    ExecutionHandle,
    register_backend,
)

# backend_id → 已注册的 runtime backend 元数据（能力快照随定义走）。
RUNTIME_BACKEND_IDS = ("pi-agent", "claude-cli", "codex-cli", "codex-app-server")


def validate_runtime_snapshot(snapshot: Any, *, backend_id: str, backend_version: str) -> dict[str, Any]:
    """快照必须是已发布 runtime 版本，且身份与 backend 一致。"""
    if not isinstance(snapshot, dict) or not snapshot:
        raise ExecutionBackendError(
            "RUNTIME_SNAPSHOT_REQUIRED",
            "runtime runs require a resolved runtime_snapshot from a published version",
        )
    if snapshot.get("lifecycle") != "published":
        raise ExecutionBackendError(
            "RUNTIME_SNAPSHOT_REQUIRED",
            "runtime snapshot must reference a published runtime version",
        )
    if snapshot.get("name") != backend_id or str(snapshot.get("version")) != backend_version:
        raise ExecutionBackendError(
            "RUNTIME_MANIFEST_INVALID",
            f"runtime snapshot {snapshot.get('name')}@{snapshot.get('version')} "
            f"does not match backend {backend_id}@{backend_version}",
        )
    if not isinstance(snapshot.get("content_hash"), str) or not snapshot["content_hash"]:
        raise ExecutionBackendError(
            "RUNTIME_SNAPSHOT_REQUIRED", "runtime snapshot requires a content hash"
        )
    return snapshot


def validate_runtime_manifest(
    manifest: dict[str, Any], *, backend_id: str, backend_version: str = "1",
) -> None:
    """runtime backend 的共享 manifest 校验（创建与 dispatch 双侧调用）。"""
    execution = manifest.get("execution") or {}
    if isinstance(execution, dict) and execution.get("backend_version"):
        backend_version = str(execution["backend_version"])
    runtime_ref = manifest.get("runtime")
    if not isinstance(runtime_ref, str):
        raise ExecutionBackendError(
            "RUNTIME_MANIFEST_INVALID", "runtime runs require manifest.runtime as name@version"
        )
    name, separator, version = runtime_ref.rpartition("@")
    if not separator or name != backend_id or version != backend_version:
        raise ExecutionBackendError(
            "RUNTIME_MANIFEST_INVALID",
            f"manifest.runtime {runtime_ref!r} does not match backend "
            f"{backend_id}@{backend_version}",
        )
    snapshot = validate_runtime_snapshot(
        manifest.get("runtime_snapshot"), backend_id=backend_id, backend_version=backend_version,
    )
    profile_payload = manifest.get("runtime_profile")
    if not isinstance(profile_payload, dict):
        raise ExecutionBackendError(
            "RUNTIME_MANIFEST_INVALID", "runtime runs require manifest.runtime_profile"
        )
    if profile_payload.get("runtime") != runtime_ref:
        raise ExecutionBackendError(
            "RUNTIME_MANIFEST_INVALID",
            f"runtime_profile references {profile_payload.get('runtime')!r}, "
            f"not {runtime_ref!r}",
        )
    try:
        profile = RuntimeProfile.model_validate(profile_payload)
    except ValidationError as error:
        # 字段 allowlist 之外的授权（如 tool_grant）在这里拒绝：声明不是权限。
        raise ExecutionBackendError(
            "RUNTIME_TOOL_GRANT_INVALID",
            f"runtime_profile fields are invalid: {error.error_count()} error(s)",
        ) from error

    model_control = snapshot.get("model_control")
    if model_control == "platform-controlled":
        provider = manifest.get("provider")
        if not isinstance(provider, dict) or not provider.get("kind"):
            raise ExecutionBackendError(
                "RUNTIME_MODEL_REQUIRED",
                "platform-controlled runtime requires a resolved provider snapshot",
            )
        if not manifest.get("model"):
            raise ExecutionBackendError(
                "RUNTIME_MODEL_REQUIRED",
                "platform-controlled runtime requires a published model reference",
            )
    elif model_control == "runner-configured":
        try:
            validate_runtime_settings(
                snapshot.get("config_schema") or {}, profile.native_settings,
            )
        except ValueError as error:
            raise ExecutionBackendError(
                "RUNTIME_MODEL_CONFIG_REQUIRED", str(error)
            ) from error
    elif model_control == "externally-managed":
        if not profile.credential_refs:
            raise ExecutionBackendError(
                "RUNTIME_AUTH_REQUIRED",
                "externally-managed runtime requires credential_refs "
                "(auth availability is probed before dispatch)",
            )
    else:
        raise ExecutionBackendError(
            "RUNTIME_MANIFEST_INVALID",
            f"runtime snapshot has unknown model_control: {model_control!r}",
        )

    if snapshot.get("tool_enforcement") == "not-enforced" and not manifest.get(
        "runtime_accept_unenforced_tools"
    ):
        raise ExecutionBackendError(
            "RUNTIME_TOOL_BOUNDARY_UNENFORCED",
            "runtime tool boundary is not enforced; pass "
            "runtime_accept_unenforced_tools=true to acknowledge and run",
        )


def runtime_validator(backend_id: str, backend_version: str) -> Callable[[dict[str, Any]], None]:
    def validate(manifest: dict[str, Any]) -> None:
        validate_runtime_manifest(
            manifest, backend_id=backend_id, backend_version=backend_version,
        )

    return validate


def register_runtime_backend(
    *,
    backend_id: str,
    version: str,
    build: Callable[[dict[str, Any]], Any],
    capabilities: dict[str, bool],
    execution_mode: str = "sample",
) -> None:
    """注册一个 runtime backend；validate 共享本模块规则。"""
    register_backend(ExecutionBackendSpec(
        id=backend_id,
        version=version,
        validate=runtime_validator(backend_id, version),
        build=build,
        capabilities=dict(capabilities),
        execution_mode=execution_mode,
    ))


def _runtime_not_implemented(backend_id: str, version: str) -> Callable[[dict[str, Any]], Any]:
    """诚实占位：执行链路未接线时显式拒绝，不假成功。"""

    def build(run: dict[str, Any]) -> Any:
        raise ExecutionBackendError(
            "RUNTIME_EXECUTION_NOT_IMPLEMENTED",
            f"runtime backend {backend_id}@{version} has no execution wiring yet",
        )

    return build


def _build_pi(run: dict[str, Any]) -> Any:
    """pi-agent@1 的执行装配：每 Case 一个 bridge session（M4-T04）。"""
    from .pi_runtime import PiRuntimeCaseExecutor

    executor = PiRuntimeCaseExecutor(run)

    def attach(service: Any, run_id: str) -> None:
        executor.bind_service(service)

    return ExecutionHandle(
        backend_id="pi-agent",
        backend_version="1",
        invoke=executor.invoke,
        capabilities={
            "interactive": False,
            "safe_to_repeat": False,
            "runtime": True,
            "events": True,
            "artifacts": True,
        },
        attach=attach,
    )


def _build_cli(backend_id: str):
    """claude-cli@1 / codex-cli@1 的执行装配（M4-T06/T07）。"""

    def build(run: dict[str, Any]) -> Any:
        from .cli_runtime import CliRuntimeCaseExecutor

        executor = CliRuntimeCaseExecutor(run, backend=backend_id)

        def attach(service: Any, run_id: str) -> None:
            executor.bind_service(service)

        return ExecutionHandle(
            backend_id=backend_id,
            backend_version="1",
            invoke=executor.invoke,
            capabilities={
                "interactive": False,
                "safe_to_repeat": False,
                "runtime": True,
                "events": True,
                "artifacts": True,
            },
            attach=attach,
        )

    return build


_RUNTIME_BUILDS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "pi-agent": _build_pi,
    "claude-cli": _build_cli("claude-cli"),
    "codex-cli": _build_cli("codex-cli"),
}


def install_runtime_backends() -> None:
    """安装 M4 runtime backend 注册（幂等；供 API/Worker 启动调用）。

    validate 共享本模块规则；build 来自 _RUNTIME_BUILDS——未接线的 backend
    保持显式 RUNTIME_EXECUTION_NOT_IMPLEMENTED 拒绝。
    """
    for backend_id in RUNTIME_BACKEND_IDS:
        capabilities = {
            "interactive": backend_id == "codex-app-server",
            "safe_to_repeat": False,
            "runtime": True,
        }
        build = _RUNTIME_BUILDS.get(backend_id) or _runtime_not_implemented(backend_id, "1")
        try:
            register_backend(ExecutionBackendSpec(
                id=backend_id,
                version="1",
                validate=runtime_validator(backend_id, "1"),
                build=build,
                capabilities=capabilities,
            ))
        except ValueError:
            # 已注册（例如测试进程重复安装）：保留现有注册。
            continue


# 四个 runtime backend 的规范 RuntimeVersion 定义（唯一事实源是
# docs/protocols/runtime-compatibility.json；此处内联同构内容供资源发布）。
CANONICAL_RUNTIME_VERSIONS: dict[str, dict[str, Any]] = {
    "pi-agent": {
        "name": "pi-agent", "version": "1",
        "definition": {
            "kind": "pi-bridge",
            "transport": "bridge-stdio-jsonl",
            "upstream_version": "@mariozechner/pi-agent-core@0.73.1",
            "adapter_version": "pi-bridge@2",
            "parser_version": "pi-jsonl-v2",
            "config_schema": {
                "properties": {
                    "model": {"type": "string"},
                    "script": {"type": "array"},
                    "system_prompt": {"type": "string"},
                    "max_steps": {"type": "integer"},
                },
                "required": ["model"],
            },
            "supported_modes": ["batch"],
            "interactive": False,
            "model_control": "runner-configured",
            "tool_control": {
                "enforcement": "bridge-sandbox",
                "enforcement_owner": "platform",
                "tools": ["read_file", "write_file", "list_files"],
                "network": "denied",
                "approval": "deny-default",
            },
            "evidence_capabilities": {
                "session_events": True, "tool_events": True, "usage": False,
                "cost": False, "model_identity": True, "artifact_collection": True,
                "cancel": True,
            },
        },
    },
    "claude-cli": {
        "name": "claude-cli", "version": "1",
        "definition": {
            "kind": "claude-cli",
            "transport": "cli-batch-json",
            "upstream_version": "@anthropic-ai/claude-code@2.1.278",
            "adapter_version": "claude-batch@1",
            "parser_version": "claude-json-v1",
            "config_schema": {
                "properties": {
                    "model": {"type": "string"},
                    "max_turns": {"type": "integer"},
                    "binary": {"type": "string"},
                },
                "required": ["model"],
            },
            "supported_modes": ["batch"],
            "interactive": False,
            "model_control": "runner-configured",
            "tool_control": {
                "enforcement": "not-enforced",
                "enforcement_owner": "runner",
                "tools": [],
                "network": "allowed",
                "approval": "deny-default",
            },
            "evidence_capabilities": {
                "session_events": True, "tool_events": True, "usage": True,
                "cost": True, "model_identity": True, "artifact_collection": True,
                "cancel": True,
            },
        },
    },
    "codex-cli": {
        "name": "codex-cli", "version": "1",
        "definition": {
            "kind": "codex-cli",
            "transport": "cli-exec-jsonl",
            "upstream_version": "@openai/codex@0.155.1",
            "adapter_version": "codex-batch@1",
            "parser_version": "codex-jsonl-v1",
            "config_schema": {
                "properties": {
                    "model": {"type": "string"},
                    "binary": {"type": "string"},
                },
                "required": ["model"],
            },
            "supported_modes": ["batch"],
            "interactive": False,
            "model_control": "runner-configured",
            "tool_control": {
                "enforcement": "not-enforced",
                "enforcement_owner": "runner",
                "tools": [],
                "network": "allowed",
                "approval": "deny-default",
            },
            "evidence_capabilities": {
                "session_events": True, "tool_events": True, "usage": True,
                "cost": False, "model_identity": True, "artifact_collection": True,
                "cancel": True,
            },
        },
    },
    "codex-app-server": {
        "name": "codex-app-server", "version": "1",
        "definition": {
            "kind": "codex-app-server",
            "transport": "app-server-jsonrpc",
            "upstream_version": "@openai/codex@0.155.1",
            "adapter_version": "codex-app-server@1",
            "parser_version": "codex-appserver-jsonrpc-v1",
            "config_schema": {
                "properties": {"model": {"type": "string"}},
                "required": ["model"],
            },
            "supported_modes": ["interactive"],
            "interactive": True,
            "model_control": "runner-configured",
            "tool_control": {
                "enforcement": "not-enforced",
                "enforcement_owner": "runner",
                "tools": [],
                "network": "allowed",
                "approval": "deny-default",
            },
            "evidence_capabilities": {
                "session_events": True, "tool_events": True, "usage": True,
                "cost": False, "model_identity": True, "artifact_collection": True,
                "cancel": True, "interactive_commands": True,
            },
        },
    },
}


def publish_canonical_runtime_versions(resources: Any) -> dict[str, Any]:
    """把规范 runtime 版本发布进资源仓库（幂等；不可变版本资源）。"""
    from datetime import UTC, datetime

    published_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    published: dict[str, Any] = {}
    for name, payload in CANONICAL_RUNTIME_VERSIONS.items():
        record = {**payload, "lifecycle": "published", "published_at": published_at}
        published[name] = resources.runtimes.put(record)
    return published


def canonical_runtime_version(name: str, definition_payload: dict[str, Any]) -> dict[str, Any]:
    """构造可发布的 RuntimeVersion 记录（存储层再做校验）。"""
    from datetime import UTC, datetime

    return RuntimeVersion.model_validate({
        "name": name,
        "version": str(definition_payload.get("version") or "1"),
        "definition": definition_payload.get("definition") or definition_payload,
        "published_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }).model_dump()
