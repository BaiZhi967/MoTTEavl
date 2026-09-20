"""Harbor 环境预检（M3-T05，需求第 5 节）。

预检**只读且零执行**：不启动任务、不构建不可信镜像、不调用模型。它把
"能不能安全地跑这个任务集"变成一组具名的 ``reason_codes``，任何一项不
满足都 fail closed（``allowed=False``），而不是"试试看"。

权限边界（M3-G14，逐项有断言）：

- 可信 Runner（Harbor 进程）持有容器/环境管理能力；
- **任务容器**不获得 Docker socket、宿主凭据目录、平台数据库文件；
- 网络策略与 Verifier 可见性无法验证时标 ``unknown`` 或按 Profile 拒绝。

安全检查真的改变了上游环境/测试可见性时，必须带上不同的
``profile_fingerprint``（``platform_custom_profile=True``），不能继续冒充
官方同口径（需求第 6 节末）。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from motte_benchmark.harbor.tasks import HarborTaskError
from motte_contracts.trial import canonical_hash

#: 任务容器绝对不允许出现的宿主路径（挂载或环境变量都不行）。
FORBIDDEN_HOST_PATHS: tuple[str, ...] = (
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/etc/shadow",
    "/etc/sudoers",
    "~/.ssh",
    "~/.aws",
    "~/.config/gcloud",
    "~/.docker",
    "~/.kube",
)
#: 任务容器不允许继承的宿主环境变量（平台内部变量，按前缀/精确名匹配）。
FORBIDDEN_ENV_NAMES: tuple[str, ...] = (
    "MOTTE_PG_DSN", "DATABASE_URL", "ARTIFACT_ROOT", "MOTTE_DB_PATH",
    "MOTTE_DB_PATH", "MOTTE_RUNNER_PYTHON", "MOTTE_LAUNCH_TOKEN",
)
FORBIDDEN_ENV_PREFIXES: tuple[str, ...] = (
    "AWS_", "OPENAI_", "ANTHROPIC_", "AZURE_", "GOOGLE_", "HF_", "MOTTE_",
)
#: 凭据类变量名的通用特征（未知厂商也要被识别，不能只查已知前缀）。
CREDENTIAL_NAME_MARKERS: tuple[str, ...] = (
    "TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "APIKEY",
    "CREDENTIAL", "ACCESS_KEY", "PRIVATE_KEY", "_KEY", "LICENSE_KEY",
)


def looks_like_credential(name: str) -> bool:
    """变量名是否像凭据（大小写不敏感，覆盖未登记的厂商）。"""
    upper = name.upper()
    return upper in FORBIDDEN_ENV_NAMES or upper.startswith(
        FORBIDDEN_ENV_PREFIXES,
    ) or any(marker in upper for marker in CREDENTIAL_NAME_MARKERS)
#: 预检支持的环境类型（与 config.py 的 allowlist 一致）。
SUPPORTED_ENVIRONMENT_TYPES: tuple[str, ...] = ("docker",)
#: 需要的宿主平台（Harbor 的 docker 环境按 linux 容器运行）。
SUPPORTED_DOCKER_PLATFORMS: tuple[str, ...] = ("linux/amd64", "linux/arm64")


def _mount_source(spec: str) -> str:
    """挂载规格的宿主侧路径（``host:container[:opts]``，无冒号即宿主路径）。"""
    head = spec.split(":", 1)[0]
    return os.path.normpath(os.path.expanduser(head))


def _forbidden_mounts(mounts: Sequence[str]) -> list[str]:
    """命中禁用宿主路径的挂载（同时比较字面路径与解析后的别名）。"""
    banned = [os.path.normpath(os.path.expanduser(item)) for item in FORBIDDEN_HOST_PATHS]
    hits: list[str] = []
    for mount in mounts:
        source = _mount_source(mount)
        candidates = {source}
        try:
            candidates.add(str(Path(source).resolve()))
        except OSError:  # pragma: no cover - 宿主路径异常
            pass
        if any(
            candidate == entry or candidate.startswith(entry + os.sep)
            for candidate in candidates
            for entry in banned
        ):
            hits.append(mount)
    return hits


def _probe_docker(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Runner 上报的 Docker 探测结果（预检不在 API 进程里执行 docker）。"""
    payload = payload or {}
    return {
        "available": bool(payload.get("available")),
        "server_version": payload.get("server_version"),
        "platform": payload.get("platform"),
        "disk_free_bytes": payload.get("disk_free_bytes"),
        "disk_required_bytes": payload.get("disk_required_bytes"),
        "images": list(payload.get("images") or []),
        "error": payload.get("error"),
    }


