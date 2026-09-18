"""适配器注册表：provider kind 的校验、构造、密钥回退与冒烟能力统一登记。

新增一种协议适配器时只需 register 一个 AdapterSpec；API 创建预检、
Worker 分发和 CLI live-smoke 都从这里取分发信息，不再各自维护 kind 列表。
注册发生在 motte_provider.config 导入时（内置适配器随包注册）。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AdapterSpec:
    """一种 provider 协议的完整描述。

    validate: strict 预检（构造级），失败必须发生在任何付费调用之前；
    build: (config, manifest) → 带 .invoke 的 provider 对象，构造失败无网络副作用；
    default_key_env: 凭据文件与环境变量都未指定时的回退变量名；
    smoke_supported: 是否可被 CLI live-smoke 显式调用（产生真实费用）；
    connection_required_fields: ProviderConnection 资源级的必填字段（如 base_url）；
    provider_cls / transport_kwargs: HTTP 适配器的构造入口与传输层协议差异。
    implementation_version: 必须显式固定的内置实现版本；默认值仅兼容旧扩展。
    """

    kind: str
    validate: Callable[[dict[str, Any]], None]
    build: Callable[[dict[str, Any], dict[str, Any]], Any]
    implementation_version: str = "unversioned"
    default_key_env: str | None = None
    smoke_supported: bool = False
    connection_required_fields: tuple[str, ...] = ()
    # HTTP 适配器：provider 类（smoke/通用构造用）；None 表示非 HTTP kind（replay）
    provider_cls: type | None = None
    # 传输层协议差异（auth 风格 / 版本头等），构造 HTTPTransport 时展开
    transport_kwargs: dict[str, Any] = field(default_factory=dict)


_SPECS: dict[str, AdapterSpec] = {}


def register(spec: AdapterSpec) -> AdapterSpec:
    if not isinstance(spec.implementation_version, str) or not spec.implementation_version.strip():
        raise ValueError("adapter implementation_version must be a non-empty string")
    if spec.kind in _SPECS:
        raise ValueError(f"adapter already registered: {spec.kind}")
    _SPECS[spec.kind] = spec
    return spec


def unregister(kind: str) -> None:
    """仅供测试隔离使用。"""
    _SPECS.pop(kind, None)


def adapter_for(kind: str) -> AdapterSpec:
    try:
        return _SPECS[kind]
    except KeyError:
        supported = ", ".join(sorted(_SPECS)) or "(none)"
        raise ValueError(f"unsupported provider kind: {kind!r} (supported: {supported})") from None


def registered_kinds() -> tuple[str, ...]:
    return tuple(sorted(_SPECS))


def smoke_kinds() -> tuple[str, ...]:
    return tuple(sorted(spec.kind for spec in _SPECS.values() if spec.smoke_supported))
