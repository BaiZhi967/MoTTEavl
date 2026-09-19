"""Execution backend registry.

Provider adapters only describe wire protocols.  This registry selects the runtime
that owns a Run (direct provider calls, deterministic replay, or an external
benchmark adapter) and records that decision in the resolved manifest.  Every
backend declares an ``execution_mode``: ``sample`` backends keep the per-case
invoke path; ``job`` backends launch exactly one external job per Run
(M2-G01) and expose a ``run_job`` hook instead.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from motte_contracts.external_job import ExternalJobSpec


class ExecutionBackendError(ValueError):
    """A Run cannot be mapped to an available execution backend."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExecutionHandle:
    backend_id: str
    backend_version: str
    # sample 模式逐 Case 调用入口；job 模式后端不提供（分派走 run_job）。
    invoke: Callable[[str], Any] | None = None
    capabilities: dict[str, bool] = field(default_factory=dict)
    # 可选：dispatch 前把 RunService 上下文接到后端（事件通道 / 调用日志 / 取消探测）
    attach: Callable[[Any, str], None] | None = None
    execution_mode: str = "sample"
    # job 模式入口：整 Run 调用一次，参数为已分派的 run 快照，返回 job 结局。
    run_job: Callable[[dict[str, Any]], dict[str, Any]] | None = None


@dataclass(frozen=True)
class ExecutionBackendSpec:
    id: str
    version: str
    validate: Callable[[dict[str, Any]], None]
    build: Callable[[dict[str, Any]], ExecutionHandle]
    capabilities: dict[str, bool] = field(default_factory=dict)
    available: bool = True
    execution_mode: str = "sample"


_SPECS: dict[tuple[str, str], ExecutionBackendSpec] = {}


def register_backend(spec: ExecutionBackendSpec) -> ExecutionBackendSpec:
    key = (spec.id, spec.version)
    if key in _SPECS:
        raise ValueError(f"execution backend already registered: {spec.id}@{spec.version}")
    _SPECS[key] = spec
    return spec


def unregister_backend(backend_id: str, version: str) -> None:
    """Remove a backend registration. Intended for isolated plugin tests."""
    _SPECS.pop((backend_id, version), None)


def backend_for(backend_id: str, version: str) -> ExecutionBackendSpec:
    try:
        spec = _SPECS[(backend_id, version)]
    except KeyError:
        known = ", ".join(f"{item.id}@{item.version}" for item in _SPECS.values()) or "(none)"
        raise ExecutionBackendError(
            "EXECUTION_BACKEND_UNSUPPORTED",
            f"unsupported execution backend: {backend_id}@{version} (known: {known})",
        ) from None
    if not spec.available:
        raise ExecutionBackendError(
            "EXECUTION_BACKEND_UNAVAILABLE",
            f"execution backend is registered but unavailable: {backend_id}@{version}",
        )
    return spec


def registered_backends() -> tuple[ExecutionBackendSpec, ...]:
    return tuple(sorted(_SPECS.values(), key=lambda spec: (spec.id, spec.version)))


def execution_descriptor(manifest: dict[str, Any]) -> dict[str, Any]:
    descriptor = manifest.get("execution")
    if not isinstance(descriptor, dict):
        raise ExecutionBackendError(
            "EXECUTION_BACKEND_REQUIRED", "resolved manifest requires execution backend metadata"
        )
    backend_id = descriptor.get("backend_id")
    version = descriptor.get("backend_version")
    if not isinstance(backend_id, str) or not backend_id:
        raise ExecutionBackendError("EXECUTION_BACKEND_REQUIRED", "execution.backend_id is required")
    if not isinstance(version, str) or not version:
        raise ExecutionBackendError("EXECUTION_BACKEND_REQUIRED", "execution.backend_version is required")
    return descriptor


def validate_execution_manifest(manifest: dict[str, Any]) -> None:
    descriptor = execution_descriptor(manifest)
    spec = backend_for(descriptor["backend_id"], descriptor["backend_version"])
    spec.validate(manifest)


def build_execution_handle(run: dict[str, Any]) -> ExecutionHandle:
    manifest = run.get("manifest") or {}
    descriptor = execution_descriptor(manifest)
    spec = backend_for(descriptor["backend_id"], descriptor["backend_version"])
    spec.validate(manifest)
    return spec.build(run)


