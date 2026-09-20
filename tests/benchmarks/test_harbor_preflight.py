"""M3-T05：环境预检与安全边界（M3-A05/A13，需求第 5 节）。

预检必须 fail closed：Docker 不可达、平台不符、镜像缺失、磁盘不足、
Agent 依赖缺失、任务容器可见宿主凭据/socket 都拒绝，并且**零模型调用、
零任务启动**。安全检查改变上游可见性时产生不同的 profile fingerprint。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from motte_benchmark.harbor.environment import (
    blocking_reasons,
    evaluate_preflight,
    load_preflight_report,
    reason_messages,
)
from motte_benchmark.harbor.tasks import HarborTaskError

PROFILE = {
    "agent_id": "oracle",
    "agent_version": "1.0.0",
    "environment": {"type": "docker"},
}
TASKS = [
    {"task_key": "sha256:t0", "normalized_relative_path": "tasks/hello-pass",
     "facts": {"has_tests": True}},
]
HEALTHY_DOCKER = {
    "available": True,
    "server_version": "27.4.0",
    "platform": "linux/arm64",
    "disk_free_bytes": 10 * 1024**3,
    "disk_required_bytes": 2 * 1024**3,
    "images": ["ubuntu:24.04"],
}
#: 需要凭据类环境变量名来验证边界；值本身是无关紧要的占位串。
CREDENTIAL_ENV_NAME = "SOME_PROVIDER_TOKEN"


def _preflight(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "profile": PROFILE,
        "tasks": TASKS,
        "docker": HEALTHY_DOCKER,
        "required_images": ["ubuntu:24.04"],
        # 上游默认口径：Harbor 自己决定网络，Verifier 用上游可见性。
        "network_policy": "allowed",
        "verifier_visibility": "upstream",
        "harbor_version": "0.23.0",
    }
    kwargs.update(overrides)
    return evaluate_preflight(**kwargs)  # type: ignore[arg-type]


def test_preflight_resource_and_visibility_fail_closed() -> None:
    """健康环境通过；任何一项缺失都拒绝且零执行。"""
    healthy = _preflight()
    assert healthy["allowed"] is True
    assert healthy["reason_codes"] == []
    assert healthy["model_calls"] == 0
    assert healthy["task_starts"] == 0
    assert healthy["platform_custom_profile"] is False

    cases = {
        "DOCKER_UNAVAILABLE": {"docker": {"available": False, "error": "cannot connect"}},
        "DOCKER_PLATFORM_UNSUPPORTED": {
            "docker": {**HEALTHY_DOCKER, "platform": "windows/amd64"},
        },
        "DOCKER_DISK_LOW": {
            "docker": {**HEALTHY_DOCKER, "disk_free_bytes": 1, "disk_required_bytes": 1024**3},
        },
        "DOCKER_IMAGE_MISSING": {"required_images": ["terminal-bench:2.0"]},
        "AGENT_DEPENDENCY_MISSING": {
            "agent_dependencies": ["harbor", "terminal-bench"],
            "installed_agent_dependencies": ["harbor"],
        },
        "NETWORK_POLICY_UNVERIFIED": {"network_policy": None},
        "VERIFIER_VISIBILITY_UNVERIFIED": {"verifier_visibility": None},
        "TASK_WITHOUT_VERIFIER": {
            "tasks": [{"task_key": "t", "facts": {"has_tests": False}}],
        },
        "NO_TASKS_SELECTED": {"tasks": []},
        "ENVIRONMENT_TYPE_UNSUPPORTED": {
            "profile": {**PROFILE, "environment": {"type": "daytona"}},
        },
        "AGENT_NOT_PINNED": {"profile": {"agent_id": "", "agent_version": "1.0.0"}},
        "AGENT_VERSION_NOT_PINNED": {"profile": {"agent_id": "oracle"}},
    }
    for expected, overrides in cases.items():
        report = _preflight(**overrides)
        assert report["allowed"] is False, expected
        assert expected in report["reason_codes"], (expected, report["reason_codes"])
        assert report["model_calls"] == 0 and report["task_starts"] == 0
        assert reason_messages(blocking_reasons(report))[expected]


def test_task_container_never_gets_host_secrets_or_docker_socket() -> None:
    """恶意任务/挂载：宿主凭据、socket 与平台数据库都必须阻断（M3-A13）。"""
    socket_report = _preflight(task_mounts=["/var/run/docker.sock:/var/run/docker.sock"])
    assert socket_report["allowed"] is False
    assert "TASK_DOCKER_SOCKET_EXPOSED" in socket_report["reason_codes"]
    assert "TASK_HOST_PATH_EXPOSED" in socket_report["reason_codes"]
    assert socket_report["checks"]["docker_socket_mounts"] == [
        "/var/run/docker.sock:/var/run/docker.sock",
    ]

    host_path = _preflight(task_mounts=[f"{Path.home()}/.ssh:/root/.ssh"])
    assert host_path["allowed"] is False
    assert "TASK_HOST_PATH_EXPOSED" in host_path["reason_codes"]

    env_report = _preflight(task_env_vars={"MOTTE_PG_DSN": "postgresql://user@host/db"})
    assert env_report["allowed"] is False
    assert "TASK_CONTAINER_HOST_ENV_EXPOSED" in env_report["reason_codes"]
    assert env_report["checks"]["task_env_exposed"] == ["MOTTE_PG_DSN"]

    inline = _preflight(runner_env_vars={CREDENTIAL_ENV_NAME: "placeholder-for-boundary-test"})
    assert inline["allowed"] is False
    assert "RUNNER_ENV_CREDENTIALS_INLINE" in inline["reason_codes"]


def test_custom_security_profile_changes_fingerprint() -> None:
    """安全/可见性改变必须换 fingerprint，不能冒充官方同口径。"""
    upstream = _preflight()
    hardened = _preflight(network_policy="none", verifier_visibility="platform-restricted")
    assert upstream["profile_fingerprint"] != hardened["profile_fingerprint"]
    assert hardened["platform_custom_profile"] is True
    assert upstream["platform_custom_profile"] is False

    mounted = _preflight(task_mounts=["/data:/data"])
    assert mounted["platform_custom_profile"] is True
    assert mounted["profile_fingerprint"] not in (
        upstream["profile_fingerprint"], hardened["profile_fingerprint"],
    )


def test_preflight_report_loading_is_strict(tmp_path: Path) -> None:
    """Runner 报告缺失/畸形时显式失败，不把"读不到"当成通过。"""
    report_path = tmp_path / "preflight.json"
    report_path.write_text(json.dumps({"docker": HEALTHY_DOCKER}), encoding="utf-8")
    assert load_preflight_report(report_path)["docker"]["available"] is True

    report_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(HarborTaskError) as malformed:
        load_preflight_report(report_path)
    assert malformed.value.code == "PREFLIGHT_REPORT_UNREADABLE"

    with pytest.raises(HarborTaskError) as missing:
        load_preflight_report(tmp_path / "absent.json")
    assert missing.value.code == "PREFLIGHT_REPORT_UNREADABLE"
