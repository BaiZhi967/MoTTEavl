"""外部 Job 应用层监督（启动边界、观察、采集与结局映射）。

应用层持有调度主权：先持久化启动意图（含 launch_token）再调用
adapter.start；恢复路径只观察/采集，绝不重启。``DurableExternalJobRunner``
把 supervisor 装配到 Job 存储与 Artifact 冻结上。

review 修复后的最终化顺序（R04）：

1. **证据冻结**（内容寻址，同内容同路径、不同内容不同路径——绝不把
   原始完整证据覆盖成截断版本）：outcome 与原始输出 bundle 先落受控
   不可变 Artifact；
2. **幂等导入**（同键同内容 no-op，同键不同内容 conflict 并保留两份摘要）；
3. **checkpoint 提交**（cursor 指标、证据引用、``import_completed`` 标记）；
4. **最后才写终态**。导入中途崩溃时 Job 仍是非终态，恢复路径重新采集
   并幂等补齐，不重复启动。

观察循环消费**持久取消**请求（R05）：跨进程的 cancel 只落库也能在有
限时间内中断已核验所有权的进程。取消后的迟到结果只作审计证据，终态
不复活。
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

_TERMINAL_JOB_STATUSES = {"settled", "failed", "cancelled", "indeterminate"}
# 原始证据 bundle 的内容预算：单文件 8 MiB、总量 64 MiB；超出只存 hash。
_RAW_FILE_BUDGET = 8 * 1024 * 1024
_RAW_TOTAL_BUDGET = 64 * 1024 * 1024


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _short(digest: str) -> str:
    return digest.replace("sha256:", "")[:16]


class ExternalJobSupervisor:
    """单 Job 生命周期监督：launch / run / recover。

    ``intent_journal`` 是启动意图的持久化通道；写入顺序必须是
    launch_intent → adapter.start → launch_started。
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
        # 最近一次采集的 cursor（parser 版本与指标汇总随采集返回）。
        self.last_cursor: dict[str, Any] = {}

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

    def run(
        self, spec: ExternalJobSpec,
        *, should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """一次完整执行：launch 后观察至终态并采集。"""
        handle = self.launch(spec)
        return self.observe(spec, handle, should_cancel=should_cancel)

    def recover(
        self, spec: ExternalJobSpec, persisted_handle: ExternalJobHandle,
        *, should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """崩溃恢复：只观察/采集，不 start；token 不可核验时 indeterminate。"""
        return self.observe(spec, persisted_handle, should_cancel=should_cancel)

    def interrupt(self, spec: ExternalJobSpec, handle: ExternalJobHandle) -> dict[str, Any]:
        """操作员取消：先中断本 Job 拥有的进程，再尽力采集部分工件。"""
        cancelled = self.adapter.interrupt(handle)
        results, error = self._collect_quiet(cancelled)
        return {
            "job_status": "cancelled",
            "results": results,
            "error": error,
            "handle": cancelled.model_dump(mode="json"),
            "cursor": dict(self.last_cursor or {}),
        }

    # ------------------------------------------------------------------ 观察

    def observe(
        self,
        spec: ExternalJobSpec,
        handle: ExternalJobHandle,
        *,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        poll_interval = float(
            spec.limits.get("poll_interval_seconds", self.poll_interval_seconds),
        )
        max_wall = spec.limits.get(
            "max_wall_seconds", self.adapter.default_limits.get("max_wall_seconds")
            if hasattr(self.adapter, "default_limits") else None,
        )
        deadline = (
            self._monotonic() + float(max_wall)
            if isinstance(max_wall, (int, float)) else None
        )
        while True:
            # 持久取消请求（R05）：另一进程/实例落库的取消在这里被消费。
            if should_cancel is not None and should_cancel():
                cancelled = self.adapter.interrupt(handle)
                results, error = self._collect_quiet(cancelled)
                return {
                    "job_status": "cancelled",
                    "results": results,
                    "error": error,
                    "handle": cancelled.model_dump(mode="json"),
                    "cursor": dict(self.last_cursor or {}),
                }
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
                    "cursor": dict(self.last_cursor or {}),
                }
            self._sleep(max(poll_interval, 0.01))

        if handle.status == ExternalJobStatus.indeterminate:
            # 无可信完成标记：保留部分采集为审计证据，结论保持不确定（R10）。
            results, collect_error = self._collect_quiet(handle)
            return {
                "job_status": "indeterminate",
                "results": results,
                "error": {
                    "code": "JOB_OUTCOME_INDETERMINATE",
                    "message": (
                        "job process cannot be verified by launch token and no "
                        "trusted completion marker exists; partial results are "
                        "kept as audit evidence; never restarted automatically"
                    ),
                    "details": {"collect_error": collect_error},
                },
                "handle": handle.model_dump(mode="json"),
                "cursor": dict(self.last_cursor or {}),
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
            "cursor": dict(self.last_cursor or {}),
        }

    def _collect_quiet(
        self, handle: ExternalJobHandle,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """采集并转成 outcome 载荷；解析前先快照原始输出 hash（R09）。"""
        cursor = dict(handle.collection_cursor or {})
        snapshotter = getattr(self.adapter, "snapshot_outputs", None)
        if callable(snapshotter):
            try:
                cursor["raw_evidence"] = snapshotter(handle)
            except Exception:  # noqa: BLE001 - 快照失败不阻断采集
                cursor["raw_evidence"] = {"complete": False, "files": {}}
        try:
            results, collected = self.adapter.collect(handle, cursor)
        except Exception as error:  # noqa: BLE001 - 采集失败进入证据
            self.last_cursor = cursor
            return ([], {
                "code": getattr(error, "code", "JOB_COLLECT_FAILED"),
                "message": str(error),
            })
        payload = [item.model_dump(mode="json") for item in results]
        self.last_cursor = dict(collected or {})
        return (payload, None)


class DurableExternalJobRunner:
    """job 模式入口装配：supervisor + Job 存储 + Artifact 冻结 + 幂等导入。

    作为 ``ExecutionHandle.run_job`` 使用：同一 Run 再次进入（崩溃后重派）
    时，已有可恢复 Job 只观察/采集，绝不重新启动；终态且
    ``checkpoint.import_completed`` 的 Job 从已导入记录重建 outcome（快路径），
    终态但导入未完成的 Job 重新采集并幂等补齐（R04）。
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

    def bind_store(self, store: Any) -> None:
        """分派时绑定 Job 存储与工件根（external-benchmark 后端经 attach 注入）。"""
        external_jobs = getattr(store, "external_jobs", None)
        if external_jobs is not None:
            self.job_store = external_jobs
        if self.artifacts is None:
            import os

            from motte_storage.artifacts import ArtifactStore

            self.artifacts = ArtifactStore(os.environ.get("ARTIFACT_ROOT", "var/artifacts"))

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
                "checkpoint": {"import_completed": False},
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

    def _should_cancel(self, run_id: str, job_id: str) -> bool:
        """跨进程持久取消（R05）：消费 run 的 cancellation 与 job 的取消状态。"""
        if self._service is not None:
            store = getattr(self._service, "store", None)
            runs = getattr(store, "runs", None)
            if runs is not None:
                try:
                    run = runs.get(run_id)
                except Exception:  # noqa: BLE001 - 读取失败不影响观察
                    run = None
                if run is not None and run.get("cancellation"):
                    return True
        if job_id:
            job = self.job_store.get_job(job_id)
            if job is not None and str(job.get("status")) == "cancelled":
                return True
        return False

    def __call__(self, run: dict[str, Any]) -> dict[str, Any]:
        from .execution_backends import external_job_spec_from_run

        spec = external_job_spec_from_run(run, work_root=self.work_root)
        existing = self.job_store.jobs_for_run(spec.run_id)
        if existing:
            # 一个 Run 只启动一个 Job：已有记录就绝不再次 start。
            persisted = existing[-1]
            status = str(persisted.get("status") or "")
            checkpoint = persisted.get("checkpoint") or {}
            if status in _TERMINAL_JOB_STATUSES and checkpoint.get("import_completed"):
                # 快路径：终态且导入完成，从已导入记录重建 outcome。
                records = self.job_store.list_records(str(persisted.get("job_id") or ""))
                outcome = {
                    "job_status": status,
                    "results": [dict(record.get("payload") or {}) for record in records],
                    "error": (checkpoint.get("outcome_error") or None),
                    "handle": persisted.get("handle") or {},
                    "cursor": dict(checkpoint.get("cursor") or {}),
                    "recovered_from": "job-store",
                }
            else:
                # 非终态，或终态但导入未完成（R04）：重新观察/采集，幂等补齐。
                handle = ExternalJobHandle.model_validate(persisted.get("handle") or {})
                job_id = str(persisted.get("job_id") or handle.job_id)
                outcome = self.supervisor.recover(
                    spec, handle,
                    should_cancel=lambda: self._should_cancel(spec.run_id, job_id),
                )
                if status == "cancelled":
                    # 取消是操作员终局：观察到的其他结局只作审计，不复活。
                    outcome["job_status"] = "cancelled"
        else:
            # 启动意图（含新 launch_token）先落 Job 存储再 start；observe 的
            # should_cancel 绑定到实际 job_id，消费跨进程持久取消（R05）。
            self.supervisor.intent_journal = self._journal
            launched = self.supervisor.launch(spec)
            outcome = self.supervisor.observe(
                spec, launched,
                should_cancel=lambda: self._should_cancel(
                    spec.run_id, str(launched.job_id),
                ),
            )
        return self._settle(spec, outcome)

    # -------------------------------------------------------------- 证据冻结

    def _raw_output_bundle(self, handle: dict[str, Any]) -> dict[str, Any]:
        """受控工作目录里的原始输出（hash+受预算内容），供重新解析/审计。"""
        work_dir = str(handle.get("work_dir") or "")
        if not work_dir:
            return {"complete": False, "files": {}, "note": "no work dir"}
        try:
            from motte_sandbox.workspace import CaseWorkspace

            workspace = CaseWorkspace(Path(work_dir), anchor=Path(work_dir).parent)
            rels = sorted(
                rel for rel in workspace.list_files() if rel.startswith("outputs/")
            )
        except Exception:  # noqa: BLE001 - 工作目录缺失/被清理：如实记录
            return {"complete": False, "files": {}, "note": "work dir unavailable"}
        files: dict[str, Any] = {}
        total = 0
        for rel in rels:
            try:
                content = workspace.read_text(rel, max_bytes=_RAW_FILE_BUDGET + 1)
            except Exception:  # noqa: BLE001 - 单文件读取失败保留其余证据
                files[rel] = {"sha256": None, "size": None, "content": None,
                              "note": "unreadable"}
                continue
            encoded = content.encode("utf-8")
            digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
            entry: dict[str, Any] = {"sha256": digest, "size": len(encoded)}
            if len(encoded) <= _RAW_FILE_BUDGET and total + len(encoded) <= _RAW_TOTAL_BUDGET:
                entry["content"] = content
                total += len(encoded)
            else:
                entry["content"] = None
                entry["note"] = "content over budget; hash only"
            files[rel] = entry
        return {"complete": bool(files), "files": files}

    def _settle(self, spec: ExternalJobSpec, outcome: dict[str, Any]) -> dict[str, Any]:
        handle = outcome.get("handle") or {}
        job_id = str(handle.get("job_id") or "")
        status = str(outcome.get("job_status") or "indeterminate")
        if not job_id:
            return outcome
        cursor = dict(outcome.get("cursor") or {})

        # (1) 证据冻结（R04/R09）：内容寻址路径，同内容幂等、不同内容不覆盖。
        evidence: dict[str, Any] = {}
        if self.artifacts is not None:
            frozen_outcome = {
                "job_status": status,
                "results": outcome.get("results") or [],
                "error": outcome.get("error"),
                "spec": spec.model_dump(mode="json"),
                "handle": handle,
                "cursor": {
                    key: value for key, value in cursor.items()
                    if key != "raw_evidence"
                },
            }
            outcome_digest = _canonical_hash(frozen_outcome)
            outcome_path = (
                f"external-jobs/{spec.run_id}/{job_id}/outcome-{_short(outcome_digest)}.json"
            )
            outcome_artifact = self.artifacts.put_bytes(
                outcome_path,
                json.dumps(frozen_outcome, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            )
            bundle = self._raw_output_bundle(handle)
            pre_snapshot = cursor.get("raw_evidence") or {}
            if isinstance(pre_snapshot, dict):
                bundle["hashes_before_parse"] = pre_snapshot
            bundle_digest = _canonical_hash(bundle)
            bundle_path = (
                f"external-jobs/{spec.run_id}/{job_id}/evidence/raw-{_short(bundle_digest)}.json"
            )
            bundle_artifact = self.artifacts.put_bytes(
                bundle_path,
                json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            )
            evidence = {
                "outcome_artifact": outcome_artifact.id,
                "outcome_sha256": outcome_artifact.sha256,
                "raw_bundle_artifact": bundle_artifact.id,
                "raw_bundle_sha256": bundle_artifact.sha256,
                "raw_files": sorted(bundle.get("files") or {}),
            }

        # (2) 幂等导入（R04：终态之前；崩溃时 Job 仍非终态，恢复可补齐）。
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

        # (3) checkpoint：cursor 指标 + 证据引用 + import_completed（R04/R09）。
        metrics: dict[str, Any] = {}
        if isinstance(cursor.get("ceval_native"), dict):
            metrics["native"] = cursor["ceval_native"]
        if isinstance(cursor.get("ceval_diagnostic"), dict):
            metrics["diagnostic"] = cursor["ceval_diagnostic"]
        if isinstance(cursor.get("parser_version"), str):
            metrics["parser_version"] = cursor["parser_version"]
        current = self.job_store.get_job(job_id) or {}
        checkpoint = dict(current.get("checkpoint") or {})
        checkpoint.update({
            "cursor": {
                "parser_version": cursor.get("parser_version"),
                "ceval_native": cursor.get("ceval_native"),
                "ceval_diagnostic": cursor.get("ceval_diagnostic"),
                "records_consumed": cursor.get("records_consumed"),
            },
            "evidence": evidence,
            "import_completed": True,
        })
        if conflicts:
            checkpoint["import_error"] = {
                "code": "EXTERNAL_IMPORT_CONFLICT",
                "conflicts": conflicts[:10],
            }
        self.job_store.update_job(job_id, {"checkpoint": checkpoint})

        # (4) 终态最后写（R04）：cancelled 原子保护，终态不复活也不降级。
        if status in _TERMINAL_JOB_STATUSES:
            applied = self.job_store.update_job(
                job_id, {"status": status, "handle": handle},
                guard_status_not="cancelled",
            )
            if applied is None:
                self.job_store.update_job(job_id, {"handle": handle})

        outcome["import"] = {
            "job_id": job_id,
            "imported": imported,
            "conflicts": conflicts,
            "artifact": evidence.get("outcome_artifact"),
            "evidence": evidence,
            "parser_version": self.parser_version,
            "metrics": metrics,
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
            self.job_store.update_job(job_id, {"checkpoint": {
                **checkpoint, "outcome_error": outcome["error"],
            }})
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