def evaluate_preflight(
    *,
    profile: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
    docker: Mapping[str, Any] | None,
    required_images: Sequence[str] = (),
    runner_env_vars: Mapping[str, str] | None = None,
    task_env_vars: Mapping[str, str] | None = None,
    task_mounts: Sequence[str] = (),
    network_policy: str | None = None,
    verifier_visibility: str | None = None,
    agent_dependencies: Sequence[str] = (),
    installed_agent_dependencies: Sequence[str] = (),
    harbor_version: str = "unknown",
) -> dict[str, Any]:
    """只读预检：返回 ``allowed``、具名 ``reason_codes`` 与 ``checks`` 明细。

    任何 reason code 都表示"不能开始"；调用方必须据此拒绝创建 Run，并且
    此时 ``model_calls`` 与 ``task_starts`` 都为 0（由调用点保证并有断言）。
    """
    reasons: list[str] = []
    checks: dict[str, Any] = {}

    environment_type = str(
        (profile.get("environment") or {}).get("type", "docker")
        if isinstance(profile.get("environment"), Mapping) else "docker",
    )
    if environment_type not in SUPPORTED_ENVIRONMENT_TYPES:
        reasons.append("ENVIRONMENT_TYPE_UNSUPPORTED")
    checks["environment_type"] = environment_type

    agent_id = profile.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        reasons.append("AGENT_NOT_PINNED")
    checks["agent_id"] = agent_id
    if not profile.get("agent_version"):
        reasons.append("AGENT_VERSION_NOT_PINNED")

    probe = _probe_docker(docker)
    checks["docker"] = probe
    if not probe["available"]:
        reasons.append("DOCKER_UNAVAILABLE")
    if probe["available"] and probe.get("platform") not in (None, *SUPPORTED_DOCKER_PLATFORMS):
        reasons.append("DOCKER_PLATFORM_UNSUPPORTED")
    free = probe.get("disk_free_bytes")
    required = probe.get("disk_required_bytes")
    if isinstance(free, int) and isinstance(required, int) and free < required:
        reasons.append("DOCKER_DISK_LOW")
    available_images = {str(image) for image in probe.get("images") or []}
    missing_images = sorted(
        image for image in required_images if image not in available_images
    )
    if missing_images:
        reasons.append("DOCKER_IMAGE_MISSING")
        checks["missing_images"] = missing_images

    missing_deps = sorted(set(agent_dependencies) - set(installed_agent_dependencies))
    if missing_deps:
        reasons.append("AGENT_DEPENDENCY_MISSING")
        checks["missing_agent_dependencies"] = missing_deps

    # 凭据边界：Runner 可以持有引用，任务容器与配置 dump 都不允许出现。
    runner_env = dict(runner_env_vars or {})
    leaked = sorted(name for name in runner_env if looks_like_credential(name))
    if leaked:
        reasons.append("RUNNER_ENV_CREDENTIALS_INLINE")
        checks["inline_credentials"] = leaked
    task_env = dict(task_env_vars or {})
    inherited = sorted(name for name in task_env if looks_like_credential(name))
    if inherited:
        reasons.append("TASK_CONTAINER_HOST_ENV_EXPOSED")
        checks["task_env_exposed"] = inherited

    normalized_mounts = [str(item) for item in task_mounts]
    forbidden = sorted(_forbidden_mounts(normalized_mounts))
    if forbidden:
        reasons.append("TASK_HOST_PATH_EXPOSED")
        checks["forbidden_mounts"] = forbidden
    docker_socket = [
        mount for mount in normalized_mounts
        if "docker.sock" in mount
    ]
    if docker_socket:
        reasons.append("TASK_DOCKER_SOCKET_EXPOSED")
        checks["docker_socket_mounts"] = docker_socket

    if network_policy is None:
        reasons.append("NETWORK_POLICY_UNVERIFIED")
    elif network_policy not in ("none", "restricted", "allowed"):
        reasons.append("NETWORK_POLICY_INVALID")
    checks["network_policy"] = network_policy

    if verifier_visibility is None:
        checks["verifier_visibility"] = "unknown"
        reasons.append("VERIFIER_VISIBILITY_UNVERIFIED")
    else:
        checks["verifier_visibility"] = verifier_visibility

    if not tasks:
        reasons.append("NO_TASKS_SELECTED")
    for task in tasks:
        facts = (task.get("facts") or {}) if isinstance(task.get("facts"), Mapping) else {}
        if facts.get("has_tests") is False:
            reasons.append("TASK_WITHOUT_VERIFIER")
            break
    checks["task_count"] = len(tasks)

    profile_fingerprint = canonical_hash({
        "harbor_version": harbor_version,
        "environment_type": environment_type,
        "network_policy": network_policy,
        "verifier_visibility": verifier_visibility,
        "task_mounts": sorted(normalized_mounts),
        "platform_custom_profile": bool(task_mounts) or network_policy not in (None, "allowed"),
    })
    custom_profile = bool(
        task_mounts
        or (network_policy not in (None, "allowed"))
        or verifier_visibility not in (None, "upstream")
    )
    return {
        "allowed": not reasons,
        "reason_codes": sorted(set(reasons)),
        "checks": checks,
        "profile_fingerprint": profile_fingerprint,
        "platform_custom_profile": custom_profile,
        "model_calls": 0,
        "task_starts": 0,
        "harbor_version": harbor_version,
    }


