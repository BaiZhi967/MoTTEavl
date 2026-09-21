"""M4-T02：上游版本锁定与分层就绪（fail closed）。

兼容矩阵的唯一事实源是 ``docs/protocols/runtime-compatibility.json``。
本模块只做本地静态检查（二进制存在、--version 输出、已安装包版本、
probe 结果），不做任何模型调用、下载、登录或全局升级；任何未知
backend、版本漂移、协议不匹配都 fail closed 并给出可区分的原因。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from .install import _parse_version

# 与 docs/protocols/runtime-compatibility.json 的 manifest_version 对齐。
COMPATIBILITY_MANIFEST_VERSION = "2026-09-20.1"
_PI_PROTOCOL_VERSION = "v2"

_REQUIRED_BACKEND_FIELDS = (
    "kind", "transport", "upstream", "adapter_version", "parser_version",
    "capabilities", "config_discovery", "readiness_rules",
)


class CompatibilityError(ValueError):
    """兼容矩阵缺失、损坏或引用未知 backend；code 供 API/CLI 映射。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def default_manifest_path() -> Path:
    """从本包位置向上解析仓库内 docs/protocols/runtime-compatibility.json。"""
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "docs" / "protocols" / "runtime-compatibility.json"
        if candidate.is_file():
            return candidate
    raise CompatibilityError(
        "COMPATIBILITY_MANIFEST_MISSING",
        "runtime-compatibility.json not found relative to the motte_harness package",
    )


