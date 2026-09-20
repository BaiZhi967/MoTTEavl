"""M3-T03：Harbor 原生配置 allowlist、期限分层与凭据引用（M3-A14，需求第 5 节）。"""
from __future__ import annotations

import json

import pytest

from motte_benchmark.harbor.config import (
    HARBOR_VERSION,
    HarborConfigError,
    agent_and_environment_hashes,
    build_harbor_config,
    credential_refs,
    frozen_config_hash,
    job_config_for_runner,
    plan_trials,
    resolve_agent_profile,
)
from motte_benchmark.harbor.tasks import HarborTaskError, environment_digest

#: 明文测试值：用来证明它绝不会出现在配置/日志/凭据引用里。
SYNTHETIC_SECRET = "synthetic-value-that-must-never-be-serialized"
CREDENTIAL_NAME = "runner_api_token"


def _profile(**overrides: object) -> dict[str, object]:
    profile: dict[str, object] = {
        "agent_id": "oracle",
        "agent_version": "1.0.0",
        "n_trials": 3,
        "timeouts": {
            "agent_sec": 120.0, "verifier_sec": 90.0, "job_sec": 900.0,
            "environment_build_sec": 300.0, "agent_setup_sec": 60.0,
        },
        "environment": {"type": "docker", "delete": True},
        "resources": {"cpus": 2, "memory_mb": 2048},
        "credentials": {CREDENTIAL_NAME: {"ref": "env:TEST_ONLY_KEY"}},
    }
    profile.update(overrides)
    return profile


def _tasks(count: int = 2) -> list[dict[str, object]]:
    return [
        {
            "task_key": f"sha256:task{index}",
            "normalized_relative_path": f"tasks/task-{index}",
            "source_id": "tb2-sample",
        }
        for index in range(count)
    ]


def _config(profile: dict[str, object] | None = None, tasks: list[dict[str, object]] | None = None):
    profile = profile or _profile()
    tasks = tasks if tasks is not None else _tasks()
    agent_hash, environment_hash = agent_and_environment_hashes(profile)
    plans = plan_trials(
        run_id="run-1", tasks=tasks, repeats=int(profile["n_trials"]),
        agent_config_hash=agent_hash, environment_hash=environment_hash,
    )
    return build_harbor_config(
        run_id="run-1", job_id="job-1", work_root="jobs", tasks=tasks, plans=plans,
        profile=profile, dataset_revision="fixtures-2026-09-20",
    ), plans


def test_harbor_config_rejects_unknown_and_pins_defaults() -> None:
    """未知 native 字段/未固定版本在创建前拒绝；默认值明确且可复核。"""
    config, plans = _config()
    assert config["harbor_version"] == HARBOR_VERSION
    assert config["planned_trial_count"] == len(plans) == 6
    assert config["job"]["n_attempts"] == 3, "planned repeats go through n_attempts"
    assert config["job"]["retry"]["max_retries"] == 0, "native retries must not stack"
    assert config["job"]["n_concurrent_trials"] == 1
    assert config["job"]["install_only"] is False
    assert config["job"]["datasets"] == [], "explicit task list only; never a whole directory"
    assert [entry["path"] for entry in config["job"]["tasks"]] == ["tasks/task-0", "tasks/task-1"]
    assert config["timeouts"] == {
        "agent_sec": 120.0, "verifier_sec": 90.0, "environment_build_sec": 300.0,
        "agent_setup_sec": 60.0, "job_sec": 900.0,
    }
    assert config["config_hash"] == frozen_config_hash(config)

    with pytest.raises(HarborConfigError) as unknown:
        build_harbor_config(
            run_id="run-1", job_id="job-1", work_root="jobs", tasks=_tasks(),
            plans=plans, profile={**_profile(), "mystery_flag": True},
            dataset_revision="fixtures-2026-09-20",
        )
    assert unknown.value.code == "HARBOR_CONFIG_UNKNOWN_FIELD"

    with pytest.raises(HarborConfigError) as unpinned:
        build_harbor_config(
            run_id="run-1", job_id="job-1", work_root="jobs", tasks=_tasks(),
            plans=plans, profile=_profile(), dataset_revision="latest",
        )
    assert unpinned.value.code == "HARBOR_CONFIG_UNPINNED"

    with pytest.raises(HarborConfigError) as unknown_timeout:
        resolve_agent_profile(_profile(timeouts={"agent_sec": 1.0, "verifier": 2.0}))
    assert unknown_timeout.value.code == "HARBOR_CONFIG_UNKNOWN_FIELD"


def test_unknown_agent_and_environment_are_refused() -> None:
    """未验证的 Agent / 环境不因"看起来能用"而放行（M3-G18）。"""
    with pytest.raises(HarborConfigError) as agent:
        resolve_agent_profile(_profile(agent_id="terminus-2"))
    assert agent.value.code == "HARBOR_AGENT_UNSUPPORTED"
    with pytest.raises(HarborConfigError) as version:
        resolve_agent_profile(_profile(agent_version="9.9.9"))
    assert version.value.code == "HARBOR_AGENT_UNSUPPORTED"
    with pytest.raises(HarborConfigError) as environment:
        resolve_agent_profile(_profile(environment={"type": "daytona"}))
    assert environment.value.code == "HARBOR_ENVIRONMENT_UNSUPPORTED"


