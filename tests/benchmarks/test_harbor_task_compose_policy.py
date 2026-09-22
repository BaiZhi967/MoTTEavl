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
import os
from pathlib import Path

import pytest

from motte_benchmark.harbor.environment import evaluate_preflight, reason_messages
from motte_benchmark.harbor.tasks import prepare_task_manifest

PROFILE = {"agent_id": "oracle", "agent_version": "1.0.0", "environment": {"type": "docker"}}


@pytest.mark.skipif(os.name != "nt", reason="Windows drive-letter compose syntax")
def test_windows_drive_volume_source_is_a_bind_mount() -> None:
    from motte_benchmark.harbor.compose import _volume_entry

    assert _volume_entry(r"C:\Users\operator\.ssh:/root/.ssh:ro") == (
        r"C:\Users\operator\.ssh", "bind",
    )
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


def _public_preflight(root: Path, *, profile: dict | None = None) -> dict:
    """走公共 SDK 预检（API/CLI/Web 共用的那一个）：零启动、零模型调用。"""
    from motte_sdk import terminalbench as tb
    from motte_sdk.service import build_run_service

    service = build_run_service(root / "runs.db")
    record = tb.prepare_terminal_bench_dataset(
        service.store, task_root=str(root), source_id="review-r2", dataset_revision="r2-1",
    )
    return tb.preflight_terminal_bench(
        record=record, profile=profile or PROFILE, docker=HEALTHY_DOCKER,
        required_images=["ubuntu:24.04"],
    )


def _assert_refused_publicly(report: dict, codes: set[str], case: str) -> None:
    assert report["allowed"] is False, (case, report["reason_codes"])
    assert codes <= set(report["reason_codes"]), (case, report["reason_codes"])
    assert report["model_calls"] == 0 and report["task_starts"] == 0, case
    messages = reason_messages(report["reason_codes"])
    assert all("未登记" not in text for text in messages.values()), (case, messages)