def docker_probe_from_env() -> dict[str, Any]:
    """Runner 侧探测（只在 Runner 进程调用；API 进程不执行 docker）。"""
    if shutil.which("docker") is None:
        return {"available": False, "error": "docker CLI not found on the runner host"}
    return {
        "available": True,
        "server_version": os.environ.get("MOTTE_DOCKER_SERVER_VERSION"),
        "platform": os.environ.get("MOTTE_DOCKER_PLATFORM"),
        "disk_free_bytes": _int_env("MOTTE_DOCKER_DISK_FREE_BYTES"),
        "disk_required_bytes": _int_env("MOTTE_DOCKER_DISK_REQUIRED_BYTES"),
        "images": [
            image for image in (os.environ.get("MOTTE_DOCKER_IMAGES") or "").split(",")
            if image
        ],
    }


def _int_env(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def load_preflight_report(path: str | Path) -> dict[str, Any]:
    """读取 Runner 上报的探测 JSON（预检报告由 Runner 生成）。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HarborTaskError(
            "PREFLIGHT_REPORT_UNREADABLE", f"cannot read preflight report {path}: {error}",
        ) from error
    if not isinstance(payload, Mapping):
        raise HarborTaskError(
            "PREFLIGHT_REPORT_INVALID", "preflight report must be a JSON object",
        )
    return dict(payload)


def blocking_reasons(report: Mapping[str, Any]) -> list[str]:
    """报告里所有阻断原因（供 API/CLI/Web 显示可操作错误）。"""
    return [str(code) for code in report.get("reason_codes") or []]


def reason_messages(codes: Iterable[str]) -> dict[str, str]:
    """原因码 → 操作员可执行的中文说明（前端不猜错误含义）。"""
    catalogue = {
        "DOCKER_UNAVAILABLE": "Runner 上无法访问 Docker daemon，无法创建任务环境。",
        "DOCKER_PLATFORM_UNSUPPORTED": "宿主平台不受支持（需要 linux/amd64 或 linux/arm64）。",
        "DOCKER_IMAGE_MISSING": "所需镜像在 Runner 上不存在（请先拉取或预热）。",
        "DOCKER_DISK_LOW": "Runner 磁盘余量低于任务所需。",
        "ENVIRONMENT_TYPE_UNSUPPORTED": "所选环境类型未在本平台验证。",
        "AGENT_NOT_PINNED": "未指定 Agent，或 Agent 未固定到具体 ID。",
        "AGENT_VERSION_NOT_PINNED": "Agent 未固定到具体版本。",
        "AGENT_DEPENDENCY_MISSING": "Agent 依赖在 Runner 环境中缺失。",
        "RUNNER_ENV_CREDENTIALS_INLINE": "Runner 环境里出现了明文凭据，必须改为引用。",
        "TASK_CONTAINER_HOST_ENV_EXPOSED": "任务容器会继承宿主凭据环境变量。",
        "TASK_HOST_PATH_EXPOSED": "任务容器挂载了宿主敏感路径。",
        "TASK_DOCKER_SOCKET_EXPOSED": "任务容器挂载了 Docker socket，必须拒绝执行。",
        "NETWORK_POLICY_UNVERIFIED": "无法验证网络策略，按 fail-closed 拒绝。",
        "NETWORK_POLICY_INVALID": "网络策略取值非法。",
        "VERIFIER_VISIBILITY_UNVERIFIED": "无法确认 Verifier/gold 可见性边界。",
        "TASK_WITHOUT_VERIFIER": "所选任务没有 Verifier（tests/），无法产生评分证据。",
        "NO_TASKS_SELECTED": "没有选择任何任务。",
    }
    return {code: catalogue.get(code, f"未登记的原因码：{code}") for code in codes}
