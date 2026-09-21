"""场景 Run 的逐步证据投影（验收 F-08）。

Web 的步骤下钻页请求 GET /api/v1/runs/{run_id}/steps，但服务端从未注册该端点
（404），页面因此永久显示「能力不可用（HTTP 404）」——M5 最核心的"流程可验证"
在 UI 上不可见。

本模块只做**只读投影**：把已持久化的证据整理成页面契约
（apps/web/src/api/client.ts 的 ScenarioRunStepsView）：

* 有 workflow-observation@1 时，按它逐步骤投影 status / 断言 / 耗时，并把
  checkpoint 的内容 hash 作为 state_hash 暴露（冻结证据是唯一来源）；
* 没有任何观察时，只回退到冻结快照里**声明的**步骤，并把 unknown 标为 true ——
  计划不等于结果，页面必须看得见这个区别；
* 一律零模型调用、零业务工具调用、不重算任何结论。
"""
from __future__ import annotations

from typing import Any

__all__ = ["build_scenario_steps_view"]


def _workflow_identity(run: dict[str, Any], observation: dict[str, Any] | None) -> dict[str, Any] | None:
    snapshot = (run.get("manifest") or {}).get("workflow_snapshot")
    if isinstance(snapshot, dict) and snapshot:
        return {
            "workflow_id": snapshot.get("workflow_id"),
            "version": snapshot.get("version"),
            "content_hash": snapshot.get("content_hash"),
        }
    if observation is not None:
        name, _sep, version = str(observation.get("workflow_ref") or "").rpartition("@")
        return {
            "workflow_id": name or None,
            "version": version or None,
            "content_hash": observation.get("workflow_content_hash"),
        }
    return None


def _declared_steps(run: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot = (run.get("manifest") or {}).get("workflow_snapshot") or {}
    steps = snapshot.get("steps") if isinstance(snapshot, dict) else None
    return [step for step in (steps or []) if isinstance(step, dict)]


def _observations(rows: list[dict[str, Any]]) -> list[tuple[str | None, dict[str, Any], dict[str, Any]]]:
    found: list[tuple[str | None, dict[str, Any], dict[str, Any]]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        result = row.get("result")
        if not isinstance(result, dict):
            continue
        observation = result.get("observation")
        if isinstance(observation, dict):
            found.append((row.get("case_id"), observation, result))
    return found


def _fixture_states(
    run: dict[str, Any], observations: list[tuple[str | None, dict[str, Any], dict[str, Any]]],
) -> list[dict[str, Any]]:
    snapshots = (run.get("manifest") or {}).get("fixture_snapshot") or {}
    if not isinstance(snapshots, dict) or not snapshots:
        return []
    cleanups: list[dict[str, Any]] = []
    for _case_id, _observation, result in observations:
        for entry in result.get("cleanup") or []:
            if isinstance(entry, dict):
                cleanups.append(entry)
    states: list[dict[str, Any]] = []
    for key, record in sorted(snapshots.items()):
        record = record if isinstance(record, dict) else {}
        fixture_id = str(record.get("fixture_id") or str(key).partition("@")[0])
        cleanup = next(
            (item for item in cleanups
             if str(item.get("fixture_id") or item.get("id") or "") == fixture_id),
            None,
        )
        states.append({
            "fixture_id": fixture_id,
            "version": record.get("version"),
            "kind": record.get("kind"),
            "owner": record.get("isolation"),
            "isolated": bool(record.get("isolation") == "per_case"),
            "snapshot": None,
            "cleanup": (
                {
                    "status": cleanup.get("status"),
                    "residual": list(cleanup.get("residual") or []),
                    "error": cleanup.get("error"),
                }
                if cleanup is not None else None
            ),
        })
    return states


def _step_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 case 顺序展开观察里的步骤；checkpoint 的证据 hash 单独挂回该步。"""
    records: list[dict[str, Any]] = []
    for case_id, observation, _result in rows:
        checkpoints = observation.get("checkpoints")
        checkpoints = checkpoints if isinstance(checkpoints, dict) else {}
        for item in observation.get("steps") or []:
            if not isinstance(item, dict):
                continue
            step_id = str(item.get("step_id") or "")
            frozen = checkpoints.get(step_id)
            record = {
                "case_id": case_id,
                "step_id": step_id,
                "kind": item.get("kind"),
                "seq": item.get("seq"),
                "status": item.get("status"),
                "duration_ms": item.get("duration_ms"),
                "detail": item.get("detail"),
                "assertions": [
                    dict(assertion) for assertion in (item.get("assertions") or [])
                    if isinstance(assertion, dict)
                ],
                "unknown": False,
            }
            if isinstance(frozen, dict):
                record["checkpoint"] = {
                    "label": frozen.get("label"),
                    "frozen": True,
                    "state_hash": frozen.get("content_hash"),
                }
            records.append(record)
    return records


def build_scenario_steps_view(run: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """把一次场景 Run 的持久化证据投影成页面契约。"""
    observations = _observations(rows)
    workflow = _workflow_identity(run, observations[0][1] if observations else None)
    fixtures = _fixture_states(run, observations)
    if observations:
        return {
            "run_id": run.get("id"),
            "status": run.get("status"),
            "case_ids": [case_id for case_id, _obs, _res in observations],
            "workflow": workflow,
            "steps": _step_records(observations),
            "fixtures": fixtures,
            "unknown": False,
        }
    # 没有可读观察：只给声明的步骤，并如实标记未知（计划 ≠ 结果）。
    steps = [
        {
            "step_id": step.get("step_id"),
            "kind": step.get("kind"),
            "tool_mode": step.get("tool_mode"),
            "timeout_sec": step.get("timeout_sec"),
            "label": step.get("label"),
            "status": None,
            "unknown": True,
        }
        for step in _declared_steps(run)
    ]
    return {
        "run_id": run.get("id"),
        "status": run.get("status"),
        "case_ids": [],
        "workflow": workflow,
        "steps": steps,
        "fixtures": fixtures,
        "unknown": True,
    }
