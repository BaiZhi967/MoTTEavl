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
        evidence_sink: Callable[[str, bytes], Any] | None = None,
    ) -> None:
        self.adapter = adapter
        # 公开可替换：DurableExternalJobRunner 装配时写入 Job 存储。
        self.intent_journal = intent_journal
        # 证据冻结通道（review R2-06）：解析前把完整输入字节写受控不可变
        # Artifact；缺省无 sink 时仍在 cursor 记录内容 hash。
        self.evidence_sink = evidence_sink
        self.poll_interval_seconds = poll_interval_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        # 最近一次采集的 cursor（parser 版本与指标汇总随采集返回）。
        self.last_cursor: dict[str, Any] = {}

    def _freeze_evidence_bytes(
        self, handle: ExternalJobHandle, files: dict[str, str],
    ) -> dict[str, Any]:
        """把冻结字节写内容寻址 Artifact，返回 hash/引用/完整性视图。"""
        bundle_files: dict[str, Any] = {}
        total = 0
        for rel, text in sorted(files.items()):
            encoded = text.encode("utf-8")
            entry: dict[str, Any] = {
                "sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
                "size": len(encoded),
            }
            if len(encoded) <= _RAW_FILE_BUDGET and total + len(encoded) <= _RAW_TOTAL_BUDGET:
                entry["content"] = text
                total += len(encoded)
            else:
                entry["content"] = None
                entry["note"] = "content over budget; hash only"
            bundle_files[rel] = entry
        # 快照不完整时如实标记（R2-06：预算不足不能冒充完整证据）。
        bundle = {
            "complete": all(
                entry["content"] is not None for entry in bundle_files.values()
            ),
            "files": bundle_files,
        }
        digest = _canonical_hash(bundle)
        artifact_id: str | None = None
        if self.evidence_sink is not None:
            artifact = self.evidence_sink(
                f"external-jobs/{handle.run_id}/{handle.job_id}/evidence/raw-{_short(digest)}.json",
                json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            )
            artifact_id = getattr(artifact, "id", None) or str(artifact)
        return {
            "hash": digest,
            "artifact": artifact_id,
            "complete": bundle["complete"],
            "files": sorted(bundle_files),
        }

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
        """采集并转成 outcome 载荷；**先冻结完整输入字节再解析**（R2-06）。"""
        cursor = dict(handle.collection_cursor or {})
        reader = getattr(self.adapter, "read_output_files", None)
        collect_from_files = getattr(self.adapter, "collect_from_files", None)
        if callable(reader) and callable(collect_from_files):
            try:
                frozen_files = reader(handle)
            except Exception as error:  # noqa: BLE001 - 读取失败进入证据
                return ([], {
                    "code": getattr(error, "code", "JOB_OUTPUT_UNREADABLE"),
                    "message": str(error),
                })
            cursor["frozen_evidence"] = self._freeze_evidence_bytes(handle, frozen_files)
            try:
                results, collected = collect_from_files(handle, cursor, frozen_files)
            except Exception as error:  # noqa: BLE001 - 采集失败进入证据
                self.last_cursor = cursor
                return ([], {
                    "code": getattr(error, "code", "JOB_COLLECT_FAILED"),
                    "message": str(error),
                })
            payload = [item.model_dump(mode="json") for item in results]
            self.last_cursor = dict(collected or {})
            return (payload, None)
        # 旧协议 adapter（无字节级读取）：解析前先快照 hash（R09 兼容路径）。
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
        # 直接注入工件存储时也要立刻接上证据冻结通道：否则"解析前的完整字节"
        # 不会落盘，恢复路径就只能依赖可清理的工作目录（R2-06/R3-07 的前置）。
        if self.artifacts is not None:
            self._wire_evidence_sink()

    def bind_service(self, service: Any) -> None:
        self._service = service

    def _evidence_sink(self, path: str, data: bytes) -> Any:
        if self.artifacts is None:
            return None
        return self.artifacts.put_bytes(path, data)

    def _wire_evidence_sink(self) -> None:
        # 证据冻结通道（review R2-06）：supervisor 解析前写受控 Artifact。
        self.supervisor.evidence_sink = self._evidence_sink

    def bind_store(self, store: Any) -> None:
        """分派时绑定 Job 存储与工件根（external-benchmark 后端经 attach 注入）。"""
        external_jobs = getattr(store, "external_jobs", None)
        if external_jobs is not None:
            self.job_store = external_jobs
        if self.artifacts is None:
            import os

            from motte_storage.artifacts import ArtifactStore

            self.artifacts = ArtifactStore(os.environ.get("ARTIFACT_ROOT", "var/artifacts"))
        self._wire_evidence_sink()

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

    def _parser_mismatch(
        self, actual: Any, *, source: str, persisted: dict[str, Any] | None = None,
        outcome: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """版本拒绝只返回诊断，不启动/导入或改写原 Job 的状态与证据。"""
        persisted, outcome = persisted or {}, outcome or {}
        checkpoint = persisted.get("checkpoint") or {}
        return {
            "job_status": "cancelled" if "cancelled" in (
                persisted.get("status"), outcome.get("job_status"),
            ) else "indeterminate",
            "results": [],
            "handle": persisted.get("handle") or outcome.get("handle") or {},
            "cursor": outcome.get("cursor") or checkpoint.get("cursor") or {},
            "error": {
                "code": "EXTERNAL_PARSER_VERSION_MISMATCH",
                "message": "frozen parser version differs from available evidence parser; "
                           "no results imported or job restarted",
                "details": {"expected": self.parser_version, "actual": actual, "source": source,
                            "persisted_job_status": persisted.get("status"),
                            "original_error": outcome.get("error") or checkpoint.get("outcome_error")},
            },
            "import": {
                "imported": 0, "conflicts": [],
                "evidence": checkpoint.get("evidence") or {},
                "parser_version": self.parser_version,
            },
        }

    def _check_parser_version(self, persisted: dict[str, Any] | None = None) -> dict[str, Any] | None:
        actual = getattr(self.supervisor.adapter, "parser_version", None)
        if actual is not None and actual != self.parser_version:
            return self._parser_mismatch(actual, source="adapter", persisted=persisted)
        checkpoint = (persisted or {}).get("checkpoint") or {}
        previous = (checkpoint.get("cursor") or {}).get("parser_version")
        if previous is not None and previous != self.parser_version:
            return self._parser_mismatch(previous, source="checkpoint", persisted=persisted)
        return None

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
                # 非终态，或终态但导入未完成（R04）：优先从**已冻结工件**
                # 幂等补齐（R3-07：不依赖可清理的工作目录），否则重新观察。
                rejected = self._check_parser_version(persisted)
                if rejected is not None:
                    return rejected
                handle = ExternalJobHandle.model_validate(persisted.get("handle") or {})
                job_id = str(persisted.get("job_id") or handle.job_id)
                artifact_outcome = self._recover_from_frozen_artifact(persisted, handle)
                if artifact_outcome is not None:
                    outcome = artifact_outcome
                else:
                    outcome = self.supervisor.recover(
                        spec, handle,
                        should_cancel=lambda: self._should_cancel(spec.run_id, job_id),
                    )
                if status == "cancelled":
                    # 取消是操作员终局：观察到的其他结局只作审计，不复活。
                    outcome["job_status"] = "cancelled"
        else:
            rejected = self._check_parser_version()
            if rejected is not None:
                return rejected
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

    def _recover_from_frozen_artifact(
        self, persisted: dict[str, Any], handle: ExternalJobHandle,
    ) -> dict[str, Any] | None:
        """从已冻结的 raw bundle 重建 outcome（review R3-07）。

        导入中途崩溃时，完整结果在导入前已冻结为工件；此处直接从工件
        内容重新解析（同一内容 hash），不依赖可清理的工作目录，也绝不
        再次启动进程。工件缺失/不完整/解析失败 → 返回 None 走观察恢复。
        """
        checkpoint = persisted.get("checkpoint") or {}
        evidence = checkpoint.get("evidence") or {}
        bundle_id = evidence.get("raw_bundle_artifact")
        if not bundle_id or self.artifacts is None:
            return None
        collector = getattr(self.supervisor.adapter, "collect_from_files", None)
        if not callable(collector):
            return None
        try:
            bundle = json.loads(self.artifacts.read_bytes(bundle_id))
            files = {
                rel: entry["content"]
                for rel, entry in (bundle.get("files") or {}).items()
                if isinstance(entry, dict) and entry.get("content") is not None
            }
            if not files:
                return None
            # 从头重采（导入幂等）：checkpoint 里的 records_consumed 是崩溃
            # 前的解析进度，直接沿用会把全部样本切掉。
            results, cursor = collector(handle, {}, files)
        except Exception:  # noqa: BLE001 - 工件恢复失败退回观察恢复
            return None
        return {
            "job_status": checkpoint.get("outcome_status") or "settled",
            "results": [item.model_dump(mode="json") for item in results],
            "error": checkpoint.get("outcome_error") or None,
            "handle": persisted.get("handle") or handle.model_dump(mode="json"),
            "cursor": cursor,
            "recovered_from": "frozen-artifact",
        }

    def _raw_output_bundle(self, handle: dict[str, Any]) -> dict[str, Any]:
        """受控工作目录里的原始输出（hash+受预算内容），供重新解析/审计。"""
        work_dir = str(handle.get("work_dir") or "")
        if not work_dir:
            return {"complete": False, "files": {}, "note": "no work dir"}
        try:
            from motte_sandbox.workspace import CaseWorkspace

            from motte_benchmark.process import RESULTS_NAME

            workspace = CaseWorkspace(Path(work_dir), anchor=Path(work_dir).parent)
            rels = sorted(
                rel for rel in workspace.list_files()
                if rel.startswith("outputs/") or rel == RESULTS_NAME
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
        current = self.job_store.get_job(job_id) or {}
        checkpoint = dict(current.get("checkpoint") or {})

        # (0) 导入已完成的恢复（R2-07）：复用原 Artifact/指标/错误事实，
        # 不重读可清理的工作目录、不替换 checkpoint 里的证据引用。
        # frozen-artifact 恢复（R3-07）在导入补齐后再次进入时同样适用。
        if (
            outcome.get("recovered_from") in ("job-store", "frozen-artifact")
            and checkpoint.get("import_completed")
        ):
            evidence = checkpoint.get("evidence") or {}
            outcome["import"] = {
                "job_id": job_id,
                # 已导入记录的重放不产生新导入（与逐条 no-op 语义一致）。
                "imported": 0,
                "records": len(outcome.get("results") or []),
                "conflicts": [],
                "artifact": evidence.get("outcome_artifact"),
                "evidence": evidence,
                "parser_version": (checkpoint.get("cursor") or {}).get("parser_version")
                                  or self.parser_version,
                "metrics": self._metrics_from_cursor(checkpoint.get("cursor") or {}),
            }
            return outcome

        actual_parser = cursor.get("parser_version")
        if (
            actual_parser is not None and actual_parser != self.parser_version
        ) or (
            outcome.get("results") and actual_parser is None
            and hasattr(self.supervisor.adapter, "parser_version")
        ):
            return self._parser_mismatch(
                actual_parser, source="collection_cursor", persisted=current, outcome=outcome,
            )

        # 冻结工件恢复且导入未完成（R3-07）：证据引用已持久化，直接补导入，
        # 不重新派生证据、不重读工作目录。
        recovery_reuse = outcome.get("recovered_from") == "frozen-artifact"

        # (1) 证据（R2-06）：优先使用解析前冻结的字节 bundle；旧协议
        # adapter（无字节级读取）退回"再读工作目录 + 与解析前快照 hash
        # 交叉核验"，不一致即拒绝最终化。
        frozen = cursor.get("frozen_evidence") if isinstance(cursor.get("frozen_evidence"), dict) else None
        evidence: dict[str, Any] = dict(checkpoint.get("evidence") or {}) if recovery_reuse else {}
        evidence_inconsistent = False
        if recovery_reuse:
            pass
        elif frozen is not None:
            evidence = {
                "raw_bundle_artifact": frozen.get("artifact"),
                "raw_bundle_hash": frozen.get("hash"),
                "raw_files": frozen.get("files") or [],
                "complete": bool(frozen.get("complete")),
                "frozen_before_parse": True,
            }
        elif self.artifacts is not None:
            bundle = self._raw_output_bundle(handle)
            pre_snapshot = cursor.get("raw_evidence") or {}
            if isinstance(pre_snapshot, dict) and pre_snapshot.get("files"):
                def _bare(value: Any) -> Any:
                    return str(value).split(":")[-1] if value is not None else None

                pre_hashes = {
                    rel: _bare(digest) for rel, digest in pre_snapshot["files"].items()
                }
                now_hashes = {
                    rel: _bare(entry.get("sha256"))
                    for rel, entry in (bundle.get("files") or {}).items()
                }
                if pre_hashes != now_hashes:
                    evidence_inconsistent = True
            bundle_digest = _canonical_hash(bundle)
            bundle_artifact = self.artifacts.put_bytes(
                f"external-jobs/{spec.run_id}/{job_id}/evidence/raw-{_short(bundle_digest)}.json",
                json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            )
            evidence = {
                "raw_bundle_artifact": bundle_artifact.id,
                "raw_bundle_sha256": bundle_artifact.sha256,
                "raw_files": sorted(bundle.get("files") or []),
                "complete": bool(bundle.get("complete")),
                "frozen_before_parse": False,
            }
        if evidence and not recovery_reuse:
            # outcome 内容寻址冻结（同内容同路径；不同内容不互相覆盖）。
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
                outcome_artifact = self.artifacts.put_bytes(
                    f"external-jobs/{spec.run_id}/{job_id}/outcome-{_short(outcome_digest)}.json",
                    json.dumps(frozen_outcome, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                )
                evidence["outcome_artifact"] = outcome_artifact.id
                evidence["outcome_sha256"] = outcome_artifact.sha256
        if evidence_inconsistent:
            # 解析后证据被改写：拒绝最终化，保留两份 hash 供审计（R2-06）。
            outcome["job_status"] = "failed"
            outcome["error"] = {
                "code": "EVIDENCE_INCONSISTENT",
                "message": (
                    "output files changed between parse and evidence freeze; "
                    "official scores cannot be bound to a single content hash"
                ),
                "details": {
                    "hashes_before_parse": cursor.get("raw_evidence"),
                    "frozen": evidence,
                },
            }
            self.job_store.update_job(job_id, {
                "status": "failed",
                "checkpoint": {**checkpoint, "import_completed": True, "evidence": evidence},
                "handle": handle,
            })
            outcome["import"] = {
                "job_id": job_id, "imported": 0, "conflicts": [],
                "evidence": evidence, "parser_version": self.parser_version,
                "metrics": {},
            }
            return outcome

        # (1.5) 超预算证据不能冒充完整（R3-08）：冻结内容缺失时在正式
        # 导入/评分前明确失败——持久 hash 不代替完整输入，正式结果必须
        # 可从证据重建。零文件证据（如取消/空输出）不在此列，由
        # EXTERNAL_EMPTY_RESULTS 等语义处理；cancelled 不被降级。
        if (
            not recovery_reuse
            and evidence
            and evidence.get("complete") is False
            and bool(evidence.get("raw_files"))
            and status != "cancelled"
        ):
            outcome["job_status"] = "failed"
            outcome["error"] = {
                "code": "EVIDENCE_INCOMPLETE",
                "message": (
                    "frozen evidence exceeds the persistence budget; official "
                    "results must be rebuildable from persisted bytes, so "
                    "finalization is refused before import/scoring"
                ),
                "details": {"evidence": evidence},
            }
            self.job_store.update_job(job_id, {
                "status": "failed",
                "checkpoint": {
                    **checkpoint, "import_completed": True,
                    "outcome_status": status, "outcome_error": outcome["error"],
                    "evidence": evidence,
                },
                "handle": handle,
            }, guard_status_not="cancelled")
            outcome["import"] = {
                "job_id": job_id, "imported": 0, "conflicts": [],
                "evidence": evidence, "parser_version": self.parser_version,
                "metrics": {},
            }
            return outcome

        # (1.6) 导入前先持久化可恢复引用（R3-07）：冻结成功后立刻把证据
        # 引用/指标/outcome 状态落 checkpoint（import_completed=False），
        # 导入中途崩溃的恢复据此从工件补齐，不依赖工作目录。
        if not recovery_reuse and evidence:
            self.job_store.update_job(job_id, {"checkpoint": {
                **checkpoint,
                "cursor": {
                    "parser_version": cursor.get("parser_version"),
                    "ceval_native": cursor.get("ceval_native"),
                    "ceval_diagnostic": cursor.get("ceval_diagnostic"),
                    "records_consumed": cursor.get("records_consumed"),
                },
                "evidence": evidence,
                "outcome_status": status,
                "outcome_error": outcome.get("error"),
                "import_completed": False,
            }})

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
        metrics = self._metrics_from_cursor(cursor)
        checkpoint.update({
            "cursor": {
                "parser_version": cursor.get("parser_version"),
                "ceval_native": cursor.get("ceval_native"),
                "ceval_diagnostic": cursor.get("ceval_diagnostic"),
                "records_consumed": cursor.get("records_consumed"),
            },
            "evidence": evidence,
            "outcome_status": status,
            "outcome_error": outcome.get("error"),
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

    @staticmethod
    def _metrics_from_cursor(cursor: dict[str, Any]) -> dict[str, Any]:
        metrics: dict[str, Any] = {}
        if isinstance(cursor.get("ceval_native"), dict):
            metrics["native"] = cursor["ceval_native"]
        if isinstance(cursor.get("ceval_diagnostic"), dict):
            metrics["diagnostic"] = cursor["ceval_diagnostic"]
        if isinstance(cursor.get("parser_version"), str):
            metrics["parser_version"] = cursor["parser_version"]
        return metrics

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
