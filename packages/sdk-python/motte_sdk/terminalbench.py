"""Terminal-Bench（Harbor）应用门面（M3-T09）。

API / CLI / Web 共用这一处事实源：准备任务、预检、创建 Run、读取
Task→Trial→证据。执行仍由 Worker + Dispatcher + DurableExternalJobRunner
负责，本模块不启动任何任务、不在请求进程里跑模型。

身份：一个 Task = 一个逻辑 Case（``case_id == task_key``），计划的重复
作为 Trial 子记录（``store.trials``）；不使用 ``task/repeat`` 冒充新的 Case。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from motte_benchmark.harbor.config import (
    HARBOR_VERSION,
    agent_and_environment_hashes,
    build_harbor_config,
    plan_trials,
)
from motte_benchmark.harbor.environment import evaluate_preflight, reason_messages
from motte_benchmark.harbor.parser import PARSER_VERSION
from motte_benchmark.harbor.tasks import (
    HarborTaskError,
    environment_digest,
    prepare_task_manifest,
    select_tasks,
)
from motte_contracts.trial import canonical_hash

#: 与 M2 的 "external-benchmark@1" 后端、Harbor Runner 对应。
BENCHMARK_ID = "terminal-bench"
SUITE = "terminal-bench-harbor"
SUITE_VERSION = "1"
SCENARIO_VERSION = "terminal-bench-harbor@1"
ADAPTER_ID = "terminal-bench-harbor"
ADAPTER_VERSION = "1"
RUNNER_VERSION = f"harbor-{HARBOR_VERSION}"
#: 首个受支持环境（上游语义）：Harbor 自己管理任务容器。
DEFAULT_ENVIRONMENT_DIGEST_EXTRA: dict[str, Any] = {}


def prepare_terminal_bench_dataset(
    store: Any,
    *,
    task_root: str,
    source_id: str,
    dataset_revision: str,
    license_id: str | None = None,
    license_evidence: str | None = None,
    source_kind: str = "local",
) -> dict[str, Any]:
    """只读准备受控任务根目录并持久化为不可变 revision。"""
    manifest = prepare_task_manifest(
        task_root, source_id=source_id, dataset_revision=dataset_revision,
        license_id=license_id, license_evidence=license_evidence, source_kind=source_kind,
    )
    # 准备时间不是身份的一部分（同字节重复准备必须幂等），持久化时剔除；
    # 首次准备时间由存储记录的 ``created_at`` 表达，manifest_hash 也不含它。
    prepared_at = manifest.pop("prepared_at", None)
    record = {
        "benchmark_id": BENCHMARK_ID,
        "dataset_revision": dataset_revision,
        "state": "ready",
        "task_root": str(task_root),
        "manifest": manifest,
        "manifest_hash": manifest["manifest_hash"],
        "prepared_at": prepared_at,
        "license_id": license_id,
        "license_evidence": license_evidence,
        "source_kind": source_kind,
    }
    datasets = getattr(store, "benchmark_datasets", None)
    if datasets is not None:
        # 同一 revision 的重复准备：内容相同时存储返回既有记录（created_at 保留），
        # 因此这里再剔除一次 volatile 的准备时间，保证幂等。
        record.pop("prepared_at", None)
        receipt = datasets.put_immutable(record)
        record = dict(receipt.get("record") or record)
    return record


def prepared_dataset(
    store: Any, *, dataset_revision: str | None = None,
) -> dict[str, Any] | None:
    """读取已准备的 revision（省略时取最新）。未准备返回 None。"""
    datasets = getattr(store, "benchmark_datasets", None)
    if datasets is None:
        return None
    record = (
        datasets.get(BENCHMARK_ID, dataset_revision) if dataset_revision
        else datasets.latest(BENCHMARK_ID)
    )
    return dict(record) if record else None


def require_prepared_dataset(store: Any, *, dataset_revision: str | None = None) -> dict[str, Any]:
    record = prepared_dataset(store, dataset_revision=dataset_revision)
    if record is None:
        raise HarborTaskError(
            "DATASET_UNPREPARED",
            "no Terminal-Bench task set has been prepared; run prepare first",
        )
    return record


def manifest_tasks(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    return list((record.get("manifest") or {}).get("tasks") or [])


def task_view(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """任务列表视图：身份 + 非秘密事实（供 API/CLI/Web 共用）。"""
    manifest = record.get("manifest") or {}
    facts = manifest.get("task_facts") or {}
    names = manifest.get("display_names") or {}
    items: list[dict[str, Any]] = []
    for task in manifest.get("tasks") or []:
        key = str(task["task_key"])
        item = dict(facts.get(key) or {})
        items.append({
            "task_key": key,
            "normalized_relative_path": task["normalized_relative_path"],
            "display_name": names.get(key, task["normalized_relative_path"]),
            "source_id": task["source_id"],
            "dataset_revision": task["dataset_revision"],
            "upstream_task_id": task.get("upstream_task_id"),
            "file_count": item.get("file_count"),
            "total_bytes": item.get("total_bytes"),
            "has_tests": item.get("has_tests"),
            "has_solution": item.get("has_solution"),
            "declared_license": item.get("declared_license"),
        })
    return items


def published_model_config(record: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze execution settings before Harbor's shared map-or-reject validation.

    ModelProfile null defaults are unset, not requested native options. Capability
    metadata (such as the context window) is not an execution parameter.
    """
    result: dict[str, Any] = {
        "provider": str(record.get("provider") or ""),
        "model": str(record.get("model") or record.get("id") or ""),
    }
    parameters = {key: value for key, value in (record.get("parameters") or {}).items()
                  if value is not None}
    if parameters:
        result["parameters"] = parameters
    reasoning_level = (record.get("reasoning") or {}).get("default_level")
    if reasoning_level is not None:
        result["reasoning_level"] = reasoning_level
    return result


