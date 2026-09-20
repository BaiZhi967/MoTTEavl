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

import math
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
#: 平台支持的 Agent 形态表（review R10）。每项声明：Harbor 原生 Agent 名、
#: 是否需要模型（oracle 是确定性校准 Agent，不需要模型调用）、是否必须显式
#: 钉住 CLI 版本、以及在 Runner 环境里**必须存在**的凭据引用名（平台只冻结
#: ``env:NAME`` 引用，值始终来自 Runner 自己的环境，绝不进平台库）。
AGENT_SPECS: Mapping[str, Mapping[str, Any]] = {
    "oracle": {
        "harbor_name": "oracle",
        "verified_versions": ("1.0.0",),
        "requires_model": False,
        "requires_pinned_version": True,
        "credential_envs": (),
    },
    "claude-code": {
        # Harbor 0.23.0 的原生 Agent（`harbor.agents.installed.claude_code`）。
        # CLI 版本由操作员显式钉住（Harbor 的 InstalledAgentOptions.version，
        # 省略即 latest → 平台拒绝），因此这里不做版本白名单，只要求显式且
        # 记录在冻结配置里；真实调用仍需单独授权（M3 兼容矩阵如实登记）。
        "harbor_name": "claude-code",
        "verified_versions": None,
        "requires_model": True,
        "requires_pinned_version": True,
        "credential_envs": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    },
}
#: 已具备实现、可在明确授权后做真实调用的 Agent（oracle 之外的首个真实 Agent）。
SUPPORTED_AGENTS: Mapping[str, str] = {"oracle": "1.0.0"}


def credential_env_names(refs: Mapping[str, str]) -> tuple[str, ...]:
    """凭据引用 → 环境变量名（``env:NAME``；其他形式在冻结时已被拒绝）。"""
    return tuple(
        str(ref).removeprefix("env:") for ref in refs.values()
        if str(ref).startswith("env:")
    )


