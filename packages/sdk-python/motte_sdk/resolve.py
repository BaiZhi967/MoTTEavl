"""manifest 引用解析：provider 连接名 / model id / 价格表版本 → inline provider 快照。

API 与 CLI 在 Run 创建期共用（Worker 只消费展开后的快照）。关系模型：
  ProviderConnection = 怎么连（kind/base_url/credentials/传输参数）
  ModelProfile       = 调什么（model 字符串/采样参数默认值/provider 连接名）
  PriceTable         = 多少钱（按 (model_id, version) 版本化）
校验全部走 motte_provider registry，保证无 Provider、未知资源、不合法参数
都在任何付费调用之前失败。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from motte_contracts.run import ResolvedManifest
from motte_provider.config import validate_provider_config
from motte_provider.capabilities import UnsupportedParameterError
from pydantic import ValidationError

from .execution_backends import ExecutionBackendError, resolve_execution

_SECRET_KEYS = {
    "api_key", "api-key", "x-api-key", "authorization", "password", "token",
    "secret", "cookie", "set-cookie", "proxy-authorization",
    "access_token", "refresh_token", "client_secret", "private_key",
    "openai_api_key", "anthropic_api_key", "moonshot_api_key", "deepseek_api_key",
}
_NORMALIZED_SECRET_KEYS = {re.sub(r"[-_]", "", key.lower()) for key in _SECRET_KEYS}

#: M5：这些键由创建期生成并冻结；客户端提交即拒绝（SNAPSHOT_RESERVED）。
_RESERVED_WORKFLOW_KEYS = frozenset({
    "workflow_snapshot", "target_snapshot", "fixture_snapshot",
})


class ManifestResolutionError(ValueError):
    """结构化解析失败；code 供 API/CLI 映射为用户可见错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def find_secret_paths(value: Any, path: str = "$") -> list[str]:
    """递归找出凭据形状的键路径（持久化前拒绝明文凭据）。"""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if re.sub(r"[-_]", "", key_text.lower()) in _NORMALIZED_SECRET_KEYS:
                found.append(f"{path}.{key_text}")
            found.extend(find_secret_paths(child, f"{path}.{key_text}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(find_secret_paths(child, f"{path}[{index}]"))
    return found


def resolve_manifest(
    manifest: dict[str, Any], resources: Any, *, allow_draft_model: bool = False
) -> dict[str, Any]:
    """展开 manifest 中的 provider/model/runtime 引用，返回展开后的副本（不改入参）。"""
    resolved = deepcopy(manifest or {})
    provider_ref = resolved.get("provider")
    model_ref = resolved.get("model")
    workflow_snapshot = _resolve_workflow_reference(resolved, resources)
    runtime_snapshot = _resolve_runtime_reference(resolved, resources)

    if provider_ref is not None and not isinstance(provider_ref, (str, dict)):
        raise ManifestResolutionError(
            "PROVIDER_CONFIG_INVALID", "manifest.provider must be an object or resource name"
        )

    # M4：runtime 控制模型时（runner-configured / externally-managed），
    # provider/model 解析按需进行；platform-controlled 维持原有强制路径。
    platform_controlled = (
        runtime_snapshot is not None
        and runtime_snapshot.get("model_control") == "platform-controlled"
    )
    if not platform_controlled and runtime_snapshot is not None and model_ref is None:
        model_ref = None  # CLI 原生认证：不伪造 provider（见 _connection_for）
    connection = _connection_for(
        provider_ref, model_ref, resources, allow_draft_model=allow_draft_model,
        runtime_snapshot=runtime_snapshot,
    )
    if model_ref is not None:
        if not isinstance(model_ref, str):
            raise ManifestResolutionError("MODEL_REF_INVALID", "manifest.model must be a model resource id")
        if isinstance(provider_ref, dict):
            raise ManifestResolutionError(
                "MODEL_PROVIDER_CONFLICT",
                "manifest.provider 为 inline 配置时不能同时用 manifest.model 引用模型档案",
            )
        profile = _model_profile(model_ref, resources, allow_draft=allow_draft_model)
        if resolved.get("tools") and profile.get("supports_tools") is False:
            raise ManifestResolutionError(
                "MODEL_CAPABILITY_UNSUPPORTED",
                f"model {model_ref} does not support tools (supports_tools=false)",
            )
    effective = _effective_provider(
        connection, provider_ref, model_ref, resolved, resources,
        allow_draft_model=allow_draft_model,
    )
    if effective is not None:
        from motte_provider.registry import adapter_for

        kind = effective.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ManifestResolutionError(
                "PROVIDER_CONFIG_INVALID", "resolved provider requires a non-empty kind"
            )
        try:
            adapter = adapter_for(kind)
        except (KeyError, ValueError) as error:
            raise ManifestResolutionError("PROVIDER_CONFIG_INVALID", str(error)) from error
        effective["adapter_id"] = adapter.kind
        effective["adapter_version"] = adapter.implementation_version
        effective["implementation_version"] = adapter.implementation_version
        resolved["provider"] = effective

    snapshots: dict[str, Any] = {}
    if workflow_snapshot is not None:
        # 逐步骤 Scenario：Workflow 快照 + 目标能力要求 + Fixture 固定版本全部
        # 在创建期冻结，客户端不能伪造（M5-G01/G07）。
        snapshots["workflow"] = workflow_snapshot["snapshot"]
        resolved["workflow_snapshot"] = workflow_snapshot["snapshot"]
        resolved["workflow"] = workflow_snapshot["ref"]
        resolved["target_snapshot"] = workflow_snapshot["target"]
        if workflow_snapshot["fixtures"]:
            resolved["fixture_snapshot"] = workflow_snapshot["fixtures"]
    if runtime_snapshot is not None:
        snapshots["runtime_version"] = runtime_snapshot["snapshot"]
        resolved["runtime_snapshot"] = runtime_snapshot["snapshot"]
        resolved["runtime"] = runtime_snapshot["ref"]
    if isinstance(model_ref, str):
        profile = _model_profile(model_ref, resources, allow_draft=allow_draft_model)
        snapshots["model_profile"] = {
            "id": model_ref,
            "generation": profile.get("generation", 1),
            "lifecycle": profile.get("lifecycle", "legacy"),
            "content_hash": _content_hash(profile),
            "profile_hash": profile.get("profile_hash"),
        }
        if profile.get("context_window") is not None:
            snapshots["model_profile"]["context_window"] = profile["context_window"]
    if connection is not None:
        snapshots["provider_connection"] = {
            "name": connection.get("name"),
            "generation": connection.get("generation", 1),
            "content_hash": _content_hash(connection),
        }
    if effective is not None and isinstance(effective.get("price_table"), dict):
        table = effective["price_table"]
        snapshots["price_table"] = {
            "model_id": table.get("model_id") or model_ref,
            "version": table.get("version"),
            "content_hash": _content_hash(table),
        }
    if snapshots:
        resolved["resource_snapshots"] = snapshots
    return resolved


#: 已发布 Scenario 的 evaluator 声明在 manifest 里的冻结位置。
WORKFLOW_EVALUATOR_SNAPSHOT_KEY = "workflow_evaluator"


def _freeze_workflow_evaluator(resolved: dict[str, Any], scenario: Any) -> None:
    """把 Scenario 声明的 evaluator 与指标配置冻结进 manifest（服务端生成）。

    指标配置在创建期就用既有 normalize_evaluator_config 校验：非法配置在这里
    具名拒绝，而不是等到评分时才失败。冻结快照带内容 hash，评分只读它，
    不再按可变名称重新解析资源（M5-T04/T05 的冻结身份要求）。
    """
    if not isinstance(scenario, Mapping):
        return
    spec = _workflow_evaluator_spec(scenario)
    if spec is None:
        return
    snapshots = dict(resolved.get("resource_snapshots") or {})
    snapshots[WORKFLOW_EVALUATOR_SNAPSHOT_KEY] = spec
    resolved["resource_snapshots"] = snapshots
    # 既有评分入口从 manifest.evaluation 读 scorer 身份；Scenario Run 没有
    # benchmark 描述符，这里用 scenario 后端与冻结 evaluator 的真实身份填满
    # EvaluationDescriptor，避免 pass 被记成 generic-v1。
    resolved["evaluation"] = {
        "benchmark_id": "scenario",
        "benchmark_version": "1",
        "adapter_id": "scenario",
        "adapter_version": "1",
        "scorer_id": spec["evaluator_id"],
        "scorer_version": spec["version"],
    }


def _workflow_evaluator_spec(scenario: Mapping[str, Any]) -> dict[str, Any] | None:
    evaluator = scenario.get("evaluator")
    if evaluator is None:
        return None
    if not isinstance(evaluator, Mapping):
        raise ManifestResolutionError(
            "WORKFLOW_EVALUATOR_INVALID", "scenario.evaluator must be an object"
        )
    unknown = sorted(set(evaluator) - {"evaluator_id", "version", "config", "description"})
    if unknown:
        raise ManifestResolutionError(
            "WORKFLOW_EVALUATOR_INVALID",
            "unknown scenario.evaluator fields: " + ", ".join(unknown),
        )
    evaluator_id = evaluator.get("evaluator_id")
    if not isinstance(evaluator_id, str) or not evaluator_id.strip():
        raise ManifestResolutionError(
            "WORKFLOW_EVALUATOR_INVALID", "scenario.evaluator requires a non-empty evaluator_id"
        )
    version = str(evaluator.get("version") or "1")
    from motte_eval import workflow as _workflow  # noqa: F401 - 注册 M5 过程指标
    from motte_eval.observation import EvaluatorConfigError, normalize_evaluator_config

    try:
        normalized = normalize_evaluator_config(evaluator.get("config"))
    except EvaluatorConfigError as error:
        raise ManifestResolutionError("WORKFLOW_EVALUATOR_INVALID", str(error)) from error
    return {
        "evaluator_id": evaluator_id,
        "version": version,
        "config": normalized,
        "config_sha256": _content_hash(normalized),
    }


def _resolve_workflow_reference(
    resolved: dict[str, Any], resources: Any
) -> dict[str, Any] | None:
    """把 manifest.workflow 引用展开为已发布的固定快照。

    没有 workflow 键时返回 None，原有 provider/runtime 路径不受影响。引用
    必须是已发布版本：draft / 缺失 / 越权都在创建期拒绝，运行期不再解析。
    """
    reference = resolved.get("workflow")
    if reference is None:
        return None
    if not isinstance(reference, str):
        raise ManifestResolutionError(
            "WORKFLOW_REF_INVALID", "manifest.workflow must be a name@version string"
        )
    from motte_scenario.compiler import WorkflowResolutionError, resolve_workflow_ref

    try:
        compiled = resolve_workflow_ref(reference, resources)
    except WorkflowResolutionError as error:
        raise ManifestResolutionError(error.code, str(error)) from error
    fixtures: dict[str, Any] = {}
    store = getattr(resources, "fixtures", None)
    for fixture_ref in compiled.fixture_refs:
        spec = (
            store.get(fixture_ref.fixture_id, str(fixture_ref.version))
            if store is not None else None
        )
        key = f"{fixture_ref.fixture_id}@{fixture_ref.version}"
        if spec is None:
            raise ManifestResolutionError(
                "WORKFLOW_FIXTURE_MISSING",
                f"workflow {reference} references unpublished fixture {key}",
            )
        leaked = find_secret_paths(spec)
        if leaked:
            raise ManifestResolutionError(
                "CREDENTIALS_REJECTED",
                f"fixture {key} contains credential fields: {', '.join(leaked)}",
            )
        fixtures[key] = {
            "fixture_id": fixture_ref.fixture_id,
            "version": str(fixture_ref.version),
            "kind": fixture_ref.kind,
            "content_hash": spec.get("content_hash"),
            "record": deepcopy(spec),
        }
    return {
        "ref": reference,
        "snapshot": compiled.snapshot,
        "target": compiled.target_requirements.model_dump(mode="json"),
        "fixtures": fixtures,
    }


def _resolve_runtime_reference(
    resolved: dict[str, Any], resources: Any
) -> dict[str, Any] | None:
    """解析 manifest.runtime / runtime_profile 引用为已发布快照。

    - runtime 必须是 ``name@version`` 且命中已发布的 RuntimeVersion；
    - runtime_profile 可以是内联对象或 ``name@version`` 资源引用（展开后
      内联进 manifest，快照记录 content_hash，无秘密原文）；
    - 未声明 runtime 时返回 None，原有 provider/model 路径不受影响。
    """
    runtime_ref = resolved.get("runtime")
    if runtime_ref is None:
        return None
    if not isinstance(runtime_ref, str):
        raise ManifestResolutionError(
            "RUNTIME_REF_INVALID", "manifest.runtime must be a name@version string"
        )
    name, separator, version = runtime_ref.rpartition("@")
    if not separator or not name or not version:
        raise ManifestResolutionError(
            "RUNTIME_REF_INVALID", f"runtime reference must be name@version: {runtime_ref!r}"
        )
    runtimes = getattr(resources, "runtimes", None)
    if runtimes is None:
        raise ManifestResolutionError(
            "RUNTIME_STORE_MISSING", "runtime resources are not available in this store"
        )
    record = runtimes.get(name, version)
    if record is None:
        raise ManifestResolutionError(
            "RUNTIME_NOT_FOUND", f"runtime not found: {runtime_ref}"
        )
    definition = record.get("definition") or {}
    snapshot = {
        "name": name,
        "version": version,
        "content_hash": _content_hash(record),
        "lifecycle": record.get("lifecycle", "published"),
        "model_control": definition.get("model_control"),
        "kind": definition.get("kind"),
        "transport": definition.get("transport"),
        "tool_enforcement": (definition.get("tool_control") or {}).get("enforcement"),
        # 执行侧落实权限交集与启动前版本门需要这两个字段（M4 review R07/R18）：
        # 声明的工具清单与 pinned 上游版本随快照冻结。
        "tools": list((definition.get("tool_control") or {}).get("tools") or []),
        "upstream_version": definition.get("upstream_version"),
        "interactive": bool(definition.get("interactive")),
        "config_schema": deepcopy(definition.get("config_schema") or {}),
    }
    profile_ref = resolved.get("runtime_profile")
    if isinstance(profile_ref, str):
        profile_name, profile_separator, profile_version = profile_ref.rpartition("@")
        if not profile_separator:
            raise ManifestResolutionError(
                "RUNTIME_PROFILE_REF_INVALID",
                f"runtime_profile reference must be name@version: {profile_ref!r}",
            )
        profile_store = getattr(resources, "runtime_profiles", None)
        if profile_store is None:
            raise ManifestResolutionError(
                "RUNTIME_STORE_MISSING",
                "runtime profile resources are not available in this store",
            )
        profile_record = profile_store.get(profile_name, profile_version)
        if profile_record is None:
            raise ManifestResolutionError(
                "RUNTIME_PROFILE_NOT_FOUND", f"runtime profile not found: {profile_ref}"
            )
        if profile_record.get("runtime") != runtime_ref:
            raise ManifestResolutionError(
                "RUNTIME_PROFILE_MISMATCH",
                f"runtime profile {profile_ref} targets {profile_record.get('runtime')!r}",
            )
        profile_payload = {
            key: deepcopy(profile_record[key])
            for key in ("runtime", "native_settings", "workspace", "budgets", "credential_refs")
            if key in profile_record
        }
        resolved["runtime_profile"] = profile_payload
    elif profile_ref is not None and not isinstance(profile_ref, dict):
        raise ManifestResolutionError(
            "RUNTIME_PROFILE_REF_INVALID",
            "manifest.runtime_profile must be an object or name@version reference",
        )
    profile_payload = resolved.get("runtime_profile")
    if not isinstance(profile_payload, dict) or profile_payload.get("runtime") != runtime_ref:
        raise ManifestResolutionError(
            "RUNTIME_PROFILE_REQUIRED",
            f"runtime runs require a runtime_profile bound to {runtime_ref}",
        )
    from motte_contracts.runtime import RuntimeProfile, validate_runtime_settings

    if find_secret_paths(profile_payload):
        raise ManifestResolutionError("CREDENTIALS_REJECTED", "plaintext credentials are not accepted")
    try:
        profile = RuntimeProfile.model_validate(profile_payload)
        validate_runtime_settings(snapshot["config_schema"], profile.native_settings)
    except ValueError as error:
        raise ManifestResolutionError("RUNTIME_PROFILE_INVALID", str(error)) from error
    profile_payload["config_hash"] = _content_hash(profile_payload)
    return {"ref": runtime_ref, "snapshot": snapshot}


def prepare_run(scenario_version: str, manifest: dict[str, Any], case_ids, resources: Any):
    """Shared creation preflight; benchmark resources expand once, never in Worker."""
    from motte_contracts import suites as contract_suites
    from motte_sdk.benchmark_plugins import plugin_for_scenario
    from motte_sdk.suites import CASE_SELECTION_KEY, resolve_managed_manifest

    manifest = deepcopy(manifest or {})
    if find_secret_paths(manifest):
        raise ManifestResolutionError("CREDENTIALS_REJECTED", "plaintext credentials are not accepted")
    requested_budget = manifest.get("budget")
    if isinstance(requested_budget, dict) and "context_preflight" in requested_budget:
        raise ManifestResolutionError(
            "SNAPSHOT_RESERVED", "budget.context_preflight is generated at creation"
        )
    if contract_suites.RESERVED_KEYS.intersection(manifest):
        raise ManifestResolutionError("SNAPSHOT_RESERVED", "benchmark snapshots are generated at creation")
    reserved_workflow = _RESERVED_WORKFLOW_KEYS.intersection(manifest)
    if reserved_workflow:
        raise ManifestResolutionError(
            "SNAPSHOT_RESERVED",
            "workflow/fixture/target snapshots are generated at creation: "
            + ", ".join(sorted(reserved_workflow)),
        )
    name, sep, version = scenario_version.rpartition("@")
    scenario = resources.scenarios.get(name, version) if sep else None
    managed = False
    try:
        managed = plugin_for_scenario(scenario) is not None
        if managed:
            try:
                manifest = resolve_managed_manifest(scenario, manifest, resources)
            except (ManifestResolutionError, ValueError):
                raise
            except Exception as error:
                raise ManifestResolutionError(
                    "PLUGIN_PREPARE_FAILED", f"benchmark plugin preparation failed: {error}"
                ) from error
        elif CASE_SELECTION_KEY in manifest:
            raise ManifestResolutionError(
                "RUN_CONFIG_INVALID",
                f"{CASE_SELECTION_KEY} is only supported for benchmark scenarios")
        resolved = resolve_manifest(manifest, resources)
        if resolved.get("workflow_snapshot") is not None:
            # Workflow Run 的 evaluator 身份与指标配置由服务端在创建期冻结：
            # 客户端提交的 resource_snapshots / evaluation 一律拒绝。
            forged = {"resource_snapshots", "evaluation"}.intersection(manifest)
            if forged:
                raise ManifestResolutionError(
                    "SNAPSHOT_RESERVED",
                    "workflow evaluator snapshots are generated at creation: "
                    + ", ".join(sorted(forged)),
                )
            _freeze_workflow_evaluator(resolved, scenario)
        ids = list(case_ids or [])
        runtime_control = (resolved.get("runtime_snapshot") or {}).get("model_control")
        if managed:
            if ids:
                # 子集改由 manifest.case_selection 声明，避免两条并行的选择通道。
                raise ValueError(
                    "benchmark case selection goes through manifest.case_selection, not case_ids")
            ids = list(manifest["cases"])
            provider = resolved.get("provider")
            if not isinstance(provider, dict) and runtime_control in (None, "platform-controlled"):
                # M4：runtime 驱动（runner-configured / externally-managed）的
                # agent-tasks 允许无平台 provider；模型路径由 runtime backend
                # 的 validate_runtime_manifest 把关。
                raise ValueError("benchmark requires a provider or model resource")
            if isinstance(provider, dict):
                preset = manifest["benchmark_provenance"]
                provider["max_retries"] = preset["max_retries"]
                if preset.get("max_output_tokens") is not None:
                    provider["parameters"] = {**(provider.get("parameters") or {}),
                                              "max_output_tokens": preset["max_output_tokens"]}
        _apply_context_preflight(resolved)
        resolved = resolve_execution(scenario_version, resolved, scenario=scenario)
        from motte_sdk.execution_backends import resolve_replay_case_ids

        execution = resolved.get("execution") or {}
        provider = resolved.get("provider") or {}
        if execution.get("backend_id") == "replay" or (
            isinstance(provider, dict) and provider.get("kind") == "replay"
        ):
            ids = resolve_replay_case_ids(resolved, ids)
        validate_resolved_manifest(resolved)
        # Pure configuration compilation only: never execute a caller-selected
        # binary from the API process. Worker still checks the installed version.
        runtime_name = str(resolved.get("runtime") or "").partition("@")[0]
        if runtime_name == "pi-agent":
            from .pi_runtime import PiRuntimeCaseExecutor

            PiRuntimeCaseExecutor({"manifest": resolved})._preflight()
        elif runtime_name in ("claude-cli", "codex-cli"):
            from .cli_runtime import CliRuntimeCaseExecutor

            CliRuntimeCaseExecutor({"manifest": resolved}, backend=runtime_name)._preflight()
    except ManifestResolutionError:
        raise
    except ExecutionBackendError as error:
        raise ManifestResolutionError(error.code, str(error)) from error
    except UnsupportedParameterError as error:
        raise ManifestResolutionError("UNSUPPORTED_PARAMETER", str(error)) from error
    except (KeyError, TypeError, AttributeError) as error:
        raise ManifestResolutionError("RUN_CONFIG_INVALID", str(error)) from error
    except ValueError as error:
        raise ManifestResolutionError("RUN_CONFIG_INVALID", str(error)) from error
    return resolved, ids


def _connection_for(
    provider_ref: Any, model_ref: Any, resources: Any, *, allow_draft_model: bool = False,
    runtime_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """取 provider 连接 payload：显式名字 > 模型档案携带的连接名。

    runtime 驱动（非 platform-controlled）且未显式声明 provider/model 时，
    返回 None：CLI 原生认证不伪造平台 Provider 快照。
    """
    if provider_ref is None and model_ref is None and runtime_snapshot is not None:
        return None
    if isinstance(provider_ref, str):
        return _provider_resource(provider_ref, resources)
    if provider_ref is None and isinstance(model_ref, str):
        profile = _model_profile(model_ref, resources, allow_draft=allow_draft_model)
        return _provider_resource(profile.get("provider"), resources)
    return None


def _effective_provider(
    connection: dict[str, Any] | None,
    provider_ref: Any,
    model_ref: Any,
    manifest: dict[str, Any],
    resources: Any,
    *,
    allow_draft_model: bool = False,
) -> dict[str, Any] | None:
    """合并连接 + 模型档案 + 价格表，产出完整的 inline provider 配置。"""
    if isinstance(provider_ref, dict):
        return provider_ref  # legacy inline 全量配置，原样返回
    if connection is None:
        return None

    effective = dict(connection)
    if model_ref is not None:
        profile = _model_profile(model_ref, resources, allow_draft=allow_draft_model)
        if provider_ref is not None and profile.get("provider") != provider_ref:
            raise ManifestResolutionError(
                "MODEL_PROVIDER_CONFLICT",
                f"model {model_ref} belongs to provider {profile.get('provider')!r}, "
                f"not {provider_ref!r}",
            )
        effective["model"] = profile.get("model") or profile["id"]
        effective["parameters"] = _merge_parameters(
            profile.get("parameters") or {}, manifest.get("parameters") or {}
        )
        ceiling = _max_output_ceiling(profile)
        if ceiling is not None:
            override = (manifest.get("parameters") or {}).get("max_output_tokens")
            if override is not None and (type(override) is not int or not 0 < override <= ceiling):
                raise ManifestResolutionError(
                    "MODEL_CONFIG_INVALID", f"max_output_tokens must be positive and <= model ceiling {ceiling}"
                )
            effective["parameters"]["max_output_tokens"] = override if override is not None else ceiling
            effective["max_output_tokens"] = ceiling
        else:
            effective.pop("max_output_tokens", None)
        from motte_contracts.model import ReasoningProfile
        from motte_contracts.reasoning import reasoning_patch

        try:
            reasoning = ReasoningProfile.model_validate(profile.get("reasoning") or {})
            level = manifest.get("reasoning_level", reasoning.default_level)
            reasoning_patch(reasoning.model_dump(), level)
        except ValueError as error:
            raise ManifestResolutionError("MODEL_CONFIG_INVALID", str(error)) from error
        effective["reasoning"] = deepcopy(reasoning.model_dump())
        effective["reasoning_level"] = level
        for field_name in ("identity_policy", "identity_aliases", "identity_alias_version"):
            if field_name in profile:
                effective[field_name] = deepcopy(profile[field_name])
        effective["model_profile_id"] = profile["id"]
        effective["model_profile_hash"] = profile.get("profile_hash") or _content_hash(profile)
        effective["price_table"] = _resolve_price_table(model_ref, connection, manifest, resources)
    elif "model" not in effective:
        # 兼容旧 payload：连接里带 model 的继续可用；不带的交给 strict 校验报错
        pass
    return effective


def _content_hash(record: dict[str, Any]) -> str:
    canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _merge_parameters(profile_params: dict[str, Any], manifest_params: dict[str, Any]) -> dict[str, Any]:
    """采样参数合并：manifest 级 > 模型档案默认值；None 值不落入配置。"""
    merged = {k: v for k, v in profile_params.items() if v is not None}
    merged.update({k: v for k, v in manifest_params.items() if v is not None})
    return merged


def _max_output_ceiling(profile: dict[str, Any]) -> int | None:
    """Canonical non-null value wins; legacy is only a fallback."""
    ceiling = profile.get("max_output_tokens")
    if ceiling is None:
        ceiling = (profile.get("parameters") or {}).get("max_output_tokens")
    if ceiling is not None and (type(ceiling) is not int or ceiling <= 0):
        raise ManifestResolutionError("MODEL_CONFIG_INVALID", "max_output_tokens must be a positive integer")
    return ceiling


def _apply_context_preflight(resolved: dict[str, Any]) -> None:
    """Attach auditable context estimates when a model profile pins its window."""
    model_snapshot = (resolved.get("resource_snapshots") or {}).get("model_profile")
    if not isinstance(model_snapshot, dict) or "context_window" not in model_snapshot:
        return
    context_window = model_snapshot.get("context_window")
    if type(context_window) is not int or context_window <= 0:
        raise ManifestResolutionError(
            "MODEL_CONFIG_INVALID", "context_window must be a positive integer"
        )

    provider = resolved.get("provider") or {}
    parameters = provider.get("parameters") if isinstance(provider, dict) else {}
    reserve = parameters.get("max_output_tokens") if isinstance(parameters, dict) else None
    if reserve is None:
        reserve = 0
    if type(reserve) is not int or reserve < 0:
        raise ManifestResolutionError(
            "MODEL_CONFIG_INVALID", "max_output_tokens reserve must be a non-negative integer"
        )

    from .context_preflight import estimate_context_preflight

    report = estimate_context_preflight(
        resolved.get("cases") or {},
        context_window=context_window,
        output_tokens_reserved=reserve,
    )
    over_limit = [
        (case_id, estimate["total_tokens_upper_bound"])
        for case_id, estimate in report["per_case"].items()
        if estimate["total_tokens_upper_bound"] > context_window
    ]
    if over_limit:
        case_id, estimate = max(over_limit, key=lambda item: item[1])
        raise ManifestResolutionError(
            "CONTEXT_WINDOW_EXCEEDED",
            f"case {case_id} context upper bound {estimate} exceeds model context_window "
            f"{context_window} (output reserve {reserve}, method {report['method']})",
        )

    budget = resolved.get("budget")
    if budget is None:
        budget = {}
    if not isinstance(budget, dict):
        raise ManifestResolutionError("RUN_CONFIG_INVALID", "budget must be an object")
    persisted_summary = {
        key: report[key]
        for key in ("method", "context_window", "output_tokens_reserved", "stats")
    }
    resolved["budget"] = {**budget, "context_preflight": persisted_summary}


def _resolve_price_table(
    model_id: str,
    connection: dict[str, Any],
    manifest: dict[str, Any],
    resources: Any,
) -> dict[str, Any] | None:
    """价格表解析：manifest 显式 pin 版本 > 连接 legacy payload > 该模型最新版本。"""
    pinned = manifest.get("price_table_version")
    if pinned:
        record = resources.price_tables.get(model_id, pinned)
        if record is None:
            raise ManifestResolutionError(
                "PRICE_TABLE_NOT_FOUND", f"price table not found: {model_id}@{pinned}"
            )
        return record
    legacy = connection.get("price_table")
    if legacy:
        return legacy
    versions = [
        record for record in resources.price_tables.list()
        if record.get("model_id") == model_id
    ]
    if not versions:
        return None
    latest = max(versions, key=lambda record: _version_key(str(record.get("version", ""))))
    return latest


def _version_key(version: str) -> tuple[tuple[int, Any], ...]:
    """自然排序键：数字段按数值比较，非数字段按字符串比较。"""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", version)
        if part != ""
    )


def _provider_resource(name: Any, resources: Any) -> dict[str, Any]:
    connection = resources.providers.get(name) if isinstance(name, str) else None
    if connection is None:
        raise ManifestResolutionError("RESOURCE_NOT_FOUND", f"provider not found: {name}")
    if connection.get("enabled") is False:
        raise ManifestResolutionError("PROVIDER_DISABLED", f"provider is disabled: {name}")
    leaked = find_secret_paths(connection)
    if leaked:
        raise ManifestResolutionError(
            "CREDENTIALS_REJECTED", f"provider resource contains credential fields: {leaked}"
        )
    return connection


def _model_profile(
    model_id: str, resources: Any, *, allow_draft: bool = False
) -> dict[str, Any]:
    profile = resources.models.get(model_id)
    if profile is None:
        raise ManifestResolutionError("MODEL_NOT_FOUND", f"model not found: {model_id}")
    if profile.get("enabled") is False:
        raise ManifestResolutionError("MODEL_DISABLED", f"model is disabled: {model_id}")
    lifecycle = profile.get("lifecycle")
    if lifecycle is not None and lifecycle not in {"draft", "published", "deprecated"}:
        raise ManifestResolutionError(
            "MODEL_LIFECYCLE_INVALID", f"model has invalid lifecycle: {model_id}"
        )
    if lifecycle == "draft" and not allow_draft:
        raise ManifestResolutionError("MODEL_NOT_PUBLISHED", f"model is not published: {model_id}")
    if lifecycle == "published" and (
        not profile.get("published_at") or profile.get("deprecated_at") is not None
    ):
        raise ManifestResolutionError(
            "MODEL_LIFECYCLE_INVALID", f"published model has invalid timestamps: {model_id}"
        )
    if lifecycle == "deprecated":
        raise ManifestResolutionError("MODEL_DEPRECATED", f"model is deprecated: {model_id}")
    return profile


def validate_resolved_manifest(manifest: dict[str, Any]) -> None:
    """展开后的 manifest 最终预检（Provider 与 Execution registry）。"""
    provider = (manifest or {}).get("provider")
    if isinstance(provider, dict):
        validate_provider_config(provider)
    if isinstance((manifest or {}).get("execution"), dict):
        from .execution_backends import validate_execution_manifest

        validate_execution_manifest(manifest)
    if isinstance((manifest or {}).get("execution"), dict):
        try:
            ResolvedManifest.model_validate(manifest)
        except ValidationError as error:
            raise ManifestResolutionError(
                "RESOLVED_MANIFEST_INVALID", str(error)
            ) from error
