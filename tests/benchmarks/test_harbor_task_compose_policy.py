"""M3 review R02：任务自带 Docker Compose 必须过策略检查（M3-T05/A13）。

复现要求（review 原文）：在合法任务里放 ``environment/docker-compose.yaml``
（含 Docker socket 与 ``/:/host`` 挂载），重新 prepare 后公共 preflight 仍
``allowed=True``。固定版 Harbor 0.23.0 会把这份 compose 当 overlay 加进
``docker compose -f ...`` 命令行，任务因此可以注入额外服务、宿主 bind
mount、privileged、host network。

本文件覆盖：volumes、privileged、host network/namespace、危险 capability、
device、security_opt、外部 bind/build context/env_file、凭据插值、
include/extends、不可解析的 compose，以及"有 compose 文件但没有检查记录"。
全部场景必须在**零任务启动、零模型调用**时被拒绝。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from motte_benchmark.harbor.environment import evaluate_preflight, reason_messages
from motte_benchmark.harbor.tasks import prepare_task_manifest

PROFILE = {"agent_id": "oracle", "agent_version": "1.0.0", "environment": {"type": "docker"}}
HEALTHY_DOCKER = {
    "available": True,
    "server_version": "27.4.0",
    "platform": "linux/arm64",
    "disk_free_bytes": 10 * 1024**3,
    "disk_required_bytes": 2 * 1024**3,
    "images": ["ubuntu:24.04"],
}
#: 复现用的恶意 compose：docker socket + 宿主根挂载 + privileged + host network。
MALICIOUS_COMPOSE = """\
services:
  main:
    privileged: true
    network_mode: host
    pid: host
    cap_add:
      - SYS_ADMIN
    devices:
      - /dev/kvm
    security_opt:
      - seccomp=unconfined
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /:/host
"""


def _write_task(root: Path, name: str, *, compose: str | None = None,
                extra: dict[str, str] | None = None) -> Path:
    task_dir = root / name
    (task_dir / "environment").mkdir(parents=True, exist_ok=True)
    (task_dir / "tests").mkdir(parents=True, exist_ok=True)
    (task_dir / "task.toml").write_text(
        f'schema_version = "1.4"\n\n[metadata]\nname = "{name}"\nlinkense = "n/a"\n',
        encoding="utf-8",
    )
    (task_dir / "instruction.md").write_text("do the thing\n", encoding="utf-8")
    (task_dir / "environment" / "Dockerfile").write_text("FROM ubuntu:24.04\n", encoding="utf-8")
    (task_dir / "tests" / "test.sh").write_text("#!/bin/bash\necho 1\n", encoding="utf-8")
    if compose is not None:
        (task_dir / "environment" / "docker-compose.yaml").write_text(compose, encoding="utf-8")
    for rel, content in (extra or {}).items():
        target = task_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return task_dir


def _facts(task_root: Path, name: str = "hello-pass") -> dict[str, object]:
    manifest = prepare_task_manifest(
        task_root, source_id="review-r02", dataset_revision="r02-1",
    )
    for task in manifest["tasks"]:
        if task["normalized_relative_path"] == name:
            return {"task_key": task["task_key"], "facts": manifest["task_facts"][task["task_key"]]}
    raise AssertionError(f"task {name} not prepared: {manifest['tasks']}")


def _preflight(task: dict[str, object]) -> dict[str, object]:
    return evaluate_preflight(
        profile=PROFILE, tasks=[task], docker=HEALTHY_DOCKER,
        required_images=["ubuntu:24.04"], network_policy="allowed",
        verifier_visibility="upstream", harbor_version="0.23.0",
    )


def _compose_codes(facts: dict[str, object]) -> set[str]:
    compose = facts["compose"]
    assert isinstance(compose, dict), facts
    return {
        str(item["code"]) if isinstance(item, dict) else str(item)
        for item in compose["violations"]
    }


def test_malicious_task_compose_is_refused_before_any_start(tmp_path: Path) -> None:
    """复现反例：任务自带 compose 含 socket 与 ``/:/host``，preflight 必须拒绝。"""
    _write_task(tmp_path, "hello-pass", compose=MALICIOUS_COMPOSE)
    task = _facts(tmp_path)
    facts = task["facts"]
    assert isinstance(facts, dict)
    assert facts.get("has_compose_file") is True, "冻结清单里必须记录任务自带 compose"

    codes = _compose_codes(facts)
    expected = {
        "TASK_DOCKER_SOCKET_EXPOSED",
        "TASK_HOST_PATH_EXPOSED",
        "TASK_COMPOSE_PRIVILEGED",
        "TASK_COMPOSE_HOST_NETWORK",
        "TASK_COMPOSE_HOST_NAMESPACE",
        "TASK_COMPOSE_DANGEROUS_CAPABILITY",
        "TASK_COMPOSE_DEVICE_EXPOSED",
        "TASK_COMPOSE_SECURITY_OPT_DISABLED",
    }
    assert expected <= codes, sorted(codes)

    report = _preflight(task)
    assert report["allowed"] is False, "恶意 compose 不能被放行"
    assert len(report["reason_codes"]) >= len(expected)
    assert report["model_calls"] == 0 and report["task_starts"] == 0
    assert report["checks"]["compose_violations"], "拒绝理由必须列在 checks 里"
    messages = reason_messages(report["reason_codes"])
    assert all("未登记" not in text for text in messages.values()), messages
    # compose 检查也进入 fingerprint 输入：检查内容变化必须改变指纹。
    assert facts["compose"]["policy_hash"].startswith("sha256:")


def test_task_compose_violation_matrix(tmp_path: Path) -> None:
    """逐项策略：外部 bind/build/env_file、凭据插值、include/extends 都具名拒绝。"""
    cases = {
        "external-bind": (
            "services:\n  main:\n    volumes:\n      - ../secrets:/secrets\n",
            "TASK_COMPOSE_EXTERNAL_BIND",
        ),
        "absolute-bind": (
            "services:\n  main:\n    volumes:\n      - /etc:/etc\n",
            "TASK_COMPOSE_EXTERNAL_BIND",
        ),
        "external-build": (
            "services:\n  main:\n    build:\n      context: /opt/elsewhere\n",
            "TASK_COMPOSE_EXTERNAL_BUILD_CONTEXT",
        ),
        "external-env-file": (
            "services:\n  main:\n    env_file:\n      - /etc/host.env\n",
            "TASK_COMPOSE_EXTERNAL_ENV_FILE",
        ),
        "credential-forward": (
            "services:\n  main:\n    environment:\n      OPENAI_KEY: ${SOME_API_TOKEN}\n",
            "TASK_COMPOSE_CREDENTIAL_FORWARD",
        ),
        "include": (
            "include:\n  - /opt/other/compose.yaml\nservices:\n  main: {}\n",
            "TASK_COMPOSE_INCLUDE_UNSUPPORTED",
        ),
        "extends": (
            "services:\n  main:\n    extends:\n      file: ../base.yaml\n      service: main\n",
            "TASK_COMPOSE_INCLUDE_UNSUPPORTED",
        ),
        "ipc-host": (
            "services:\n  main:\n    ipc: host\n",
            "TASK_COMPOSE_HOST_NAMESPACE",
        ),
    }
    for name, (compose, code) in cases.items():
        root = tmp_path / name
        _write_task(root, "hello-pass", compose=compose)
        task = _facts(root)
        assert code in _compose_codes(task["facts"]), (name, task["facts"]["compose"])
        report = _preflight(task)
        assert report["allowed"] is False, name
        # 每个新原因码都必须有可执行的中文说明（前端不猜错误含义）。
        messages = reason_messages(report["reason_codes"])
        assert all("未登记" not in text for text in messages.values()), messages


def test_uninspectable_compose_fails_closed(tmp_path: Path) -> None:
    """读不到/解析不了 compose 绝不能放行（fail closed）。"""
    root = tmp_path / "broken"
    _write_task(root, "hello-pass", compose="services: [unclosed\n")
    task = _facts(root)
    assert "TASK_COMPOSE_UNINSPECTABLE" in _compose_codes(task["facts"])
    assert _preflight(task)["allowed"] is False

    # 有 compose 文件但 facts 里没有检查记录：同样按不可检查拒绝。
    facts = dict(task["facts"])
    facts.pop("compose")
    report = _preflight({"task_key": task["task_key"], "facts": facts})
    assert report["allowed"] is False
    assert "TASK_COMPOSE_UNINSPECTABLE" in report["reason_codes"]


def test_public_preflight_refuses_the_malicious_task(tmp_path: Path) -> None:
    """复现路径本身：公共 preflight（SDK 入口）必须拒绝且零执行。

    review 的复现是"重新 prepare 后公共 preflight 仍 allowed=True"——这里走
    ``preflight_terminal_bench``（API/CLI/Web 共用的那一个），确认原因码与
    可执行中文说明都到位。
    """
    from motte_sdk import terminalbench as tb
    from motte_sdk.service import build_run_service

    _write_task(tmp_path, "hello-pass", compose=MALICIOUS_COMPOSE)
    service = build_run_service(tmp_path / "runs.db")
    record = tb.prepare_terminal_bench_dataset(
        service.store, task_root=str(tmp_path), source_id="review-r02",
        dataset_revision="r02-1",
    )
    report = tb.preflight_terminal_bench(
        record=record, profile=PROFILE, docker=HEALTHY_DOCKER,
        required_images=["ubuntu:24.04"],
    )
    assert report["allowed"] is False, report["reason_codes"]
    assert "TASK_COMPOSE_PRIVILEGED" in report["reason_codes"]
    assert "TASK_DOCKER_SOCKET_EXPOSED" in report["reason_codes"]
    assert report["model_calls"] == 0 and report["task_starts"] == 0
    assert report["messages"]["TASK_COMPOSE_PRIVILEGED"]
    assert report["platform_custom_profile"] is True, "任务自带 compose 改变了环境口径"
    assert report["checks"]["tasks_with_compose"] == 1


def test_benign_compose_is_accepted_with_evidence(tmp_path: Path) -> None:
    """任务自己的额外服务（无宿主暴露）允许通过，但必须留下可审计记录。"""
    benign = (
        "services:\n"
        "  main:\n"
        "    environment:\n"
        "      GREETING: hello\n"
        "  sidecar:\n"
        "    image: redis:7-alpine\n"
    )
    _write_task(tmp_path, "hello-pass", compose=benign)
    task = _facts(tmp_path)
    facts = task["facts"]
    assert _compose_codes(facts) == set()
    assert facts["compose"]["services"] == ["main", "sidecar"]
    assert facts["compose"]["files"], "检查过的 compose 文件必须登记"
    report = _preflight(task)
    assert report["allowed"] is True, report["reason_codes"]
    assert report["checks"]["compose_violations"] == []


def test_task_without_compose_records_no_compose_fact(tmp_path: Path) -> None:
    """没有自带 compose 的任务不产生 compose facts，也不新增原因码。"""
    _write_task(tmp_path, "hello-pass")
    task = _facts(tmp_path)
    assert task["facts"].get("has_compose_file") is False
    assert "compose" not in task["facts"]
    assert _preflight(task)["allowed"] is True


def test_compose_policy_facts_are_json_serializable(tmp_path: Path) -> None:
    """facts 进入 manifest（JSON），不能包含不可序列化对象。"""
    _write_task(tmp_path, "hello-pass", compose=MALICIOUS_COMPOSE)
    manifest = prepare_task_manifest(tmp_path, source_id="review-r02", dataset_revision="r02-1")
    text = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    assert "TASK_COMPOSE_PRIVILEGED" in text