def agent_capability_reasons(profile: Mapping[str, Any]) -> list[str]:
    """Agent 能力检查的**不抛异常**版本：预检与创建共用同一判定（review R20）。

    预检说"可以创建"而创建随后 422/500 是双重标准：两处必须给出同一组结论，
    差别只在于预检汇总成 ``reason_codes``、创建抛出带 code 的错误。
    """
    reasons: list[str] = []
    agent_id = profile.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id.strip():
        return ["AGENT_NOT_PINNED"]
    spec = AGENT_SPECS.get(agent_id.strip())
    if spec is None:
        return ["AGENT_UNSUPPORTED"]
    version = profile.get("agent_version")
    if not isinstance(version, str) or version.strip().lower() in _PLACEHOLDER_VALUES:
        reasons.append("AGENT_VERSION_NOT_PINNED")
    elif (
        spec["verified_versions"] is not None
        and version.strip() not in spec["verified_versions"]
    ):
        reasons.append("AGENT_VERSION_UNSUPPORTED")
    model = profile.get("model") or {}
    if spec["requires_model"] and not (
        isinstance(model, Mapping) and model.get("model")
    ):
        reasons.append("AGENT_MODEL_REQUIRED")
    required = tuple(str(name) for name in spec["credential_envs"])
    if required:
        try:
            names = set(credential_env_names(credential_refs(profile.get("credentials"))))
        except HarborConfigError:
            names = set()
            reasons.append("CREDENTIAL_REF_REQUIRED")
        if not names & set(required):
            reasons.append("AGENT_CREDENTIAL_REF_MISSING")
    return reasons

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

    review R10：真实 Agent 也由同一处判定——不在允许表里、版本没显式钉住、
    需要模型却没有模型、需要凭据引用却没有引用，都在创建前拒绝（fail closed），
    而不是"先创建再在 Runner 里失败"。这里只做**能力与引用**校验，凭据值永远
    不进入平台。
    """
    agent_id = _pinned(profile.get("agent_id"), "profile.agent_id")
    spec = AGENT_SPECS.get(agent_id)
    if spec is None:
        raise HarborConfigError(
            "HARBOR_AGENT_UNSUPPORTED",
            f"agent {agent_id!r} is not in the verified set {sorted(AGENT_SPECS)}; "
            "an unverified agent must not be advertised as supported (M3-G18)",
        )
    agent_version = _pinned(profile.get("agent_version"), "profile.agent_version")
    verified = spec["verified_versions"]
    if verified is not None and agent_version not in verified:
        raise HarborConfigError(
            "HARBOR_AGENT_UNSUPPORTED",
            f"agent {agent_id} is verified at version {sorted(verified)}, "
            f"not {agent_version!r}",
        )
    model = profile.get("model") or {}
    if not isinstance(model, Mapping):
        raise HarborConfigError("HARBOR_CONFIG_INVALID", "profile.model must be an object")
    if spec["requires_model"] and not model.get("model"):
        raise HarborConfigError(
            "HARBOR_AGENT_MODEL_REQUIRED",
            f"agent {agent_id} performs real model calls; profile.model.model is required "
            "(oracle is the only agent that runs without a model)",
        )
    credential_names = credential_env_names(credential_refs(profile.get("credentials")))
    required_envs = tuple(str(name) for name in spec["credential_envs"])
    if required_envs and not set(required_envs) & set(credential_names):
        raise HarborConfigError(
            "HARBOR_AGENT_CREDENTIAL_REF_MISSING",
            f"agent {agent_id} needs one of {list(required_envs)} as a credential "
            "reference, e.g. {'credentials': {'provider': {'ref': "
            f"'env:{required_envs[0]}'}}}}; values are never passed to the platform",
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
    setup_timeout_sec: float | None = None,
) -> dict[str, Any]:
    """原生 Agent 段：真实 Agent 的 CLI 版本走 ``kwargs.version``（review R10）。

    Harbor 0.23.0 的 ``InstalledAgentOptions.version`` 是"要安装的 CLI 版本，
    省略即 latest"，因此非 oracle Agent 必须把钉住的版本显式传进去——否则
    平台声称的"固定 Agent 版本"就只是文档。
    """
    spec = AGENT_SPECS.get(str(frozen["agent_id"]), {})
    agent: dict[str, Any] = {"name": spec.get("harbor_name") or frozen["agent_id"]}
    model = frozen.get("model") or {}
    if model.get("model"):
        provider = model.get("provider")
        agent["model_name"] = (
            f"{provider}/{model['model']}" if provider else str(model["model"])
        )
    elif frozen["agent_id"] != "oracle":
        raise HarborConfigError(
            "HARBOR_AGENT_MODEL_REQUIRED",
            f"agent {frozen['agent_id']} requires profile.model.model",
        )
    if model.get("ref"):
        raise HarborConfigError(
            "HARBOR_CONFIG_INVALID",
            "profile.model.ref is not mapped in the pinned schema; remove it or extend "
            "the allowlist with a real harbor field",
        )
    if spec.get("requires_pinned_version") and str(frozen["agent_id"]) != "oracle":
        agent["kwargs"] = {"version": str(frozen["agent_version"])}
    if timeout_sec is not None:
        # Harbor 0.23.0：agent 期限 = override_timeout_sec（否则任务自带值）
        # 再乘 agent_timeout_multiplier（平台固定 1.0）。
        agent["override_timeout_sec"] = timeout_sec
    if setup_timeout_sec is not None:
        # setup 阶段用独立字段 override_setup_timeout_sec（默认 360 秒）。
        agent["override_setup_timeout_sec"] = setup_timeout_sec
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
    if timeouts.get("environment_build_sec") is not None:
        # Harbor 0.23.0 只能按任务自带 ``environment.build_timeout_sec`` 乘
        # ``environment_build_timeout_multiplier`` 缩放，JobConfig 里没有
        # per-trial 覆盖字段。平台拒绝"接受秒数但不执行"（review R07）。
        raise HarborConfigError(
            "HARBOR_TIMEOUT_UNSUPPORTED",
            "environment_build_sec cannot be enforced by harbor "
            f"{HARBOR_VERSION}: the build deadline is the task's own "
            "environment.build_timeout_sec scaled by a multiplier, with no per-trial "
            "override; remove it from profile.timeouts (the task value stays in effect)",
        )
    agent_setup = (
        _positive_number(timeouts["agent_setup_sec"], "timeouts.agent_setup_sec")
        if timeouts.get("agent_setup_sec") is not None else None
    )
    agent_timeout = (
        _positive_number(timeouts["agent_sec"], "timeouts.agent_sec")
        if timeouts.get("agent_sec") is not None else None
    )
    verifier_timeout = (
        _positive_number(timeouts["verifier_sec"], "timeouts.verifier_sec")
        if timeouts.get("verifier_sec") is not None else None
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
        "agents": [_agent_section(frozen, agent_timeout, agent_setup)],
        "tasks": task_entries,
        # 显式任务列表：平台永远不把整个目录当 dataset（避免同名任务被上游合并）。
        "datasets": [],
        "metrics": [],
    }
    # 倍率一律 1.0：请求的秒数必须原样生效，不能靠"任务自带值 × 倍率"表达
    # （review R07）。每项的实际生效秒数记录在 effective_timeouts 里。
    multipliers = {
        "timeout_multiplier": 1.0,
        "agent_timeout_multiplier": 1.0,
        "verifier_timeout_multiplier": 1.0,
        "agent_setup_timeout_multiplier": 1.0,
        "environment_build_timeout_multiplier": 1.0,
    }
    job.update(multipliers)
    _reject_unknown("job", job, ALLOWED_JOB_KEYS)

    timeouts_record = {
        "agent_sec": agent_timeout,
        "verifier_sec": verifier_timeout,
        "environment_build_sec": None,
        "agent_setup_sec": agent_setup,
        "job_sec": job_timeout,
    }
    effective_timeouts = _effective_timeouts(
        requested=timeouts_record, multipliers=multipliers,
    )
    config: dict[str, Any] = {
        "schema": CONFIG_SCHEMA,
        "harbor_version": HARBOR_VERSION,
        "harbor_schema_version": HARBOR_SCHEMA_VERSION,
        "deployment_topology": DEPLOYMENT_TOPOLOGY,
        "dataset_revision": revision,
        "job": job,
        "timeouts": timeouts_record,
        # 每项**实际生效**的秒数与由谁强制（review R07）：请求值与生效值必须
        # 可核对，不能让秒数只落在旁路字段里。
        "effective_timeouts": effective_timeouts,
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


#: 每项期限的强制者：Harbor 原生字段 vs 平台 Supervisor（review R07）。
_TIMEOUT_ENFORCEMENT: Mapping[str, dict[str, Any]] = {
    "agent_sec": {
        "harbor_field": "agents[].override_timeout_sec",
        "multiplier_field": "agent_timeout_multiplier",
        "enforced_by": "harbor",
    },
    "verifier_sec": {
        "harbor_field": "verifier.override_timeout_sec",
        "multiplier_field": "verifier_timeout_multiplier",
        "enforced_by": "harbor",
    },
    "agent_setup_sec": {
        "harbor_field": "agents[].override_setup_timeout_sec",
        "multiplier_field": "agent_setup_timeout_multiplier",
        "enforced_by": "harbor",
    },
    "environment_build_sec": {
        "harbor_field": None,
        "multiplier_field": None,
        "enforced_by": "task-authored",
        "note": (
            "harbor 0.23.0 只能用任务自带 environment.build_timeout_sec 乘倍率；"
            "平台不接受该参数（HARBOR_TIMEOUT_UNSUPPORTED）"
        ),
    },
    "job_sec": {
        "harbor_field": None,
        "multiplier_field": None,
        "enforced_by": "platform-supervisor",
        "note": "经 ExternalJobSpec.limits.max_wall_seconds 由 Supervisor 强制",
    },
}


def _effective_timeouts(
    *, requested: Mapping[str, Any], multipliers: Mapping[str, float],
) -> dict[str, Any]:
    """请求值 × 倍率 = 实际生效值；倍率非 1.0 时必须能解释差异。

    review R07：平台固定倍率 1.0，因此这里断言 ``effective == requested``——
    任何"接受秒数但没真的执行"的映射都会在创建时暴露，而不是等到运行结束。
    """
    out: dict[str, Any] = {}
    for key, requested_value in requested.items():
        spec = dict(_TIMEOUT_ENFORCEMENT.get(key) or {})
        multiplier = 1.0
        if spec.get("multiplier_field"):
            multiplier = float(multipliers.get(str(spec["multiplier_field"]), 1.0))
        if requested_value is None:
            out[key] = {
                "requested_sec": None,
                "multiplier": multiplier,
                "effective_sec": None,
                "enforced_by": spec.get("enforced_by"),
                "harbor_field": spec.get("harbor_field"),
            }
            if spec.get("note"):
                out[key]["note"] = spec["note"]
            continue
        effective = float(requested_value) * multiplier
        if multiplier == 1.0 and not math.isclose(
            effective, float(requested_value), rel_tol=0.0, abs_tol=1e-9,
        ):
            raise HarborConfigError(
                "HARBOR_TIMEOUT_MAPPING_INVALID",
                f"{key}: multiplier 1.0 must keep the requested value, got "
                f"requested={requested_value} effective={effective}",
            )
        out[key] = {
            "requested_sec": float(requested_value),
            "multiplier": multiplier,
            "effective_sec": effective,
            "enforced_by": spec.get("enforced_by"),
            "harbor_field": spec.get("harbor_field"),
        }
        if spec.get("note"):
            out[key]["note"] = spec["note"]
    return out


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
