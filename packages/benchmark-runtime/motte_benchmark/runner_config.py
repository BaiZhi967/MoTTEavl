"""受控 Runner adapter 配置加载（review R01）。

API/Worker/CLI 从**同一受控配置**加载外部 Job adapter：配置文件显式列出
每个 benchmark 的 argv / extra_env（或离线 module），不经在线安装用户插件，
也不从任意 Python 配置动态加载。没有配置且默认固定环境 wrapper 不存在时，
adapter 保持未注册（RUNNER_NOT_CONNECTED），创建入口在入队前拒绝——
绝不静默落到不可执行的默认 argv。

配置文件位置：``MOTTE_RUNNER_CONFIG`` 环境变量指向的 JSON，缺省
``var/runner/adapters.json``（存在才加载）。形如::

    {"adapters": [
        {"benchmark": "ceval", "argv": ["/opt/motte-runner/bin/opencompass-entry"]},
        {"benchmark": "cmmlu", "argv": ["/opt/motte-runner/bin/opencompass-entry"]}
    ]}

argv 支持 ``{work_dir}`` / ``{job_id}`` / ``{run_id}`` / ``{launch_token}``
占位符（由 ProcessJobAdapter 逐项替换）；extra_env 只放非秘密值——凭据一律
按引用写进 runner 配置（``{"ref": "env:NAME"}``）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .protocol import BenchmarkRuntimeError

DEFAULT_WRAPPER = "/opt/motte-runner/bin/opencompass-entry"
#: Harbor（Terminal-Bench）Runner 的固定 wrapper，独立于 OpenCompass。
HARBOR_DEFAULT_WRAPPER = "/opt/motte-runner/bin/harbor-entry"
HARBOR_BENCHMARKS = ("terminal-bench",)
_KNOWN_BENCHMARKS = ("ceval", "cmmlu", *HARBOR_BENCHMARKS)
_LOADED = False


def runner_config_path() -> Path:
    configured = os.environ.get("MOTTE_RUNNER_CONFIG", "").strip()
    if configured:
        return Path(configured)
    return Path("var/runner/adapters.json")


def load_runner_adapter_config(path: str | Path) -> dict[str, Any]:
    """读取并校验受控 adapter 配置；非法条目整体拒绝（不部分生效）。"""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("adapters"), list):
        raise BenchmarkRuntimeError(
            "RUNNER_CONFIG_INVALID",
            "runner config must be an object with an adapters list: " + str(path),
        )
    seen: set[str] = set()
    for entry in document["adapters"]:
        if not isinstance(entry, dict):
            raise BenchmarkRuntimeError(
                "RUNNER_CONFIG_INVALID", "each adapter entry must be an object",
            )
        benchmark = str(entry.get("benchmark") or "")
        if benchmark not in _KNOWN_BENCHMARKS:
            raise BenchmarkRuntimeError(
                "RUNNER_CONFIG_INVALID",
                f"unknown benchmark in runner config: {benchmark!r} "
                f"(known: {', '.join(_KNOWN_BENCHMARKS)})",
            )
        if benchmark in seen:
            raise BenchmarkRuntimeError(
                "RUNNER_CONFIG_INVALID",
                "duplicate adapter entry for benchmark: " + benchmark,
            )
        seen.add(benchmark)
        argv = entry.get("argv")
        module = entry.get("module")
        if (isinstance(argv, list) and argv and module is None):
            if not all(isinstance(item, str) for item in argv):
                raise BenchmarkRuntimeError(
                    "RUNNER_CONFIG_INVALID",
                    "adapter argv must be a list of strings: " + benchmark,
                )
        elif isinstance(module, str) and module and argv is None:
            pass  # 离线/测试形态：python -m <module>
        else:
            raise BenchmarkRuntimeError(
                "RUNNER_CONFIG_INVALID",
                "adapter entry requires exactly one of argv or module: " + benchmark,
            )
        extra_env = entry.get("extra_env") or {}
        if not isinstance(extra_env, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in extra_env.items()
        ):
            raise BenchmarkRuntimeError(
                "RUNNER_CONFIG_INVALID",
                "adapter extra_env must be a string map: " + benchmark,
            )
    return document


def _register_harbor(entry: dict[str, Any]) -> None:
    """注册 Harbor 适配器（独立 runner 包装器，不套用 OpenCompass 形态）。"""
    from . import registry
    from .harbor.adapter import ADAPTER_ID, HarborJobAdapter

    argv = entry.get("argv")
    extra_env = {str(k): str(v) for k, v in (entry.get("extra_env") or {}).items()}
    limits = entry.get("default_limits") or None
    data_root = entry.get("data_root")
    if argv is None and entry.get("module") is None:
        argv = [HARBOR_DEFAULT_WRAPPER]
    if argv is not None:
        registry.register_adapter(
            ADAPTER_ID,
            lambda a=argv, e=extra_env, lim=limits, root=data_root: HarborJobAdapter(
                argv=[str(item) for item in a], extra_env=e, default_limits=lim,
                data_root=root,
            ),
            replace=True,
        )
        return
    module = entry.get("module")
    registry.register_adapter(
        ADAPTER_ID,
        lambda m=module, e=extra_env, lim=limits, root=data_root: HarborJobAdapter(
            module=m, extra_env=e, default_limits=lim, data_root=root,
        ),
        replace=True,
    )


def _register_from_entry(entry: dict[str, Any]) -> None:
    from . import registry
    from .opencompass.adapter import OpenCompassJobAdapter

    benchmark = str(entry.get("benchmark"))
    if benchmark in HARBOR_BENCHMARKS:
        _register_harbor(entry)
        return
    argv = entry.get("argv")
    extra_env = {str(k): str(v) for k, v in (entry.get("extra_env") or {}).items()}
    limits = entry.get("default_limits") or None
    if argv is None:
        module = entry.get("module")
        registry.register_adapter(
            f"{benchmark}-opencompass",
            lambda b=benchmark, m=module, e=extra_env, lim=limits: OpenCompassJobAdapter(  # noqa: E501
                dataset=b, module=m, extra_env=e, default_limits=lim,
            ),
            replace=True,
        )
        return
    registry.register_adapter(
        f"{benchmark}-opencompass",
        lambda b=benchmark, a=argv, e=extra_env, lim=limits: OpenCompassJobAdapter(
            dataset=b, argv=[str(item) for item in a], extra_env=e, default_limits=lim,
        ),
        replace=True,
    )


def ensure_builtin_adapters(*, force: bool = False) -> list[str]:
    """按受控配置注册 ceval/cmmlu adapter（幂等）；返回已注册 adapter id。

    优先级：``MOTTE_RUNNER_CONFIG`` 指定的配置文件 > 默认固定环境 wrapper
    （``/opt/motte-runner/bin/opencompass-entry`` 实际存在时）。两者都不满足
    时不注册——目录状态如实呈现 RUNNER_NOT_CONNECTED。
    """
    global _LOADED
    if _LOADED and not force:
        return list(_registered_snapshot())
    path = runner_config_path()
    if path.is_file():
        config = load_runner_adapter_config(path)
        for entry in config["adapters"]:
            _register_from_entry(entry)
    else:
        if Path(DEFAULT_WRAPPER).exists():
            # 固定环境 wrapper 已部署：两个 OpenCompass benchmark 共用同一入口。
            for benchmark in ("ceval", "cmmlu"):
                _register_from_entry({"benchmark": benchmark, "argv": [DEFAULT_WRAPPER]})
        if Path(HARBOR_DEFAULT_WRAPPER).exists():
            # Harbor Runner 独立部署：Harbor 自己管理任务环境生命周期。
            _register_harbor({"benchmark": "terminal-bench"})
    _LOADED = True
    return list(_registered_snapshot())


def _registered_snapshot() -> tuple[str, ...]:
    from . import registry

    return registry.registered_adapter_ids()


__all__ = [
    "DEFAULT_WRAPPER",
    "HARBOR_BENCHMARKS",
    "HARBOR_DEFAULT_WRAPPER",
    "ensure_builtin_adapters",
    "load_runner_adapter_config",
    "runner_config_path",
]