def test_indirect_compose_resources_are_refused_before_any_start(tmp_path: Path) -> None:
    """M3-R2-04 反例：命名卷 driver_opts bind、顶层 secrets 文件、插值挂载。

    review 复现里这三类间接挂载都 ``allowed=True``：服务 volume 只看当前字符串
    是否"表现为 bind"，命名卷直接放行、不展开顶层定义，secrets/configs 的
    ``file:`` 源与插值后的真实路径都没有进入等价检查。这里逐个断言公共预检
    拒绝、原因码具名、且没有任务脚本或容器被启动。
    """
    cases: dict[str, tuple[str, set[str]]] = {
        # (a) 命名卷的 driver_opts 构成宿主 bind（device=/）。
        "named-volume-driver-bind": (
            "services:\n"
            "  main:\n"
            "    volumes: [hostdata:/host]\n"
            "volumes:\n"
            "  hostdata:\n"
            "    driver: local\n"
            "    driver_opts:\n"
            "      type: none\n"
            "      o: bind\n"
            "      device: /\n",
            {"TASK_HOST_PATH_EXPOSED", "TASK_COMPOSE_EXTERNAL_BIND"},
        ),
        # 同一形态但挂的是 docker.sock：必须命中 socket 专用码。
        "named-volume-driver-socket": (
            "services:\n"
            "  main:\n"
            "    volumes: [sock:/sock]\n"
            "volumes:\n"
            "  sock:\n"
            "    driver_opts:\n"
            "      type: none\n"
            "      o: bind\n"
            "      device: /var/run/docker.sock\n",
            {"TASK_DOCKER_SOCKET_EXPOSED", "TASK_COMPOSE_EXTERNAL_BIND"},
        ),
        # (b) 顶层 secrets 的文件源指向宿主敏感路径。
        "secret-file": (
            "services:\n"
            "  main:\n"
            "    secrets: [host_secret]\n"
            "secrets:\n"
            "  host_secret:\n"
            "    file: /etc/shadow\n",
            {"TASK_HOST_PATH_EXPOSED", "TASK_COMPOSE_EXTERNAL_SECRET_FILE"},
        ),
        # 同上但走 configs，且是任务目录之外的相对路径。
        "config-file-outside": (
            "services:\n"
            "  main:\n"
            "    configs: [outside]\n"
            "configs:\n"
            "  outside:\n"
            "    file: ../../etc/host.conf\n",
            {"TASK_COMPOSE_EXTERNAL_SECRET_FILE"},
        ),
        # (c) R3-01: 默认值不是有效值；未冻结插值环境时必须拒绝。
        "interpolated-mount-default": (
            "services:\n"
            "  main:\n"
            "    volumes:\n"
            "      - ${HOST_ROOT:-/}:/host\n",
            {"TASK_COMPOSE_UNRESOLVED_INTERPOLATION"},
        ),
        # 无默认值（或 $VAR）的插值无法求值：拒绝而不是当作安全。
        "interpolated-mount-unresolved": (
            "services:\n"
            "  main:\n"
            "    volumes:\n"
            "      - $HOST_DIR/data:/data\n",
            {"TASK_COMPOSE_UNRESOLVED_INTERPOLATION"},
        ),
        # 花括号形态但没有默认值：同样无法求值。
        "interpolated-braced-unresolved": (
            "services:\n"
            "  main:\n"
            "    volumes:\n"
            "      - ${HOST_DIR}:/data\n",
            {"TASK_COMPOSE_UNRESOLVED_INTERPOLATION"},
        ),
        # 插值出现在 secrets 文件源：同样不能把默认值视为有效路径。
        "interpolated-secret-file": (
            "services:\n"
            "  main:\n"
            "    secrets: [s]\n"
            "secrets:\n"
            "  s:\n"
            "    file: ${HOST_ROOT:-/}etc/shadow\n",
            {"TASK_COMPOSE_UNRESOLVED_INTERPOLATION"},
        ),
        # 未定义的命名卷引用：无法证明不暴露宿主路径。
        "undeclared-volume": (
            "services:\n"
            "  main:\n"
            "    volumes: [scratch:/scratch]\n",
            {"TASK_COMPOSE_UNRESOLVED_RESOURCE"},
        ),
        # external: true 的卷由 compose 之外创建，无法核验。
        "external-volume": (
            "services:\n"
            "  main:\n"
            "    volumes: [shared:/shared]\n"
            "volumes:\n"
            "  shared:\n"
            "    external: true\n",
            {"TASK_COMPOSE_EXTERNAL_RESOURCE"},
        ),
        # 未定义的 secret 引用：同样无法核验。
        "undeclared-secret": (
            "services:\n"
            "  main:\n"
            "    secrets: [mystery]\n",
            {"TASK_COMPOSE_UNRESOLVED_RESOURCE"},
        ),
    }
    for name, (compose, codes) in cases.items():
        root = tmp_path / name
        _write_task(root, "hello-pass", compose=compose)
        task = _facts(root)
        assert codes <= _compose_codes(task["facts"]), (name, task["facts"]["compose"])
        _assert_refused_publicly(_public_preflight(root), codes, name)


def test_declared_task_local_resources_are_accepted(tmp_path: Path) -> None:
    """任务目录内、无宿主暴露的资源声明仍然允许（不是"有资源就拒绝"）。"""
    compose = (
        "services:\n"
        "  main:\n"
        "    volumes:\n"
        "      - ./data:/data\n"
        "      - scratch:/scratch\n"
        "    secrets: [local_secret]\n"
        "volumes:\n"
        "  scratch: {}\n"
        "secrets:\n"
        "  local_secret:\n"
        "    file: ./secret.txt\n"
    )
    _write_task(
        tmp_path, "hello-pass", compose=compose, extra={"environment/data/.keep": ""},
    )
    (tmp_path / "hello-pass/environment/secret.txt").write_text("local\n", encoding="utf-8")
    task = _facts(tmp_path)
    codes = _compose_codes(task["facts"])
    assert codes == set(), codes
    report = _public_preflight(tmp_path)
    assert report["allowed"] is True, report["reason_codes"]


