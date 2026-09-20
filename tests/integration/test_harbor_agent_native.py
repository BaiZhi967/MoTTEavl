"""review R10 的离线原生校验：固定 Harbor 真的接受平台生成的 Agent 配置。

分工：``tests/benchmarks/test_harbor_agent_mapping.py`` 断言平台侧映射与判定；
这里把同一份冻结配置交给**真实 Harbor 0.23.0 的原生模型**（``JobConfig`` +
Agent 自己的 options 模型）验证——``kwargs.version`` / ``model_name`` /
``override_*`` 到底是不是这个版本接受的字段，不能靠文档猜。

校验走生产的 ``HarborJobAdapter`` + ``ProcessJobAdapter`` 启动路径，用
``--validate-config`` 只校验不执行：零模型调用、零任务启动、零容器。

需要 ``MOTTE_HARBOR_RUNNER_PYTHON`` 指向固定 Harbor 环境
（``scripts/runner/install-harbor``）；未配置时跳过，且 skip 不作为通过证据。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from motte_benchmark.harbor.adapter import ADAPTER_ID, HarborJobAdapter
from motte_contracts.external_job import ExternalJobSpec
from motte_sdk import terminalbench as tb

TASKS_ROOT = Path(__file__).resolve().parents[1] / "fixtures/benchmarks/harbor/tasks"
#: 校验用的合成凭据值：不是任何真实凭据，仅用于确认引用能被 Runner 环境解析。
SYNTHETIC_CREDENTIAL_VALUE = "validation-only-placeholder"


def _runner_python() -> Path | None:
    value = os.environ.get("MOTTE_HARBOR_RUNNER_PYTHON")
    if not value:
        return None
    path = Path(value)
    return path if path.is_file() else None


def _record(tmp_path: Path) -> dict:
    from motte_benchmark.harbor.tasks import prepare_task_manifest

    manifest = prepare_task_manifest(
        TASKS_ROOT, source_id="motte-harbor-fixtures", dataset_revision="fixtures-2026-09-20",
    )
    manifest.pop("prepared_at", None)
    return {
        "manifest": manifest, "manifest_hash": manifest["manifest_hash"],
        "dataset_revision": "fixtures-2026-09-20", "task_root": str(TASKS_ROOT),
        "source_kind": "local",
    }


def _spec(inputs: dict, profile: dict, work_root: Path) -> ExternalJobSpec:
    external = inputs["manifest"]["external_benchmark"]
    return ExternalJobSpec(
        run_id="run-native", adapter_id=ADAPTER_ID, adapter_version="1",
        runner_version="harbor-0.23.0", execution_config_hash=inputs["manifest_hash"],
        dataset_revision="fixtures-2026-09-20",
        selected_case_ids=list(inputs["case_ids"]), profile=profile,
        work_root=str(work_root), environment_digest=external["environment_digest"],
        limits=external["limits"], runner_config=external["runner_config"],
    )


def _validate(
    tmp_path: Path, profile: dict, *, extra_env: dict[str, str] | None = None,
) -> tuple[int, dict | None, Path]:
    """用生产适配器启动固定 Runner 的"只校验"模式，返回 (exit_code, 摘要, 工作目录)。

    ``PYTHONPATH`` 指向**仓库源码**（而不是 Runner 环境里的拷贝）：校验的必须是
    当前提交的桥接代码与固定 Harbor 的组合，而不是上一次部署的快照。
    """
    runner = _runner_python()
    if runner is None:
        pytest.skip("real Harbor runner required (MOTTE_HARBOR_RUNNER_PYTHON)")
    inputs = tb.build_run_inputs(
        record=_record(tmp_path), run_id="run-native", job_id="job-native", profile=profile,
    )
    repo_root = Path(__file__).resolve().parents[2]
    env = {
        "PYTHONPATH": os.pathsep.join([
            str(repo_root / "packages/benchmark-runtime"),
            str(repo_root / "packages/contracts"),
        ]),
        **(extra_env or {}),
    }
    adapter = HarborJobAdapter(
        argv=[str(runner.parent / "harbor-entry"), "--validate-config"],
        data_root=str(TASKS_ROOT), extra_env=env,
    )
    handle = adapter.prepare(_spec(inputs, profile, tmp_path / "jobs"))
    started = adapter.start(_spec(inputs, profile, tmp_path / "jobs"), handle)
    deadline = time.monotonic() + 120
    current = started
    while current.status.value == "active" and time.monotonic() < deadline:
        time.sleep(0.2)
        current = adapter.poll(current)
    exit_code = int((current.owned_resources or {}).get("exit_code") or 0)
    work_dir = Path(current.work_dir)
    summary_path = work_dir / "harbor" / "config-validation.json"
    summary = json.loads(summary_path.read_text("utf-8")) if summary_path.is_file() else None
    return (exit_code, summary, work_dir)


def test_real_claude_code_profile_is_accepted_by_pinned_harbor(tmp_path: Path) -> None:
    """真实 Agent 配置（模型 + 钉住 CLI 版本 + 期限覆盖）被固定 Harbor 接受。"""
    profile = tb.terminal_bench_profile(
        agent_id="claude-code", agent_version="2.0.30",
        model={"provider": "anthropic", "model": "claude-sonnet-4-5"},
        credentials={"provider": {"ref": "env:ANTHROPIC_API_KEY"}},
        n_trials=2,
        timeouts={"agent_sec": 120, "agent_setup_sec": 240, "verifier_sec": 60},
    )
    exit_code, summary, _work_dir = _validate(
        tmp_path, profile,
        extra_env={"ANTHROPIC_API_KEY": SYNTHETIC_CREDENTIAL_VALUE},
    )
    assert exit_code == 0, summary
    assert summary is not None
    agent = summary["agents"][0]
    assert agent["name"] == "claude-code"
    assert agent["agent_class"] == "ClaudeCode"
    assert agent["cli_version"] == "2.0.30", "CLI 版本必须是钉住的安装版本"
    assert agent["model_name"] == "anthropic/claude-sonnet-4-5"
    assert agent["options_validated"] is True
    assert summary["credentials"]["resolved"] is True
    assert summary["planned_trials"] == 4
    # 只校验不执行：没有任何 Job 目录被创建。
    assert not (Path(summary["jobs_dir"]) / "job-native").exists()


def test_real_claude_code_execution_options_are_delivered(tmp_path: Path) -> None:
    """M3-R2-11：推理档位/模型参数/工具策略/预算真的落到原生执行参数。

    用固定 Harbor 的 **真实 options 模型**校验（``ClaudeCodeOptions`` 是
    ``extra='forbid'``，字段名或取值不对就是失败），并核对"配置不同 → 下发参数
    不同"、CLI/env 形态与原生选项一致。零模型调用、零容器。
    """
    credentials = {"provider": {"ref": "env:ANTHROPIC_API_KEY"}}
    rich = tb.terminal_bench_profile(
        agent_id="claude-code", agent_version="2.0.30",
        model={
            "provider": "anthropic", "model": "claude-sonnet-4-5",
            "reasoning_level": "medium", "parameters": {"max_thinking_tokens": 2048},
        },
        credentials=credentials,
        tools={"allowed_tools": ["Read", "Bash"], "denied": ["WebFetch"]},
        limits={"max_turns": 7, "max_budget_usd": "1.25"},
    )
    exit_code, summary, _work_dir = _validate(
        tmp_path, rich, extra_env={"ANTHROPIC_API_KEY": SYNTHETIC_CREDENTIAL_VALUE},
    )
    assert exit_code == 0, summary
    assert summary is not None
    applied = summary["agents"][0]["applied_options"]
    assert applied["reasoning_effort"] == "medium"
    assert applied["max_thinking_tokens"] == 2048
    assert applied["max_turns"] == 7
    assert applied["max_budget_usd"] == "1.25"
    assert applied["allowed_tools"] == "Read,Bash"
    assert applied["disallowed_tools"] == "WebFetch"
    # CLI/env 形态：Harbor 把 Env 型选项编译成环境变量、Cli 型选项编译成标志。
    assert summary["agents"][0]["applied_env"] == {"MAX_THINKING_TOKENS": "2048"}
    cli = summary["agents"][0]["applied_cli"]
    assert any(part.startswith("--effort medium") for part in cli)
    assert any(part.startswith("--max-turns 7") for part in cli)
    assert any(part.startswith("--max-budget-usd 1.25") for part in cli)
    assert any(part.startswith("--allowedTools Read,Bash") for part in cli)

    # 配置不同 → 下发参数不同（不能"接受但用默认值"）。
    plain = tb.terminal_bench_profile(
        agent_id="claude-code", agent_version="2.0.30",
        model={"provider": "anthropic", "model": "claude-sonnet-4-5"},
        credentials=credentials,
    )
    plain_exit, plain_summary, _dir = _validate(
        tmp_path / "plain", plain,
        extra_env={"ANTHROPIC_API_KEY": SYNTHETIC_CREDENTIAL_VALUE},
    )
    assert plain_exit == 0, plain_summary
    assert plain_summary is not None
    assert plain_summary["agents"][0]["applied_options"] != applied


def test_unsupported_execution_fields_are_refused_before_runner_start(tmp_path: Path) -> None:
    """不支持的模型/工具/预算配置在创建前拒绝：连 Runner 都不启动。"""
    from motte_benchmark.harbor.config import HarborConfigError

    profile = tb.terminal_bench_profile(
        agent_id="claude-code", agent_version="2.0.30",
        model={"provider": "anthropic", "model": "claude-sonnet-4-5"},
        credentials={"provider": {"ref": "env:ANTHROPIC_API_KEY"}},
        limits={"max_tokens": 4096},
    )
    with pytest.raises(HarborConfigError) as error:
        tb.build_run_inputs(
            record=_record(tmp_path), run_id="run-native", job_id="job-native",
            profile=profile,
        )
    assert error.value.code == "HARBOR_PROFILE_UNSUPPORTED_FIELD"
    assert "limits.max_tokens" in str(error.value)
    # 零执行：连工作目录都没有建。
    assert not (tmp_path / "jobs").exists()


def test_real_runner_records_env_boundary_and_drops_undeclared_host_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M3-R2-05：真实 Runner 落盘"compose 能看到哪些变量名"，未声明宿主秘密不在其中。

    合成秘密只放在**平台进程环境**里（不是 extra_env），因此它既不该进 Runner，
    也不该出现在 compose 能看到的名字里；声明的 Agent 凭据则必须照旧可用。
    """
    from motte_benchmark.env_boundary import (
        looks_like_host_secret,
        runner_env_observation_violations,
        runner_env_outside_boundary,
    )

    monkeypatch.setenv("MOTTE_TEST_HOST_TOKEN", "synthetic-host-token-not-real")
    monkeypatch.setenv("UNRELATED_HOST_SECRET", "synthetic-unrelated-secret")

    plain = tb.terminal_bench_profile()
    exit_code, summary, work_dir = _validate(tmp_path / "plain", plain)
    assert exit_code == 0, summary
    record = json.loads((work_dir / "harbor" / "env-boundary.json").read_text("utf-8"))
    assert record["boundary"] == "runner-to-compose"
    assert record["compose_env_inherits_os_env"] is True
    assert "MOTTE_TEST_HOST_TOKEN" not in record["runner_env_names"]
    assert "UNRELATED_HOST_SECRET" not in record["runner_env_names"]
    # 桥接身份与白名单运行时变量照旧可见（Runner 自己要用）。
    assert "PATH" in record["runner_env_names"]
    assert "MOTTE_JOB_ID" in record["bridge_env_names"]
    assert runner_env_observation_violations(record) == []
    # 启动 wrapper 自己产生的 shell 变量确实可见，但它们不含任何凭据类名字。
    outside = runner_env_outside_boundary(record)
    assert "MOTTE_TEST_HOST_TOKEN" not in outside
    assert all(not looks_like_host_secret(name) for name in outside), outside
    # 平台侧校验摘要与 Runner 记录同源（同一份观察）。
    assert summary["env_boundary"]["runner_env_names"] == record["runner_env_names"]

    # 声明的 Agent 凭据仍然下发（真实 Claude Agent 需要它）。
    agent_profile = tb.terminal_bench_profile(
        agent_id="claude-code", agent_version="2.0.30",
        model={"provider": "anthropic", "model": "claude-sonnet-4-5"},
        credentials={"provider": {"ref": "env:ANTHROPIC_API_KEY"}},
    )
    agent_exit, agent_summary, agent_dir = _validate(
        tmp_path / "agent", agent_profile,
        extra_env={"ANTHROPIC_API_KEY": SYNTHETIC_CREDENTIAL_VALUE},
    )
    assert agent_exit == 0, agent_summary
    agent_record = json.loads(
        (agent_dir / "harbor" / "env-boundary.json").read_text("utf-8"),
    )
    assert "ANTHROPIC_API_KEY" in agent_record["declared_credentials"]
    assert "ANTHROPIC_API_KEY" in agent_record["runner_env_names"]
    assert "MOTTE_TEST_HOST_TOKEN" not in agent_record["runner_env_names"]
    assert runner_env_observation_violations(agent_record) == []


def test_oracle_profile_validates_without_model_or_credentials(tmp_path: Path) -> None:
    """oracle 仍是零模型、零凭据的确定性校准路径（回归）。"""
    exit_code, summary, _work_dir = _validate(tmp_path, tb.terminal_bench_profile(n_trials=1))
    assert exit_code == 0, summary
    assert summary is not None
    agent = summary["agents"][0]
    assert agent["name"] == "oracle" and agent["cli_version"] is None
    assert agent["applied_options"] is None, "oracle 没有可下发的执行参数"
    assert summary["credentials"]["referenced"] == {}


def test_missing_credential_env_is_rejected_before_start(tmp_path: Path) -> None:
    """凭据引用解析不到环境变量时启动前失败（fail closed），且不留下虚假校验结论。"""
    profile = tb.terminal_bench_profile(
        agent_id="claude-code", agent_version="2.0.30",
        model={"provider": "anthropic", "model": "claude-sonnet-4-5"},
        credentials={"provider": {"ref": "env:ANTHROPIC_API_KEY"}},
    )
    exit_code, summary, _work_dir = _validate(tmp_path, profile)
    assert exit_code == 5, summary
    assert summary is None, "失败的运行不得留下 config-validation.json"
