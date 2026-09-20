"""M3 review R2-05 的真实层：合成秘密在真实 compose 语义下如何下发。

review 要求"用合成秘密测试实际下发环境，不能只匹配 ``${...}`` 文本"。这里用
真实 ``docker compose config`` 解析（不启动容器、不调用模型）证明两件事：

1. 列表透传 ``- KEY``、mapping 空值 ``KEY: null`` 与 ``${VAR}`` 插值在真实
   compose 语义下确实会把 compose 进程环境的同名变量解析进服务定义；
2. 平台边界产物（``env_boundary.plan_process_env``）裁剪后的环境里，宿主里
   未声明的合成秘密不再参与解析（默认值生效），也就是说"名字能被任务猜到"
   不再等于"值能进容器"。

docker 不可用时跳过（skip 不是通过证据）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from motte_benchmark.env_boundary import plan_process_env

#: 合成秘密（形状像凭据、不是任何真实值）。
SYNTHETIC_HOST_SECRET = "synthetic-compose-leak-probe-value"
PROBE_NAME = "MOTTE_TEST_HOST_TOKEN"
#: 非凭据探针：用来观察"未声明变量 → 默认值生效"这条边界。
NON_SECRET_PROBE = "HOST_PROBE_VALUE"

TASK_COMPOSE = """\
services:
  main:
    image: alpine:3.20
    environment:
      - {probe}
  sidecar:
    image: alpine:3.20
    environment:
      {probe}: null
      derived: "${{{probe}:-absent}}"
      non_secret: "${{{non_secret}:-absent}}"
"""


def _compose_command() -> list[str]:
    if shutil.which("docker") is None:
        pytest.skip("real Docker CLI required")
    probe = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True, check=False,
    )
    if probe.returncode != 0:
        pytest.skip("docker compose plugin required")
    return ["docker", "compose"]


def _compose_config(tmp_path: Path, env: dict[str, str]) -> dict:
    """真实 ``docker compose config``：解析插值与透传，不启动任何容器。"""
    directory = tmp_path / "compose"
    directory.mkdir(parents=True, exist_ok=True)
    compose_path = directory / "docker-compose.yaml"
    compose_path.write_text(
        TASK_COMPOSE.format(probe=PROBE_NAME, non_secret=NON_SECRET_PROBE),
        encoding="utf-8",
    )
    result = subprocess.run(
        [*_compose_command(), "--project-directory", str(directory),
         "-f", str(compose_path), "config", "--format", "json"],
        capture_output=True, text=True, check=False, env=env, cwd=str(directory),
    )
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert isinstance(document, dict)
    return document


def _service_env(config: dict, service: str) -> dict[str, str]:
    return dict((config["services"][service].get("environment") or {}))


def test_real_compose_resolves_passthrough_forms_from_process_env(tmp_path: Path) -> None:
    """真实 compose：列表透传与 null mapping 都取宿主同名变量（复现语义）。"""
    full_env = {**dict(os.environ), PROBE_NAME: SYNTHETIC_HOST_SECRET}
    config = _compose_config(tmp_path, full_env)
    main_env = _service_env(config, "main")
    sidecar_env = _service_env(config, "sidecar")
    # 列表项 ``- KEY`` 与 ``KEY: null`` 都解析成了宿主里的合成秘密。
    assert main_env.get(PROBE_NAME) == SYNTHETIC_HOST_SECRET, main_env
    assert sidecar_env.get(PROBE_NAME) == SYNTHETIC_HOST_SECRET, sidecar_env
    # 插值取的是宿主值（不是默认值）。
    assert sidecar_env.get("derived") == SYNTHETIC_HOST_SECRET, sidecar_env
    assert SYNTHETIC_HOST_SECRET in json.dumps(config)


def test_pruned_boundary_env_keeps_host_secret_out_of_compose(tmp_path: Path) -> None:
    """"值能进容器"由宿主环境决定：裁剪后的环境里未声明秘密无法参与解析。"""
    host_env = {
        **dict(os.environ),
        PROBE_NAME: SYNTHETIC_HOST_SECRET,
        NON_SECRET_PROBE: "host-value",
    }
    child_env, record = plan_process_env(
        host_env,
        declared=(),
        injected={"MOTTE_FAKE_MODE": "ok"},
        identity={"MOTTE_JOB_ID": "job-1", "MOTTE_RUN_ID": "run-1"},
    )
    assert PROBE_NAME in record["dropped_credentials"]
    assert PROBE_NAME not in child_env
    # 非凭据探针同样不下发（它在白名单之外）。
    assert NON_SECRET_PROBE not in child_env

    config = _compose_config(tmp_path, child_env)
    main_env = _service_env(config, "main")
    sidecar_env = _service_env(config, "sidecar")
    # 值不再出现：列表透传解析成空、插值回落到默认值。
    assert SYNTHETIC_HOST_SECRET not in json.dumps(config)
    assert main_env.get(PROBE_NAME) in (None, "")
    assert sidecar_env.get(PROBE_NAME) in (None, "")
    assert sidecar_env.get("derived") == "absent"
    assert sidecar_env.get("non_secret") == "absent"
