"""Agent file-task suite: dataset publication, run snapshot, scoring, aggregate.

prepare 在创建期冻结 selected-case 快照（含隐藏断言，只留在评分侧）；
score 只读已落库的 case result（冻结 Observation），不发生任何模型调用；
aggregate 按指标聚合并声明分母。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from motte_contracts.agent_tasks import (
    AGENT_BACKEND_ID,
    AGENT_BACKEND_VERSION,
    AGENT_MODES,
    PLUGIN_VERSION,
    SCENARIO_MODE,
    SCORER_ID,
    SCORER_VERSION,
    SUITE,
    agent_task_metrics,
    case_ids_sha256,
    normalize_agent_tasks_dataset,
    scenario_for_agent_tasks,
)
from motte_contracts.identity import canonical_sha256
from motte_contracts.selection import CASE_SELECTION_KEY, RUN_SELECTION_KEY, select_cases

SNAPSHOT_KEY = "benchmark_snapshot"
PROVENANCE_KEY = "benchmark_provenance"
SNAPSHOT_CONTENT_HASH_KEY = "selected_content_sha256"
RESERVED_KEYS = frozenset({SNAPSHOT_KEY, PROVENANCE_KEY, "benchmark_cases", "cases", "tools"})
MAX_AUTO_VERSION_ATTEMPTS = 256

DEFAULT_RUN_BUDGET = {"max_steps": 8, "max_tool_calls": 16, "wall_time_sec": 120.0}


class AgentTasksSnapshotIntegrityError(ValueError):
    """持久化 agent 快照缺失或与内容身份不符。"""

    code = "AGENT_TASKS_SNAPSHOT_INTEGRITY"

    def __init__(self, reason: str) -> None:
        super().__init__(f"agent-tasks snapshot integrity check failed: {reason}")
        self.evidence = {"code": self.code, "reason": reason}


def persist_agent_tasks_dataset(
    record: dict[str, Any], resources: Any, *, version: str | None = None,
) -> dict[str, Any]:
    """发布不可变 agent 任务数据集 + 场景（合成 fixture；无外部来源治理要求）。"""
    from motte_sdk.datasets import next_dataset_version
    from motte_storage.resource_store import ResourceConflictError

    candidate = normalize_agent_tasks_dataset(record)
    fingerprint = candidate["dataset_fingerprint"]
    auto_version = version is None
    retry_version: str | None = None
    for _ in range(MAX_AUTO_VERSION_ATTEMPTS):
        target_version = version or retry_version or next_dataset_version(candidate, resources)
        existing = resources.datasets.get(candidate["name"], target_version)
        published = (
            existing
            if existing is not None and existing.get("dataset_fingerprint") == fingerprint
            else {**candidate, "version": target_version}
        )
        published = normalize_agent_tasks_dataset(published)
        scenario = scenario_for_agent_tasks(published)
        try:
            resources.publish_dataset_scenario(published, scenario)
        except ResourceConflictError:
            if not auto_version:
                raise
            occupied = [
                int(item["version"])
                for repository in (resources.datasets, resources.scenarios)
                for item in repository.list()
                if item.get("name") == candidate["name"]
                and isinstance(item.get("version"), str)
                and item["version"].isdigit()
            ]
            retry_version = str(max(occupied, default=0) + 1)
            continue
        return {
            "imported": f"{published['name']}@{target_version}",
            "scenario": f"{scenario['name']}@{scenario['version']}",
            "suite": SUITE,
            "plugin_version": PLUGIN_VERSION,
            "cases": len(published["cases"]),
            "cases_sha256": published["cases_sha256"],
            "dataset_fingerprint": fingerprint,
        }
    raise ResourceConflictError("could not allocate an atomic agent-tasks dataset/scenario version")


def _snapshot_content_sha256(snapshot: dict[str, Any]) -> str:
    payload = {key: deepcopy(value) for key, value in snapshot.items()
               if key != SNAPSHOT_CONTENT_HASH_KEY}
    return canonical_sha256(payload)


def resolve_agent_tasks_manifest(
    scenario: dict[str, Any], manifest: dict[str, Any], resources: Any,
) -> dict[str, Any]:
    """插件 prepare：校验模式/模型能力/预算，冻结 selected-case 快照。"""
    name, separator, version = str(scenario["dataset"]).rpartition("@")
    if not separator:
        raise ValueError("scenario dataset must be a name@version reference")
    stored = resources.datasets.get(name, version)
    if stored is None:
        raise ValueError(f"dataset not found: {scenario['dataset']}")
    dataset = normalize_agent_tasks_dataset(stored)
    if RESERVED_KEYS.intersection(manifest):
        raise ValueError("agent-tasks snapshots and tools cannot be overridden")

    agent_request = manifest.get("agent") or {}
    if not isinstance(agent_request, dict):
        raise ValueError("manifest.agent must be an object")
    mode = agent_request.get("mode", "legacy-json")
    if mode not in AGENT_MODES:
        raise ValueError(f"unsupported agent mode: {mode!r} (known: {', '.join(AGENT_MODES)})")
    model_ref = manifest.get("model")
    if not isinstance(model_ref, str) or not model_ref:
        raise ValueError("agent-tasks runs require a published model profile reference")

    # native-tool 创建期拒绝：不支持 tools 的模型在付费调用之前失败（M1-G03/A06）
    if mode == "native-tool":
        profile = resources.models.get(model_ref)
        if profile is None:
            raise ValueError(f"model not found: {model_ref}")
        if profile.get("lifecycle") == "draft":
            raise ValueError(f"model is not published: {model_ref}")
        if profile.get("supports_tools") is False:
            raise ValueError(
                f"model {model_ref} does not support tools (supports_tools=false); "
                "native-tool mode cannot run"
            )

    from motte_agent.budget import BudgetConfigError, ExecutionBudget

    run_budget = {**DEFAULT_RUN_BUDGET, **(agent_request.get("budget") or {})}
    try:
        ExecutionBudget.from_config(run_budget)
    except BudgetConfigError as error:
        raise ValueError(f"invalid agent budget: {error}") from error

    selection = select_cases(dataset, manifest.get(CASE_SELECTION_KEY))
    selection["case_ids_sha256"] = case_ids_sha256(selection["case_ids"])
    by_id = {case["case_id"]: case for case in dataset["cases"]}
    selected_cases = [deepcopy(by_id[case_id]) for case_id in selection["case_ids"]]

    resolved = deepcopy(manifest)
    agent_config = resolved.setdefault("agent_config", {})
    agent_config["mode"] = mode
    agent_config["prompt_version"] = (
        "builtin-react-native@1" if mode == "native-tool" else "builtin-react-legacy@2"
    )
    agent_config["budget"] = run_budget
    agent_config["tools"] = ["list_files", "read_file", "write_file"]
    resolved["agent"] = f"{AGENT_BACKEND_ID}@{AGENT_BACKEND_VERSION}"
    resolved["cases"] = {
        case["case_id"]: {"case_id": case["case_id"], "prompt": case["input"]}
        for case in selected_cases
    }
    resolved[CASE_SELECTION_KEY] = {
        key: value for key, value in selection.items() if key != "case_ids"
    }
    snapshot = {
        "schema_version": 1,
        "suite": SUITE,
        "scenario": {
            "name": scenario["name"], "version": scenario["version"],
            "plugin_version": PLUGIN_VERSION,
        },
        "dataset": {
            "ref": scenario["dataset"], "name": dataset["name"],
            "version": dataset["version"],
            "fingerprint": dataset["dataset_fingerprint"],
            "cases_sha256": dataset["cases_sha256"],
            "total_cases": len(dataset["cases"]),
        },
        "selection": deepcopy(selection),
        "selected_cases": selected_cases,
    }
    snapshot[SNAPSHOT_CONTENT_HASH_KEY] = _snapshot_content_sha256(snapshot)
    resolved[SNAPSHOT_KEY] = snapshot
    resolved[PROVENANCE_KEY] = {
        "suite": SUITE,
        "plugin_version": PLUGIN_VERSION,
        "id": dataset["name"],
        "version": dataset["version"],
        "dataset": scenario["dataset"],
        "dataset_fingerprint": dataset["dataset_fingerprint"],
        "cases_sha256": dataset["cases_sha256"],
        "selected_count": len(selected_cases),
        "mode": mode,
        "prompt_version": agent_config["prompt_version"],
        "scorer": SCORER_ID,
        "scorer_version": SCORER_VERSION,
        "max_retries": 0,
        RUN_SELECTION_KEY: {key: value for key, value in selection.items()
                            if key != "case_ids"},
    }
    return resolved


def selected_agent_cases_from_snapshot(run: dict[str, Any]) -> list[dict[str, Any]]:
    """校验持久化身份后返回 selected cases（隐藏断言只在评分侧展开）。"""
    manifest = run.get("manifest")
    snapshot = manifest.get(SNAPSHOT_KEY) if isinstance(manifest, dict) else None
    if not isinstance(snapshot, dict) or snapshot.get("suite") != SUITE:
        raise AgentTasksSnapshotIntegrityError("run has no agent-tasks snapshot")
    supplied_hash = snapshot.get(SNAPSHOT_CONTENT_HASH_KEY)
    if not isinstance(supplied_hash, str) or not supplied_hash:
        raise AgentTasksSnapshotIntegrityError(f"snapshot is missing {SNAPSHOT_CONTENT_HASH_KEY}")
    if supplied_hash != _snapshot_content_sha256(snapshot):
        raise AgentTasksSnapshotIntegrityError("snapshot content hash mismatch")
    selected = snapshot.get("selected_cases")
    selection = snapshot.get("selection")
    if not isinstance(selected, list) or not isinstance(selection, dict):
        raise AgentTasksSnapshotIntegrityError("snapshot identity fields are invalid")
    from motte_contracts.agent_tasks import AgentTaskCase

    try:
        cases = [AgentTaskCase.model_validate(item).model_dump(mode="json") for item in selected]
    except Exception as error:  # noqa: BLE001
        raise AgentTasksSnapshotIntegrityError(f"selected cases are invalid: {error}") from error
    ids = [case["case_id"] for case in cases]
    if len(set(ids)) != len(ids):
        raise AgentTasksSnapshotIntegrityError("duplicate case_id in selected-case snapshot")
    if ids != selection.get("case_ids") or case_ids_sha256(ids) != selection.get("case_ids_sha256"):
        raise AgentTasksSnapshotIntegrityError("selected-case snapshot does not match selection")
    if run.get("case_ids") != ids:
        raise AgentTasksSnapshotIntegrityError("run case_ids do not match the snapshot order")
    expected_provider_cases = {
        case["case_id"]: {"case_id": case["case_id"], "prompt": case["input"]}
        for case in cases
    }
    if manifest.get("cases") != expected_provider_cases:
        raise AgentTasksSnapshotIntegrityError("provider case projection mismatch")
    return cases


def _artifact_reader_from_env() -> Any:
    import os
    from pathlib import Path

    from motte_storage.artifacts import ArtifactStore

    return ArtifactStore(Path(os.environ.get("ARTIFACT_ROOT", "var/artifacts")))


def agent_tasks_scores(run: dict[str, Any], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从冻结 Observation 评分：零模型调用；case 缺结果时产生可解释缺口行。"""
    from motte_eval.observation import evaluate_observation, metric_result_to_score

    cases = selected_agent_cases_from_snapshot(run)
    selected_ids = {case["case_id"] for case in cases}
    rows: dict[str, dict[str, Any]] = {}
    for row in results:
        case_id = row.get("case_id") if isinstance(row, dict) else None
        if not isinstance(case_id, str) or case_id not in selected_ids:
            raise ValueError(f"result case_id is not selected: {case_id!r}")
        if case_id in rows:
            raise ValueError(f"duplicate result case_id: {case_id}")
        rows[case_id] = row

    artifact_store = _artifact_reader_from_env()
    scores: list[dict[str, Any]] = []
    for case in cases:
        case_id = case["case_id"]
        metrics = agent_task_metrics(case)
        row = rows.get(case_id)
        observation = row.get("result", {}).get("observation") if row else None
        if row is None or row.get("outcome") == "not_attempted" or not isinstance(observation, dict):
            reason = "case_not_attempted" if (row is None or row.get("outcome") == "not_attempted") \
                else "observation_missing"
            for metric in metrics:
                score = metric_result_to_score(_gap_metric(metric, reason), case_id)
                scores.append(score)
            continue
        from motte_contracts.evaluation import (
            FrozenObservation,
            observation_evidence_hash,
        )

        try:
            frozen = FrozenObservation.model_validate(observation)
        except Exception:  # noqa: BLE001 - 观察损坏按缺证据处理
            for metric in metrics:
                scores.append(metric_result_to_score(
                    _gap_metric(metric, "observation_invalid"), case_id,
                ))
            continue
        # #10：重算 evidence_hash 并校验归属——篡改 run/case/workspace/产物清单
        # 而保留旧 hash 的观察一律拒评
        hash_payload = {
            key: value for key, value in observation.items()
            if key not in {"evidence_hash", "recorded_at"}
        }
        ownership_error: str | None = None
        if observation_evidence_hash(hash_payload) != observation.get("evidence_hash"):
            ownership_error = "observation_hash_mismatch"
        elif frozen.run_id != run.get("id"):
            ownership_error = "observation_run_mismatch"
        elif frozen.case_id != case_id:
            ownership_error = "observation_case_mismatch"
        if ownership_error is not None:
            for metric in metrics:
                scores.append(metric_result_to_score(
                    _gap_metric(metric, ownership_error), case_id,
                ))
            continue
        evaluated = evaluate_observation(
            frozen, {"metrics": metrics},
            artifact_reader=artifact_store.read_bytes,
        )
        for metric in evaluated:
            scores.append(metric_result_to_score(metric, case_id))
    return scores


