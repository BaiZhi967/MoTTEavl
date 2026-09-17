"""Run manifest 中 provider 配置的校验与构造（API 创建预检与 Worker 执行共用）。

kind 的分发统一走 registry：validate_provider_config / build_provider 按
注册的 AdapterSpec 派发；HTTP 适配器共用 build_case_provider 的构造流程
（凭据解析链、传输参数、registry 里的 auth 风格与版本头）。
"""
from __future__ import annotations

from typing import Any

from .anthropic_messages import (
    ANTHROPIC_VERSION,
    AnthropicMessagesProvider,
    validate_config as _validate_anthropic,
)
from .capabilities import UnsupportedParameterError
from .credentials import resolve_api_key
from .openai_compatible import (
    CaseDrivenProvider,
    OpenAICompatibleProvider,
    validate_config as _validate_openai_compatible,
)
from .openai_responses import OpenAIResponsesProvider, validate_config as _validate_responses
from .pricing import parse_price_table
from .registry import AdapterSpec, adapter_for, register, registered_kinds, smoke_kinds
from .transport import HTTPTransport

__all__ = [
    "AdapterSpec",
    "UnsupportedParameterError",
    "build_case_provider",
    "build_provider",
    "registered_kinds",
    "smoke_kinds",
    "validate_connection_config",
    "validate_provider_config",
]


def _validate_replay(config: dict[str, Any]) -> None:
    fixture = config.get("fixture")
    if fixture is not None and not isinstance(fixture, dict):
        raise ValueError("replay provider fixture must be an object")


def _build_replay(config: dict[str, Any], manifest: dict[str, Any]):
    # lazy import：provider-runtime 不在模块级依赖 motte_sdk（sdk 也不依赖本包，方向安全）
    from motte_sdk.replay_run import ReplayProvider

    return ReplayProvider(config.get("fixture") or {})


def _build_http(config: dict[str, Any], manifest: dict[str, Any]):
    manifest = manifest or {}
    return build_case_provider(
        config,
        manifest.get("cases") or {},
        tools=manifest.get("tools"),
    )


register(AdapterSpec(
    kind="openai_compatible",
    validate=_validate_openai_compatible,
    build=_build_http,
    default_key_env="OPENAI_API_KEY",
    smoke_supported=True,
    connection_required_fields=("base_url",),
    provider_cls=OpenAICompatibleProvider,
))
register(AdapterSpec(
    kind="anthropic_messages",
    validate=_validate_anthropic,
    build=_build_http,
    default_key_env="ANTHROPIC_API_KEY",
    smoke_supported=True,
    connection_required_fields=("base_url",),
    provider_cls=AnthropicMessagesProvider,
    transport_kwargs={"auth": "x-api-key", "default_headers": {"anthropic-version": ANTHROPIC_VERSION}},
))
register(AdapterSpec(
    kind="openai_responses",
    validate=_validate_responses,
    build=_build_http,
    default_key_env="OPENAI_API_KEY",
    smoke_supported=True,
    connection_required_fields=("base_url",),
    provider_cls=OpenAIResponsesProvider,
))
register(AdapterSpec(
    kind="replay",
    validate=_validate_replay,
    build=_build_replay,
))


def provider_class_for(kind: str) -> type | None:
    """HTTP 适配器的 provider 类；replay 等非 HTTP kind 返回 None。"""
    return adapter_for(kind).provider_cls


def validate_provider_config(config: dict[str, Any]) -> None:
    """strict 预检：不合法的配置在任何付费调用之前失败；按注册适配器分发。"""
    kind = config.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ValueError("provider config requires kind")
    adapter_for(kind).validate(config)


def build_provider(config: dict[str, Any], manifest: dict[str, Any] | None = None):
    """按 kind 构造带 .invoke 的 provider 对象；构造失败不产生任何网络调用。"""
    validate_provider_config(config)
    return adapter_for(config["kind"]).build(config, manifest or {})


_POSITIVE_NUMBER_FIELDS = ("timeout", "backoff_initial_ms", "backoff_max_ms")
_NON_NEGATIVE_INT_FIELDS = ("max_retries",)


def validate_connection_config(config: dict[str, Any]) -> None:
    """ProviderConnection 资源级校验：kind 已注册、连接必填字段与传输参数类型。

    model / parameters / price_table 属于 ModelProfile，不在连接上强制。
    """
    kind = config.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ValueError("provider connection requires kind")
    spec = adapter_for(kind)
    missing = [field_name for field_name in spec.connection_required_fields if not config.get(field_name)]
    if missing:
        raise ValueError(f"provider connection for {kind} requires: {', '.join(missing)}")
    for field_name in _POSITIVE_NUMBER_FIELDS:
        value = config.get(field_name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"provider connection field {field_name} must be a positive number")
    for field_name in _NON_NEGATIVE_INT_FIELDS:
        value = config.get(field_name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"provider connection field {field_name} must be a non-negative integer")
    for field_name in ("credentials", "api_key_env"):
        value = config.get(field_name)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"provider connection field {field_name} must be a name string, not a secret")


def build_case_provider(
    config: dict[str, Any],
    cases: dict[str, dict[str, Any]] | None = None,
    *,
    tools: list[dict[str, Any]] | None = None,
    api_key: str | None = None,
) -> CaseDrivenProvider:
    """按 kind 构造 HTTP case provider；凭据链与传输参数来自 registry 的 AdapterSpec。"""
    validate_provider_config(config)
    kind = config["kind"]
    provider_cls = provider_class_for(kind)
    if provider_cls is None:
        raise ValueError(f"provider kind is not an HTTP adapter: {kind!r}")
    spec = adapter_for(kind)
    if api_key is None:
        # 密钥解析链：显式传参 > 凭据文件 profile（credentials 字段，缺省回退资源 name）> 环境变量
        api_key = resolve_api_key(
            config.get("credentials") or config.get("name"),
            env_name=config.get("api_key_env") or spec.default_key_env,
        )
    transport = HTTPTransport(
        config["base_url"],
        api_key,
        timeout=config.get("timeout", 30.0),
        max_retries=config.get("max_retries", 2),
        backoff_initial=float(config.get("backoff_initial_ms", 500)) / 1000,
        backoff_max=float(config.get("backoff_max_ms", 30000)) / 1000,
        **spec.transport_kwargs,
    )
    provider = provider_cls(
        transport,
        config["model"],
        parameters=config.get("parameters"),
        price_table=parse_price_table(config.get("price_table")),
        max_output_tokens=config.get("max_output_tokens"),
        reasoning=config.get("reasoning"),
        reasoning_level=config.get("reasoning_level"),
    )
    return CaseDrivenProvider(provider, cases or {}, tools=tools)
