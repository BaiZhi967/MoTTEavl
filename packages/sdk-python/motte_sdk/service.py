from __future__ import annotations

from collections.abc import Callable, Iterable
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from motte_storage.integrity import RunConflictError, validate_scores

RUN_STATES = (
    "queued",
    "preparing",
    "running",
    "collecting",
    "scoring",
    "completed",
    "failed",
    "cancelled",
    "unsupported",
    "profile_stale",
    "needs_review",
)

# 状态机迁移表；running/collecting/scoring 允许回到 running，用于 Worker 崩溃后的中断恢复。
TRANSITIONS: dict[str, set[str]] = {
    "queued": {"preparing", "cancelled", "unsupported", "profile_stale"},
    "preparing": {"running", "failed", "cancelled", "unsupported"},
    "running": {"collecting", "failed", "cancelled", "running", "needs_review"},
    "collecting": {"scoring", "failed", "cancelled", "running", "needs_review"},
    "scoring": {"completed", "failed", "cancelled", "running", "needs_review"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
    "unsupported": set(),
    "profile_stale": set(),
    "needs_review": set(),
}

RETRYABLE = {"failed", "cancelled", "unsupported", "profile_stale", "needs_review"}


def build_run_service(db_path: str | Path | None = None) -> RunService:
    """API、CLI、Worker 共用的服务构造入口；存储后端经 motte_storage 工厂选择。"""
    from motte_storage.factory import create_run_store

    return RunService(create_run_store(db_path))


class RunService:
    """Shared run lifecycle used by API, CLI, and worker entry points."""

    TERMINAL = {"completed", "failed", "cancelled", "unsupported", "profile_stale", "needs_review"}

    def __init__(
        self,
        store: Any,
        provider: Callable[[str], Any] | None = None,
        *,
        event_observer: Callable[[dict[str, Any]], None] | None = None,
        progress_observer: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self._event_observers = [event_observer] if event_observer is not None else []
        self._progress_observers = [progress_observer] if progress_observer is not None else []
        # job 模式入口可注册的取消钩子（运行中的外部 Job 中断）。
        self._external_interrupters: dict[str, Any] = {}

    def add_event_observer(self, observer: Callable[[dict[str, Any]], None]) -> None:
        self._event_observers.append(observer)

    def add_progress_observer(self, observer: Callable[[dict[str, Any]], None]) -> None:
        self._progress_observers.append(observer)

    @staticmethod
    def _validate_frozen_benchmark_snapshot(run: dict[str, Any]) -> None:
        manifest = run.get("manifest")
        manifest = manifest if isinstance(manifest, dict) else {}
        provenance = manifest.get("benchmark_provenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        snapshot = manifest.get("benchmark_snapshot")
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        scenario_identity = snapshot.get("scenario")
        dataset_identity = snapshot.get("dataset")
        scenario_identity = scenario_identity if isinstance(scenario_identity, dict) else {}
        dataset_identity = dataset_identity if isinstance(dataset_identity, dict) else {}
        is_direct_v2 = (
            provenance.get("suite") == "direct-llm"
            and str(provenance.get("plugin_version") or "") == "2"
        ) or (
            snapshot.get("schema_version") == 2
            and str(scenario_identity.get("plugin_version") or "") == "2"
            and dataset_identity.get("contract_version") == 2
        )
        if is_direct_v2:
            from .direct_llm_v2 import selected_cases_from_snapshot

            selected_cases_from_snapshot(run)
        elif provenance.get("suite") == "agent-tasks":
            from .agent_tasks import selected_agent_cases_from_snapshot

            selected_agent_cases_from_snapshot(run)

    def create_run(
        self,
        scenario_version: str,
        manifest: dict[str, Any],
        case_ids: Iterable[str] = (),
        *,
        requested_manifest: dict[str, Any] | None = None,
        parent_run_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """创建 Run。

        ``run_id`` 允许调用方**先**冻结 Run 身份再创建：M3 的 Trial 计划由
        ``run_id`` 派生 ``trial_id``，因此计划与最终 Run 身份必须一致；省略
        时按既有行为生成新 id。
        """
        selected = list(case_ids)
        if len(selected) != len(set(selected)):
            raise ValueError("case_ids must be unique")
        if scenario_version.startswith("replay@") or (
            isinstance(manifest.get("execution"), dict)
            and manifest["execution"].get("backend_id") == "replay"
        ):
            from .execution_backends import resolve_replay_case_ids

            selected = resolve_replay_case_ids(manifest, selected)
        if run_id is None:
            run_id = f"run-{uuid4().hex}"
        elif not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a nonempty string when provided")
        now = datetime.now(UTC).isoformat()
        run: dict[str, Any] = {
            "id": run_id,
            "schema_version": 2,
            "revision": 1,
            "scenario_version": scenario_version,
            "status": "queued",
            "requested_manifest": deepcopy(
                manifest if requested_manifest is None else requested_manifest
            ),
            "manifest": deepcopy(manifest),
            "case_ids": selected,
            "created_at": now,
            "updated_at": now,
        }
        if parent_run_id is not None:
            run["parent_run_id"] = parent_run_id
        self._validate_frozen_benchmark_snapshot(run)
        from .benchmark_plugins import validate_frozen_trial_plans

        validate_frozen_trial_plans(self.store, run)
        event = {"run_id": run_id, "type": "queued", "status": "queued"}
        self.store.runs.create(run, event=event)
        if self._trial_shaped(run):
            self._ensure_trial_plans(run)
        self._notify_latest_event(run_id)
        return self._view(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return self._view(run_id)

    def execute(
        self,
        run_id: str,
        case_ids: Iterable[str] | None = None,
        provider: Callable[[str], Any] | None = None,
        expectations: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run = self._load(run_id)
        if run["status"] == "completed":
            return self._view(run_id)
        if run["status"] in self.TERMINAL:
            raise ValueError("run is terminal")
        if run["status"] not in {"queued", "preparing"}:
            raise RunConflictError("run must be atomically claimed before execution")
        invoke = provider if provider is not None else self.provider
        requested_case_ids = list(case_ids) if case_ids is not None else None
        if run.get("manifest", {}).get("benchmark_provenance"):
            self._validate_frozen_benchmark_snapshot(run)
            if requested_case_ids is not None and requested_case_ids != run["case_ids"]:
                raise ValueError("benchmark selected case ids are immutable")
            return self._execute_benchmark(run, invoke)
        if requested_case_ids is not None and run.get("case_ids") and requested_case_ids != run["case_ids"]:
            raise ValueError("selected case ids are immutable")
        ids = requested_case_ids if requested_case_ids is not None else list(run.get("case_ids") or [])
        if ids and not run.get("case_ids"):
            expected_revision = run["revision"]
            run["case_ids"] = ids
            run["updated_at"] = datetime.now(UTC).isoformat()
            self.store.runs.update(run, expected_revision=expected_revision)
        if run["status"] == "queued":
            self._transition(run_id, "preparing")
        elif run["status"] == "preparing":
            # Worker 抢占已把状态置为 preparing（无事件）；这里幂等补齐事件，保证三入口 Trace 一致。
            if not any(event["type"] == "preparing" for event in self.store.events.list_for_run(run_id)):
                self._emit(run_id, "preparing", {"status": "preparing"})
        if self._load(run_id)["status"] != "running":
            self._transition(run_id, "running")
        done = {row["case_id"] for row in self.store.case_runs.list_for_run(run_id)}
        total = len(ids)
        try:
            for ordinal, case_id in enumerate(ids, 1):
                if case_id in done:
                    self._notify_progress({
                        "event": "case_skipped", "run_id": run_id, "case_id": case_id,
                        "ordinal": ordinal, "total": total, "reason": "already_persisted",
                    })
                    continue
                cancelled = self._honor_cancellation(run_id)
                if cancelled is not None:
                    return cancelled
                self._notify_progress({
                    "event": "case_started", "run_id": run_id, "case_id": case_id,
                    "ordinal": ordinal, "total": total,
                })
                started = perf_counter()
                try:
                    attempt = self._begin_case_attempt(self._load(run_id), case_id)
                except RunConflictError:
                    cancelled = self._honor_cancellation(run_id)
                    if cancelled is not None:
                        return cancelled
                    raise
                try:
                    if invoke is None:
                        raise ValueError("execution backend did not provide an invoke hook")
                    result = invoke(case_id)
                except Exception as error:
                    evidence = deepcopy(getattr(error, "evidence", None))
                    result = evidence or {"error": {
                        "class": getattr(error, "error_class", None) or type(error).__name__,
                        "message": str(error),
                    }}
                    entry = {"run_id": run_id, "case_id": case_id, "result": result}
                    self._complete_case_attempt(attempt, entry, failed=True)
                    self._notify_progress({
                        "event": "case_finished", "run_id": run_id, "case_id": case_id,
                        "ordinal": ordinal, "total": total,
                        "duration_ms": round((perf_counter() - started) * 1000, 3),
                        "outcome": "call_failed", "error_class": getattr(error, "error_class", None)
                        or type(error).__name__,
                    })
                    cancelled = self._honor_cancellation(run_id)
                    if cancelled is not None:
                        return cancelled
                    raise
                expected = self._expected_for(invoke, expectations, case_id)
                entry = {"run_id": run_id, "case_id": case_id, "result": result}
                from .replay_run import NO_EXPECTATION

                if expected is not NO_EXPECTATION:
                    entry["expected"] = expected
                self._complete_case_attempt(attempt, entry, failed=False)
                self._notify_progress({
                    "event": "case_finished", "run_id": run_id, "case_id": case_id,
                    "ordinal": ordinal, "total": total,
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                    "outcome": "responded", **self._result_summary(result),
                })
        except Exception as error:
            return self._fail_or_quarantine(run_id, error)
        cancelled = self._honor_cancellation(run_id)
        if cancelled is not None:
            return cancelled
        results = self.store.case_runs.list_for_run(run_id)
        self._transition(run_id, "collecting")
        cancelled = self._honor_cancellation(run_id)
        if cancelled is not None:
            return cancelled
        self._transition(run_id, "scoring")
        cancelled = self._honor_cancellation(run_id)
        if cancelled is not None:
            return cancelled
        try:
            scores = self._score_results(run_id, results, emit_events=False)
            cancelled = self._honor_cancellation(run_id)
            if cancelled is not None:
                return cancelled
            self._append_scoring_pass(
                run_id, scores, source="initial", final_status="completed"
            )
        except Exception as error:
            return self._fail_or_quarantine(run_id, error)
        return self._view(run_id)

    def execute_external_job(
        self,
        run_id: str,
        run: dict[str, Any],
        job_entry: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        """job 模式分派主流程：一次外部 Job 覆盖全部 selected case（M2-G01）。

        ``job_entry`` 整 Run 只调用一次，不逐题 invoke。采集检查点、幂等导入
        与同键冲突处理由 external job 存储（M2-T03）补齐；本方法固定单次启动
        边界、结局→Run 状态映射，并保证每个 selected case 都有处置状态
        （M2-G11：未尝试的 case 不消失）。
        """
        # 计划先落库（review R2-01）：取消/超时终态化时要有计划单元可补处置，
        # 否则"跑过一半被取消"看起来像"什么都没跑"，覆盖分母也会消失。
        if run.get("id") != run_id:
            raise ValueError("frozen trial plan Run identity does not match dispatch")
        self._ensure_trial_plans(run)
        if run["status"] == "queued":
            self._transition(run_id, "preparing")
        if self._load(run_id)["status"] != "running":
            self._transition(run_id, "running")
        # 运行中的外部 Job 可被操作员取消中断（尽力而为，失败不阻断取消）。
        interrupt_run = getattr(job_entry, "interrupt_run", None)
        if callable(interrupt_run):
            self._external_interrupters[run_id] = interrupt_run
        try:
            outcome = job_entry(run)
        except Exception as error:
            return self._fail_or_quarantine(run_id, error)
        finally:
            self._external_interrupters.pop(run_id, None)
        if not isinstance(outcome, dict):
            return self._fail(run_id, ValueError(
                "external job entry must return an outcome object"
            ))
        # 取消可能已经由另一实例（或同进程的 cancel 调用）终态化：此时仍然要
        # 冻结并导入**已确定的结果**，不能让已完成 Trial 随取消一起丢失
        # （review R2-01/R05）。终态本身不复活，也不自动重跑。
        already_terminal = self._load(run_id)["status"] in self.TERMINAL
        if not already_terminal:
            self._transition(run_id, "collecting")
            self._transition(run_id, "scoring")
        trial_records, task_rows, unmapped = self._external_job_records(run_id, run, outcome)
        # 先按 trial_id 完整导入全部 Trial 结果（含失败/取消/未尝试的处置）：
        # 任务级 Case 聚合不能替代 Trial 原始结果存储（review R01，P0）。
        trial_import = self._import_trials(run, trial_records)
        accepted = set(trial_import.get("accepted") or [])
        if trial_import.get("skipped"):
            accepted = {record["trial_id"] for record in trial_records}
        invalid_import = list(trial_import.get("invalid") or [])
        # 被拒绝的计划单元也要有明确处置（不能从覆盖分母里消失），但绝不把
        # 被拒 payload 写成证据：只写平台补出的 indeterminate 占位。
        self._dispose_rejected_trials(run, trial_records, accepted, invalid_import)
        # 任务级 Case 行由**已导入的 Trial 结果**拼装：它们是派生聚合视图，
        # 不承载 Trial 身份，因此一个 Task 的多个 repeat 不再互相覆盖。
        # 非 Trial 形态保持既有的一行一 Case 映射（M2 语义不变）。
        if self._trial_shaped(run):
            task_rows = self._task_case_rows(
                run_id, [str(item) for item in run.get("case_ids") or []],
                [record for record in trial_records if record["trial_id"] in accepted],
            )
        if not already_terminal:
            for case_id in list(run.get("case_ids") or []):
                self.store.case_runs.upsert(task_rows[case_id])
        if trial_import.get("conflicts"):
            # 同一 Trial 的原始证据变化（hash 冲突）必须阻断最终化，而不是覆盖。
            outcome.setdefault("error", None)
            outcome["error"] = outcome.get("error") or {
                "code": "EXTERNAL_IMPORT_CONFLICT",
                "message": "trial evidence conflicted with the stored trial; "
                           "existing evidence is preserved",
            }
            errors_from_trials = True
        else:
            errors_from_trials = False
        if trial_import.get("replaced_placeholders"):
            # 占位被真实证据替换：留审计事件，说明"取消时的占位"已被覆盖。
            self.emit_run_event(run_id, "trial_placeholders_replaced", {
                "count": len(trial_import["replaced_placeholders"]),
                "replaced": trial_import["replaced_placeholders"][:20],
            })
        if already_terminal:
            return self._finish_after_terminal_import(
                run_id, trial_import, invalid_import, unmapped,
            )
        cancelled = self._honor_cancellation(run_id)
        if cancelled is not None:
            return cancelled
        rows = self.store.case_runs.list_for_run(run_id)
        job_status = str(outcome.get("job_status") or "indeterminate")
        if outcome.get("import", {}).get("conflicts"):
            job_status = "failed"
        errors = [
            row["result"]["error"] for row in rows
            if isinstance(row.get("result"), dict) and row["result"].get("error")
        ]
        for record in trial_records:
            error = record.get("error")
            if isinstance(error, dict) and error:
                errors.append(error)
        if invalid_import:
            errors.append({
                "code": "TRIAL_IMPORT_INVALID",
                "message": (
                    "trial payload identity did not match the frozen plan; "
                    "the offending payload was rejected and kept out of storage"
                ),
                "details": {"rejected": invalid_import[:10], "count": len(invalid_import)},
            })
        outcome_error = outcome.get("error")
        if isinstance(outcome_error, dict) and outcome_error:
            errors.append(outcome_error)
        if unmapped:
            # 冻结映射之外的 Runner 行（R03）：可见、可审计，且不让 Run 假装
            # 干净完成。
            errors.append({
                "code": "EXTERNAL_UNMAPPED_RESULTS",
                "message": (
                    "runner produced rows outside the frozen case mapping; "
                    "they are quarantined and never attributed to other cases"
                ),
                "details": {"unmapped": unmapped[:10], "count": len(unmapped)},
            })
        selected_ids = list(run.get("case_ids") or [])
        responded = sum(
            1 for row in rows
            if row.get("outcome") == "responded" and row.get("case_id") in set(selected_ids)
        )
        if selected_ids and responded == 0:
            # 零结果不能伪装成功完成可计分任务（review R11）；正常无 gold 的
            # 预测任务 responded>0，仍按 unscored 完成而非执行失败。
            errors.append({
                "code": "EXTERNAL_EMPTY_RESULTS",
                "message": (
                    "external job returned no case results for a non-empty "
                    "selection; run cannot be finalized as completed"
                ),
            })
        if errors_from_trials:
            errors.append({
                "code": "TRIAL_IMPORT_CONFLICT",
                "message": "trial evidence conflicted with stored trial results",
            })
        if job_status == "cancelled":
            final_status = "cancelled"
        elif job_status == "indeterminate":
            final_status = "needs_review"
        elif job_status == "settled" and not errors:
            final_status = "completed"
        else:
            # settled 带失败 case 或 job failed：部分结果已保留，终态 failed。
            final_status = "failed"
        self._ensure_trial_dispositions(
            run, disposition="cancelled" if final_status == "cancelled" else "indeterminate",
        )
        metrics = {}
        import_view = outcome.get("import")
        if isinstance(import_view, dict):
            metrics = import_view.get("metrics") or {}
        outcome_error_view = outcome.get("error")
        runner_exit_code = (
            (outcome_error_view.get("details") or {}).get("exit_code")
            if isinstance(outcome_error_view, dict) else None
        )
        if runner_exit_code is None:
            runner_exit_code = (
                (outcome.get("handle") or {}).get("owned_resources", {}).get("exit_code")
            )
        if metrics:
            # native/diagnostic 双口径进入版本化指标事实（review R09）：
            # 持久 run 事件 + Job checkpoint（分派侧），供报告与 UI 查询。
            self.emit_run_event(run_id, "external_job_metrics", {
                "job_id": import_view.get("job_id"),
                "parser_version": metrics.get("parser_version"),
                "native": metrics.get("native"),
                "diagnostic": metrics.get("diagnostic"),
                "evidence": import_view.get("evidence"),
            })
        try:
            scores = self._score_results(run_id, rows, False)
            cancelled = self._honor_cancellation(run_id)
            if cancelled is not None:
                return cancelled
            self._append_scoring_pass(
                run_id,
                scores,
                source="initial",
                final_status=final_status,
                final_error=errors[0] if errors else None,
                extra_summary=(
                    {"external_job_metrics": metrics} if metrics else None
                ),
                aggregate_context={
                    "external_outcome": {
                        "job_status": job_status,
                        "runner_exit_code": runner_exit_code,
                    },
                },
            )
        except Exception as error:
            return self._fail_or_quarantine(run_id, error)
        return self._view(run_id)

    @staticmethod
    def _frozen_plan_index(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """冻结计划索引：``trial_id`` 与 ``<task_key>#<repeat_index>`` → 计划单元。"""
        external = (run.get("manifest") or {}).get("external_benchmark") or {}
        plan = external.get("runner_config") or {}
        trials = (plan.get("plan") or {}).get("trials") or []
        index: dict[str, dict[str, Any]] = {}
        for item in trials:
            if not isinstance(item, dict):
                continue
            trial_id = item.get("trial_id")
            task_key = item.get("task_key")
            repeat_index = item.get("repeat_index")
            if isinstance(trial_id, str) and trial_id:
                index[trial_id] = item
            if isinstance(task_key, str) and isinstance(repeat_index, int):
                index[f"{task_key}#{repeat_index}"] = item
        return index

    @staticmethod
    def _synthesized_trial_result(
        plan: dict[str, Any] | None, item: dict[str, Any], *,
        declared_status: str | None,
    ) -> dict[str, Any]:
        """Runner 行没有 payload 时，按冻结计划补出**完整处置**（review R01）。

        错误/取消/未尝试同样要留下身份与处置：缺行会让覆盖分母悄悄变小，
        而"没有证据"与"没有记录"是两件事。状态未知时按 ``indeterminate``
        如实表达（不猜成功也不猜失败）。
        """
        status = str(declared_status or "failed")
        disposition = "not_attempted" if status == "not_attempted" else "indeterminate"
        payload: dict[str, Any] = {
            "disposition": disposition,
            "verifier_observation": {"status": "missing_verifier_evidence"},
            "coverage": {
                "items": {
                    "cost_usd": "unavailable",
                    "model_identity": "unavailable",
                    "trajectory": "unavailable",
                },
                "reason": "runner produced no trial payload for this planned unit",
            },
        }
        if outcome_error := item.get("error"):
            payload["error"] = deepcopy(outcome_error)
        if isinstance(plan, dict):
            payload["trial_id"] = plan["trial_id"]
            payload["task_key"] = plan["task_key"]
            payload["repeat_index"] = plan["repeat_index"]
        payload["synthesized_by"] = "run-service:trial-disposition"
        return payload

    @staticmethod
    def _trial_shaped(run: dict[str, Any]) -> bool:
        """Run 是否是"一 Job 多计划 Trial"形态（Terminal-Bench/Harbor）。

        结构信号是冻结 manifest 里的 Trial 计划；非 Trial 形态（GSM8K/C-Eval
        等 M2 外部套件）继续走既有的一行一 Case 映射，语义不变。
        """
        external = (run.get("manifest") or {}).get("external_benchmark") or {}
        return bool((external.get("runner_config") or {}).get("plan"))

    def _external_job_records(
        self, run_id: str, run: dict[str, Any], outcome: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """把 job 结局拆成 ``(Trial 记录, 任务级 Case 行, 隔离行)``。

        review R01（P0）：过去按 ``case_id`` 建字典，同一 Task 的第二个 repeat
        会覆盖第一个，Run 完成时仍丢结果；Trial 形态的 Run 现在**一个计划 Trial
        一条记录**，任务级 Case 行只是派生聚合、不承载 Trial 身份。非 Trial
        形态沿用既有映射（M2 语义逐字不变）。冻结选样之外的 Runner 行
        （unmapped/映射冲突）继续被隔离成审计记录，绝不归属到其它题目。
        """
        if not self._trial_shaped(run):
            rows, unmapped = self._legacy_case_rows(run_id, run, outcome)
            return ([], rows, unmapped)
        selected = [str(item) for item in run.get("case_ids") or []]
        selected_set = set(selected)
        plan_index = self._frozen_plan_index(run)
        by_trial: dict[str, dict[str, Any]] = {}
        unmapped: list[dict[str, Any]] = []
        for item in outcome.get("results") or []:
            if not isinstance(item, dict):
                continue
            payload = item.get("output") if isinstance(item.get("output"), dict) else None
            trial_id = str(payload.get("trial_id") or "") if payload else ""
            task_key = str(payload.get("task_key") or "") if payload else ""
            plan: dict[str, Any] | None = None
            if trial_id:
                candidate = plan_index.get(trial_id)
                plan = candidate if isinstance(candidate, dict) else None
                if plan is None:
                    # 不属于**本 Run 冻结计划**的 Trial 身份：显式隔离（review R2-03）。
                    # 直接放行会让结果落到别的 Run 的 Trial 名下；静默忽略又会
                    # 让本 Run 假完成。两者都不可接受。
                    unmapped.append({
                        "case_id": task_key or item.get("case_id"),
                        "stable_case_key": item.get("stable_case_key"),
                        "error": {
                            "code": "EXTERNAL_TRIAL_FOREIGN",
                            "message": (
                                f"trial {trial_id} does not belong to this run's frozen "
                                "plan; the row is quarantined and never attributed"
                            ),
                        },
                    })
                    continue
                # 冻结计划身份是权威：payload 自称的 task_key/repeat_index 必须一致。
                mismatch = [
                    field for field in ("task_key", "repeat_index")
                    if payload is not None and field in payload
                    and payload[field] != plan.get(field)
                ]
                if mismatch:
                    unmapped.append({
                        "case_id": task_key or item.get("case_id"),
                        "stable_case_key": item.get("stable_case_key"),
                        "error": {
                            "code": "EXTERNAL_TRIAL_IDENTITY_MISMATCH",
                            "message": (
                                f"trial {trial_id} payload disagrees with the frozen plan "
                                f"on {mismatch}"
                            ),
                        },
                    })
                    continue
            else:
                # 没有 payload 的行只能靠冻结计划归属；无法归属即隔离（不猜）。
                for key in (item.get("stable_case_key"), item.get("case_id")):
                    candidate = plan_index.get(str(key or ""))
                    if isinstance(candidate, dict):
                        plan = candidate
                        break
                if plan is None:
                    unmapped.append({
                        "case_id": item.get("case_id"),
                        "stable_case_key": item.get("stable_case_key"),
                        "error": item.get("error") or {
                            "code": "EXTERNAL_ROW_UNIDENTIFIED",
                            "message": (
                                "runner row has neither a trial payload nor a frozen "
                                "plan match; it is quarantined and never attributed"
                            ),
                        },
                    })
                    continue
                trial_id = str(plan["trial_id"])
                task_key = str(plan["task_key"])
            if task_key not in selected_set:
                unmapped.append({
                    "case_id": task_key,
                    "stable_case_key": item.get("stable_case_key"),
                    "error": item.get("error"),
                })
                continue
            if trial_id in by_trial:
                # 同一 Trial 出现两行：保留先到者，重复行进审计（不静默丢弃）。
                unmapped.append({
                    "case_id": task_key,
                    "stable_case_key": item.get("stable_case_key"),
                    "error": {
                        "code": "EXTERNAL_TRIAL_DUPLICATE",
                        "message": f"trial {trial_id} was reported more than once",
                    },
                })
                continue
            record: dict[str, Any] = {
                "trial_id": trial_id,
                "task_key": task_key,
                "repeat_index": (
                    payload.get("repeat_index") if payload
                    else (plan or {}).get("repeat_index")
                ),
                "status": item.get("status"),
                "error": deepcopy(item.get("error")) if item.get("error") else None,
                "payload": (
                    deepcopy(payload) if payload
                    else self._synthesized_trial_result(
                        plan, item, declared_status=item.get("status"),
                    )
                ),
            }
            if record["repeat_index"] is None and isinstance(plan, dict):
                record["repeat_index"] = plan.get("repeat_index")
            by_trial[trial_id] = record

        trials = [by_trial[key] for key in sorted(
            by_trial, key=lambda key: (by_trial[key]["task_key"], by_trial[key]["repeat_index"] or 0),
        )]
        return (trials, self._task_case_rows(run_id, selected, trials), unmapped)

    @staticmethod
    def _task_case_rows(
        run_id: str, selected: list[str], trial_records: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """任务级 Case 行：一行一个逻辑 Task，内容是**派生聚合**而非某个 Trial。"""
        grouped: dict[str, list[dict[str, Any]]] = {case_id: [] for case_id in selected}
        for record in trial_records:
            grouped.setdefault(str(record["task_key"]), []).append(record)
        rows: dict[str, dict[str, Any]] = {}
        for case_id, records in grouped.items():
            observed = [
                record for record in records
                if isinstance(record.get("payload"), dict)
                and record["payload"].get("synthesized_by") is None
            ]
            dispositions: dict[str, int] = {}
            for record in records:
                disposition = str(
                    (record.get("payload") or {}).get("disposition") or "unknown",
                )
                dispositions[disposition] = dispositions.get(disposition, 0) + 1
            if not observed:
                # 该任务一个 Trial 都没产出证据：任务级行保持 not_attempted，
                # 不伪造 case 级错误（Trial 层的错误已单独进入 Run 错误清单，
                # 这里再塞一条会遮蔽真正的首个错误码）。
                rows[case_id] = {
                    "run_id": run_id, "case_id": case_id,
                    "outcome": "not_attempted", "result": None,
                }
                continue
            rows[case_id] = {
                "run_id": run_id, "case_id": case_id, "outcome": "responded",
                # 任务级行不是可评分的原始结果：质量由 Trial 层判定（需求 4.2）。
                # "不可评分"标记放在 result 里——公共 CaseRun 契约是 extra=forbid，
                # 顶层多一个键会让 GET /runs/{id} 直接 500。
                "result": {
                    "task_key": case_id,
                    "aggregate_only": True,
                    "unscored": True,
                    "planned_trials": len(records),
                    "observed_trials": len(observed),
                    "dispositions": dispositions,
                },
            }
        for case_id in selected:
            rows.setdefault(case_id, {
                "run_id": run_id, "case_id": case_id,
                "outcome": "not_attempted", "result": None,
            })
        return rows

    @staticmethod
    def _legacy_case_rows(
        run_id: str, run: dict[str, Any], outcome: dict[str, Any],
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """非 Trial 形态的既有映射（M2）：一行一 Case，缺失记 not_attempted。"""
        selected = set(run.get("case_ids") or [])
        rows: dict[str, dict[str, Any]] = {}
        unmapped: list[dict[str, Any]] = []
        for item in outcome.get("results") or []:
            if not isinstance(item, dict):
                continue
            case_id = item.get("case_id") or item.get("source_case_id")
            payload = item.get("output") if isinstance(item.get("output"), dict) else {}
            # Trial 形态的 Runner（M3）：一行是一个计划的 Trial，逻辑 Case 是
            # payload 里的 task_key；没有该字段时退回既有 stable case 身份。
            if isinstance(payload.get("task_key"), str) and payload.get("trial_id"):
                case_id = str(payload["task_key"])
            if not isinstance(case_id, str) or not case_id:
                continue
            if case_id not in selected:
                unmapped.append({
                    "case_id": case_id,
                    "stable_case_key": item.get("stable_case_key"),
                    "error": item.get("error"),
                })
                continue
            status = item.get("status")
            if status == "failed":
                rows[case_id] = {
                    "run_id": run_id, "case_id": case_id, "outcome": "call_failed",
                    "result": {"error": deepcopy(item.get("error")) or {
                        "code": "EXTERNAL_JOB_CASE_FAILED",
                        "message": "external job reported a failed case without error detail",
                    }},
                }
            elif status == "not_attempted":
                rows[case_id] = {
                    "run_id": run_id, "case_id": case_id,
                    "outcome": "not_attempted", "result": None,
                }
            elif status == "unscored":
                # "不可评分"标记放进 result：公共 CaseRun 契约是 extra=forbid，
                # 顶层多键会让 GET /runs/{id} 的响应校验失败（500）。
                payload = deepcopy(item.get("output"))
                rows[case_id] = {
                    "run_id": run_id, "case_id": case_id, "outcome": "responded",
                    "result": (
                        {**payload, "unscored": True}
                        if isinstance(payload, dict) else {"unscored": True, "output": payload}
                    ),
                }
            else:
                rows[case_id] = {
                    "run_id": run_id, "case_id": case_id, "outcome": "responded",
                    "result": deepcopy(item.get("output")),
                }
        for case_id in run.get("case_ids") or []:
            if case_id not in rows:
                rows[case_id] = {
                    "run_id": run_id, "case_id": case_id,
                    "outcome": "not_attempted", "result": None,
                }
        return (rows, unmapped)

    def _honor_cancellation(self, run_id: str) -> dict[str, Any] | None:
        run = self._load(run_id)
        cancellation = run.get("cancellation")
        if run["status"] in self.TERMINAL or not isinstance(cancellation, dict):
            return self._view(run_id) if run["status"] == "cancelled" else None
        return self.cancel(run_id, reason=cancellation.get("reason"), _settle=True)

    def cancel(
        self, run_id: str, reason: str | None = None, *, _settle: bool = False
    ) -> dict[str, Any]:
        run = self._load(run_id)
        if run["status"] in self.TERMINAL:
            return self._view(run_id)
        cancellation = {"reason": reason} if reason is not None else {}
        open_attempts = self.store.attempts.list_open(run_id)
        interrupter = self._external_interrupters.get(run_id)
        deferrable = open_attempts or (
            not _settle and run["status"] in {"running", "collecting", "scoring"}
        )
        if deferrable and interrupter is None:
            now = datetime.now(UTC).isoformat()
            cancellation["requested_at"] = now
            self.store.runs.update(
                {**run, "cancellation": cancellation, "updated_at": now},
                expected_revision=run["revision"],
                expected_status=run["status"],
                event={
                    "run_id": run_id,
                    "type": "cancellation_requested",
                    "status": run["status"],
                    **({"reason": reason} if reason is not None else {}),
                },
            )
            self._notify_latest_event(run_id)
            return self._view(run_id)
        if interrupter is not None:
            # job 模式：先持久化取消请求，再中断本 Run 拥有的外部 Job 进程，
            # 迟到输出只作审计证据，终态不复活。
            now = datetime.now(UTC).isoformat()
            cancellation["requested_at"] = now
            self.store.runs.update(
                {**run, "cancellation": cancellation, "updated_at": now},
                expected_revision=run["revision"],
                expected_status=run["status"],
                event={
                    "run_id": run_id,
                    "type": "cancellation_requested",
                    "status": run["status"],
                    **({"reason": reason} if reason is not None else {}),
                },
            )
            self._notify_latest_event(run_id)
            try:
                interrupter(run_id)
            except Exception as error:  # noqa: BLE001 - 中断失败不阻断取消落账
                self.emit_run_event(run_id, "external_job_interrupt_failed", {
                    "error_class": type(error).__name__,
                    "message": str(error),
                })
            refreshed = self._load(run_id)
            if refreshed["status"] in self.TERMINAL:
                return self._view(run_id)
            run = refreshed
        changes = {"cancellation": cancellation} if cancellation else {}
        if run.get("manifest", {}).get("benchmark_provenance"):
            try:
                self._finish_unattempted(
                    run_id,
                    final_status="cancelled",
                    final_changes=changes,
                    terminal_event_payload={"reason": reason} if reason is not None else {},
                )
            except RunConflictError:
                # A claim won the no-open-attempts race; leave a durable request for
                # the executor to settle after its in-flight attempt closes.
                current = self._load(run_id)
                if current["status"] in self.TERMINAL:
                    return self._view(run_id)
                cancellation["requested_at"] = datetime.now(UTC).isoformat()
                self.store.runs.update(
                    {**current, "cancellation": cancellation, "updated_at": cancellation["requested_at"]},
                    expected_revision=current["revision"], expected_status=current["status"],
                    event={"run_id": run_id, "type": "cancellation_requested", "status": current["status"]},
                )
        else:
            self._transition(
                run_id,
                "cancelled",
                changes=changes,
                event_payload={"reason": reason} if reason is not None else {},
            )
        return self._view(run_id)

    def rescore(self, run_id: str) -> dict[str, Any]:
        run = self._load(run_id)
        if (run.get("manifest", {}).get("import_source") or {}).get("kind") == "inspect":
            raise ValueError("Inspect imports are read-only; no observation evaluator is configured")
        self._validate_frozen_benchmark_snapshot(run)
        benchmark_terminal = run.get("manifest", {}).get("benchmark_provenance") and run["status"] in self.TERMINAL
        if run["status"] != "completed" and not benchmark_terminal:
            raise ValueError("only completed runs or terminal benchmarks can be rescored")
        scores = self._score_results(
            run_id, self.store.case_runs.list_for_run(run_id), emit_events=False
        )
        scoring_pass = self._append_scoring_pass(run_id, scores, source="rescore")
        self._emit(run_id, "rescored", {
            "status": run["status"],
            "scoring_pass_id": scoring_pass["id"],
        })
        return self._view(run_id)

    def mark_unsupported(self, run_id: str, code: str, message: str | None = None) -> dict[str, Any]:
        run = self._load(run_id)
        if run["status"] in self.TERMINAL:
            return self._view(run_id)
        error: dict[str, Any] = {"code": code}
        if message is not None:
            error["message"] = message
        if run.get("manifest", {}).get("benchmark_provenance"):
            self._finish_unattempted(
                run_id, final_status="unsupported", final_error=error
            )
        else:
            self._transition(run_id, "unsupported", changes={"error": error})
        return self._view(run_id)

    def mark_profile_stale(self, run_id: str, reason: str | None = None) -> dict[str, Any]:
        run = self._load(run_id)
        if run["status"] in self.TERMINAL:
            return self._view(run_id)
        if run["status"] != "queued":
            raise ValueError("profile staleness is detected before execution")
        error: dict[str, Any] = {"code": "PROFILE_STALE"}
        if reason is not None:
            error["message"] = reason
        if self._trial_shaped(run):
            self._finish_unattempted(run_id, final_status="profile_stale", final_error=error)
            return self._view(run_id)
        return self._transition(run_id, "profile_stale", changes={"error": error})

    def retry(
        self, run_id: str, *, refreshed_manifest: dict[str, Any] | None = None,
        case_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        parent = self._load(run_id)
        if (parent.get("manifest", {}).get("import_source") or {}).get("kind") == "inspect":
            raise ValueError("Inspect imports are read-only; upload the identical source to resume an import")
        self._validate_frozen_benchmark_snapshot(parent)
        if parent["status"] not in RETRYABLE:
            raise ValueError(
                "only failed, cancelled, unsupported, profile_stale, or needs_review runs can be retried"
            )
        if parent["status"] == "profile_stale" and refreshed_manifest is None:
            raise ValueError("profile_stale retry requires a freshly resolved manifest")
        open_attempts = self.store.attempts.list_open(run_id)
        if open_attempts and parent["status"] != "needs_review":
            raise ValueError("run has unresolved attempts and must be quarantined before retry")
        manifest = deepcopy(
            refreshed_manifest if refreshed_manifest is not None else parent.get("manifest", {})
        )
        selected = list(case_ids) if case_ids is not None else list(parent.get("case_ids") or [])
        # Trial 身份是 Run 作用域的：子 Run 必须重新派生 TrialPlan / 原生 Job 身份，
        # 否则结果会导入到父 Trial 名下（父证据被判 identical 或冲突），子 Run
        # 自己没有 Trial（review R2-02）。任务内容与实验条件保持不变。
        child_run_id = f"run-{uuid4().hex}"
        manifest = self._refreeze_trial_identity(
            manifest, run_id=child_run_id, job_id=f"job-{uuid4().hex}",
        )
        return self.create_run(
            parent["scenario_version"],
            manifest,
            selected,
            requested_manifest=deepcopy(
                parent.get("requested_manifest")
                if parent.get("requested_manifest") is not None
                else parent.get("manifest") or {}
            ),
            parent_run_id=run_id,
            run_id=child_run_id,
        )

    def _refreeze_trial_identity(
        self, manifest: dict[str, Any], *, run_id: str, job_id: str,
    ) -> dict[str, Any]:
        """把 manifest 里 Run 作用域的 Trial/Job 身份重新派生给新 Run。

        非 Trial 形态（M2 套件）原样返回；Trial 形态交给套件自己的重新冻结
        实现（``motte_sdk.terminalbench.refreeze_for_run``），未知套件显式失败
        而不是照抄父身份。
        """
        from motte_sdk.benchmark_plugins import suite_for_run

        run = {"id": run_id, "manifest": manifest}
        suite = suite_for_run(run)
        if suite is None:
            return manifest
        if suite[0] != "terminal-bench-harbor":
            return manifest
        from motte_sdk.terminalbench import refreeze_for_run

        return refreeze_for_run(manifest, run_id=run_id, job_id=job_id)

    def events(self, run_id: str) -> list[dict[str, Any]]:
        return self.store.events.list_for_run(run_id)

    def emit_run_event(
        self, run_id: str, event_type: str, payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """后端证据通道：追加一条持久 trace 事件并广播，返回含 seq 的存储事件。"""
        try:
            stored = self.store.events.append({
                "run_id": run_id, "type": event_type, **(payload or {}),
            })
            self._notify_event(stored)
            return stored
        except Exception:  # noqa: BLE001 - 证据通道故障不阻断执行
            return None

    def events_after(self, run_id: str, seq: int) -> list[dict[str, Any]]:
        return self.store.events.list_after(run_id, seq)

    def _load(self, run_id: str) -> dict[str, Any]:
        run = self.store.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def _view(self, run_id: str) -> dict[str, Any]:
        run = deepcopy(self._load(run_id))
        run["cases"] = self.store.case_runs.list_for_run(run_id)
        scoring_pass = self.store.scoring_passes.current(run_id) if self.store.scoring_passes else None
        if scoring_pass is not None:
            run["scores"] = self.store.score_sets.list_for_pass(scoring_pass["id"])
            run["current_scoring_pass_id"] = scoring_pass["id"]
            run["scoring_pass"] = scoring_pass
        else:
            run["scores"] = self.store.scores.list_for_run(run_id)
        return run

    def _append_scoring_pass(
        self, run_id: str, scores: list[dict[str, Any]], *, source: str,
        final_status: str | None = None, final_error: dict[str, Any] | None = None,
        final_changes: dict[str, Any] | None = None,
        terminal_event_payload: dict[str, Any] | None = None,
        require_no_open_attempts: bool = False,
        skip_aggregate: bool = False,
        allow_aggregate_failure: bool = False,
        case_rows: list[dict[str, Any]] | None = None,
        extra_summary: dict[str, Any] | None = None,
        aggregate_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run = self._load(run_id)
        scores = validate_scores(deepcopy(scores))
        evaluation = (run.get("manifest") or {}).get("evaluation") or {}
        provenance = (run.get("manifest") or {}).get("benchmark_provenance") or {}
        scorer_version = str(
            evaluation.get("scorer_version")
            or provenance.get("scorer_version")
            or next((row.get("scorer_version") for row in scores if row.get("scorer_version")), "generic-v1")
        )
        scorer_id = str(evaluation.get("scorer_id") or provenance.get("scorer") or scorer_version)
        selected_case_ids = set(run.get("case_ids") or [])
        if not selected_case_ids:
            selected_case_ids = {
                row["case_id"] for row in self.store.case_runs.list_for_run(run_id)
                if isinstance(row.get("case_id"), str)
            }
        # Cancellation/recovery may finalize a pass before every selected case has a score;
        # membership is mandatory, complete coverage is intentionally not.
        raw_score_case_ids = [score.get("case_id") for score in scores]
        if any(not isinstance(case_id, str) or not case_id for case_id in raw_score_case_ids):
            raise ValueError("scores require nonempty string case IDs")
        score_case_ids = set(raw_score_case_ids)
        unexpected_case_ids = score_case_ids - selected_case_ids
        if unexpected_case_ids:
            raise ValueError(
                "scores contain case IDs outside the run selection: "
                + ", ".join(sorted(str(case_id) for case_id in unexpected_case_ids))
            )
        if any(score.get("scoring_pass_id") is not None for score in scores):
            raise ValueError("scoring_pass_id is assigned by the scoring pass, not by plugins")
        previous = self.store.scoring_passes.current(run_id)
        from .commands import intervention_summary

        interventions = (
            deepcopy((previous.get('summary') or {}).get('interventions'))
            if source == 'rescore' and previous else None
        )
        if interventions is None:
            interventions = intervention_summary(self, run_id)
        manifest_bytes = json.dumps(
            run.get("manifest") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        passed = sum(score.get("passed") is True for score in scores)
        multi_metric = any(score.get("metric_id") for score in scores)
        metric_summary: dict[str, dict[str, int]] | None = None
        if multi_metric:
            metric_summary = {}
            for score in scores:
                bucket = metric_summary.setdefault(str(score["metric_id"]), {
                    "scored": 0, "passed": 0, "failed": 0,
                    "insufficient_evidence": 0, "evaluator_error": 0, "not_applicable": 0,
                })
                status = str(score.get("metric_status") or (
                    "scored" if score.get("passed") is not None else "not_applicable"
                ))
                if status in bucket:
                    bucket[status] += 1
                if status == "scored":
                    if score.get("passed") is True:
                        bucket["passed"] += 1
                    elif score.get("passed") is False:
                        bucket["failed"] += 1
        aggregate = None
        if provenance and not skip_aggregate:
            from .benchmark_plugins import aggregate_with_plugin

            # 聚合消费固定 Run 事实 + 当前已持久化的 case 行 + 调用方给的
            # 外部结局上下文（退出码等），不依赖调用前未落库的中间态。
            run_view = {
                **run,
                "cases": self.store.case_runs.list_for_run(run_id),
                "trial_results": self._stored_trial_payloads(run_id),
                **(deepcopy(aggregate_context) or {}),
            }
            try:
                aggregate = aggregate_with_plugin(run_view, scores)
                if not isinstance(aggregate, dict):
                    raise ValueError("benchmark aggregate must be an object")
                from motte_contracts.report import ReportSummary

                recognized = {
                    key: value for key, value in aggregate.items()
                    if key in ReportSummary.model_fields and key != "aggregate"
                }
                ReportSummary.model_validate({
                    "cases": recognized.get("cases", len(scores)),
                    "scored": recognized.get("scored", len(scores)),
                    "passed": recognized.get("passed", sum(
                        score.get("passed") is True for score in scores
                    )),
                    "failed": recognized.get("failed", sum(
                        score.get("passed") is False for score in scores
                    )),
                    **recognized,
                })
                selected_count = len(selected_case_ids)
                selected = recognized.get("selected")
                if selected is not None and selected != selected_count:
                    raise ValueError("benchmark aggregate selected count does not match the run")
                outcome_keys = (
                    "correct", "wrong_answer", "no_expectation", "parse_failure",
                    "call_failed", "not_attempted",
                )
                counts = [
                    recognized[key] for key in outcome_keys if recognized.get(key) is not None
                ]
                if any(count > selected_count for count in counts) or sum(counts) > selected_count:
                    raise ValueError("benchmark aggregate outcome counts exceed selected cases")
                attempted = recognized.get("attempted")
                responded = recognized.get("responded")
                not_attempted = recognized.get("not_attempted")
                if attempted is not None and attempted > selected_count:
                    raise ValueError("benchmark aggregate attempted count exceeds selected cases")
                if responded is not None and (
                    responded > selected_count or attempted is not None and responded > attempted
                ):
                    raise ValueError("benchmark aggregate responded count is inconsistent")
                if attempted is not None and not_attempted is not None and (
                    attempted + not_attempted > selected_count
                ):
                    raise ValueError("benchmark aggregate attempt counts exceed selected cases")
            except Exception as error:
                if not allow_aggregate_failure:
                    raise
                aggregate = None
                aggregate_details = {"error_type": type(error).__name__}
                if final_error:
                    final_error = {
                        **final_error,
                        "details": {
                            **(final_error.get("details") or {}),
                            "aggregate_error": aggregate_details,
                        },
                    }
                else:
                    final_error = {
                        "code": "AGGREGATE_UNAVAILABLE",
                        "message": "terminal run aggregate could not be computed",
                        "details": aggregate_details,
                    }
        pass_id = f"pass-{uuid4().hex}"
        record = {
            "id": pass_id,
            "run_id": run_id,
            "scorer_id": scorer_id,
            "scorer_version": scorer_version,
            "created_at": datetime.now(UTC).isoformat(),
            "source": source,
            "source_run_revision": run["revision"],
            "source_snapshot_hash": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
            "previous_pass_id": previous.get("id") if previous else None,
            "summary": {
                "scores": len(scores),
                "passed": passed,
                **({"multi_metric": True, "metrics": metric_summary} if metric_summary else {}),
                **({"aggregate": deepcopy(aggregate)} if aggregate is not None else {}),
                **(deepcopy(extra_summary) if extra_summary else {}),
                'interventions': interventions,
            },
            "scores": deepcopy(scores),
        }
        now = datetime.now(UTC).isoformat()
        existing_events = self.store.events.list_for_run(run_id)
        last_seq = existing_events[-1]["seq"] if existing_events else 0
        stored = self.store.scoring_passes.append(
            record,
            scores,
            expected_run_revision=run["revision"],
            expected_run_status=run["status"],
            event={
                "run_id": run_id,
                "type": "scoring_pass_created",
                "scoring_pass_id": pass_id,
                "source": source,
                "scorer_id": scorer_id,
                "scorer_version": scorer_version,
            },
            score_events=([{
                "run_id": run_id,
                "type": "score",
                "scoring_pass_id": pass_id,
                "case_id": score["case_id"],
                **({"metric_id": score["metric_id"]} if score.get("metric_id") else {}),
                **({"passed": score["passed"]} if "passed" in score else {}),
            } for score in scores] if source != "terminal-unattempted" else []),
            final_status=final_status,
            run_changes={
                "updated_at": now,
                **({"finished_at": now} if final_status in self.TERMINAL else {}),
                **deepcopy(final_changes or {}),
                **({"error": deepcopy(final_error)} if final_error is not None else {}),
            },
            terminal_event={
                "run_id": run_id, "type": final_status, "status": final_status,
                **deepcopy(terminal_event_payload or {}),
                **({"error": deepcopy(final_error)} if final_error is not None else {}),
            } if final_status is not None else None,
            require_no_open_attempts=require_no_open_attempts,
            case_rows=case_rows,
        )
        # The append-only pass is authoritative. A compatibility mirror failure must not
        # make an already committed pass look unsuccessful to callers.
        try:
            self.store.scores.replace_for_run(run_id, scores)
        except Exception:
            pass
        for event in self.store.events.list_after(run_id, last_seq):
            self._notify_event(event)
        return stored

    def _import_trials(
        self, run: dict[str, Any], trial_records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Terminal-Bench 形态：把采集到的 Trial 结果落进 Trial 存储。

        输入是**每个计划 Trial 一条记录**（``_external_job_records``），因此
        同一 Task 的多个 repeat 各自落库、互不覆盖；记录里的 payload 可能
        是按冻结计划补出的处置（无 payload 的失败/取消行），错误/取消/未尝试
        同样有身份。非 Trial 套件直接返回空结果。
        """
        from motte_sdk.benchmark_plugins import import_terminal_bench_trials, suite_for_run

        suite = suite_for_run(run)
        if suite is None or suite[0] != "terminal-bench-harbor":
            return {"skipped": True}
        results = [
            {
                "case_id": record["task_key"],
                "trial_id": record["trial_id"],
                "result": record.get("payload"),
            }
            for record in trial_records
            if isinstance(record.get("payload"), dict)
        ]
        try:
            return import_terminal_bench_trials(self.store, run, results)
        except Exception as error:  # noqa: BLE001 - 批次级失败也要结构化
            return {
                "conflicts": [],
                "accepted": [],
                "invalid": [{
                    "trial_id": None,
                    "code": "TRIAL_IMPORT_FAILED",
                    "error_type": type(error).__name__,
                    "message": str(error),
                }],
            }

    def _managed_scoring_rows(
        self, run: dict[str, Any], results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """评分输入解析：Trial 套件用 Trial 存储，其余沿用传入的 Case 行。

        review R01：Task 级 Case 行是派生聚合（一行一个 Task），把它当评分
        输入会让质量分母变成"任务数"并丢掉其余 repeat；Trial 套件的评分输入
        必须来自按 ``trial_id`` 完整导入的 Trial 结果。冻结证据是唯一来源，
        因此 rescore 与首次评分走同一条路径。
        """
        from motte_sdk.benchmark_plugins import suite_for_run

        suite = suite_for_run(run)
        if suite is None or suite[0] != "terminal-bench-harbor":
            return results
        trials = getattr(self.store, "trials", None)
        if trials is None:
            return results
        rows: list[dict[str, Any]] = []
        for record in trials.list_for_run(run["id"]):
            payload = record.get("result")
            if not isinstance(payload, dict):
                continue
            rows.append({
                "case_id": record["task_key"],
                "trial_id": record["trial_id"],
                "result": payload,
                "outcome": "responded",
            })
        # Trial 存储为空（例如 manifest 没冻结计划）时保留既有行为。
        return rows or results

    def _score_results(
        self,
        run_id: str,
        results: list[dict[str, Any]],
        emit_events: bool,
    ) -> list[dict[str, Any]]:
        run = self._load(run_id)
        if run.get("manifest", {}).get("benchmark_provenance"):
            from motte_sdk.suites import managed_scores

            scores = managed_scores(run, self._managed_scoring_rows(run, results))
            if emit_events:
                for score in scores:
                    self._emit(run_id, "score", score)
            return scores
        if self._scenario_shaped(run):
            # M5-T05：Scenario Run 的评分输入是执行器冻结的 FrozenObservation
            # （R6）。评分只读冻结证据与它声明的产物；普通离线 rescore 走同一条
            # 路径，因此天然复用同一份证据、不碰业务工具与模型。
            from .scenario_backend import scenario_scores

            return scenario_scores(run, results)
        scores: list[dict[str, Any]] = []
        for entry in results:
            if "expected" in entry:
                passed = self._comparable(entry["result"]) == entry["expected"]
                scores.append({"case_id": entry["case_id"], "passed": passed})
                if emit_events:
                    self._emit(run_id, "score", {"case_id": entry["case_id"], "passed": passed})
        return scores

    @staticmethod
    def _scenario_shaped(run: dict[str, Any]) -> bool:
        """这份 Run 是否由 scenario@1 逐步骤驱动（决定评分装配路径）。"""
        execution = ((run.get("manifest") or {}).get("execution") or {})
        return (
            isinstance(execution, dict)
            and execution.get("backend_id") == "scenario"
            and (run.get("manifest") or {}).get("workflow_snapshot") is not None
        )

    def _begin_case_attempt(
        self, run: dict[str, Any], case_id: str, *, trial_id: str | None = None,
    ) -> dict[str, Any]:
        """开始一次 Case attempt；``trial_id`` 存在时冲突域收窄到该 Trial。

        ``attempt_no`` 仍是该 (run, case) 内全序编号（既有唯一键不变），
        传输重试只会产生新的 attempt_no，不会产生新的 Trial（M3-G04）。
        """
        previous = [
            item for item in self.store.attempts.list_for_run(run["id"])
            if item.get("case_id") == case_id
        ]
        now = datetime.now(UTC).isoformat()
        backend = ((run.get("manifest") or {}).get("execution") or {}).get("backend_id")
        attempt = self.store.attempts.begin({
            "run_id": run["id"],
            "case_id": case_id,
            "attempt_no": len(previous) + 1,
            "execution_backend_id": backend,
            "idempotency_key": f"{run['id']}:{case_id}:{trial_id or '-'}:{len(previous) + 1}",
            "prepared_at": now,
            "trial_id": trial_id,
        }, expected_run_revision=run["revision"], expected_run_status=run["status"])
        try:
            return self.store.attempts.dispatch(
                attempt["id"],
                expected_revision=attempt["revision"],
                run_id=run["id"],
                expected_run_revision=run["revision"],
                expected_run_status=run["status"],
            )
        except RunConflictError:
            current = self.store.attempts.get(attempt["id"])
            if current is not None and current.get("status") == "prepared":
                self.store.attempts.transition(
                    attempt["id"], expected_revision=current["revision"],
                    expected_status="prepared", status="failed",
                    changes={
                        "finished_at": datetime.now(UTC).isoformat(),
                        "error": {"code": "DISPATCH_ABORTED"},
                    },
                )
            raise

    def _complete_case_attempt(
        self,
        attempt: dict[str, Any],
        case_run: dict[str, Any],
        *,
        failed: bool,
    ) -> None:
        self._complete_open_attempt(attempt, case_run, failed=failed, emit_event=True)

    def _complete_open_attempt(
        self,
        attempt: dict[str, Any],
        case_run: dict[str, Any],
        *,
        failed: bool,
        emit_event: bool,
    ) -> None:
        """完成一个开放的 case attempt 并落盘结果；emit_event=False 用于
        事件已先行发出的补跑/落盘路径（时间线不重复报同一结果）。"""
        result = case_run.get("result")
        event_type = "case_call_failed" if failed else "model_response"
        self.store.attempts.complete(
            attempt["id"],
            expected_revision=attempt["revision"],
            status="failed" if failed else "succeeded",
            changes={
                "finished_at": datetime.now(UTC).isoformat(),
                "result": deepcopy(result),
            },
            case_run=case_run,
            event={
                "run_id": attempt["run_id"],
                "type": event_type,
                "case_id": attempt["case_id"],
                "result": deepcopy(result),
            } if emit_event else None,
        )
        self._notify_latest_event(attempt["run_id"])

    def _stored_trial_payloads(self, run_id: str) -> list[dict[str, Any]]:
        """Trial 存储里的载荷（一个计划 Trial 一条），供聚合与视图消费。"""
        trials = getattr(self.store, "trials", None)
        if trials is None:
            return []
        payloads: list[dict[str, Any]] = []
        for record in trials.list_for_run(run_id):
            payload = record.get("result")
            if isinstance(payload, dict):
                payloads.append(deepcopy(payload))
        return payloads

    def _ensure_trial_plans(self, run: dict[str, Any]) -> dict[str, Any]:
        """创建时落库，执行/终态化时幂等补齐旧 Run 的冻结计划。

        幂等：重复调用只会得到 ``identical``。计划先落库之后，取消/超时终态化
        才有计划单元可补处置，跨进程取消请求也不会因为"计划还不存在"而让
        覆盖分母消失。
        """
        from motte_sdk.benchmark_plugins import suite_for_run, validate_frozen_trial_plans

        suite = suite_for_run(run)
        if suite is None or suite[0] != "terminal-bench-harbor":
            return {"skipped": True}
        trials = getattr(self.store, "trials", None)
        if trials is None:
            return {"skipped": True}
        plan = validate_frozen_trial_plans(self.store, run)
        if not plan:
            return {"skipped": True}
        created = trials.create_plans([dict(item) for item in plan])
        statuses: dict[str, int] = {}
        for item in created:
            statuses[str(item["status"])] = statuses.get(str(item["status"]), 0) + 1
        conflicts = [item for item in created if item["status"] == "conflict"]
        if conflicts:
            # 计划冲突（同 trial_id 异内容）在启动前就要显式失败，不能带着
            # "身份已经漂移"的计划去跑。
            raise RunConflictError(
                f"frozen trial plan conflicts with stored plans: "
                f"{[item['trial_id'] for item in conflicts][:5]}"
            )
        return {"planned": len(plan), "statuses": statuses}

    def _dispose_rejected_trials(
        self, run: dict[str, Any], trial_records: list[dict[str, Any]],
        accepted: set[str], invalid: list[dict[str, Any]],
    ) -> list[str]:
        """被拒绝的计划单元补 indeterminate 占位（不是证据，但必须有处置）。"""
        rejected = {
            str(item.get("trial_id")) for item in invalid if item.get("trial_id")
        }
        rejected -= accepted
        if not rejected:
            return []
        plan_index = self._frozen_plan_index(run)
        pending = [
            record for record in trial_records
            if record["trial_id"] in rejected
            and record["trial_id"] in plan_index
            and self.store.trials is not None
            and (self.store.trials.get(record["trial_id"]) or {}).get("result") is None
        ]
        if not pending:
            return []
        from motte_sdk.benchmark_plugins import import_terminal_bench_trials

        results = [
            {
                "case_id": record["task_key"],
                "trial_id": record["trial_id"],
                "result": {
                    "trial_id": record["trial_id"],
                    "task_key": record["task_key"],
                    "repeat_index": record.get("repeat_index"),
                    "disposition": "indeterminate",
                    "verifier_observation": {"status": "missing_verifier_evidence"},
                    "coverage": {
                        "items": {
                            "cost_usd": "unavailable",
                            "model_identity": "unavailable",
                            "trajectory": "unavailable",
                        },
                        "reason": "the runner payload for this trial was rejected",
                    },
                    "synthesized_by": "run-service:rejected-payload",
                },
            }
            for record in pending
        ]
        import_terminal_bench_trials(self.store, run, results)
        return [item["trial_id"] for item in results]

    def _finish_after_terminal_import(
        self, run_id: str, trial_import: dict[str, Any],
        invalid: list[dict[str, Any]], unmapped: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Run 已被（并发）取消/终态化，但仍要保存已冻结证据的结论。

        终态不复活、任务级快照不改写；这里只为**本次执行**已导入的 Trial 证据
        补一个可审计的评分结论（新 pass，不改变 Run 状态），让"取消前已完成的
        Trial"在报告里可见，并留下事件说明结果是在终态之后落库的。
        """
        run = self._load(run_id)
        stored = self._stored_trial_payloads(run_id)
        scores: list[dict[str, Any]] = []
        error: Exception | None = None
        try:
            scores = self._score_results(run_id, [], False)
        except Exception as scoring_error:  # noqa: BLE001 - 终态后导入不能反噬
            error = scoring_error
        self.emit_run_event(run_id, "external_job_results_after_terminal", {
            "run_status": run["status"],
            "accepted": len(trial_import.get("accepted") or []),
            "invalid": len(invalid),
            "unmapped": len(unmapped),
            "trials_with_evidence": len(stored),
            "scores": len(scores),
            **({"scoring_error": type(error).__name__} if error else {}),
        })
        if scores:
            try:
                self._append_scoring_pass(
                    run_id, scores, source="terminal-import",
                    extra_summary={"terminal_import": {
                        "accepted": len(trial_import.get("accepted") or []),
                        "invalid": len(invalid),
                        "unmapped": len(unmapped),
                    }},
                )
            except Exception as append_error:  # noqa: BLE001 - 结论失败不改终态
                self.emit_run_event(run_id, "terminal_import_scoring_failed", {
                    "error_type": type(append_error).__name__,
                    "message": str(append_error),
                })
        return self._view(run_id)

    def _ensure_trial_dispositions(
        self, run: dict[str, Any], *, disposition: str,
    ) -> list[dict[str, Any]]:
        """给**尚未产出结果**的计划 Trial 补上终态处置（review R01/T07）。

        取消、超时、unsupported 都必须让每个计划单元有处置：缺行会让覆盖
        分母悄悄变小，也会让"跑过一个 Trial 后被取消"看起来像"什么都没跑"。
        已落盘的 Trial 结果绝不覆盖（原始证据不可改写）。
        """
        from motte_sdk.benchmark_plugins import import_terminal_bench_trials, suite_for_run

        suite = suite_for_run(run)
        if suite is None or suite[0] != "terminal-bench-harbor":
            return []
        trials = getattr(self.store, "trials", None)
        if trials is None:
            return []
        self._ensure_trial_plans(run)
        pending = [
            record for record in trials.list_for_run(run["id"])
            if record.get("result") is None
        ]
        if not pending:
            return []
        results = [
            {
                "case_id": record["task_key"],
                "trial_id": record["trial_id"],
                "result": {
                    "trial_id": record["trial_id"],
                    "task_key": record["task_key"],
                    "repeat_index": record.get("repeat_index"),
                    "disposition": disposition,
                    "verifier_observation": {"status": "missing_verifier_evidence"},
                    "coverage": {
                        "items": {
                            "cost_usd": "unavailable",
                            "model_identity": "unavailable",
                            "trajectory": "unavailable",
                        },
                        "reason": f"run reached a terminal state ({disposition}) before this trial produced evidence",
                    },
                    "synthesized_by": "run-service:terminal-disposition",
                },
            }
            for record in pending
        ]
        import_terminal_bench_trials(self.store, run, results)
        return [item["trial_id"] for item in results]

    def _finish_unattempted(
        self, run_id: str, *, final_status: str | None = None,
        final_error: dict[str, Any] | None = None,
        final_changes: dict[str, Any] | None = None,
        terminal_event_payload: dict[str, Any] | None = None,
    ) -> None:
        run = self._load(run_id)
        existing_rows = self.store.case_runs.list_for_run(run_id)
        done = {row["case_id"] for row in existing_rows}
        open_case_ids = {
            attempt["case_id"] for attempt in self.store.attempts.list_open(run_id)
        }
        synthetic_rows = [
            {"run_id": run_id, "case_id": case_id, "outcome": "not_attempted", "result": None}
            for case_id in run["case_ids"]
            if case_id not in done and case_id not in open_case_ids
        ]
        # 计划 Trial 也要有终态处置：取消/超时后"哪些 Trial 没跑"必须可查，
        # 而不是只有 Task 层的 not_attempted（review R01/T07）。
        self._ensure_trial_dispositions(
            run,
            disposition=(
                "cancelled" if final_status == "cancelled"
                else "indeterminate" if run.get("started_at") else "not_attempted"
            ),
        )
        rows = existing_rows + synthetic_rows
        scoring_error: Exception | None = None
        try:
            scores = self._score_results(run_id, rows, False)
        except Exception as error:
            scoring_error = error
            scores = [
                {
                    "case_id": row["case_id"],
                    "passed": None,
                    "details": {
                        "code": "SCORING_UNAVAILABLE",
                        "error": str(error),
                    },
                }
                for row in rows
            ]
        effective_error = deepcopy(final_error)
        if scoring_error is not None:
            scoring_details = {"error_type": type(scoring_error).__name__}
            if effective_error:
                effective_error = {
                    **effective_error,
                    "details": {
                        **(effective_error.get("details") or {}),
                        "scoring_error": scoring_details,
                    },
                }
            else:
                effective_error = {
                    "code": "SCORING_UNAVAILABLE",
                    "message": "terminal run could not be fully scored",
                    "details": scoring_details,
                }
        append_args = {
            "source": "terminal-unattempted",
            "final_status": final_status,
            "final_error": effective_error,
            "final_changes": final_changes,
            "terminal_event_payload": terminal_event_payload,
            "require_no_open_attempts": True,
            "allow_aggregate_failure": True,
            "case_rows": synthetic_rows,
        }
        try:
            self._append_scoring_pass(run_id, scores, **append_args)
        except Exception as error:
            if isinstance(error, RunConflictError):
                raise
            # A malformed plugin result must not veto cancellation/unsupported
            # terminalization. Persist a valid unjudged evidence set instead.
            fallback: list[dict[str, Any]] = []
            seen: set[str] = set()
            for row in rows:
                case_id = row.get("case_id")
                if isinstance(case_id, str) and case_id and case_id not in seen:
                    seen.add(case_id)
                    fallback.append({
                        "case_id": case_id,
                        "passed": None,
                        "details": {"code": "SCORING_UNAVAILABLE"},
                    })
            fallback_details = {"error_type": type(error).__name__}
            if effective_error:
                append_args["final_error"] = {
                    **effective_error,
                    "details": {
                        **(effective_error.get("details") or {}),
                        "scoring_error": fallback_details,
                    },
                }
            else:
                append_args["final_error"] = {
                    "code": "SCORING_UNAVAILABLE",
                    "message": "terminal run could not be fully scored",
                    "details": fallback_details,
                }
            self._append_scoring_pass(run_id, fallback, **append_args)

    def _execute_benchmark(self, run, invoke):
        from motte_eval.execution import CONTINUE_ERROR_CLASSES
        from motte_provider.errors import classify_exception

        run_id = run["id"]
        if run["status"] == "queued":
            self._transition(run_id, "preparing")
        if self._load(run_id)["status"] != "running":
            self._transition(run_id, "running")
        rows = self.store.case_runs.list_for_run(run_id)
        done = {row["case_id"] for row in rows}
        # A durable stop marker in the failed case also survives a crash before
        # final run status/scores are persisted.
        stop = any(row.get("stop_run") for row in rows)
        total = len(run["case_ids"])
        # 瞬时失败（可继续错误类）暂不落盘：attempt 保持开放，收尾前统一补跑一次。
        pending: dict[str, tuple[int, dict[str, Any], dict[str, Any]]] = {}
        for ordinal, case_id in enumerate(run["case_ids"], 1):
            if case_id in done:
                self._notify_progress({
                    "event": "case_skipped", "run_id": run_id, "case_id": case_id,
                    "ordinal": ordinal, "total": total, "reason": "already_persisted",
                })
                continue
            cancelled = self._cancel_checkpoint(run_id, pending)
            if cancelled is not None:
                return cancelled
            if stop:
                self.store.case_runs.upsert({"run_id": run_id, "case_id": case_id,
                                            "outcome": "not_attempted", "result": None})
                self._notify_progress({
                    "event": "case_skipped", "run_id": run_id, "case_id": case_id,
                    "ordinal": ordinal, "total": total, "reason": "run_stopped",
                })
                continue
            self._notify_progress({
                "event": "case_started", "run_id": run_id, "case_id": case_id,
                "ordinal": ordinal, "total": total,
            })
            started = perf_counter()
            try:
                attempt = self._begin_case_attempt(self._load(run_id), case_id)
            except RunConflictError:
                cancelled = self._cancel_checkpoint(run_id, pending)
                if cancelled is not None:
                    return cancelled
                raise
            try:
                if invoke is None:
                    raise ValueError("benchmark requires an execution backend invoke hook")
                result = invoke(case_id)
            except Exception as error:
                if getattr(error, "quarantine", False):
                    # 副作用后证据边界失败：不确定状态走隔离（needs_review），
                    # attempt 仍处 dispatching → _fail_or_quarantine 判定为不确定。
                    return self._fail_or_quarantine(run_id, error)
                result = deepcopy(getattr(error, "evidence", None)) or {
                    "error": {"class": classify_exception(error), "message": str(error)}}
            error = result.get("error") if isinstance(result, dict) else None
            # agent 后端有文件副作用：dispatch 后结果不确定的整段补跑被禁止，
            # 瞬态失败立即落盘交由操作员显式 retry（M1-G09）。
            allow_second_chance = (
                run.get("manifest", {}).get("execution", {}).get("backend_id") != "builtin-agent"
            )
            if error and error.get("class") in CONTINUE_ERROR_CLASSES and allow_second_chance:
                pending[case_id] = (ordinal, attempt, result)
                self._emit(run_id, "case_call_failed",
                           {"case_id": case_id, "result": deepcopy(result)})
                self._notify_progress({
                    "event": "case_finished", "run_id": run_id, "case_id": case_id,
                    "ordinal": ordinal, "total": total,
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                    "outcome": "call_failed", "reason": "second_chance_pending",
                    **self._result_summary(result),
                })
                continue
            # 走到这里的一定不是瞬时错误；非空 error 即持久停止标记。
            stop = bool(error)
            entry = {"run_id": run_id, "case_id": case_id,
                     "result": result, "stop_run": stop}
            self._complete_case_attempt(attempt, entry, failed=bool(error))
            self._notify_progress({
                "event": "case_finished", "run_id": run_id, "case_id": case_id,
                "ordinal": ordinal, "total": total,
                "duration_ms": round((perf_counter() - started) * 1000, 3),
                "outcome": "call_failed" if error else "responded",
                **self._result_summary(result),
            })
        cancelled = self._cancel_checkpoint(run_id, pending)
        if cancelled is not None:
            return cancelled
        cancelled = self._sweep_transient_failures(run_id, invoke, pending)
        if cancelled is not None:
            return cancelled
        self._transition(run_id, "collecting")
        cancelled = self._honor_cancellation(run_id)
        if cancelled is not None:
            return cancelled
        self._transition(run_id, "scoring")
        cancelled = self._honor_cancellation(run_id)
        if cancelled is not None:
            return cancelled
        rows = self.store.case_runs.list_for_run(run_id)
        errors = [row["result"]["error"] for row in rows
                  if isinstance(row.get("result"), dict) and row["result"].get("error")]
        final_status = "failed" if errors else "completed"
        try:
            scores = self._score_results(run_id, rows, False)
            cancelled = self._honor_cancellation(run_id)
            if cancelled is not None:
                return cancelled
            self._append_scoring_pass(
                run_id,
                scores,
                source="initial",
                final_status=final_status,
                final_error=errors[0] if errors else None,
            )
        except Exception as error:
            return self._fail_or_quarantine(run_id, error)
        return self._view(run_id)

    def _sweep_transient_failures(
        self, run_id: str, invoke: Callable[[str], Any],
        pending: dict[str, tuple[int, dict[str, Any], dict[str, Any]]],
    ) -> dict[str, Any] | None:
        """瞬时失败题（server/network/timeout/rate_limit）收尾前补跑一次。

        网关瞬时 5xx 一类的不稳定不该让整次基准一票否决：主循环全部跑完后，
        对挂在 pending 里的瞬时失败题再调一次；补跑成功则用恢复结果完成该题
        记账（首次错误以 first_attempt_error 嵌入结果留证），仍失败则落盘
        最终错误。存储契约不变：每题一条 case 结果、一个 attempt 记账。
        """
        from motte_provider.errors import classify_exception

        total = len(self._load(run_id)["case_ids"])
        for case_id in list(pending):
            if case_id not in pending:
                continue  # 已因取消被统一落盘
            cancelled = self._cancel_checkpoint(run_id, pending)
            if cancelled is not None:
                return cancelled
            ordinal, attempt, first_result = pending.pop(case_id)
            self._notify_progress({
                "event": "case_started", "run_id": run_id, "case_id": case_id,
                "ordinal": ordinal, "total": total, "reason": "second_chance",
            })
            started = perf_counter()
            try:
                result = invoke(case_id)
            except Exception as error:
                result = deepcopy(getattr(error, "evidence", None)) or {
                    "error": {"class": classify_exception(error), "message": str(error)}}
            error = result.get("error") if isinstance(result, dict) else None
            if not error:
                result = {**result,
                          "first_attempt_error": deepcopy(first_result["error"])}
            entry = {"run_id": run_id, "case_id": case_id,
                     "result": result, "stop_run": bool(error)}
            # 首次失败的 case_call_failed 事件已在主循环发过，这里只在补跑
            # 成功时发 model_response，避免时间线重复报同一失败。
            self._complete_open_attempt(attempt, entry, failed=bool(error),
                                        emit_event=not error)
            self._notify_progress({
                "event": "case_finished", "run_id": run_id, "case_id": case_id,
                "ordinal": ordinal, "total": total,
                "duration_ms": round((perf_counter() - started) * 1000, 3),
                "outcome": "call_failed" if error else "responded",
                "reason": "second_chance",
                **self._result_summary(result),
            })
        return None

    def _cancel_checkpoint(
        self, run_id: str,
        pending: dict[str, tuple[int, dict[str, Any], dict[str, Any]]],
    ) -> dict[str, Any] | None:
        """取消检查点：先把挂起的瞬时失败题按首次错误落盘，取消才能到终态。"""
        if pending and isinstance(self._load(run_id).get("cancellation"), dict):
            self._settle_pending(run_id, pending)
        return self._honor_cancellation(run_id)

    def _settle_pending(
        self, run_id: str,
        pending: dict[str, tuple[int, dict[str, Any], dict[str, Any]]],
    ) -> None:
        """把挂起的瞬时失败题按首次错误完成记账（事件已在主循环发过）。"""
        total = len(self._load(run_id)["case_ids"])
        for case_id in list(pending):
            ordinal, attempt, result = pending.pop(case_id)
            entry = {"run_id": run_id, "case_id": case_id,
                     "result": result, "stop_run": False}
            self._complete_open_attempt(attempt, entry, failed=True, emit_event=False)
            self._notify_progress({
                "event": "case_finished", "run_id": run_id, "case_id": case_id,
                "ordinal": ordinal, "total": total, "reason": "second_chance_aborted",
            })

    @staticmethod
    def _comparable(result: Any) -> Any:
        """envelope 形状的结果取 content 参与比较；其余按原值。"""
        if isinstance(result, dict) and "content" in result:
            return result["content"]
        return result

    def _fail_or_quarantine(self, run_id: str, error: Exception) -> dict[str, Any]:
        uncertain = [
            attempt for attempt in self.store.attempts.list_for_run(run_id)
            if attempt.get("status") in {"dispatching", "indeterminate"}
        ]
        if not uncertain:
            return self._fail(run_id, error)
        run = self._load(run_id)
        attempt_ids = [attempt["id"] for attempt in uncertain]
        now = datetime.now(UTC).isoformat()
        self.store.attempts.quarantine_indeterminate(
            run_id,
            expected_run_revision=run["revision"],
            expected_run_status=run["status"],
            changes={
                "updated_at": now,
                "finished_at": now,
                "error": {
                    "code": "CALL_OUTCOME_INDETERMINATE",
                    "message": "the provider returned but durable case completion failed",
                    "details": {
                        "attempt_ids": attempt_ids,
                        # 保留原始异常类型与消息（R3 #2）：采集/清理等次生失败不得覆盖
                        "persistence_error": f"{type(error).__name__}: {error}",
                    },
                },
            },
            event={
                "run_id": run_id,
                "type": "needs_review",
                "status": "needs_review",
                "attempt_ids": attempt_ids,
            },
        )
        self._notify_latest_event(run_id)
        return self._view(run_id)

    def _fail(self, run_id: str, error: Exception) -> dict[str, Any]:
        run = self._load(run_id)
        error_payload: dict[str, Any] = {"type": type(error).__name__, "message": str(error)}
        error_class = getattr(error, "error_class", None)
        if error_class is not None:
            error_payload["class"] = error_class
        evidence = getattr(error, "evidence", None)
        if evidence is not None:
            error_payload["evidence"] = evidence
        if run["status"] in self.TERMINAL:
            return self._view(run_id)
        if self._trial_shaped(run):
            self._finish_unattempted(run_id, final_status="failed", final_error=error_payload)
            return self._view(run_id)
        return self._transition(run_id, "failed", changes={"error": error_payload})

    def _transition(
        self,
        run_id: str,
        status: str,
        *,
        changes: dict[str, Any] | None = None,
        event_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run = self._load(run_id)
        previous = run["status"]
        if status not in TRANSITIONS.get(previous, set()):
            raise ValueError(f"invalid transition: {previous} -> {status}")
        now = datetime.now(UTC).isoformat()
        patch = {**(changes or {}), "updated_at": now}
        if status == "running" and not run.get("started_at"):
            patch["started_at"] = now
        if status in self.TERMINAL:
            patch["finished_at"] = now
        self.store.runs.transition(
            run_id,
            expected_revision=run["revision"],
            expected_status=previous,
            status=status,
            changes=patch,
            event={
                "run_id": run_id,
                "type": status,
                "status": status,
                **(event_payload if event_payload is not None else (changes or {})),
            },
        )
        self._notify_latest_event(run_id)
        return self._view(run_id)

    @staticmethod
    def _expected_for(
        invoke: Callable[[str], Any] | None,
        expectations: dict[str, Any] | None,
        case_id: str,
    ) -> Any:
        from .replay_run import NO_EXPECTATION

        if expectations is not None:
            return expectations.get(case_id, NO_EXPECTATION)
        receiver = getattr(invoke, "__self__", None)
        expected_for = getattr(invoke, "expected_for", None)
        if not callable(expected_for):
            expected_for = getattr(receiver, "expected_for", None)
        if callable(expected_for):
            value = expected_for(case_id)
            fixture = getattr(invoke, "fixture", None)
            if not isinstance(fixture, dict):
                fixture = getattr(receiver, "fixture", None)
            if value is None and not (
                isinstance(fixture, dict) and "expected" in fixture.get(case_id, {})
            ):
                return NO_EXPECTATION
            return value
        return NO_EXPECTATION

    @staticmethod
    def _result_summary(result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {}
        summary: dict[str, Any] = {}
        metering = result.get("metering")
        if isinstance(metering, dict):
            for key in ("latency_ms", "attempts", "retry_count", "error_class"):
                value = metering.get(key)
                if isinstance(value, (int, float, str)) or value is None:
                    summary[key] = value
        usage = result.get("usage")
        if isinstance(usage, dict):
            for source, target in (("prompt_tokens", "prompt_tokens"),
                                   ("completion_tokens", "completion_tokens"),
                                   ("total_tokens", "total_tokens")):
                value = usage.get(source)
                if isinstance(value, int) and not isinstance(value, bool):
                    summary[target] = value
        cost = result.get("cost")
        if isinstance(cost, dict):
            if isinstance(cost.get("total"), (int, float)) and not isinstance(cost.get("total"), bool):
                summary["cost_total"] = cost["total"]
            if isinstance(cost.get("price_table_version"), str):
                summary["price_table_version"] = cost["price_table_version"]
        error = result.get("error")
        if isinstance(error, dict) and isinstance(error.get("class"), str):
            summary["error_class"] = error["class"]
        return summary

    def _notify_progress(self, progress: dict[str, Any]) -> None:
        for observer in tuple(self._progress_observers):
            try:
                observer(deepcopy(progress))
            except Exception:
                pass

    def _emit(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        stored = self.store.events.append({"run_id": run_id, "type": event_type, **payload})
        self._notify_event(stored)

    def _notify_latest_event(self, run_id: str) -> None:
        events = self.store.events.list_for_run(run_id)
        if events:
            self._notify_event(events[-1])

    def _notify_event(self, event: dict[str, Any]) -> None:
        for observer in tuple(self._event_observers):
            try:
                observer(deepcopy(event))
            except Exception:
                pass
