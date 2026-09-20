"""Harbor 原生配置转换（M3-T03，需求第 5 节）。

把"冻结的 Terminal-Bench 任务清单 + TrialPlan + Agent Profile"转换成固定
版本 Harbor 能**真正加载**的原生 ``JobConfig`` 载荷：

- 只允许 allowlist 字段；未知 native 参数在创建前拒绝（不猜、不静默丢）；
- Agent/Verifier/Job 三层期限独立记录，``n_trials``（计划重复）与
  ``retries``（传输重试）分开，重试默认 0、不叠加；
- 凭据只保留 ``{"ref": "env:NAME"}`` 引用，任何原始值拒绝，配置 dump /
  日志 / Artifact 里绝不出现秘密；
- 输出非秘密快照 + ``enforcement_owner``：模型身份、工具策略、token/成本
  观测在 runner-native transport 下由 Runner 观测，平台只记录边界。

字段名与取值来自锁定版本 Harbor 0.23.0 的真实模型
（``harbor.models.job.config.JobConfig`` / ``TaskConfig`` / ``TrialConfig``），
由 ``scripts/runner/verify-harbor-local`` 在真实 Runner 环境里加载验证；
本模块不 import harbor（API 进程不依赖 Runner 的第三方包）。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from motte_benchmark.harbor.tasks import HarborTaskError
from motte_contracts.trial import canonical_hash

#: 锁定版本（M3-G01/G06）：package、数据 revision 与兼容矩阵必须一致。
HARBOR_VERSION = "0.23.0"
HARBOR_SCHEMA_VERSION = "1.4"
CONFIG_SCHEMA = "motte-harbor-config@1"
#: 环境描述：首个受支持部署拓扑（Harbor 自己管理任务容器）。
DEPLOYMENT_TOPOLOGY = "harbor-owns-task-environments"

#: 允许出现的 JobConfig 顶层键（真实字段名，其余一律拒绝）。
ALLOWED_JOB_KEYS: frozenset[str] = frozenset({
    "job_name", "jobs_dir", "n_attempts", "install_only", "timeout_multiplier",
    "agent_timeout_multiplier", "verifier_timeout_multiplier",
    "agent_setup_timeout_multiplier", "environment_build_timeout_multiplier",
    "debug", "n_concurrent_trials", "quiet", "retry", "environment", "verifier",
    "metrics", "agents", "user_agent", "datasets", "tasks", "artifacts",
    "extra_instruction_paths", "extra_instructions", "source_jobs",
})
#: 允许出现的 AgentConfig 键。
ALLOWED_AGENT_KEYS: frozenset[str] = frozenset({
    "name", "import_path", "model_name", "n_concurrent", "concurrency_group",
    "skills", "override_timeout_sec", "override_setup_timeout_sec", "max_timeout_sec",
    "resume_trajectory", "load_trajectory", "extra_allowed_hosts", "include_logs",
    "exclude_logs", "kwargs", "env", "mcp_servers",
})
#: 允许出现的 EnvironmentConfig / VerifierConfig / RetryConfig 键。
ALLOWED_ENVIRONMENT_KEYS: frozenset[str] = frozenset({
    "type", "import_path", "force_build", "delete", "cpu_enforcement_policy",
    "memory_enforcement_policy", "override_cpus", "override_memory_mb",
    "override_storage_mb", "override_gpus", "override_tpu", "mounts",
    "extra_docker_compose", "env", "kwargs", "extra_allowed_hosts",
})
ALLOWED_VERIFIER_KEYS: frozenset[str] = frozenset({
    "override_timeout_sec", "max_timeout_sec", "include_logs", "exclude_logs",
    "env", "import_path", "kwargs", "disable",
})
ALLOWED_RETRY_KEYS: frozenset[str] = frozenset({
    "max_retries", "include_exceptions", "exclude_exceptions", "wait_multiplier",
})
#: 允许出现的 TaskConfig（显式任务列表项）键。
ALLOWED_TASK_KEYS: frozenset[str] = frozenset({
    "path", "git_url", "git_commit_id", "name", "ref", "overwrite", "download_dir",
    "source",
})
#: 平台支持的 Agent 形态：先只开放确定性校准 Agent，真实 Agent 需另行授权。
SUPPORTED_AGENTS: Mapping[str, str] = {"oracle": "1.0.0"}

#: 允许出现的三层期限键（Agent / Verifier / 环境构建与 Job 总期限分开）。
_TIMEOUT_KEYS = frozenset({
    "agent_sec", "verifier_sec", "job_sec", "environment_build_sec", "agent_setup_sec",
})

#: 平台侧硬性策略：不允许把 gold solution 当输入、不允许网络放大。
_FORBIDDEN_AGENT_KEYS = frozenset({"load_trajectory", "resume_trajectory"})
_PLACEHOLDER_VALUES = {"", "latest", "tbd", "todo", "placeholder", "unpinned", "unknown", "n/a"}


class HarborConfigError(HarborTaskError):
    """原生配置转换失败；code 前缀进入证据。"""


def _reject_unknown(section: str, payload: Mapping[str, Any], allowed: frozenset[str]) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise HarborConfigError(
            "HARBOR_CONFIG_UNKNOWN_FIELD",
            f"unknown {section} field(s) {unknown}; the pinned harbor "
            f"{HARBOR_VERSION} schema accepts {sorted(allowed)}. Refusing to guess a "
            "mapping (需求第 5 节：未知原生参数创建前拒绝)",
        )


def _pinned(value: Any, label: str) -> str:
    if not isinstance(value, str) or value.strip().lower() in _PLACEHOLDER_VALUES:
        raise HarborConfigError(
            "HARBOR_CONFIG_UNPINNED", f"{label} must be a real pinned value, not {value!r}",
        )
    return value.strip()


def _positive_int(value: Any, label: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HarborConfigError("HARBOR_CONFIG_INVALID", f"{label} must be an integer")
    if value < 0 or (value == 0 and not allow_zero):
        raise HarborConfigError(
            "HARBOR_CONFIG_INVALID",
            f"{label} must be {'>= 0' if allow_zero else '> 0'}, got {value}",
        )
    return value


def _positive_number(value: Any, label: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HarborConfigError("HARBOR_CONFIG_INVALID", f"{label} must be a number")
    if float(value) < 0 or (float(value) == 0 and not allow_zero):
        raise HarborConfigError(
            "HARBOR_CONFIG_INVALID",
            f"{label} must be {'>= 0' if allow_zero else '> 0'}, got {value}",
        )
    return float(value)


def credential_refs(credentials: Mapping[str, Any] | None) -> dict[str, str]:
    """凭据只允许引用形式；任何原始值都按秘密处理并拒绝。"""
    if credentials is None:
        return {}
    refs: dict[str, str] = {}
    for name, value in credentials.items():
        if (
            isinstance(value, Mapping)
            and set(value) == {"ref"}
            and isinstance(value.get("ref"), str)
            and value["ref"].startswith("env:")
            and len(value["ref"]) > len("env:")
        ):
            refs[str(name)] = str(value["ref"])
            continue
        raise HarborConfigError(
            "SECRET_VALUE_IN_CREDENTIALS",
            f"{name}: pass credentials by reference ({{'ref': 'env:NAME'}}), never as values",
        )
    return refs


def resolve_agent_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    """校验并冻结 Agent Profile 的 allowlist 部分（需求第 5 节）。

    ``profile`` 的关键字段：``agent_id`` / ``agent_version`` /
    ``n_trials`` / ``retries`` / ``timeouts`` / ``resources`` / ``environment``
    / ``model`` / ``tools`` / ``credentials`` / ``aggregation``。
    """
    agent_id = _pinned(profile.get("agent_id"), "profile.agent_id")
    if agent_id not in SUPPORTED_AGENTS:
        raise HarborConfigError(
            "HARBOR_AGENT_UNSUPPORTED",
            f"agent {agent_id!r} is not in the verified set {sorted(SUPPORTED_AGENTS)}; "
            "an unverified agent must not be advertised as supported (M3-G18)",
        )
    agent_version = _pinned(profile.get("agent_version"), "profile.agent_version")
    expected = SUPPORTED_AGENTS[agent_id]
    if agent_version != expected:
        raise HarborConfigError(
            "HARBOR_AGENT_UNSUPPORTED",
            f"agent {agent_id} is verified at version {expected}, not {agent_version!r}",
        )
    trials = _positive_int(profile.get("n_trials", 1), "profile.n_trials")
    if trials > 32:
        raise HarborConfigError(
            "HARBOR_CONFIG_INVALID", f"n_trials {trials} exceeds the platform ceiling 32",
        )
    retries = profile.get("retries") or {}
    _reject_unknown("profile.retries", retries, frozenset({"runner", "provider_transport", "operator"}))
    native_retries = _positive_int(retries.get("runner", 0), "retries.runner", allow_zero=True)
    if native_retries:
        # 需求第 5 节：重试默认不叠加。首版不开放 Harbor 自身重试。
        raise HarborConfigError(
            "HARBOR_RETRY_NOT_ALLOWED",
            "native runner retries are not enabled in this phase; planned repeats are "
            "expressed with n_trials, transport retries stay 0",
        )
    timeouts = profile.get("timeouts") or {}
    if not isinstance(timeouts, Mapping):
        raise HarborConfigError("HARBOR_CONFIG_INVALID", "profile.timeouts must be an object")
    _reject_unknown("profile.timeouts", timeouts, _TIMEOUT_KEYS)
    model = profile.get("model") or {}
    if not isinstance(model, Mapping):
        raise HarborConfigError("HARBOR_CONFIG_INVALID", "profile.model must be an object")
    _reject_unknown(
        "profile.model", model,
        frozenset({"provider", "model", "reasoning_level", "parameters", "ref"}),
    )
    environment = profile.get("environment") or {}
    env_type = _pinned(environment.get("type", "docker"), "profile.environment.type")
    if env_type not in ("docker",):
        raise HarborConfigError(
            "HARBOR_ENVIRONMENT_UNSUPPORTED",
            f"environment type {env_type!r} is not verified for this deployment "
            f"(supported: ['docker'])",
        )
    return {
        "agent_id": agent_id,
        "agent_version": agent_version,
        "n_trials": trials,
        "native_retries": native_retries,
        "model": dict(model),
        "environment": {
            "type": env_type,
            "delete": bool(environment.get("delete", True)),
        },
        "resources": dict(profile.get("resources") or {}),
        "timeouts": dict(profile.get("timeouts") or {}),
        "tools": dict(profile.get("tools") or {}),
        "credentials": credential_refs(profile.get("credentials")),
        "aggregation": str(profile.get("aggregation") or "first-trial"),
        "limits": dict(profile.get("limits") or {}),
    }


def _agent_section(
    frozen: Mapping[str, Any], timeout_sec: float | None,
) -> dict[str, Any]:
    agent: dict[str, Any] = {"name": frozen["agent_id"]}
    model = frozen.get("model") or {}
    if model.get("model"):
        provider = model.get("provider")
        agent["model_name"] = (
            f"{provider}/{model['model']}" if provider else str(model["model"])
        )
    elif frozen["agent_id"] != "oracle":
        raise HarborConfigError(
            "HARBOR_CONFIG_INVALID",
            f"agent {frozen['agent_id']} requires profile.model.model",
        )
    if model.get("ref"):
        raise HarborConfigError(
            "HARBOR_CONFIG_INVALID",
            "profile.model.ref is not mapped in the pinned schema; remove it or extend "
            "the allowlist with a real harbor field",
        )
    if timeout_sec is not None:
        agent["override_timeout_sec"] = timeout_sec
    _reject_unknown("agent", agent, ALLOWED_AGENT_KEYS)
    return agent


def _environment_section(frozen: Mapping[str, Any]) -> dict[str, Any]:
    resources = frozen.get("resources") or {}
    _reject_unknown(
        "profile.resources", resources,
        frozenset({"cpus", "memory_mb", "storage_mb", "gpus"}),
    )
    section: dict[str, Any] = {
        "type": frozen["environment"]["type"],
        "delete": frozen["environment"]["delete"],
    }
    mapping = {
        "cpus": "override_cpus",
        "memory_mb": "override_memory_mb",
        "storage_mb": "override_storage_mb",
        "gpus": "override_gpus",
    }
    for key, harbor_key in mapping.items():
        if resources.get(key) is not None:
            section[harbor_key] = _positive_int(resources[key], f"resources.{key}")
    _reject_unknown("environment", section, ALLOWED_ENVIRONMENT_KEYS)
    return section


def _verifier_section(frozen: Mapping[str, Any]) -> dict[str, Any]:
    timeouts = frozen.get("timeouts") or {}
    _reject_unknown("profile.timeouts", timeouts, _TIMEOUT_KEYS)
    section: dict[str, Any] = {}
    if timeouts.get("verifier_sec") is not None:
        section["override_timeout_sec"] = _positive_number(
            timeouts["verifier_sec"], "timeouts.verifier_sec",
        )
    _reject_unknown("verifier", section, ALLOWED_VERIFIER_KEYS)
    return section


def _retry_section(frozen: Mapping[str, Any]) -> dict[str, Any]:
    """Harbor 自身的重试必须显式关闭：计划重复用 n_trials 表达。"""
    section = {"max_retries": int(frozen["native_retries"])}
    section["exclude_exceptions"] = None
    _reject_unknown("retry", section, ALLOWED_RETRY_KEYS)
    return section


def plan_trials(
    *,
    run_id: str,
    tasks: Sequence[Mapping[str, Any]],
    repeats: int,
    agent_config_hash: str,
    environment_hash: str,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """冻结 TrialPlan：每 Task ``repeats`` 个计划单元，repeat_index 事前确定。"""
    from motte_contracts.trial import trial_id_for  # noqa: PLC0415 - 契约依赖

    plans: list[dict[str, Any]] = []
    for task in tasks:
        task_key = str(task["task_key"])
        for repeat_index in range(repeats):
            plans.append({
                "trial_id": trial_id_for(
                    run_id=run_id, task_key=task_key, repeat_index=repeat_index,
                    agent_config_hash=agent_config_hash, environment_hash=environment_hash,
                    seed=seed,
                ),
                "run_id": run_id,
                "task_key": task_key,
                "repeat_index": repeat_index,
                "seed": seed,
                "agent_config_hash": agent_config_hash,
                "environment_hash": environment_hash,
            })
    return plans


def build_harbor_config(
    *,
    run_id: str,
    job_id: str,
    work_root: str,
    tasks: Sequence[Mapping[str, Any]],
    plans: Sequence[Mapping[str, Any]],
    profile: Mapping[str, Any],
    dataset_revision: str,
) -> dict[str, Any]:
    """生成冻结的 Runner 配置（``jobs_dir`` 为相对工作目录的固定名）。

    ``tasks`` 是已准备的任务身份（含 ``normalized_relative_path``），
    ``plans`` 是冻结的 TrialPlan；两者的 Task 集合必须一致，否则拒绝——
    "计划里有的任务不存在"绝不能被静默忽略。
    """
    revision = _pinned(dataset_revision, "dataset_revision")
    frozen = resolve_agent_profile(profile)
    _reject_unknown("profile", profile, _PROFILE_KEYS)
    timeouts = frozen["timeouts"]
    _reject_unknown("profile.timeouts", timeouts, _TIMEOUT_KEYS)
    job_timeout = (
        _positive_number(timeouts["job_sec"], "timeouts.job_sec")
        if timeouts.get("job_sec") is not None else None
    )
    environment_build = (
        _positive_number(timeouts["environment_build_sec"], "timeouts.environment_build_sec")
        if timeouts.get("environment_build_sec") is not None else None
    )
    agent_setup = (
        _positive_number(timeouts["agent_setup_sec"], "timeouts.agent_setup_sec")
        if timeouts.get("agent_setup_sec") is not None else None
    )
    agent_timeout = (
        _positive_number(timeouts["agent_sec"], "timeouts.agent_sec")
        if timeouts.get("agent_sec") is not None else None
    )

    planned_keys = sorted({str(plan["task_key"]) for plan in plans})
    if sorted({str(task["task_key"]) for task in tasks}) != planned_keys:
        raise HarborConfigError(
            "HARBOR_PLAN_TASK_MISMATCH",
            "trial plans and selected tasks do not cover the same task set",
        )
    per_task: dict[str, int] = {}
    for plan in plans:
        per_task[str(plan["task_key"])] = per_task.get(str(plan["task_key"]), 0) + 1
    for task_key, count in per_task.items():
        if count != frozen["n_trials"]:
            raise HarborConfigError(
                "HARBOR_PLAN_INCOMPLETE",
                f"task {task_key} has {count} planned trials, expected "
                f"{frozen['n_trials']}",
            )
    repeats = {int(plan["repeat_index"]) for plan in plans}
    if repeats != set(range(frozen["n_trials"])):
        raise HarborConfigError(
            "HARBOR_PLAN_INCOMPLETE",
            f"repeat_index must cover 0..{frozen['n_trials'] - 1}, got {sorted(repeats)}",
        )

    ordered = sorted(tasks, key=lambda task: str(task["normalized_relative_path"]))
    task_entries = [
        {"path": str(task["normalized_relative_path"]), "source": str(task["source_id"])}
        for task in ordered
    ]
    for entry in task_entries:
        _reject_unknown("task", entry, ALLOWED_TASK_KEYS)

    job_name = job_id
    job: dict[str, Any] = {
        "job_name": job_name,
        "jobs_dir": str(work_root),
        # 计划重复 = Harbor 的 n_attempts；原生 retry 保持 0，两者不混写。
        "n_attempts": frozen["n_trials"],
        "install_only": False,
        "n_concurrent_trials": 1,
        "quiet": True,
        "retry": _retry_section(frozen),
        "environment": _environment_section(frozen),
        "verifier": _verifier_section(frozen),
        "agents": [_agent_section(frozen, agent_timeout)],
        "tasks": task_entries,
        # 显式任务列表：平台永远不把整个目录当 dataset（避免同名任务被上游合并）。
        "datasets": [],
        "metrics": [],
    }
    if job_timeout is not None:
        job["timeout_multiplier"] = 1.0
    if environment_build is not None:
        job["environment_build_timeout_multiplier"] = 1.0
    if agent_setup is not None:
        job["agent_setup_timeout_multiplier"] = 1.0
    _reject_unknown("job", job, ALLOWED_JOB_KEYS)

    timeouts_record = {
        "agent_sec": agent_timeout,
        "verifier_sec": timeouts.get("verifier_sec"),
        "environment_build_sec": environment_build,
        "agent_setup_sec": agent_setup,
        "job_sec": job_timeout,
    }
    config: dict[str, Any] = {
        "schema": CONFIG_SCHEMA,
        "harbor_version": HARBOR_VERSION,
        "harbor_schema_version": HARBOR_SCHEMA_VERSION,
        "deployment_topology": DEPLOYMENT_TOPOLOGY,
        "dataset_revision": revision,
        "job": job,
        "timeouts": timeouts_record,
        "n_trials": frozen["n_trials"],
        "planned_trial_count": len(plans),
        "planned_task_count": len(ordered),
        "trials": [deepcopy(dict(plan)) for plan in plans],
        "credentials": dict(frozen["credentials"]),
        "aggregation": frozen["aggregation"],
        "model": deepcopy(dict(frozen["model"])),
        "tools": deepcopy(dict(frozen["tools"])),
        "limits": deepcopy(dict(frozen["limits"])),
        "observation_boundary": {
            # 没有保真 Provider 扩展点时如实声明：模型身份与用量由 Runner 观测。
            "transport": "runner-native",
            "observed_by": "harbor-runner",
            "enforced_by": "harbor-runner",
            "platform_can_verify": ["runner_exit", "reward_files", "trial_results"],
            "platform_cannot_verify": [
                "provider_request_identity", "provider_usage_and_cost",
            ],
        },
        "security": {
            "task_containers_get_docker_socket": False,
            "task_containers_get_platform_db": False,
            "task_containers_get_host_credentials": False,
            "platform_custom_profile": False,
        },
    }
    config["config_hash"] = canonical_hash({
        key: value for key, value in config.items() if key != "config_hash"
    })
    return config


_PROFILE_KEYS = frozenset({
    # 平台级身份（ExternalJobSpec 要求钉住），不映射到 Harbor 原生字段。
    "benchmark_id", "benchmark_version",
    "agent_id", "agent_version", "n_trials", "retries", "model", "environment",
    "resources", "timeouts", "tools", "credentials", "aggregation", "limits",
})


def frozen_config_hash(config: Mapping[str, Any]) -> str:
    """配置的规范哈希（重算而非信任传入值）。"""
    return canonical_hash({
        key: value for key, value in config.items() if key != "config_hash"
    })


def agent_and_environment_hashes(profile: Mapping[str, Any]) -> tuple[str, str]:
    """计划里冻结的 ``agent_config_hash`` / ``environment_hash``。"""
    frozen = resolve_agent_profile(profile)
    agent_config_hash = canonical_hash({
        "agent_id": frozen["agent_id"],
        "agent_version": frozen["agent_version"],
        "model": frozen["model"],
        "tools": frozen["tools"],
        "timeouts": frozen["timeouts"],
    })
    environment_hash = canonical_hash({
        "environment": frozen["environment"],
        "resources": frozen["resources"],
        "harbor_version": HARBOR_VERSION,
        "harbor_schema_version": HARBOR_SCHEMA_VERSION,
        "deployment_topology": DEPLOYMENT_TOPOLOGY,
    })
    return agent_config_hash, environment_hash


def job_config_for_runner(config: Mapping[str, Any], *, dataset_root: str) -> dict[str, Any]:
    """把冻结配置投影成 Harbor 原生 JobConfig（相对任务路径 → 绝对路径）。

    绝对路径只在 Runner 侧生成（工作目录属于 Runner），冻结配置本身保持
    相对路径，因此同一份配置在不同宿主上仍然指向同一批受控任务。
    """
    from motte_benchmark.harbor.tasks import resolve_within  # noqa: PLC0415 - 路径安全

    tasks = []
    for entry in config["job"]["tasks"]:
        relative = str(entry["path"])
        tasks.append({
            "path": str(resolve_within(dataset_root, relative)),
            "source": entry.get("source"),
        })
    job = deepcopy(dict(config["job"]))
    job["tasks"] = tasks
    job["jobs_dir"] = str(config["job"]["jobs_dir"])
    return job
