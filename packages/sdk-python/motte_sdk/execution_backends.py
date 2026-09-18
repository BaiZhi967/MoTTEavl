"""Execution backend registry.

Provider adapters only describe wire protocols.  This registry selects the runtime
that owns a Run (direct provider calls, deterministic replay, or an external
benchmark adapter) and records that decision in the resolved manifest.
"""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


class ExecutionBackendError(ValueError):
    """A Run cannot be mapped to an available execution backend."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExecutionHandle:
    backend_id: str
    backend_version: str
    invoke: Callable[[str], Any]
    capabilities: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionBackendSpec:
    id: str
    version: str
    validate: Callable[[dict[str, Any]], None]
    build: Callable[[dict[str, Any]], ExecutionHandle]
    capabilities: dict[str, bool] = field(default_factory=dict)
    available: bool = True


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
    if runtime_fields and requested is None:
        raise ExecutionBackendError(
            "EXECUTION_BACKEND_UNSUPPORTED",
            "manifest declares runtime fields without a registered execution backend: "
            + ", ".join(runtime_fields),
        )
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
    descriptor = {
        "backend_id": spec.id,
        "backend_version": spec.version,
        "capabilities": dict(spec.capabilities),
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


def _build_direct(run: dict[str, Any]) -> ExecutionHandle:
    from motte_provider.config import build_provider

    manifest = run.get("manifest") or {}
    provider = build_provider(manifest["provider"], manifest)
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
    if (
        not isinstance(fixture, dict)
        or not fixture
        or any(
            not isinstance(case_id, str)
            or not case_id
            or not isinstance(item, dict)
            or "output" not in item
            for case_id, item in fixture.items()
        )
    ):
        raise ExecutionBackendError(
            "REPLAY_FIXTURE_INVALID",
            "replay fixture must contain non-empty object cases with output fields",
        )
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


def _validate_external(manifest: dict[str, Any]) -> None:
    _reject_unconnected_runtime_fields(manifest)
    external = manifest.get("external_benchmark")
    if not isinstance(external, dict) or not external.get("adapter_id"):
        raise ExecutionBackendError(
            "EXTERNAL_BENCHMARK_CONFIG_INVALID",
            "external-benchmark backend requires external_benchmark.adapter_id",
        )


def _build_external(run: dict[str, Any]) -> ExecutionHandle:
    raise ExecutionBackendError(
        "EXECUTION_BACKEND_UNAVAILABLE",
        "external-benchmark adapter execution is not connected in this release",
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
register_backend(ExecutionBackendSpec(
    id="external-benchmark",
    version="1",
    validate=_validate_external,
    build=_build_external,
    capabilities={"interactive": False, "safe_to_repeat": False},
    available=False,
))