def test_compose_environment_passthrough_is_refused(tmp_path: Path) -> None:
    """M3-R2-05 反例：列表 ``KEY`` 与 mapping ``KEY: null`` 是宿主环境透传。

    Compose 的这两种写法没有 ``=``/没有值，语义是"把 compose 进程的宿主同名
    变量传进容器"；Runner 进程环境里就有真实 Agent 凭据，因此命中凭据名必须
    在预检阶段具名拒绝——只匹配 ``${...}`` 文本会漏掉它们。
    """
    cases: dict[str, tuple[str, set[str]]] = {
        "list-passthrough": (
            "services:\n"
            "  main:\n"
            "    environment:\n"
            "      - ANTHROPIC_API_KEY\n",
            {"TASK_COMPOSE_CREDENTIAL_PASSTHROUGH", "TASK_CONTAINER_HOST_ENV_EXPOSED"},
        ),
        "null-mapping": (
            "services:\n"
            "  main:\n"
            "    environment:\n"
            "      ANTHROPIC_API_KEY: null\n",
            {"TASK_COMPOSE_CREDENTIAL_PASSTHROUGH", "TASK_CONTAINER_HOST_ENV_EXPOSED"},
        ),
        "empty-mapping": (
            "services:\n"
            "  main:\n"
            "    environment:\n"
            "      ANTHROPIC_API_KEY: \"\"\n",
            {"TASK_COMPOSE_CREDENTIAL_PASSTHROUGH", "TASK_CONTAINER_HOST_ENV_EXPOSED"},
        ),
        "secrets-environment": (
            "services:\n"
            "  main:\n"
            "    secrets: [key_secret]\n"
            "secrets:\n"
            "  key_secret:\n"
            "    environment: MOTTE_PG_DSN\n",
            {"TASK_COMPOSE_CREDENTIAL_PASSTHROUGH", "TASK_CONTAINER_HOST_ENV_EXPOSED"},
        ),
        "platform-db-passthrough": (
            "services:\n"
            "  main:\n"
            "    environment:\n"
            "      - MOTTE_PG_DSN\n",
            {"TASK_COMPOSE_CREDENTIAL_PASSTHROUGH", "TASK_CONTAINER_HOST_ENV_EXPOSED"},
        ),
        "interpolated-value": (
            "services:\n"
            "  main:\n"
            "    environment:\n"
            "      FOO: ${AWS_SECRET_ACCESS_KEY}\n",
            {"TASK_COMPOSE_CREDENTIAL_FORWARD"},
        ),
    }
    for name, (compose, codes) in cases.items():
        root = tmp_path / name
        _write_task(root, "hello-pass", compose=compose)
        task = _facts(root)
        assert codes <= _compose_codes(task["facts"]), (name, task["facts"]["compose"])
        _assert_refused_publicly(_public_preflight(root), codes, name)


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


@pytest.mark.parametrize("source", [
    "${HOME:-./data}", "${HOME-./data}", "${HOME:+./data}",
    "${HOME+./data}", "${MISSING:-${HOME}}", "$HOME", "${HOME}",
])
def test_host_interpolation_cannot_hide_behind_safe_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    """R3-01: HOME survives Runner pruning, so a safe fallback proves nothing."""
    from motte_benchmark.env_boundary import plan_process_env

    monkeypatch.setenv("HOME", "/synthetic-runner-home")
    child_env, _ = plan_process_env({"HOME": "/synthetic-runner-home"})
    assert child_env["HOME"] == "/synthetic-runner-home"
    _write_task(tmp_path, "hello-pass", compose=(
        f'services:\n  main:\n    volumes: ["{source}:/host"]\n'
    ))
    _assert_refused_publicly(
        _public_preflight(tmp_path), {"TASK_COMPOSE_UNRESOLVED_INTERPOLATION"}, source,
    )


@pytest.mark.parametrize("content", [
    "COPY=${ANTHROPIC_API_KEY}\n", 'COPY="${ANTHROPIC_API_KEY}"\n',
    "COPY=${ANTHROPIC_API_KEY:-absent}\n", "COPY=$ANTHROPIC_API_KEY\n",
    "COPY=${OTHER:-${ANTHROPIC_API_KEY}}\n",
])
def test_env_file_cannot_forward_declared_agent_credentials(tmp_path: Path, content: str) -> None:
    """R3-02: a task must not copy the credential intentionally available to its Agent."""
    from motte_benchmark.env_boundary import plan_process_env

    child_env, _ = plan_process_env(
        {"ANTHROPIC_API_KEY": "synthetic-agent-secret"}, declared=("ANTHROPIC_API_KEY",),
    )
    assert child_env["ANTHROPIC_API_KEY"] == "synthetic-agent-secret"
    _write_task(tmp_path, "hello-pass", compose=(
        "services:\n  main:\n    env_file: ./task.env\n"
    ), extra={"environment/task.env": content})
    report = _public_preflight(tmp_path)
    _assert_refused_publicly(report, {"TASK_COMPOSE_CREDENTIAL_FORWARD"}, content)
    assert "synthetic-agent-secret" not in json.dumps(report)


