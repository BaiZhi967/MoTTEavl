"""review R10：真实 Agent 的模型/版本/凭据引用映射与预检一致性。

反例（review 原文）：allowlist 仅 ``oracle@1.0.0``，真实 Agent 一律
``HARBOR_AGENT_UNSUPPORTED``；API 校验 published model 后没有把模型配置传入
TB profile；CLI 指定 ``--model NONEXISTENT-MODEL`` 仍成功 queued；冻结
``external.profile.model={}``。同时 GET preflight 对不支持的 agent_id 返回
``ok=true``（预检放行、创建失败的双重标准）。

这里覆盖：类型化 Agent 表的映射（Harbor 原生字段）、显式版本钉住、模型与
凭据引用的必需性，以及"预检结论 == 创建结论"。
"""
from __future__ import annotations

import pytest

from motte_benchmark.harbor.config import (
    AGENT_SPECS,
    HarborConfigError,
    build_harbor_config,
    plan_trials,
    resolve_agent_profile,
)
from motte_benchmark.harbor.environment import evaluate_preflight, reason_messages
from motte_sdk.terminalbench import terminal_bench_profile

TASK_KEYS = ("task-a",)


def _tasks() -> list[dict]:
    return [{
        "task_key": "task-a", "normalized_relative_path": "tasks/task-a",
        "source_id": "src",
    }]


def _config(profile: dict) -> dict:
    plans = plan_trials(
        run_id="run-agent", tasks=_tasks(), repeats=int(profile["n_trials"]),
        agent_config_hash="sha256:a", environment_hash="sha256:e",
    )
    return build_harbor_config(
        run_id="run-agent", job_id="job-agent", work_root="jobs",
        tasks=_tasks(), plans=plans, profile=profile, dataset_revision="rev-1",
    )


def _agent_profile(**overrides) -> dict:
    profile = terminal_bench_profile(
        agent_id="claude-code",
        agent_version="2.0.30",
        model={"provider": "anthropic", "model": "claude-sonnet-4-5"},
        credentials={"provider": {"ref": "env:ANTHROPIC_API_KEY"}},
    )
    profile.update(overrides)
    return profile


def _reasons(profile: dict, *, tasks: list[dict] | None = None) -> list[str]:
    report = evaluate_preflight(
        profile=profile,
        tasks=tasks if tasks is not None else [
            {"task_key": key, "facts": {"has_tests": True}} for key in TASK_KEYS
        ],
        docker={"available": True, "server_version": "27.4.0", "platform": "linux/arm64"},
        network_policy="allowed",
        verifier_visibility="upstream",
    )
    return list(report["reason_codes"])


def test_real_agent_maps_model_and_pinned_cli_version():
    """真实 Agent 的模型与 CLI 版本必须进入原生配置，而不是只留在旁路字段。"""
    config = _config(_agent_profile())
    agent = config["job"]["agents"][0]
    assert agent["name"] == "claude-code"
    assert agent["model_name"] == "anthropic/claude-sonnet-4-5"
    # Harbor 0.23.0 的 InstalledAgentOptions.version 决定安装哪个 CLI 版本。
    assert agent["kwargs"] == {"version": "2.0.30"}
    # 凭据只以引用进入冻结配置，值永不出现在平台侧。
    assert config["credentials"] == {"provider": "env:ANTHROPIC_API_KEY"}
    assert all(
        str(value).startswith("env:") for value in config["credentials"].values()
    )


def test_oracle_needs_no_model_and_no_credentials():
    """oracle 是确定性校准 Agent：不需要模型，也不会带 credentials 段。"""
    config = _config(terminal_bench_profile())
    agent = config["job"]["agents"][0]
    assert agent["name"] == "oracle"
    assert "model_name" not in agent
    assert "kwargs" not in agent
    assert config["credentials"] == {}
    assert _reasons(terminal_bench_profile()) == []


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"agent_id": "not-a-real-agent"}, "HARBOR_AGENT_UNSUPPORTED"),
        ({"agent_version": "latest"}, "HARBOR_CONFIG_UNPINNED"),
        ({"model": {}}, "HARBOR_AGENT_MODEL_REQUIRED"),
        ({"credentials": {}}, "HARBOR_AGENT_CREDENTIAL_REF_MISSING"),
        ({"credentials": {"provider": "sk-plaintext"}}, "SECRET_VALUE_IN_CREDENTIALS"),
    ],
)
def test_agent_capability_gaps_are_refused_before_creation(overrides, code):
    """能力缺口在创建前具名拒绝（不靠 Runner 里失败）。"""
    with pytest.raises(HarborConfigError) as error:
        _config(_agent_profile(**overrides))
    assert error.value.code == code


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"agent_id": "not-a-real-agent"}, "AGENT_UNSUPPORTED"),
        ({"agent_version": "latest"}, "AGENT_VERSION_NOT_PINNED"),
        ({"model": {}}, "AGENT_MODEL_REQUIRED"),
        ({"credentials": {}}, "AGENT_CREDENTIAL_REF_MISSING"),
        ({"credentials": {"provider": "sk-plaintext"}}, "CREDENTIAL_REF_REQUIRED"),
    ],
)
def test_preflight_refuses_what_creation_refuses(overrides, reason):
    """预检与创建同一判定（review R20）：预检必须拒绝，且原因码可解释。"""
    reasons = _reasons(_agent_profile(**overrides))
    assert reason in reasons, reasons
    assert reason in reason_messages(reasons)


def test_agent_spec_table_pins_the_first_real_agent():
    """"首个真实 Agent"是显式登记的实现，不是"放开所有 Agent"。"""
    assert set(AGENT_SPECS) == {"oracle", "claude-code"}
    assert AGENT_SPECS["claude-code"]["requires_model"] is True
    assert AGENT_SPECS["claude-code"]["credential_envs"] == (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    )
    # 未验证的 Agent 名不会因为"名字看起来对"而被接受。
    with pytest.raises(HarborConfigError) as error:
        resolve_agent_profile({"agent_id": "codex", "agent_version": "1.0.0"})
    assert error.value.code == "HARBOR_AGENT_UNSUPPORTED"
