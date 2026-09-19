"""Worker 调度循环：从持久化存储抢占 queued Run 并执行。

默认 loop 模式只依赖 SQLite（本地开发零外部服务）；celery 模式见 celery_app.py。
"""
from __future__ import annotations

import time
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any

from motte_sdk.dispatcher import RunDispatcher
from motte_sdk.execution_backends import backend_for, build_execution_handle, legacy_execution
from motte_sdk.execution_lock import worker_execution_lock
from motte_sdk.service import RunService
from motte_storage.integrity import RunConflictError

from .reporting import WorkerReporter


def provider_for_run(run: dict[str, Any]):
    """Compatibility name: dispatch the pinned execution backend and return its invoke hook."""
    manifest = run.get("manifest") or {}
    dispatched = run
    if not isinstance(manifest.get("execution"), dict):
        dispatched = {**run, "manifest": legacy_execution(run)}
    return build_execution_handle(dispatched).invoke


class WorkerLoop:
    def __init__(
        self, service: RunService, reporter: WorkerReporter | None = None, *,
        execution_lock_held: bool = False,
    ) -> None:
        self.service = service
        self.dispatcher = RunDispatcher(service)
        self.reporter = reporter or WorkerReporter()
        self._execution_lock_held = execution_lock_held
        # Worker 与 API/CLI 从同一受控配置加载外部 Job adapter（review R01）：
        # 未配置时 adapter 不注册，外部 Run 在分派层 RUNNER_NOT_CONNECTED。
        from motte_benchmark.runner_config import ensure_builtin_adapters

        ensure_builtin_adapters()
        self._db_path = getattr(service.store.runs, "_path", None)
        self._postgres_dsn = getattr(service.store.runs, "_dsn", None)
        self._storage_backend = "postgres" if self._postgres_dsn is not None else (
            "sqlite" if self._db_path is not None else None
        )
        self._persistent_store = self._storage_backend is not None
        self.service.add_event_observer(self.reporter.observe_event)
        self.service.add_progress_observer(self.reporter.observe_progress)

    def _execution_guard(self):
        if self._execution_lock_held or not self._persistent_store:
            return nullcontext()
        return worker_execution_lock(
            self._db_path,
            backend=self._storage_backend,
            postgres_dsn=self._postgres_dsn,
        )

    def recover_interrupted(self) -> list[str]:
        """Recover safe work and atomically quarantine uncertain external calls."""
        with self._execution_guard():
            return self._recover_interrupted_unlocked()

    def _recover_interrupted_unlocked(self) -> list[str]:
        store = self.service.store
        interrupted = {"preparing", "running", "collecting", "scoring"}
        for run in store.runs.list():
            attempts = store.attempts.list_for_run(run["id"])
            prepared = [item for item in attempts if item.get("status") == "prepared"]
            for attempt in prepared:
                try:
                    store.attempts.transition(
                        attempt["id"],
                        expected_revision=attempt["revision"],
                        expected_status="prepared",
                        status="failed",
                        changes={
                            "finished_at": datetime.now(UTC).isoformat(),
                            "error": {"code": "PREPARED_ATTEMPT_RECOVERED"},
                        },
                    )
                except RunConflictError:
                    # Another executor completed the preparation while recovery scanned it.
                    pass
            uncertain = [
                item for item in attempts
                if item.get("status") in {"dispatching", "indeterminate"}
            ]
            if not uncertain:
                continue
            if run.get("status") == "needs_review" and all(
                item["status"] == "indeterminate" for item in uncertain
            ):
                continue
            safe_to_repeat = False
            try:
                manifest = run.get("manifest") or {}
                if not isinstance(manifest.get("execution"), dict):
                    manifest = legacy_execution(run)
                descriptor = manifest["execution"]
                backend_for(descriptor["backend_id"], descriptor["backend_version"])
                persisted_capabilities = descriptor.get("capabilities")
                if isinstance(persisted_capabilities, dict) and "safe_to_repeat" in persisted_capabilities:
                    safe_to_repeat = persisted_capabilities["safe_to_repeat"] is True
                else:
                    # Legacy manifests have no pinned capability snapshot.
                    safe_to_repeat = backend_for(
                        descriptor["backend_id"], descriptor["backend_version"]
                    ).capabilities.get("safe_to_repeat", False)
            except (KeyError, ValueError):
                safe_to_repeat = False
            if safe_to_repeat:
                if run.get("status") in interrupted:
                    for attempt in uncertain:
                        if attempt["status"] == "dispatching":
                            store.attempts.transition(
                                attempt["id"],
                                expected_revision=attempt["revision"],
                                expected_status="dispatching",
                                status="failed",
                                changes={
                                    "finished_at": datetime.now(UTC).isoformat(),
                                    "error": {"code": "SAFE_REPLAY_INTERRUPTED"},
                                },
                            )
                continue
            attempt_ids = [item["id"] for item in uncertain]
            now = datetime.now(UTC).isoformat()
            quarantined = store.attempts.quarantine_indeterminate(
                run["id"],
                expected_run_revision=run["revision"],
                expected_run_status=run["status"],
                changes={
                    "updated_at": now,
                    "finished_at": now,
                    "error": {
                        "code": "CALL_OUTCOME_INDETERMINATE",
                        "message": "a dispatched external call was interrupted before durable completion",
                        "details": {"attempt_ids": attempt_ids},
                    },
                },
                event={
                    "run_id": run["id"],
                    "type": "needs_review",
                    "status": "needs_review",
                    "attempt_ids": attempt_ids,
                },
            )
            if quarantined:
                self.reporter.emit(
                    "run_needs_review",
                    run_id=run["id"],
                    reason="indeterminate_external_call",
                )
        requeued = store.runs.requeue_interrupted()
        for run_id in requeued:
            self.reporter.emit("run_requeued", run_id=run_id, reason="worker_restart")
        return requeued

    def claim_and_execute(self, run_id: str | None = None) -> dict | None:
        """Claim and dispatch one Run through the shared backend path."""
        with self._execution_guard():
            return self._claim_and_execute_unlocked(run_id)

    def run_once(self, run_id: str | None = None) -> dict | None:
        """Recover and execute while holding the guard for the actual store."""
        with self._execution_guard():
            self._recover_interrupted_unlocked()
            return self._claim_and_execute_unlocked(run_id)

    def _claim_and_execute_unlocked(self, run_id: str | None = None) -> dict | None:
        claimed = self.dispatcher.claim(run_id)
        if claimed is None:
            return None
        run_id = claimed["id"]
        self.reporter.emit(
            "run_claimed",
            run_id=run_id,
            status=claimed.get("status"),
            selected=len(claimed.get("case_ids") or []),
        )
        result = self.dispatcher.execute_claimed(claimed)
        self._report_finished(result)
        return result

    def _report_finished(self, result: dict[str, Any]) -> None:
        cases = result.get("cases") or []
        scores = result.get("scores") or []
        failed = sum(
            isinstance(row.get("result"), dict) and isinstance(row["result"].get("error"), dict)
            for row in cases
        )
        not_attempted = sum(
            row.get("outcome") == "not_attempted" or row.get("result") is None for row in cases
        )
        self.reporter.emit(
            "run_finished",
            run_id=result.get("id"),
            status=result.get("status"),
            selected=len(result.get("case_ids") or []),
            persisted=len(cases),
            scored=len(scores),
            failed=failed,
            not_attempted=not_attempted,
        )

    def run_forever(self, poll_interval: float = 1.0, *, recover: bool = True) -> None:
        if recover:
            self.recover_interrupted()
        while True:
            if self.claim_and_execute() is None:
                time.sleep(poll_interval)