@pytest.mark.parametrize("content", [
    "COPY=${UNKNOWN:+value}\n", "COPY=$$ESCAPED\n", "INHERIT\n",
    "COPY='multiline\nvalue'\n", "COPY=value\\\ncontinued\n", "COPY=value\x00\n",
])
def test_env_file_unsupported_dynamic_forms_fail_closed(tmp_path: Path, content: str) -> None:
    _write_task(tmp_path, "hello-pass", compose=(
        "services:\n  main:\n    env_file: ./task.env\n"
    ), extra={"environment/task.env": content})
    report = _public_preflight(tmp_path)
    assert report["allowed"] is False, report
    assert report["task_starts"] == 0


def test_literal_task_env_file_remains_allowed(tmp_path: Path) -> None:
    _write_task(tmp_path, "hello-pass", compose=(
        "services:\n  main:\n    env_file:\n      - path: ./task.env\n        required: true\n"
    ), extra={"environment/task.env": "# local values\nGREETING=hello world\nEMPTY=\nCOUNT=3\n"})
    report = _public_preflight(tmp_path)
    assert report["allowed"] is True, report["reason_codes"]


def test_missing_env_file_is_not_considered_safe(tmp_path: Path) -> None:
    _write_task(tmp_path, "hello-pass", compose=(
        "services:\n  main:\n    env_file: ./missing.env\n"
    ))
    _assert_refused_publicly(
        _public_preflight(tmp_path), {"TASK_COMPOSE_UNINSPECTABLE"}, "missing env_file",
    )


@pytest.mark.parametrize("compose", [
    'services:\n  main:\n    volumes: [{type: bind, source: "${HOME:-./data}", target: /data}]\n',
    'services:\n  main:\n    build: {context: "${HOME:-.}"}\n',
    'services:\n  main:\n    build: {context: ., dockerfile: "${HOME:-Dockerfile}"}\n',
    'services:\n  main:\n    env_file: "${HOME:-./task.env}"\n',
    'services:\n  main:\n    secrets: [s]\nsecrets:\n  s: {file: "${HOME:-./secret}"}\n',
    'services:\n  main:\n    volumes: [v:/data]\nvolumes:\n  v:\n    driver_opts: {device: "${HOME:-./data}"}\n',
])
def test_all_host_source_fields_refuse_runtime_interpolation(tmp_path: Path, compose: str) -> None:
    _write_task(tmp_path, "hello-pass", compose=compose)
    _assert_refused_publicly(
        _public_preflight(tmp_path), {"TASK_COMPOSE_UNRESOLVED_INTERPOLATION"}, compose,
    )


@pytest.mark.parametrize("file_kind", ["symlink", "directory", "invalid-utf8", "oversized"])
def test_env_file_requires_bounded_regular_utf8_file(tmp_path: Path, file_kind: str) -> None:
    from motte_benchmark.harbor.compose import inspect_task_compose

    task_dir = _write_task(tmp_path, "hello-pass", compose=(
        "services:\n  main:\n    env_file: ./task.env\n"
    ))
    env_file = task_dir / "environment/task.env"
    if file_kind == "symlink":
        outside = tmp_path / "outside.env"
        outside.write_text("COPY=synthetic-secret\n", encoding="utf-8")
        env_file.symlink_to(outside)
    elif file_kind == "directory":
        env_file.mkdir()
    else:
        env_file.write_bytes(b"\xff" if file_kind == "invalid-utf8" else b"a" * (1024**2 + 1))
    policy = inspect_task_compose(tmp_path, "hello-pass")
    assert "TASK_COMPOSE_UNINSPECTABLE" in {item["code"] for item in policy["violations"]}
    assert "synthetic-secret" not in json.dumps(policy)


@pytest.mark.parametrize("entry", [
    "{}", "[null]", "{path: task.env, required: unknown}",
    "{path: task.env, format: unknown}", "{path: task.env, unknown: true}",
])
def test_env_file_unknown_forms_are_not_silently_ignored(tmp_path: Path, entry: str) -> None:
    _write_task(tmp_path, "hello-pass", compose=(
        f"services:\n  main:\n    env_file: {entry}\n"
    ), extra={"environment/task.env": "GREETING=hello\n"})
    _assert_refused_publicly(
        _public_preflight(tmp_path), {"TASK_COMPOSE_UNINSPECTABLE"}, entry,
    )