def terminal_bench_profile(
    *,
    agent_id: str = "oracle",
    agent_version: str = "1.0.0",
    n_trials: int = 1,
    model: Mapping[str, Any] | None = None,
    timeouts: Mapping[str, Any] | None = None,
    resources: Mapping[str, Any] | None = None,
    credentials: Mapping[str, Any] | None = None,
    aggregation: str = "first-trial",
    environment: Mapping[str, Any] | None = None,
    tools: Mapping[str, Any] | None = None,
    limits: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一次性 Profile（allowlist 检查在 build_harbor_config 内完成）。"""
    return {
        "benchmark_id": BENCHMARK_ID,
        "benchmark_version": SUITE_VERSION,
        "agent_id": agent_id,
        "agent_version": agent_version,
        "n_trials": n_trials,
        "model": dict(model or {}),
        "timeouts": dict(timeouts or {}),
        "resources": dict(resources or {}),
        "credentials": dict(credentials or {}),
        "aggregation": aggregation,
        "environment": dict(environment or {"type": "docker", "delete": True}),
        "retries": {"runner": 0, "provider_transport": 0, "operator": 0},
        "tools": dict(tools or {}),
        "limits": dict(limits or {}),
    }


def job_limits(profile: Mapping[str, Any]) -> dict[str, Any]:
    """执行限额：声明多少就执行多少（R07）。

    ``job_sec`` 必须变成 Supervisor 真实读取的 ``max_wall_seconds``；只把
    秒数记进旁路字段会让 UI/API/CLI 接受的期限完全不生效。未声明时不下发
    该键，交由适配器的有限默认上界兜底。
    """
    limits: dict[str, Any] = {"poll_interval_seconds": 0.2}
    timeouts = profile.get("timeouts") or {}
    job_sec = timeouts.get("job_sec") if isinstance(timeouts, Mapping) else None
    if job_sec is not None:
        limits["max_wall_seconds"] = job_sec
    return limits


def preflight_terminal_bench(
    *,
    record: Mapping[str, Any],
    profile: Mapping[str, Any],
    task_keys: Sequence[str] | None = None,
    docker: Mapping[str, Any] | None = None,
    required_images: Sequence[str] = (),
    network_policy: str | None = "allowed",
    verifier_visibility: str | None = "upstream",
    agent_dependencies: Sequence[str] = (),
    installed_agent_dependencies: Sequence[str] = (),
) -> dict[str, Any]:
    """只读预检（零模型调用、零任务启动），返回原因码与可执行说明。"""
    tasks = select_tasks(record.get("manifest") or {}, task_keys=list(task_keys or []) or None)
    facts = (record.get("manifest") or {}).get("task_facts") or {}
    enriched = [
        {"task_key": task["task_key"], "facts": facts.get(task["task_key"]) or {}}
        for task in tasks
    ]
    report = evaluate_preflight(
        profile=profile, tasks=enriched, docker=docker, required_images=required_images,
        network_policy=network_policy, verifier_visibility=verifier_visibility,
        agent_dependencies=agent_dependencies,
        installed_agent_dependencies=installed_agent_dependencies,
        harbor_version=HARBOR_VERSION,
    )
    report["messages"] = reason_messages(report["reason_codes"])
    report["tasks"] = [task["task_key"] for task in tasks]
    report["dataset_revision"] = record.get("dataset_revision")
    return report


def frozen_task_files(
    manifest: Mapping[str, Any], tasks: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    """选中任务的文件 hash 清单：``{task_key: {相对路径: sha256}}``。

    冻结在 manifest 里之后，执行侧的不可变副本可以在启动前逐字节复验，
    "排队后任务文件被改动"必须在启动前被拒绝，而不是照旧执行。
    """
    hashes = manifest.get("file_hashes") or {}
    files: dict[str, dict[str, str]] = {}
    for task in tasks:
        content_hash = str(task.get("task_content_hash") or "")
        per_task = hashes.get(content_hash)
        if not isinstance(per_task, Mapping) or not per_task:
            raise HarborTaskError(
                "TASK_FILE_HASHES_MISSING",
                "prepared manifest has no file hashes for a selected task; "
                "re-prepare the task set before freezing a run",
            )
        files[str(task["task_key"])] = {
            str(rel): str(digest) for rel, digest in per_task.items()
        }
    return files


def build_run_inputs(
    *,
    record: Mapping[str, Any],
    run_id: str,
    job_id: str,
    profile: Mapping[str, Any],
    task_keys: Sequence[str] | None = None,
    work_root: str = "jobs",
) -> dict[str, Any]:
    """冻结 manifest 与 Runner 配置（不启动任何东西）。"""
    manifest = record.get("manifest") or {}
    tasks = select_tasks(manifest, task_keys=list(task_keys or []) or None)
    task_keys = [str(task["task_key"]) for task in tasks]
    task_files = frozen_task_files(manifest, tasks)

    agent_hash, environment_hash = agent_and_environment_hashes(profile)
    plans = plan_trials(
        run_id=run_id, tasks=tasks, repeats=int(profile["n_trials"]),
        agent_config_hash=agent_hash, environment_hash=environment_hash,
    )
    native = build_harbor_config(
        run_id=run_id, job_id=job_id, work_root=work_root, tasks=tasks, plans=plans,
        profile=profile, dataset_revision=str(record.get("dataset_revision")),
    )
    plan_payload = {
        "schema": "motte-harbor-plan@1",
        "tasks": [
            {
                "task_key": task["task_key"],
                "normalized_relative_path": task["normalized_relative_path"],
                "source_id": task["source_id"],
            }
            for task in tasks
        ],
        "trials": plans,
        "planned_trial_count": len(plans),
        "dataset_revision": record.get("dataset_revision"),
        # 执行侧据此物化并复验不可变任务副本（R03）：只冻结相对路径不足
        # 以防"排队后任务文件被改动"。
        "task_files": task_files,
        "task_content_hashes": {
            str(task["task_key"]): str(task["task_content_hash"]) for task in tasks
        },
    }
    digest = environment_digest(
        harbor_version=HARBOR_VERSION,
        terminal_bench_revision=str(record.get("dataset_revision")),
        # 计划里冻结的"环境"是宿主平台 + 容器环境类型；这里记录容器平台形态，
        # 真实镜像摘要由 Runner 上报的 preflight 报告补充（缺省即 unknown）。
        docker_platform=native["job"]["environment"].get("type"),
        image_digest=None,
        agent_id=str(profile["agent_id"]),
        agent_version=str(profile["agent_version"]),
        extra={
            "platform_custom_profile": False,
            "deployment_topology": native["deployment_topology"],
        },
    )
    external = {
        "adapter_id": ADAPTER_ID,
        "adapter_version": ADAPTER_VERSION,
        "runner_version": RUNNER_VERSION,
        "dataset_revision": str(record.get("dataset_revision")),
        "environment_digest": digest,
        "profile": dict(profile),
        # 期限是**执行上界**，不是显示值：``max_wall_seconds`` 由 Supervisor
        # 强制（超时中断 + 保留部分证据），秒数本身不落旁路。
        "limits": job_limits(profile),
        "parser_version": PARSER_VERSION,
        "runner_config": {
            "harbor": native,
            "plan": plan_payload,
            "tasks": [
                {
                    "task_key": task["task_key"],
                    "normalized_relative_path": task["normalized_relative_path"],
                }
                for task in tasks
            ],
            "trials": plans,
        },
    }
    frozen_manifest = {
        "benchmark_provenance": {
            "suite": SUITE,
            "plugin_version": SUITE_VERSION,
            "id": BENCHMARK_ID,
            "version": HARBOR_VERSION,
            "dataset_revision": record.get("dataset_revision"),
            "selected_count": len(task_keys),
            "aggregation": profile.get("aggregation", "first-trial"),
            "parser_version": PARSER_VERSION,
            "harbor_version": HARBOR_VERSION,
            "manifest_hash": record.get("manifest_hash"),
            "trace": {
                "task_root": record.get("task_root"),
                "source_kind": record.get("source_kind"),
                "license_id": record.get("license_id"),
                "license_evidence": record.get("license_evidence"),
            },
        },
        "evaluation": {
            "suite": SUITE,
            "plugin_version": SUITE_VERSION,
            "scorer": "harbor-terminal-bench",
            "scorer_version": PARSER_VERSION,
        },
        "execution": {"backend_id": "external-benchmark", "backend_version": "1"},
        "external_benchmark": external,
        "case_expectations": {},
        # 任务内容身份进入比较资格（R09）：同 revision 字符串相同不证明内容相同。
        "case_content_hashes": {
            str(task["task_key"]): str(task["task_content_hash"]) for task in tasks
        },
        "selected_tasks": task_keys,
        "task_manifest": {
            "source_id": manifest.get("source_id"),
            "dataset_revision": manifest.get("dataset_revision"),
            "manifest_hash": record.get("manifest_hash"),
            "task_keys": task_keys,
            # 受控相对路径随 manifest 冻结：视图不需要再读任务根目录。
            "task_resources": [
                {
                    "task_key": task["task_key"],
                    "normalized_relative_path": task["normalized_relative_path"],
                }
                for task in tasks
            ],
            "trials": plans,
        },
    }
    return {
        "scenario_version": SCENARIO_VERSION,
        "manifest": frozen_manifest,
        "case_ids": task_keys,
        "trials": plans,
        "native_config": native,
        "manifest_hash": canonical_hash(frozen_manifest),
    }


# ------------------------------------------------------------------ 重新冻结


def refreeze_for_run(
    manifest: Mapping[str, Any], *, run_id: str, job_id: str,
) -> dict[str, Any]:
    """把冻结 manifest 的 **Run 作用域身份**重新派生给新 Run（review R2-02）。

    retry 不能照抄父 Run 的 TrialPlan：``trial_id`` 由 ``run_id`` 派生，照抄会
    让子 Run 的结果导入到父 Trial 名下（父证据被"identical"或冲突），子 Run
    自己没有 Trial。这里保留任务内容、Profile 与实验条件（它们是 run-independent
    的），只重新生成 TrialPlan、原生 Job 身份与所有引用它的字段。

    非 Trial 形态的 manifest 原样返回（M2 套件不涉及 Trial 身份）。
    """
    external = dict(manifest.get("external_benchmark") or {})
    runner_config = dict(external.get("runner_config") or {})
    plan = dict(runner_config.get("plan") or {})
    tasks = list(plan.get("tasks") or [])
    profile = dict(external.get("profile") or {})
    if not tasks or not profile:
        return dict(manifest)

    agent_hash, environment_hash = agent_and_environment_hashes(profile)
    plans = plan_trials(
        run_id=run_id, tasks=tasks, repeats=int(profile.get("n_trials") or 1),
        agent_config_hash=agent_hash, environment_hash=environment_hash,
        seed=profile.get("seed") if isinstance(profile.get("seed"), int) else None,
    )
    native = build_harbor_config(
        run_id=run_id, job_id=job_id,
        # work_root 是冻结配置里的受控相对目录（run-independent）。
        work_root=str((runner_config.get("harbor") or {}).get("job", {}).get("jobs_dir") or "jobs"),
        tasks=tasks, plans=plans, profile=profile,
        dataset_revision=str(external.get("dataset_revision") or ""),
    )
    plan_payload = {
        **plan,
        "trials": plans,
        "planned_trial_count": len(plans),
        "dataset_revision": external.get("dataset_revision"),
    }
    runner_config.update({"harbor": native, "plan": plan_payload, "trials": plans})
    external["runner_config"] = runner_config
    frozen = dict(manifest)
    frozen["external_benchmark"] = external
    task_manifest = dict(frozen.get("task_manifest") or {})
    task_manifest["trials"] = plans
    frozen["task_manifest"] = task_manifest
    return frozen


# ------------------------------------------------------------------ 读取视图


def task_rows(store: Any, run_id: str, *, manifest: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Task → Trial 汇总视图（从 Trial 存储重建，不重跑任何任务）。"""
    from motte_eval.harbor import coverage_gate, task_aggregate

    trials = list(store.trials.list_for_run(run_id)) if store.trials is not None else []
    # 身份以**冻结计划**为准（结果的载荷可能不完整），避免用结果里的字段
    # 反推任务身份而把行分到空 task_key。
    payloads = [
        {
            **(row.get("result") or {}),
            "trial_id": row["trial_id"],
            "task_key": row["task_key"],
            "repeat_index": row.get("repeat_index"),
        }
        for row in trials if row.get("result")
    ]
    planned = (manifest or {}).get("task_manifest", {}).get("trials") or []
    planned_per_task: dict[str, int] = {}
    for plan in planned:
        planned_per_task[str(plan["task_key"])] = planned_per_task.get(
            str(plan["task_key"]), 0,
        ) + 1
    plan_paths = {
        str(task["task_key"]): task.get("normalized_relative_path")
        for task in ((manifest or {}).get("task_manifest", {}).get("task_resources") or [])
    }
    aggregation = str(
        ((manifest or {}).get("benchmark_provenance") or {}).get("aggregation")
        or "first-trial",
    )
    aggregate = task_aggregate(
        trials=payloads, aggregation=aggregation, planned_per_task=planned_per_task,
    )
    gate = coverage_gate(aggregate)
    # 覆盖门禁与成本随聚合一起返回：API/CLI/Web 读同一份结论，不各自重算。
    from motte_eval.harbor import cost_summary, duration_summary

    aggregate = {**aggregate, "gate": gate, "cost": cost_summary(payloads),
                 "durations": duration_summary(payloads)}
    rows: list[dict[str, Any]] = []
    for task_key, item in sorted(aggregate["per_task"].items()):
        task_trial_rows = [
            row for row in trials if str(row.get("task_key")) == task_key
        ]
        valid = sum(1 for row in task_trial_rows if (row.get("result") or {}).get("verifier_observation"))
        rows.append({
            "task_key": task_key,
            "display_name": task_key,
            "normalized_relative_path": plan_paths.get(task_key),
            **item,
            # Task 层通过率单独给出（分母 = 该 Task 的**有效** Trial 数），
            # 与整体覆盖分开，避免"任务少但 Trial 多"时读错分母。
            "valid_trial_pass_rate": (
                round(item["passed_trials"] / item["valid_trials"], 6)
                if item["valid_trials"] else None
            ),
            "observed_with_verifier": valid,
            "aggregate": aggregate,
            "gate": gate,
        })
    return rows


def trial_rows(store: Any, run_id: str, task_key: str) -> list[dict[str, Any]]:
    """某 Task 的全部计划 Trial（含尚未产出结果的）。"""
    from motte_eval.harbor import trial_row

    if store.trials is None:
        return []
    rows: list[dict[str, Any]] = []
    for row in store.trials.list_for_task(run_id, task_key):
        result = row.get("result")
        if result is None:
            plan = row.get("plan") or {}
            rows.append({
                "trial_id": row["trial_id"],
                "repeat_index": row.get("repeat_index"),
                "disposition": "pending",
                "verifier_status": "pending",
                "reward": None,
                "valid": False,
                "coverage": {},
                "source_trial_id": None,
                "planned": True,
                "status": row.get("status"),
                "seed": plan.get("seed"),
            })
            continue
        scored = trial_row(result)
        rows.append({
            "trial_id": row["trial_id"],
            "repeat_index": row.get("repeat_index"),
            "disposition": result.get("disposition"),
            "verifier_status": (result.get("verifier_observation") or {}).get("status"),
            "reward": scored["value"],
            "valid": scored["judged"],
            "coverage": result.get("coverage") or {},
            "source_trial_id": result.get("source_trial_id"),
            "planned": True,
            "status": row.get("status"),
            "seed": (row.get("plan") or {}).get("seed"),
        })
    return rows


def trial_detail(store: Any, run_id: str, trial_id: str) -> dict[str, Any] | None:
    """单 Trial 详情：终止、Verifier、证据引用与完整度（严格校验 run 归属）。

    公共展示边界：错误、日志等**内容**字段复用既有脱敏（review R08），身份与
    hash（``trial_id`` / ``task_key`` / ``sha256`` / ``artifact_id``）逐字保留，
    绝不用"改写身份"的方式假装脱敏。
    """
    if store.trials is None:
        return None
    row = store.trials.get(trial_id)
    if row is None or str(row.get("run_id")) != run_id:
        return None
    result = row.get("result") or {}
    observation = result.get("verifier_observation") or {}
    refs = list(result.get("artifact_refs") or [])
    terminal = next(
        (ref for ref in refs if ref.get("kind") in ("harbor-trial-log", "harbor-agent-log")),
        None,
    )
    detail = {
        "run_id": run_id,
        "trial_id": trial_id,
        "task_key": row.get("task_key"),
        "repeat_index": row.get("repeat_index"),
        "disposition": result.get("disposition") or "pending",
        "termination": result.get("termination") or {},
        "verifier_observation": observation,
        "usage": result.get("usage") or {},
        "coverage": result.get("coverage") or {},
        "artifacts": refs,
        "evidence_complete": all(ref.get("complete") for ref in refs) if refs else False,
        "terminal_ref": terminal,
        "source_hash": row.get("source_hash"),
        "parser_version": row.get("parser_version"),
    }
    if terminal is not None:
        # 终端文本走同一受控读取（归属 → hash → 预算 → 脱敏），UI 不再"只有引用"。
        detail["terminal"] = trial_terminal_text(store, run_id, trial_id)
    return redact_display_text(detail)


# ------------------------------------------------------------------ 内容读取

#: 单次内容读取的文本预算（超出只给截断标记与完整大小，不静默丢尾部）。
MAX_DISPLAY_TEXT = 64 * 1024

#: 内容字段名特征：只脱敏这些字段，身份/hash 字段逐字保留（review R08）。
_DISPLAY_TEXT_MARKERS = (
    "message", "error", "detail", "stdout", "stderr", "log", "note", "reason",
    "exception", "traceback", "output", "text", "payload", "command", "arguments",
)


def redact_display_text(value: Any) -> Any:
    """展示边界脱敏：递归处理内容字段，身份/hash 字段原样返回。

    不能对整个 detail 直接调用 ``redact_secrets``：它的键名规则会把
    ``task_key`` 这类**身份**字段也替换成 ``[REDACTED]``，等于改写身份。
    """
    from motte_trace.redaction import redact_secrets

    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key).lower()
            if any(marker in name for marker in _DISPLAY_TEXT_MARKERS):
                redacted[key] = redact_secrets(item)
            else:
                redacted[key] = redact_display_text(item)
        return redacted
    if isinstance(value, list):
        return [redact_display_text(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value)
    return value


def trial_artifact_ref(store: Any, run_id: str, trial_id: str, artifact_id: str) -> dict[str, Any] | None:
    """在 Trial 的证据引用里定位一个 Artifact（严格校验 Run/Trial 归属）。"""
    row = store.trials.get(trial_id) if store.trials is not None else None
    if row is None or str(row.get("run_id")) != run_id:
        return None
    for ref in (row.get("result") or {}).get("artifact_refs") or []:
        if str(ref.get("artifact_id") or "") == artifact_id:
            return dict(ref)
    return None


class FrozenEvidenceReader:
    """从**冻结证据**读取单个工件，不依赖可被清理的工作目录（review R11/R19）。

    Trial 引用里的 ``artifact_id`` 是冻结 bundle 内的相对路径（文本类证据），
    也可能是内容寻址的独立 Artifact（二进制证据）。两条路径都只读冻结副本，
    因此"工作目录被删掉"之后详情仍然可读；内容 hash 与引用不符时如实失败。
    """

    def __init__(self, store: Any, run_id: str, artifacts: Any = None) -> None:
        self._store = store
        self._run_id = run_id
        self._artifacts = artifacts
        self._bundle: dict[str, Any] | None = None
        self.job_id: str | None = None
        self.bundle_artifact: str | None = None
        jobs = getattr(store, "external_jobs", None)
        if jobs is not None:
            for record in reversed(jobs.jobs_for_run(run_id)):
                evidence = (record.get("checkpoint") or {}).get("evidence") or {}
                artifact = evidence.get("raw_bundle_artifact")
                if artifact:
                    self.job_id = str(record.get("job_id") or "")
                    self.bundle_artifact = str(artifact)
                    break

    def artifacts(self) -> Any:
        if self._artifacts is None:
            import os

            from motte_storage.artifacts import ArtifactStore

            self._artifacts = ArtifactStore(os.environ.get("ARTIFACT_ROOT", "var/artifacts"))
        return self._artifacts

    def _files(self) -> dict[str, Any]:
        if self._bundle is None:
            self._bundle = {}
            if self.bundle_artifact:
                import json

                try:
                    raw = self.artifacts().read_bytes(self.bundle_artifact)
                    payload = json.loads(raw.decode("utf-8"))
                except (OSError, ValueError, UnicodeDecodeError):
                    payload = {}
                files = payload.get("files")
                if isinstance(files, Mapping):
                    self._bundle = dict(files)
        return self._bundle

    def read_bytes(self, artifact_id: str) -> bytes:
        """按引用读取字节：内容寻址 Artifact 优先，其次冻结 bundle 内的路径。"""
        if artifact_id.startswith("external-jobs/"):
            return self.artifacts().read_bytes(artifact_id)
        entry = self._files().get(artifact_id)
        if not isinstance(entry, Mapping):
            raise FileNotFoundError(f"artifact {artifact_id} is not in the frozen evidence")
        content = entry.get("content")
        if not isinstance(content, str):
            raise ValueError(
                "frozen evidence keeps only the hash for this file; bytes are not stored"
            )
        return content.encode("utf-8")


def evidence_reader(store: Any, run_id: str, artifacts: Any = None) -> FrozenEvidenceReader:
    return FrozenEvidenceReader(store, run_id, artifacts)


def read_artifact_text(reader: Any, ref: Mapping[str, Any], *, max_bytes: int = MAX_DISPLAY_TEXT) -> dict[str, Any]:
    """按引用读取工件内容：校验 hash → 有界解码 → 脱敏，不返回超预算正文。

    ``reader`` 需要提供 ``read_bytes(artifact_id)``（``FrozenEvidenceReader``）；
    缺失或 hash 不符时返回 ``verified=False``，而不是返回一段无法验证的文本。
    """
    artifact_id = str(ref.get("artifact_id") or "")
    payload: dict[str, Any] = {
        "artifact_id": artifact_id,
        "kind": ref.get("kind"),
        "sha256": ref.get("sha256"),
        "size_bytes": ref.get("size_bytes"),
        "media_type": ref.get("media_type"),
        "source_path": ref.get("source_path"),
        "encoding": None,
        "text": None,
        "truncated": False,
        "verified": False,
        "note": None,
    }
    if not artifact_id:
        payload["note"] = "artifact reference has no readable content (hash only)"
        return payload
    if reader is None:
        payload["note"] = "no evidence reader is available in this process"
        return payload
    try:
        data = reader.read_bytes(artifact_id)
    except FileNotFoundError as error:
        payload["note"] = str(error)
        return payload
    except ValueError as error:
        payload["note"] = str(error)
        return payload
    except (OSError, KeyError) as error:
        payload["note"] = f"artifact content unavailable: {type(error).__name__}"
        return payload
    expected = str(ref.get("sha256") or "").removeprefix("sha256:")
    if expected:
        import hashlib

        if hashlib.sha256(data).hexdigest() != expected:
            payload["note"] = "artifact content hash differs from the frozen reference"
            return payload
        payload["verified"] = True
    else:
        payload["verified"] = None
    if len(data) > max_bytes:
        # 预算边界可能切在多字节字符中间：先按预算截断，再回退到**完整字符
        # 边界**（review R2-13）。直接严格 decode 会把一个合法的 UTF-8 长日志
        # 整体判成"二进制"，那是把预算问题伪装成编码问题。
        data = _truncate_utf8(data, max_bytes)
        payload["truncated"] = True
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        payload["encoding"] = "binary"
        payload["note"] = payload["note"] or (
            "content is not valid UTF-8; the frozen artifact can be exported through "
            "the controlled raw-export path"
        )
        return payload
    payload["encoding"] = "utf-8"
    from motte_trace.redaction import redact_secrets

    payload["text"] = redact_secrets(text)
    if payload["note"] is not None:
        # note 是展示字段（可能带路径/上游消息）：与 text 同一脱敏边界。
        payload["note"] = redact_secrets(str(payload["note"]))
    return payload


def _truncate_utf8(data: bytes, max_bytes: int) -> bytes:
    """按预算截断但保留完整字符边界（增量解码，丢弃结尾的不完整序列）。"""
    import codecs

    prefix = data[:max_bytes]
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        # The budget counts consumed bytes, including any incomplete final code
        # point retained by the decoder. Never read beyond it to complete a word.
        return decoder.decode(prefix, final=False).encode("utf-8")
    except UnicodeDecodeError:
        # Preserve genuinely invalid input for the caller's binary classification.
        return prefix


def trial_terminal_text(
    store: Any, run_id: str, trial_id: str, *, reader: Any = None,
    max_bytes: int = MAX_DISPLAY_TEXT,
) -> dict[str, Any] | None:
    """Trial 的终端/日志文本（有界 + 脱敏），供 CLI/Web 直接消费。"""
    row = store.trials.get(trial_id) if store.trials is not None else None
    if row is None or str(row.get("run_id")) != run_id:
        return None
    refs = (row.get("result") or {}).get("artifact_refs") or []
    terminal = next(
        (dict(item) for item in refs
         if item.get("kind") in ("harbor-trial-log", "harbor-agent-log")),
        None,
    )
    if terminal is None:
        return None
    return read_artifact_text(reader or evidence_reader(store, run_id), terminal, max_bytes=max_bytes)


def trial_artifact_content(
    store: Any, run_id: str, trial_id: str, artifact_id: str, *, reader: Any = None,
) -> dict[str, Any] | None:
    """按 Artifact 身份读取 Trial 证据内容（归属 → hash → 预算 → 脱敏）。"""
    ref = trial_artifact_ref(store, run_id, trial_id, artifact_id)
    if ref is None:
        return None
    return read_artifact_text(reader or evidence_reader(store, run_id), ref)