def _gap_metric(metric: dict[str, Any], reason: str):  # noqa: ANN001
    from motte_contracts.evaluation import MetricResult, MetricStatus

    return MetricResult(
        metric_id=metric["metric_id"],
        status=MetricStatus.insufficient_evidence,
        evaluator_id=SCORER_ID, evaluator_version=SCORER_VERSION,
        reason=reason, denominator=False,
    )


def aggregate_agent_tasks(run: dict[str, Any], scores: list[dict[str, Any]]) -> dict[str, Any]:
    from motte_contracts.evaluation import MetricResult, MetricStatus

    cases = selected_agent_cases_from_snapshot(run)
    reconstructed: list[MetricResult] = []
    for score in scores:
        status_raw = score.get("metric_status")
        if not status_raw:
            status_raw = (
                "scored" if (score.get("passed") is not None or score.get("value") is not None)
                else "not_applicable"
            )
        reconstructed.append(MetricResult(
            metric_id=score.get("metric_id") or "legacy",
            status=MetricStatus(status_raw),
            value=score.get("value"),
            passed=score.get("passed"),
            evaluator_id=score.get("evaluator_id") or SCORER_ID,
            evaluator_version=score.get("evaluator_version") or SCORER_VERSION,
        ))
    results = reconstructed
    from motte_eval.observation import aggregate_metric_results

    aggregate = aggregate_metric_results(results)
    return {
        **aggregate,
        "selected": len(cases),
        "denominator": "scored_metrics",
    }