def load_runtime_compatibility(path: Path | str | None = None) -> dict[str, Any]:
    manifest_path = Path(path) if path is not None else default_manifest_path()
    if not manifest_path.is_file():
        raise CompatibilityError(
            "COMPATIBILITY_MANIFEST_MISSING", f"compatibility manifest not found: {manifest_path}"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CompatibilityError(
            "COMPATIBILITY_MANIFEST_INVALID", f"compatibility manifest unreadable: {error}"
        ) from error
    if not isinstance(manifest.get("backends"), dict) or not manifest["backends"]:
        raise CompatibilityError(
            "COMPATIBILITY_MANIFEST_INVALID", "compatibility manifest has no backends"
        )
    for backend_id, backend in manifest["backends"].items():
        missing = [field for field in _REQUIRED_BACKEND_FIELDS if field not in backend]
        if missing or not str(backend.get("upstream", {}).get("version") or ""):
            raise CompatibilityError(
                "COMPATIBILITY_MANIFEST_INVALID",
                f"backend {backend_id} is missing pinned fields: {missing or ['upstream.version']}",
            )
    return manifest


def _backend_entry(backend_id: str, manifest: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    candidates = [(key, value) for key, value in manifest['backends'].items()
        if key.rpartition('@')[0] == backend_id]
    if not candidates:
        raise CompatibilityError(
            "COMPATIBILITY_BACKEND_UNKNOWN",
            f"runtime backend is not in the compatibility matrix: {backend_id}",
        )
    return max(candidates, key=lambda item: int(item[0].rpartition('@')[2]))


def resolve_pinned_version(backend_id: str, path: Path | str | None = None) -> str:
    manifest = load_runtime_compatibility(path)
    _, entry = _backend_entry(backend_id, manifest)
    return str(entry["upstream"]["version"])


def _pi_installed_version(node_modules_root: Path | None) -> str | None:
    if node_modules_root is None:
        node_modules_root = (
            Path(__file__).resolve().parents[3] / "bridges" / "pi" / "node_modules"
        )
    package = (
        node_modules_root / "@mariozechner" / "pi-agent-core" / "package.json"
    )
    if not package.is_file():
        return None
    try:
        return str(json.loads(package.read_text(encoding="utf-8"))["version"])
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def readiness_for_backend(
    backend_id: str,
    *,
    manifest_path: Path | str | None = None,
    binary_checker: Callable[[str], str | None] | None = None,
    version_output: str | None = None,
    probe_result: dict[str, Any] | None = None,
    node_modules_root: Path | None = None,
) -> dict[str, Any]:
    """计算 installed / protocol_ready / execution_ready 分层状态与原因。

    只读本地状态；binary_checker 未注入时用 shutil.which 静态检查。
    execution_ready 永远不能由 --version/能力声明推出（M4-G02），只有
    probe_result 显式携带 scripted-task 证据时才为真。
    """
    import shutil

    manifest = load_runtime_compatibility(manifest_path)
    _, entry = _backend_entry(backend_id, manifest)
    pinned = str(entry["upstream"]["version"])
    kind = str(entry.get("kind") or "")
    reasons: dict[str, str] = {}

    # installed 层只回答"东西在不在"：包/二进制存在即 installed=true；
    # 版本与 pinned 的比对属于协议兼容层（M4-A01：未安装与版本不兼容
    # 必须是两个不同的原因，都在 fail-closed 路径上）。
    installed = False
    installed_version: str | None = None
    version_drift: str | None = None
    if kind == "pi-bridge":
        installed_version = _pi_installed_version(node_modules_root)
        if installed_version is None:
            reasons["installed"] = (
                "@mariozechner/pi-agent-core is not installed in bridges/pi "
                "(run pnpm --dir bridges/pi install; the platform never auto-installs)"
            )
        else:
            installed = True
            if installed_version != pinned:
                version_drift = (
                    f"pi-agent-core version drift: installed {installed_version}, "
                    f"pinned {pinned}"
                )
    else:
        binary = str(entry.get("upstream", {}).get("binary") or backend_id)
        path = (
            binary_checker(binary)
            if binary_checker is not None
            else (shutil.which(binary) or (binary if Path(binary).is_file() else None))
        )
        if path is None:
            reasons["installed"] = f"{binary} binary is not installed (no auto-install)"
        else:
            installed = True
            if version_output is None:
                # 调用方未提供 --version 输出（避免子进程时）；仅存在性不足以判定
                version_drift = f"{binary} present but --version output not provided"
            else:
                installed_version = _parse_version(version_output)
                if installed_version is None:
                    version_drift = f"{binary} --version output could not be parsed"
                elif installed_version != pinned:
                    version_drift = (
                        f"{binary} version drift: installed {installed_version}, "
                        f"pinned {pinned}"
                    )

    protocol_ready = False
    if not installed:
        reasons["protocol_ready"] = "backend is not installed"
    elif version_drift is not None:
        reasons["protocol_ready"] = version_drift
    elif kind == "pi-bridge":
        if probe_result is None:
            reasons["protocol_ready"] = "bridge probe result not provided"
        elif probe_result.get("protocol") != _PI_PROTOCOL_VERSION:
            reasons["protocol_ready"] = (
                f"bridge protocol mismatch: {probe_result.get('protocol')!r} "
                f"(expected {_PI_PROTOCOL_VERSION!r})"
            )
        else:
            protocol_ready = True
    elif probe_result is not None and probe_result.get("protocol_ok") is False:
        reasons["protocol_ready"] = str(
            probe_result.get("reason") or "protocol verification failed"
        )
    else:
        # CLI：--version 与 pinned 一致即协议就绪；执行就绪另需真实任务证据。
        protocol_ready = True

    execution_ready = False
    if not protocol_ready:
        reasons["execution_ready"] = "protocol is not ready"
    elif probe_result is not None and probe_result.get("execution_ready") is True:
        execution_ready = True
    else:
        reasons["execution_ready"] = (
            "execution readiness requires a real scripted task/cancel evidence "
            "(live or recorded integration proof), never inferred from --version"
        )

    return {
        "backend": backend_id,
        "pinned_version": pinned,
        "installed": installed,
        "installed_version": installed_version,
        "protocol_ready": protocol_ready,
        "execution_ready": execution_ready,
        "reasons": reasons,
    }


def version_from_output(output: str) -> str | None:
    """公开包装：从 --version 输出提取版本（供 probe 层复用）。"""
    return _parse_version(output)


def probe_readiness_inputs(
    backend_id: str,
    *,
    manifest_path: Path | str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """执行零成本探测，为 readiness_for_backend 提供真实输入（R19）。

    - pi-bridge：spawn 一次 bridge probe（无模型调用），取协议版本；
    - CLI backend：跑 ``binary --version``（离线、无副作用），取输出文本。

    探测失败返回 None 输入（readiness 据此给出 protocol_ready=false 与
    可区分的原因），绝不把"未提供"冒充"探测通过"。
    """
    import shutil
    import subprocess

    manifest = load_runtime_compatibility(manifest_path)
    _, entry = _backend_entry(backend_id, manifest)
    kind = str(entry.get("kind") or "")
    if kind == "pi-bridge":
        try:
            from motte_agent.pi import PiAgentRuntime

            runtime = PiAgentRuntime(timeout_seconds=timeout)
            if not runtime.available():
                return {"probe_result": None}
            probe = runtime.probe()
            return {"probe_result": {
                "protocol": probe.get("protocol"),
                "bridge_version": probe.get("version"),
                "execution_ready": False,
            }}
        except Exception:  # noqa: BLE001 - probe 失败按未提供处理
            return {"probe_result": None}
    binary = str(entry.get("upstream", {}).get("binary") or backend_id)
    resolved = shutil.which(binary) or (binary if Path(binary).is_file() else None)
    if resolved is None:
        return {"version_output": None}
    try:
        completed = subprocess.run(  # noqa: S603 - 受控固定 argv，无 shell
            [resolved, "--version"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"version_output": None}
    return {"version_output": (completed.stdout or "") + (completed.stderr or "")}


def probed_readiness(
    backend_id: str,
    *,
    manifest_path: Path | str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """readiness_for_backend + 真实探测输入（公共入口统一走这里，R19）。"""
    inputs = probe_readiness_inputs(
        backend_id, manifest_path=manifest_path, timeout=timeout,
    )
    return readiness_for_backend(backend_id, manifest_path=manifest_path, **inputs)


def backend_ids(path: Path | str | None = None) -> tuple[str, ...]:
    manifest = load_runtime_compatibility(path)
    return tuple(dict.fromkeys(key.rpartition('@')[0] for key in manifest['backends']))
