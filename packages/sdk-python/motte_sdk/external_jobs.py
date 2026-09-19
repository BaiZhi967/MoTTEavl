"""外部 Job 应用层监督（启动边界、观察、采集与结局映射）。

应用层持有调度主权：先持久化启动意图（含 launch_token）再调用
adapter.start；恢复路径只观察/采集，绝不重启。``DurableExternalJobRunner``
把 supervisor 装配到 Job 存储与 Artifact 冻结上：outcome 先冻结为受控
不可变工件，再按 job_id + record key + parser_version 幂等导入；同键不同
内容是 conflict，停止最终化并保留两份摘要。取消后的迟到结果只作审计
证据，终态不复活。
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from motte_contracts.external_job import (
    ExternalJobHandle,
    ExternalJobSpec,
    ExternalJobStatus,
    new_launch_token,
)

# 通用进程 Job 的导入 parser 版本；C-Eval/CMMLU 迁入后由各自 parser 提供。
GENERIC_PARSER_VERSION = "process-generic@1"


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class ExternalJobSupervisor:
    """单 Job 生命周期监督：launch / run / recover。

    ``intent_journal`` 是启动意图的持久化通道（T03 替换为 Job 存储事务）；
    写入顺序必须是 launch_intent → adapter.start → launch_started。
    """

    def __init__(
        self,
        adapter: Any,
        *,
        intent_journal: Callable[[dict[str, Any]], None] | None = None,
        poll_interval_seconds: float = 0.1,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.adapter = adapter
        # 公开可替换：DurableExternalJobRunner 装配时写入 Job 存储。
        self.intent_journal = intent_journal
        self.poll_interval_seconds = poll_interval_seconds
        self._sleep = sleep
        self._monotonic = monotonic

    def _record(self, entry: dict[str, Any]) -> None:
        if self.intent_journal is not None:
            self.intent_journal(entry)

    def launch(self, spec: ExternalJobSpec) -> ExternalJobHandle:
        """prepare → 持久化启动意图（新 token）→ start → 持久化句柄。"""
        prepared = self.adapter.prepare(spec)
        token = new_launch_token()
        launching = prepared.model_copy(update={
            # 每次启动换发不可复用的新 token，先于 start 持久化。
            "launch_token": token,
            "status": ExternalJobStatus.launching,
            "launch_identity": {
                **(prepared.launch_identity or {}),
                "intent_recorded_at": _now_iso(),
                "launch_token": token,
            },
        })
        self._record({
            "event": "launch_intent",
            "spec": spec.model_dump(mode="json"),
            "handle": launching.model_dump(mode="json"),
        })
        started = self.adapter.start(spec, launching)
        self._record({
            "event": "launch_started",
            "handle": started.model_dump(mode="json"),
        })
        return started

    def run(self, spec: ExternalJobSpec) -> dict[str, Any]:
        """一次完整执行：launch 后观察至终态并采集。"""
        handle = self.launch(spec)
        return self.observe(spec, handle)

    def recover(self, spec: ExternalJobSpec, persisted_handle: ExternalJobHandle) -> dict[str, Any]:
        """崩溃恢复：只观察/采集，不 start；token 不可核验时 indeterminate。"""
        return self.observe(spec, persisted_handle)

    def interrupt(self, spec: ExternalJobSpec, handle: ExternalJobHandle) -> dict[str, Any]:
        """操作员取消：先中断本 Job 拥有的进程，再尽力采集部分工件。"""
        cancelled = self.adapter.interrupt(handle)
        results, error = self._collect_quiet(cancelled)
        return {
            "job_status": "cancelled",
            "results": results,
            "error": error,
            "handle": cancelled.model_dump(mode="json"),
        }

    # ------------------------------------------------------------------ 观察

    def observe(
        self, spec: ExternalJobSpec, handle: ExternalJobHandle,
    ) -> dict[str, Any]:
        poll_interval = float(
            spec.limits.get("poll_interval_seconds", self.poll_interval_seconds),
        )
        max_wall = spec.limits.get("max_wall_seconds")
        deadline = (
            self._monotonic() + float(max_wall)
            if isinstance(max_wall, (int, float)) else None
        )
        while True:
            handle = self.adapter.poll(handle)
            if handle.status != ExternalJobStatus.active:
                break
            if deadline is not None and self._monotonic() > deadline:
                interrupted = self.adapter.interrupt(handle)
                results, error = self._collect_quiet(interrupted)
                return {
                    "job_status": "failed",
                    "results": results,
                    "error": {
                        "code": "JOB_TIMEOUT",
                        "message": "external job exceeded max_wall_seconds",
                        "details": {"max_wall_seconds": max_wall},
                    },
                    "handle": interrupted.model_dump(mode="json"),
                }
            self._sleep(max(poll_interval, 0.01))

        if handle.status == ExternalJobStatus.indeterminate:
            return {
                "job_status": "indeterminate",
                "results": [],
                "error": {
                    "code": "JOB_OUTCOME_INDETERMINATE",
                    "message": (
                        "job process cannot be verified by launch token and no "
                        "controlled results exist; never restarted automatically"
                    ),
                },
                "handle": handle.model_dump(mode="json"),
            }

        results, collect_error = self._collect_quiet(handle)
        exit_code = handle.owned_resources.get("exit_code")
        error = collect_error
        if handle.status == ExternalJobStatus.failed and error is None:
            error = {
                "code": "JOB_NONZERO_EXIT",
                "message": "external job process exited nonzero; partial results kept",
                "details": {"exit_code": exit_code},
            }
        return {
            "job_status": handle.status.value,
            "results": results,
            "error": error,
            "handle": handle.model_dump(mode="json"),
        }

    def _collect_quiet(
        self, handle: ExternalJobHandle,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """采集并转成 outcome 载荷；采集失败记录错误而不是抛出。"""
        try:
            results, _cursor = self.adapter.collect(
                handle, dict(handle.collection_cursor or {}),
            )
        except Exception as error:  # noqa: BLE001 - 采集失败进入证据
            return ([], {
                "code": getattr(error, "code", "JOB_COLLECT_FAILED"),
                "message": str(error),
            })
        payload = [item.model_dump(mode="json") for item in results]
        return (payload, None)


class DurableExternalJobRunner:
    """job 模式入口装配：supervisor + Job 存储 + Artifact 冻结 + 幂等导入。

    作为 ``ExecutionHandle.run_job`` 使用：同一 Run 再次进入（崩溃后重派）
    时，已有可恢复 Job 只观察/采集，绝不重新启动；outcome 先冻结为受控
    Artifact，再逐条幂等导入（同键同内容 no-op，同键不同内容 conflict 并
    把结局改为 failed）。取消后 ``import_late_results`` 只写审计事件。
    """

    def __init__(
        self,
        supervisor: ExternalJobSupervisor,
        job_store: Any,
        *,
        artifacts: Any = None,
        work_root: str | Path = "var/external-jobs",
        parser_version: str = GENERIC_PARSER_VERSION,
    ) -> None:
        self.supervisor = supervisor
        self.job_store = job_store
        self.artifacts = artifacts
        self.work_root = str(work_root)
        self.parser_version = parser_version
        self._service: Any = None

    def bind_service(self, service: Any) -> None:
        self._service = service

    # -------------------------------------------------------------- 启动日志

    def _journal(self, record: dict[str, Any]) -> None:
        event = record.get("event")
        if event == "launch_intent":
            handle = record.get("handle") or {}
            self.job_store.begin_job({
                "job_id": handle.get("job_id"),
                "run_id": handle.get("run_id"),
                "status": ExternalJobStatus.launching.value,
                "launch_token": handle.get("launch_token"),
                "spec": record.get("spec") or {},
                "handle": handle,
                "checkpoint": {},
            })
        elif event == "launch_started":
            handle = record.get("handle") or {}
            job_id = str(handle.get("job_id") or "")
            current = self.job_store.get_job(job_id)
            if current is not None and current.get("status") == "cancelled":
                # 取消落在启动窗口内：立即中断刚启动的进程，不复活取消状态。
                try:
                    self.supervisor.adapter.interrupt(
                        ExternalJobHandle.model_validate(handle),
                    )
                except Exception:  # noqa: BLE001 - 中断失败保留句柄供审计
                    pass
                self.job_store.update_job(job_id, {"handle": handle})
                return
            self.job_store.update_job(job_id, {
                "status": ExternalJobStatus.active.value,
                "handle": handle,
            })

    # ---------------------------------------------------------------- 执行

    def __call__(self, run: dict[str, Any]) -> dict[str, Any]:
        from .execution_backends import external_job_spec_from_run

        spec = external_job_spec_from_run(run, work_root=self.work_root)
        existing = self.job_store.jobs_for_run(spec.run_id)
        if existing:
            # 一个 Run 只启动一个 Job：已有记录就绝不再次 start。
            persisted = existing[-1]
            status = str(persisted.get("status") or "")
            if status in {"launching", "active", "collecting"}:
                handle = ExternalJobHandle.model_validate(persisted.get("handle") or {})
                outcome = self.supervisor.recover(spec, handle)
            else:
                # 终态 Job（采集后、最终化前崩溃）：从已导入记录重建 outcome，
                # 再次导入同键同内容为 no-op，不产生重复评分/工件关联。
                records = self.job_store.list_records(str(persisted.get("job_id") or ""))
                outcome = {
                    "job_status": status,
                    "results": [dict(record.get("payload") or {}) for record in records],
                    "error": None,
                    "handle": persisted.get("handle") or {},
                    "recovered_from": "job-store",
                }
        else:
            # 启动意图（含新 launch_token）先落 Job 存储再 start。
            self.supervisor.intent_journal = self._journal
            outcome = self.supervisor.run(spec)
        outcome = self._settle(spec, outcome)
        return outcome

    def _settle(self, spec: ExternalJobSpec, outcome: dict[str, Any]) -> dict[str, Any]:
        handle = outcome.get("handle") or {}
        job_id = str(handle.get("job_id") or "")
        status = str(outcome.get("job_status") or "indeterminate")
        if not job_id:
            return outcome
        if status in {"settled", "failed", "cancelled", "indeterminate"}:
            # cancelled 是操作员终局：中断后迟到的 failed/indeterminate 观察
            # 不覆盖取消状态（原子条件更新，终态不复活也不降级）。
            applied = self.job_store.update_job(
                job_id, {"status": status, "handle": handle},
                guard_status_not="cancelled",
            )
            if applied is None:
                self.job_store.update_job(job_id, {"handle": handle})
        # 原始 outcome 先冻结为受控不可变 Artifact，再进入导入。
        artifact_id: str | None = None
        if self.artifacts is not None:
            frozen = {
                "job_status": status,
                "results": outcome.get("results") or [],
                "error": outcome.get("error"),
                "spec": spec.model_dump(mode="json"),
                "handle": handle,
            }
            artifact = self.artifacts.put_bytes(
                f"external-jobs/{spec.run_id}/{job_id}/outcome.json",
                json.dumps(frozen, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            )
            artifact_id = artifact.id
        imported = 0
        conflicts: list[dict[str, Any]] = []
        for item in outcome.get("results") or []:
            key = str(item.get("stable_case_key") or item.get("source_case_id") or "")
            if not key:
                continue
            result = self.job_store.import_record(
                job_id, key, self.parser_version,
                _canonical_hash(item), item,
            )
            if result.get("status") == "imported":
                imported += 1
            elif result.get("status") == "conflict":
                conflicts.append({
                    "source_record_key": key,
                    "existing": result.get("existing"),
                    "incoming": result.get("incoming"),
                })
        outcome["import"] = {
            "job_id": job_id,
            "imported": imported,
            "conflicts": conflicts,
            "artifact": artifact_id,
            "parser_version": self.parser_version,
        }
        if conflicts:
            # 停止最终化：结局改 failed 并保留两份来源摘要。
            outcome["job_status"] = "failed"
            outcome["error"] = {
                "code": "EXTERNAL_IMPORT_CONFLICT",
                "message": "same import key arrived with different content; "
                           "both summaries are preserved in the conflict ledger",
                "details": {"conflicts": conflicts[:10]},
            }
        return outcome

    # -------------------------------------------------------------- 取消

    def interrupt_run(self, run_id: str) -> dict[str, Any] | None:
        """操作员取消钩子：中断本 Run 活跃 Job 拥有的进程并落取消状态。"""
        recoverable = self.job_store.recoverable_for_run(run_id)
        if not recoverable:
            return None
        persisted = recoverable[0]
        handle = ExternalJobHandle.model_validate(persisted.get("handle") or {})
        cancelled = self.supervisor.interrupt(
            ExternalJobSpec.model_validate(persisted.get("spec") or {}), handle,
        )
        cancelled_handle = cancelled.get("handle") or {}
        self.job_store.update_job(
            str(cancelled_handle.get("job_id") or ""),
            {"status": ExternalJobStatus.cancelled.value, "handle": cancelled_handle},
        )
        return cancelled

    # -------------------------------------------------------------- 迟到结果

    def import_late_results(
        self, run_id: str, records: list[dict[str, Any]], *, job_id: str | None = None,
    ) -> dict[str, Any]:
        """迟到数据只作审计证据：不改 Run 状态、不新增评分（M2-A09）。"""
        jobs = self.job_store.jobs_for_run(run_id)
        if not jobs:
            raise KeyError("no external job for run: " + run_id)
        target = next(
            (job for job in jobs if job_id is None or job.get("job_id") == job_id),
            jobs[-1],
        )
        resolved_job_id = str(target.get("job_id"))
        imported = 0
        conflicts: list[dict[str, Any]] = []
        for record in records:
            key = str(record.get("case_id") or record.get("source_case_id") or "")
            if not key:
                continue
            result = self.job_store.import_record(
                resolved_job_id, key, self.parser_version,
                _canonical_hash(record), record,
            )
            if result.get("status") == "imported":
                imported += 1
            elif result.get("status") == "conflict":
                conflicts.append({
                    "source_record_key": key,
                    "existing": result.get("existing"),
                    "incoming": result.get("incoming"),
                })
        if self._service is not None:
            self._service.emit_run_event(run_id, "external_job_late_results", {
                "job_id": resolved_job_id,
                "imported": imported,
                "conflicts": conflicts,
                "audit_only": True,
            })
        return {
            "job_id": resolved_job_id,
            "imported": imported,
            "conflicts": conflicts,
            "audit_only": True,
        }
