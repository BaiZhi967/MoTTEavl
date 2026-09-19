"""显式内置 adapter 注册表。

只接受代码内显式注册的 adapter 工厂；不经在线安装用户插件，也不从任意
Python 配置动态加载。Runner 不存在时给出可列举的拒绝原因（M2-A01）。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .protocol import BenchmarkRuntimeError

AdapterFactory = Callable[[], Any]

_FACTORIES: dict[str, AdapterFactory] = {}


def register_adapter(
    adapter_id: str, factory: AdapterFactory, *, replace: bool = False,
) -> None:
    if not adapter_id or not callable(factory):
        raise ValueError("adapter registration requires an id and a callable factory")
    if adapter_id in _FACTORIES and not replace:
        raise BenchmarkRuntimeError(
            "ADAPTER_DUPLICATE", f"adapter already registered: {adapter_id}",
        )
    _FACTORIES[adapter_id] = factory


def unregister_adapter(adapter_id: str) -> None:
    _FACTORIES.pop(adapter_id, None)


def adapter_for(adapter_id: str) -> Any:
    try:
        factory = _FACTORIES[adapter_id]
    except KeyError:
        known = ", ".join(sorted(_FACTORIES)) or "(none)"
        raise BenchmarkRuntimeError(
            "ADAPTER_UNKNOWN",
            f"external benchmark adapter is not registered: {adapter_id} (known: {known})",
        ) from None
    return factory()


def registered_adapter_ids() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))
