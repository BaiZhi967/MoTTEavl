"""manifest 引用解析：provider 连接名 / model id / 价格表版本 → inline provider 快照。

API 与 CLI 在 Run 创建期共用（Worker 只消费展开后的快照）。关系模型：
  ProviderConnection = 怎么连（kind/base_url/credentials/传输参数）
  ModelProfile       = 调什么（model 字符串/采样参数默认值/provider 连接名）
  PriceTable         = 多少钱（按 (model_id, version) 版本化）
校验全部走 motte_provider registry，保证无 Provider、未知资源、不合法参数
都在任何付费调用之前失败。
"""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from motte_provider.config import validate_provider_config

_SECRET_KEYS = {
    "api_key", "api-key", "x-api-key", "authorization", "password", "token",
    "secret", "cookie", "set-cookie", "proxy-authorization",
}


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
            if key_text.lower() in _SECRET_KEYS:
                found.append(f"{path}.{key_text}")
            found.extend(find_secret_paths(child, f"{path}.{key_text}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(find_secret_paths(child, f"{path}[{index}]"))
    return found


def resolve_manifest(manifest: dict[str, Any], resources: Any) -> dict[str, Any]:
    """展开 manifest 中的 provider/model 引用，返回展开后的副本（不改入参）。"""
    resolved = deepcopy(manifest or {})
    provider_ref = resolved.get("provider")
    model_ref = resolved.get("model")

    if provider_ref is not None and not isinstance(provider_ref, (str, dict)):
        raise ManifestResolutionError(
            "PROVIDER_CONFIG_INVALID", "manifest.provider must be an object or resource name"
        )

    connection = _connection_for(provider_ref, model_ref, resources)
    if model_ref is not None:
        if not isinstance(model_ref, str):
            raise ManifestResolutionError("MODEL_REF_INVALID", "manifest.model must be a model resource id")
        if isinstance(provider_ref, dict):
            raise ManifestResolutionError(
                "MODEL_PROVIDER_CONFLICT",
                "manifest.provider 为 inline 配置时不能同时用 manifest.model 引用模型档案",
            )
        profile = _model_profile(model_ref, resources)
        if resolved.get("tools") and profile.get("supports_tools") is False:
            raise ManifestResolutionError(
                "MODEL_CAPABILITY_UNSUPPORTED",
                f"model {model_ref} does not support tools (supports_tools=false)",
            )
    effective = _effective_provider(connection, provider_ref, model_ref, resolved, resources)
    if effective is not None:
        resolved["provider"] = effective
    return resolved


def _connection_for(provider_ref: Any, model_ref: Any, resources: Any) -> dict[str, Any] | None:
    """取 provider 连接 payload：显式名字 > 模型档案携带的连接名。"""
    if isinstance(provider_ref, str):
        return _provider_resource(provider_ref, resources)
    if provider_ref is None and isinstance(model_ref, str):
        profile = _model_profile(model_ref, resources)
        return _provider_resource(profile.get("provider"), resources)
    return None


def _effective_provider(
    connection: dict[str, Any] | None,
    provider_ref: Any,
    model_ref: Any,
    manifest: dict[str, Any],
    resources: Any,
) -> dict[str, Any] | None:
    """合并连接 + 模型档案 + 价格表，产出完整的 inline provider 配置。"""
    if isinstance(provider_ref, dict):
        return provider_ref  # legacy inline 全量配置，原样返回
    if connection is None:
        return None

    effective = dict(connection)
    if model_ref is not None:
        profile = _model_profile(model_ref, resources)
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
        effective["price_table"] = _resolve_price_table(model_ref, connection, manifest, resources)
    elif "model" not in effective:
        # 兼容旧 payload：连接里带 model 的继续可用；不带的交给 strict 校验报错
        pass
    return effective


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


def _model_profile(model_id: str, resources: Any) -> dict[str, Any]:
    profile = resources.models.get(model_id)
    if profile is None:
        raise ManifestResolutionError("MODEL_NOT_FOUND", f"model not found: {model_id}")
    if profile.get("enabled") is False:
        raise ManifestResolutionError("MODEL_DISABLED", f"model is disabled: {model_id}")
    return profile


def validate_resolved_manifest(manifest: dict[str, Any]) -> None:
    """展开后的 manifest 最终预检（走 registry strict 校验）。"""
    provider = (manifest or {}).get("provider")
    if isinstance(provider, dict):
        validate_provider_config(provider)