def resolve_execution(
    scenario_version: str,
    manifest: dict[str, Any],
    *,
    scenario: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pin a backend in a resolved manifest and reject ambiguous runtime fields.

    New Runs never infer their runtime in the Worker.  Legacy Runs are adapted by
    ``legacy_execution`` at read/dispatch time only.
    """
    resolved = deepcopy(manifest)
    requested = resolved.get("execution")
    runtime_fields = [name for name in ("agent", "skills", "harness", "pi") if resolved.get(name)]
    scenario_mode = (scenario or {}).get("mode")
    agent_requested = (
        resolved.get("agent") in (None, "builtin-agent@1") and scenario_mode == "agent-tasks"
    ) or resolved.get("agent") == "builtin-agent@1"
    if runtime_fields and requested is None and not agent_requested:
        raise ExecutionBackendError(
            "EXECUTION_BACKEND_UNSUPPORTED",
            "manifest declares runtime fields without a registered execution backend: "
            + ", ".join(runtime_fields),
        )
    if agent_requested and requested is None:
        non_agent_fields = [name for name in ("skills", "harness", "pi") if resolved.get(name)]
        if non_agent_fields:
            raise ExecutionBackendError(
                "EXECUTION_BACKEND_UNSUPPORTED",
                "builtin-agent does not support runtime fields: " + ", ".join(non_agent_fields),
            )
        requested = {"backend_id": "builtin-agent", "backend_version": "1"}
    if requested is None:
        provider = resolved.get("provider")
        mode = (scenario or {}).get("mode")
        if scenario_version.startswith("replay@"):
            backend_id = "replay"
        elif mode == "external-benchmark":
            backend_id = "external-benchmark"
        elif mode == "direct-llm" or scenario_version.startswith("direct-llm@"):
            backend_id = "direct-llm"
        elif scenario_version.startswith("json_extract@"):
            backend_id = (
                "direct-llm"
                if isinstance(provider, dict) and provider.get("kind") != "replay"
                else "replay"
            )
        elif isinstance(provider, dict):
            backend_id = "replay" if provider.get("kind") == "replay" else "direct-llm"
        else:
            raise ExecutionBackendError(
                "EXECUTION_BACKEND_REQUIRED",
                "run requires an explicit execution backend or resolved provider",
            )
        requested = {"backend_id": backend_id, "backend_version": "1"}
    elif not isinstance(requested, dict):
        raise ExecutionBackendError("EXECUTION_BACKEND_INVALID", "manifest.execution must be an object")

    backend_id = requested.get("backend_id")
    version = str(requested.get("backend_version") or "1")
    spec = backend_for(str(backend_id or ""), version)
    requested_mode = requested.get("execution_mode")
    if requested_mode is not None and requested_mode != spec.execution_mode:
        raise ExecutionBackendError(
            "EXECUTION_MODE_CONFLICT",
            f"execution backend {spec.id}@{spec.version} dispatches in "
            f"{spec.execution_mode} mode, not {requested_mode}",
        )
    descriptor = {
        "backend_id": spec.id,
        "backend_version": spec.version,
        "capabilities": dict(spec.capabilities),
        "execution_mode": spec.execution_mode,
    }
    resolved["execution"] = descriptor
    spec.validate(resolved)
    return resolved


def legacy_execution(run: dict[str, Any]) -> dict[str, Any]:
    """Project a pre-v2 Run onto backend metadata without mutating history."""
    manifest = run.get("manifest") or {}
    if isinstance(manifest.get("execution"), dict):
        return manifest
    provider = manifest.get("provider")
    scenario_version = run.get("scenario_version", "")
    if scenario_version.startswith("replay@"):
        backend_id = "replay"
    elif scenario_version.startswith("direct-llm@"):
        if not isinstance(provider, dict):
            raise ExecutionBackendError(
                "EXECUTION_BACKEND_REQUIRED", "legacy direct-llm run has no provider"
            )
        backend_id = "direct-llm"
    elif scenario_version.startswith("json_extract@"):
        backend_id = (
            "direct-llm"
            if isinstance(provider, dict) and provider.get("kind") != "replay"
            else "replay"
        )
    elif isinstance(provider, dict):
        backend_id = "replay" if provider.get("kind") == "replay" else "direct-llm"
    else:
        raise ExecutionBackendError(
            "EXECUTION_BACKEND_REQUIRED", "legacy run has no provider or replay scenario"
        )
    return {**manifest, "execution": {
        "backend_id": backend_id,
        "backend_version": "1",
        "capabilities": dict(backend_for(backend_id, "1").capabilities),
        "execution_mode": backend_for(backend_id, "1").execution_mode,
    }}


def _reject_unconnected_runtime_fields(manifest: dict[str, Any]) -> None:
    runtime_fields = [name for name in ("agent", "skills", "harness", "pi") if manifest.get(name)]
    if runtime_fields:
        raise ExecutionBackendError(
            "EXECUTION_BACKEND_UNSUPPORTED",
            "execution backend does not support runtime fields: " + ", ".join(runtime_fields),
        )


def _validate_direct(manifest: dict[str, Any]) -> None:
    _reject_unconnected_runtime_fields(manifest)
    provider = manifest.get("provider")
    if not isinstance(provider, dict) or not provider.get("kind"):
        raise ExecutionBackendError(
            "PROVIDER_CONFIG_INVALID", "direct-llm backend requires manifest.provider.kind"
        )
    if provider.get("kind") != "replay" and (
        "fixture" in provider or "replay_fixture" in manifest
    ):
        raise ExecutionBackendError(
            "EXECUTION_BACKEND_CONFLICT",
            "replay fixtures require a replay provider",
        )


def _provider_execution_manifest(
    manifest: dict[str, Any], provider_config: dict[str, Any],
) -> dict[str, Any]:
    """Project only allowlisted request-building inputs across the adapter boundary."""
    projected: dict[str, Any] = {"cases": deepcopy(manifest.get("cases") or {})}
    for key in ("tools", "parameters"):
        if key in manifest:
            projected[key] = deepcopy(manifest[key])
    if provider_config.get("kind") == "replay" and "replay_fixture" in manifest:
        projected["replay_fixture"] = deepcopy(manifest["replay_fixture"])
    return projected


def _build_direct(run: dict[str, Any]) -> ExecutionHandle:
    from motte_provider.config import build_provider

    manifest = run.get("manifest") or {}
    provider_config = manifest["provider"]
    provider = build_provider(
        provider_config, _provider_execution_manifest(manifest, provider_config)
    )
    return ExecutionHandle(
        backend_id="direct-llm",
        backend_version="1",
        invoke=provider.invoke,
        capabilities=dict(_SPECS[("direct-llm", "1")].capabilities),
    )


def resolve_replay_case_ids(
    manifest: dict[str, Any], case_ids: list[str] | tuple[str, ...] | None = None
) -> list[str]:
    """Validate explicit replay fixtures and derive their selected case IDs.

    A replay run created before the two-step API attachment may intentionally have
    no fixture yet. Once either fixture field is present, however, it must be
    non-empty, shaped, and cover every selected case.
    """
    provider = manifest.get("provider")
    provider_fixture = (
        provider.get("fixture")
        if isinstance(provider, dict) and "fixture" in provider
        else None
    )
    has_provider_fixture = isinstance(provider, dict) and "fixture" in provider
    has_manifest_fixture = "replay_fixture" in manifest
    manifest_fixture = manifest.get("replay_fixture") if has_manifest_fixture else None
    if not has_provider_fixture and not has_manifest_fixture:
        return list(case_ids or [])
    if has_provider_fixture and has_manifest_fixture and provider_fixture != manifest_fixture:
        raise ExecutionBackendError(
            "REPLAY_FIXTURE_CONFLICT", "provider.fixture and replay_fixture must match"
        )
    fixture = manifest_fixture if has_manifest_fixture else provider_fixture
    if not isinstance(fixture, dict) or not fixture:
        raise ExecutionBackendError(
            "REPLAY_FIXTURE_INVALID",
            "replay fixture must contain non-empty object cases with output fields",
        )
    try:
        from motte_contracts.run import ReplayCase
        for case_id, item in fixture.items():
            if not isinstance(case_id, str) or not case_id:
                raise ValueError("case IDs must be nonempty strings")
            ReplayCase.model_validate(item)
    except Exception as error:
        raise ExecutionBackendError(
            "REPLAY_FIXTURE_INVALID",
            "replay fixture cases must match the ReplayCase contract",
        ) from error
    selected = list(case_ids or fixture)
    missing = [case_id for case_id in selected if case_id not in fixture]
    if missing:
        raise ExecutionBackendError(
            "REPLAY_CASE_NOT_FOUND",
            f"replay fixture has no selected cases: {missing}",
        )
    return selected


def _validate_replay(manifest: dict[str, Any]) -> None:
    _reject_unconnected_runtime_fields(manifest)
    provider = manifest.get("provider")
    if provider is not None and (
        not isinstance(provider, dict) or provider.get("kind") not in (None, "replay")
    ):
        raise ExecutionBackendError(
            "EXECUTION_BACKEND_CONFLICT", "replay backend cannot use a non-replay provider"
        )
    if (isinstance(provider, dict) and "fixture" in provider) or "replay_fixture" in manifest:
        resolve_replay_case_ids(manifest)


def _build_replay(run: dict[str, Any]) -> ExecutionHandle:
    from motte_sdk.replay_run import ReplayProvider

    manifest = run.get("manifest") or {}
    provider_config = manifest.get("provider") or {}
    fixture = provider_config.get("fixture") or manifest.get("replay_fixture") or {}
    provider = ReplayProvider(fixture)
    return ExecutionHandle(
        backend_id="replay",
        backend_version="1",
        invoke=provider.invoke,
        capabilities=dict(_SPECS[("replay", "1")].capabilities),
    )


def validate_external_job_manifest(manifest: dict[str, Any]) -> None:
    """job 模式 manifest 的共享校验：外部配置必须钉住全部版本（M2-G06）。

    缺 runner/profile/environment 版本的外部 Job 一律在创建/分派前拒绝，
    不允许占位 revision/digest 进入执行。
    """
    _reject_unconnected_runtime_fields(manifest)
    external = manifest.get("external_benchmark")
    if not isinstance(external, dict) or not str(external.get("adapter_id") or "").strip():
        raise ExecutionBackendError(
            "EXTERNAL_BENCHMARK_CONFIG_INVALID",
            "external-benchmark backend requires external_benchmark.adapter_id",
        )
    if not str(external.get("adapter_version") or "").strip():
        raise ExecutionBackendError(
            "EXTERNAL_BENCHMARK_CONFIG_INVALID",
            "external-benchmark backend requires external_benchmark.adapter_version",
        )
    missing = [
        name for name in ("runner_version", "dataset_revision", "environment_digest")
        if not str(external.get(name) or "").strip()
    ]
    profile = external.get("profile")
    if not isinstance(profile, dict):
        missing.append("profile")
    else:
        missing.extend(
            f"profile.{name}"
            for name in ("benchmark_id", "benchmark_version")
            if not str(profile.get(name) or "").strip()
        )
    runner_config = external.get("runner_config")
    if (
        not isinstance(runner_config, dict)
        or not isinstance(runner_config.get("cases"), list)
        or not runner_config.get("cases")
    ):
        # Runner 必须拿到可消费的冻结输入（review R01）：没有逐题配置的
        # 外部 Run 在创建层拒绝，不留到分派时空跑。
        missing.append("runner_config.cases")
    if missing:
        raise ExecutionBackendError(
            "EXTERNAL_JOB_VERSION_REQUIRED",
            "external job requires pinned versions for: " + ", ".join(missing),
        )


def external_job_spec_from_run(run: dict[str, Any], *, work_root: str) -> ExternalJobSpec:
    """把已冻结的 run 投影成 ExternalJobSpec；selected case 来自 run.case_ids。

    execution_config_hash 由 manifest.external_benchmark 的规范 JSON 决定，
    同一配置重复投影得到同一 hash。API 只创建 Run 不启动 Job；本函数在
    分派侧调用，供 job supervisor 固定启动输入。
    """
    manifest = run.get("manifest") or {}
    external = manifest.get("external_benchmark")
    if not isinstance(external, dict) or not external:
        raise ExecutionBackendError(
            "EXTERNAL_BENCHMARK_CONFIG_INVALID",
            "external job run requires manifest.external_benchmark",
        )
    selected = [str(case_id) for case_id in (run.get("case_ids") or [])]
    if not selected:
        raise ExecutionBackendError(
            "EXTERNAL_JOB_SELECTION_EMPTY",
            "external job run requires at least one selected case",
        )
    config_bytes = json.dumps(
        external, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    try:
        return ExternalJobSpec(
            run_id=str(run.get("id") or ""),
            adapter_id=str(external.get("adapter_id") or ""),
            adapter_version=str(external.get("adapter_version") or ""),
            runner_version=str(external.get("runner_version") or ""),
            execution_config_hash="sha256:" + hashlib.sha256(config_bytes).hexdigest(),
            dataset_revision=str(external.get("dataset_revision") or ""),
            selected_case_ids=selected,
            profile=deepcopy(external.get("profile")) or {},
            work_root=work_root,
            environment_digest=str(external.get("environment_digest") or ""),
            limits=deepcopy(external.get("limits")) or {},
            retry_policy=deepcopy(external.get("retry_policy")) or {},
            runner_config=deepcopy(external.get("runner_config")) or {},
        )
    except ValidationError as error:
        raise ExecutionBackendError(
            "EXTERNAL_JOB_SPEC_INVALID",
            f"external job spec failed contract validation: {error.error_count()} error(s)",
        ) from error


_validate_external = validate_external_job_manifest


def _build_external(run: dict[str, Any]) -> ExecutionHandle:
    """装配 job 模式入口：adapter 来自显式注册表，存储/工件经 attach 绑定。

    API 只创建 Run 不启动 Job；分派时 Worker 经 dispatcher 进入
    DurableExternalJobRunner（一个 Run 一个 Job、崩溃恢复只观察）。
    """
    from motte_benchmark.protocol import BenchmarkRuntimeError
    from motte_benchmark.registry import adapter_for

    from .external_jobs import DurableExternalJobRunner, ExternalJobSupervisor

    manifest = run.get("manifest") or {}
    external = manifest.get("external_benchmark") or {}
    adapter_id = str(external.get("adapter_id") or "")
    try:
        adapter = adapter_for(adapter_id)
    except BenchmarkRuntimeError as error:
        raise ExecutionBackendError(error.code, str(error)) from error
    supervisor = ExternalJobSupervisor(adapter, poll_interval_seconds=0.1)
    runner = DurableExternalJobRunner(
        supervisor,
        job_store=None,
        work_root=os.environ.get("MOTTE_JOB_WORK_ROOT", "var/external-jobs"),
        parser_version=str(external.get("parser_version") or "opencompass@1"),
    )

    def attach(service: Any, run_id: str) -> None:
        runner.bind_store(service.store)
        runner.bind_service(service)

    return ExecutionHandle(
        backend_id="external-benchmark",
        backend_version="1",
        execution_mode="job",
        run_job=runner,
        attach=attach,
    )


register_backend(ExecutionBackendSpec(
    id="direct-llm",
    version="1",
    validate=_validate_direct,
    build=_build_direct,
    capabilities={"interactive": False, "safe_to_repeat": False},
))
register_backend(ExecutionBackendSpec(
    id="replay",
    version="1",
    validate=_validate_replay,
    build=_build_replay,
    capabilities={"interactive": False, "safe_to_repeat": True},
))
def _validate_agent(manifest: dict[str, Any]) -> None:
    from .agent_backend import AgentBackendError, validate_agent_manifest

    try:
        validate_agent_manifest(manifest)
    except AgentBackendError as error:
        raise ExecutionBackendError(error.code, str(error)) from error


def _build_agent(run: dict[str, Any]) -> ExecutionHandle:
    from .agent_backend import AgentCaseExecutor, build_agent_provider

    provider = build_agent_provider(run.get("manifest") or {})
    executor = AgentCaseExecutor(run, provider_complete=provider.provider.complete)

    def attach(service: Any, run_id: str) -> None:
        executor.bind_service(service)

    return ExecutionHandle(
        backend_id="builtin-agent",
        backend_version="1",
        invoke=executor.invoke,
        capabilities=dict(_SPECS[("builtin-agent", "1")].capabilities),
        attach=attach,
    )


register_backend(ExecutionBackendSpec(
    id="builtin-agent",
    version="1",
    validate=_validate_agent,
    build=_build_agent,
    capabilities={"interactive": False, "safe_to_repeat": False},
    available=True,
))
register_backend(ExecutionBackendSpec(
    id="external-benchmark",
    version="1",
    validate=_validate_external,
    build=_build_external,
    capabilities={"interactive": False, "safe_to_repeat": False},
    # M2-T07 起可用：adapter 未注册时在 build（分派）层 ADAPTER_UNKNOWN 拒绝。
    available=True,
    execution_mode="job",
))