def test_native_retries_are_not_stacked() -> None:
    """runner retry 不能与计划重复叠加。"""
    with pytest.raises(HarborConfigError) as retries:
        resolve_agent_profile(_profile(retries={"runner": 2}))
    assert retries.value.code == "HARBOR_RETRY_NOT_ALLOWED"
    frozen = resolve_agent_profile(_profile(retries={"runner": 0, "provider_transport": 3}))
    assert frozen["native_retries"] == 0


def test_credentials_are_refs_only_and_never_leak() -> None:
    """原始凭据值拒绝；引用不把秘密泄漏到配置 dump。"""
    with pytest.raises(HarborConfigError) as raw:
        credential_refs({CREDENTIAL_NAME: SYNTHETIC_SECRET})
    assert raw.value.code == "SECRET_VALUE_IN_CREDENTIALS"
    with pytest.raises(HarborConfigError):
        credential_refs({"k": {"ref": SYNTHETIC_SECRET}})
    with pytest.raises(HarborConfigError):
        credential_refs({"k": {"ref": "env:"}})

    config, _plans = _config(_profile(credentials={CREDENTIAL_NAME: {"ref": "env:TEST_ONLY_KEY"}}))
    assert config["credentials"] == {CREDENTIAL_NAME: "env:TEST_ONLY_KEY"}
    serialized = json.dumps(config, ensure_ascii=False, sort_keys=True)
    assert SYNTHETIC_SECRET not in serialized
    assert config["observation_boundary"]["transport"] == "runner-native"
    assert "provider_usage_and_cost" in config["observation_boundary"]["platform_cannot_verify"]


def test_plan_and_task_set_must_match() -> None:
    """计划集合与任务集合不一致时拒绝；repeat 不连续也拒绝。"""
    profile = _profile()
    agent_hash, environment_hash = agent_and_environment_hashes(profile)
    tasks = _tasks(2)
    plans = plan_trials(
        run_id="run-1", tasks=tasks, repeats=3,
        agent_config_hash=agent_hash, environment_hash=environment_hash,
    )
    with pytest.raises(HarborConfigError) as mismatch:
        build_harbor_config(
            run_id="run-1", job_id="job-1", work_root="jobs", tasks=tasks[:1],
            plans=plans, profile=profile, dataset_revision="rev-1",
        )
    assert mismatch.value.code == "HARBOR_PLAN_TASK_MISMATCH"

    dropped = [plan for plan in plans if plan["repeat_index"] != 2]
    with pytest.raises(HarborConfigError) as incomplete:
        build_harbor_config(
            run_id="run-1", job_id="job-1", work_root="jobs", tasks=tasks,
            plans=dropped, profile=profile, dataset_revision="rev-1",
        )
    assert incomplete.value.code == "HARBOR_PLAN_INCOMPLETE"


def test_change_of_agent_or_environment_changes_the_frozen_identity() -> None:
    """更换 Agent/native 配置/环境 → 计划与配置 hash 改变（M3-A14）。"""
    first_config, first_plans = _config()
    other_config, other_plans = _config(_profile(resources={"cpus": 4, "memory_mb": 4096}))

    assert first_config["config_hash"] != other_config["config_hash"]
    assert first_plans[0]["agent_config_hash"] == other_plans[0]["agent_config_hash"]
    assert first_plans[0]["environment_hash"] != other_plans[0]["environment_hash"]
    assert first_plans[0]["trial_id"] != other_plans[0]["trial_id"]

    digest = environment_digest(
        harbor_version=HARBOR_VERSION, terminal_bench_revision="tb2@7e917f3",
        docker_platform="linux/arm64", image_digest=None, agent_id="oracle",
        agent_version="1.0.0",
    )
    hardened = environment_digest(
        harbor_version=HARBOR_VERSION, terminal_bench_revision="tb2@7e917f3",
        docker_platform="linux/arm64", image_digest=None, agent_id="oracle",
        agent_version="1.0.0", extra={"verifier_visibility": "platform-custom"},
    )
    assert digest != hardened


def test_runner_job_config_projects_relative_paths_to_absolute(tmp_path) -> None:
    """Runner 侧才生成绝对任务路径；越界路径拒绝。"""
    config, _plans = _config()
    job = job_config_for_runner(config, dataset_root=str(tmp_path))
    assert all(str(entry["path"]).startswith(str(tmp_path)) for entry in job["tasks"])
    assert job["n_attempts"] == config["job"]["n_attempts"]
    assert job["retry"]["max_retries"] == 0

    escaped = json.loads(json.dumps(config))
    escaped["job"]["tasks"][0]["path"] = "../outside"
    with pytest.raises(HarborTaskError) as error:
        job_config_for_runner(escaped, dataset_root=str(tmp_path))
    assert error.value.code == "TASK_PATH_INVALID"
