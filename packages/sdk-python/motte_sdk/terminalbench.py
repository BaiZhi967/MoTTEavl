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
        "tools": {},
        "limits": {},
    }


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
        "limits": {"poll_interval_seconds": 0.2},
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
    """单 Trial 详情：终止、Verifier、证据引用与完整度（严格校验 run 归属）。"""
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
    return {
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
