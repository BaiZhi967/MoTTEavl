"""M3 review R2-05：Runner 进程环境 → compose/任务容器的显式边界。

复现（review 原文）：``environment: [ANTHROPIC_API_KEY]``（列表）与
``environment: {ANTHROPIC_API_KEY: null}``（mapping 空值）都能通过公共预检。
Compose 的这两种语义是"把 compose 进程的宿主同名变量传进容器"，而 Harbor
0.23.0 的 ``_run_docker_compose_command`` 用 ``_compose_env_vars(include_os_env=True)``
把 **Runner 的整个 os.environ** 交给 ``docker compose``；平台的
``ProcessJobAdapter`` 又把宿主环境整体继承给 Runner。于是宿主里任何"名字能被
任务 compose 猜到"的变量都可能进入任务容器。

这里验证最小化边界（不是文本匹配，而是真实子进程的环境）：

1. Runner 子进程只继承：显式白名单（PATH/HOME/DOCKER_* 等运行必需项）、冻结
   配置里声明的 ``env:NAME`` 凭据引用、平台显式注入（``extra_env`` + 每次启动
   换发的桥接身份）；宿主里其它变量——包括合成秘密——一律不下发；
2. 边界记录（``env_boundary``）逐项给出"名字 + 来源"，可被平台冻结核验；
3. 出现无法解释的凭据类变量时**启动前**拒绝（fail closed），不产生执行副作用。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from motte_benchmark.process import ProcessJobAdapter
from motte_benchmark.protocol import BenchmarkRuntimeError
from motte_contracts.external_job import ExternalJobSpec, ExternalJobStatus

#: 合成值：形状像凭据、不是任何真实凭据（review 要求不能只匹配 ${...} 文本）。
SYNTHETIC_HOST_TOKEN = "synthetic-host-token-value-not-a-real-credential"
SYNTHETIC_HOME_SECRET = "synthetic-home-secret-value"
#: 合成平台 DSN：宿主确实带着它，但任务/Runner 都不该看到。
SYNTHETIC_PLATFORM_DSN = "synthetic-dsn-placeholder-not-a-real-database"
#: 环境变量名的合成凭据（Runner 侧必须看到的 Agent 凭据）。
DECLARED_AGENT_KEY = "MOTTE_AGENT_KEY"
DECLARED_AGENT_KEY_VALUE = "synthetic-agent-key-value"

DUMP_ENV = "import json, os; print(json.dumps(dict(os.environ)))"


def _spec(work_root: Path, *, profile: dict | None = None,
          runner_config: dict | None = None) -> ExternalJobSpec:
    return ExternalJobSpec(
        run_id="run-env-boundary",
        adapter_id="process-env-boundary",
        adapter_version="1",
        runner_version="env-boundary-1",
        execution_config_hash="sha256:" + "3" * 64,
        dataset_revision="rev-env",
        selected_case_ids=["case-1"],
        profile=profile if profile is not None else {
            "benchmark_id": "terminal-bench",
            "benchmark_version": "1",
            "agent_id": "claude-code",
            "agent_version": "2.0.30",
            "credentials": {"provider": {"ref": f"env:{DECLARED_AGENT_KEY}"}},
        },
        work_root=str(work_root),
        environment_digest="sha256:" + "4" * 64,
        limits={},
        runner_config=runner_config or {},
    )


def _dump_child_env(
    tmp_path: Path, adapter: ProcessJobAdapter, spec: ExternalJobSpec,
) -> dict[str, str]:
    """真实启动子进程并读回它的 os.environ（固定 argv、不经 shell）。"""
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    handle = adapter.prepare(spec)
    started = adapter.start(spec, handle)
    deadline = time.monotonic() + 30
    current = started
    while current.status == ExternalJobStatus.active and time.monotonic() < deadline:
        time.sleep(0.05)
        current = adapter.poll(current)
    assert current.status != ExternalJobStatus.active, "子进程必须已退出"
    tail = adapter.output_tail()
    lines = tail["stdout"].decode("utf-8").strip().splitlines()
    payload = json.loads(lines[-1])
    assert isinstance(payload, dict)
    return {str(key): str(value) for key, value in payload.items()}


def _env_dumping_adapter(**kwargs) -> ProcessJobAdapter:
    return ProcessJobAdapter(argv=[sys.executable, "-c", DUMP_ENV], **kwargs)


def test_runner_child_env_is_minimal_and_keeps_declared_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """宿主秘密不下发；白名单与非秘密注入项仍然可用；声明的 Agent 凭据照旧。"""
    monkeypatch.setenv("MOTTE_HOST_TOKEN", SYNTHETIC_HOST_TOKEN)  # 合成秘密：不在白名单
    monkeypatch.setenv("MOTTE_PG_DSN", SYNTHETIC_PLATFORM_DSN)
    monkeypatch.setenv("UNRELATED_HOME_SECRET", SYNTHETIC_HOME_SECRET)
    monkeypatch.setenv(DECLARED_AGENT_KEY, DECLARED_AGENT_KEY_VALUE)
    adapter = _env_dumping_adapter(extra_env={"MOTTE_FAKE_MODE": "ok"})
    spec = _spec(tmp_path / "work")
    child_env = _dump_child_env(tmp_path, adapter, spec)

    # 1) 合成秘密一律不下发（值层面，不是名字匹配）。
    for name in ("MOTTE_HOST_TOKEN", "MOTTE_PG_DSN", "UNRELATED_HOME_SECRET"):
        assert name not in child_env, name
    assert SYNTHETIC_HOST_TOKEN not in child_env.values()
    assert SYNTHETIC_PLATFORM_DSN not in child_env.values()
    assert SYNTHETIC_HOME_SECRET not in child_env.values()

    # 2) Runner 自己运行需要的白名单变量仍然在（否则 Runner 根本起不来）。
    for name in ("PATH", "HOME"):
        assert child_env.get(name), name
    # 3) 显式注入（平台受控配置）仍然下发。
    assert child_env["MOTTE_FAKE_MODE"] == "ok"
    # 4) 声明的凭据引用照旧可用（真实 Claude Agent 必需）。
    assert child_env[DECLARED_AGENT_KEY] == DECLARED_AGENT_KEY_VALUE
    # 5) 桥接身份随每次启动注入（中断/核验需要）。
    assert child_env["MOTTE_JOB_ID"] and child_env["MOTTE_RUN_ID"] == spec.run_id
    assert child_env["MOTTE_LAUNCH_TOKEN"]

    record = adapter.start_calls[-1]["env_boundary"]
    assert record["schema"].startswith("motte-runner-env-boundary")
    assert record["boundary"] == "platform-to-runner"
    assert record["violations"] == []
    assert "MOTTE_HOST_TOKEN" in record["dropped_credentials"]
    assert "MOTTE_PG_DSN" in record["dropped_credentials"]
    assert record["dropped_other_count"] >= 1, "其余宿主变量同样不下发"
    assert DECLARED_AGENT_KEY in record["declared_credentials"]
    assert record["forwarded"][DECLARED_AGENT_KEY] == "declared-credential"
    assert record["forwarded"]["PATH"] == "allowlist"
    assert record["forwarded"]["MOTTE_FAKE_MODE"] == "injected"
    # 记录只有变量名与来源，没有值。
    serialized = json.dumps(record)
    assert SYNTHETIC_HOST_TOKEN not in serialized
    assert DECLARED_AGENT_KEY_VALUE not in serialized


def test_credentials_declared_in_runner_config_are_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """凭据引用写在 runner 配置里（CEval/OpenCompass 形态）时同样按引用下发。"""
    monkeypatch.setenv("MOTTE_RUNNER_CONFIG_KEY", DECLARED_AGENT_KEY_VALUE)
    adapter = _env_dumping_adapter()
    spec = _spec(
        tmp_path / "work",
        profile={"benchmark_id": "ceval", "benchmark_version": "1"},
        runner_config={
            "credentials": {"api_key": {"ref": "env:MOTTE_RUNNER_CONFIG_KEY"}},
        },
    )
    child_env = _dump_child_env(tmp_path, adapter, spec)
    assert child_env["MOTTE_RUNNER_CONFIG_KEY"] == DECLARED_AGENT_KEY_VALUE
    record = adapter.start_calls[-1]["env_boundary"]
    assert "MOTTE_RUNNER_CONFIG_KEY" in record["declared_credentials"]
    assert record["violations"] == []


def test_undeclared_credential_injection_refuses_to_start(tmp_path: Path) -> None:
    """平台显式注入的凭据类变量也必须能追溯到声明：否则启动前拒绝（fail closed）。"""
    adapter = _env_dumping_adapter(
        # 注入了一个凭据类变量，但冻结配置里没有任何 env:NAME 引用指向它。
        extra_env={"SOME_UNTRACKED_TOKEN": SYNTHETIC_HOST_TOKEN},
    )
    spec = _spec(
        tmp_path / "work",
        profile={"benchmark_id": "terminal-bench", "benchmark_version": "1"},
    )
    handle = adapter.prepare(spec)
    with pytest.raises(BenchmarkRuntimeError) as error:
        adapter.start(spec, handle)
    assert error.value.code == "RUNNER_ENV_BOUNDARY_VIOLATION"
    assert "SOME_UNTRACKED_TOKEN" in str(error.value)
    assert adapter.start_calls == [], "拒绝必须发生在产生执行副作用之前"
    assert adapter.spawned_processes() == []
