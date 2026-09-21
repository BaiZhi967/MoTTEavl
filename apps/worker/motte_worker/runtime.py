"""Worker 调度循环：从持久化存储抢占 queued Run 并执行。

默认 loop 模式只依赖 SQLite（本地开发零外部服务）；celery 模式见 celery_app.py。

M5-R8：Judge 作业与普通 Run 共用这一个循环、这一把执行锁：

* 恢复：Run 恢复之后调用 ScoringJobService.recover_interrupted()（prepared 回队列、
  dispatching 标不确定，绝不自动重发付费调用）；
* 领取顺序：每一轮**先**领取至多一个 Judge 作业，**再**领取一个 Run。两类队列
  在同一轮都推进，因此任何一类都不会因为另一类持续入队而永远得不到调度；
  显式指定 run_id 时只领取那个 Run。
* 执行：Judge Provider 由提交期冻结的快照构造（FrozenProviderFactory），
  执行期只解析非秘密的凭据引用。
"""
from __future__ import annotations

import time
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any

from motte_sdk.dispatcher import RunDispatcher
from motte_sdk.execution_backends import backend_for, build_execution_handle, legacy_execution
from motte_sdk.execution_lock import worker_execution_lock
from motte_sdk.scoring_jobs import FrozenProviderFactory, ScoringJobService
from motte_sdk.service import RunService
from motte_storage.integrity import RunConflictError

from .reporting import WorkerReporter

#: "未指定"哨兵：显式传 None 表示关闭 Judge 领取（只跑普通 Run）。
_UNSET: Any = object()


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
        execution_lock_held: bool = False, scoring_jobs: Any = _UNSET,
        judge_provider_factory: Any = _UNSET,
    ) -> None:
        self.service = service
        self.dispatcher = RunDispatcher(service)
        self.reporter = reporter or WorkerReporter()
        self._execution_lock_held = execution_lock_held
        if scoring_jobs is _UNSET:
            factory = (
                FrozenProviderFactory() if judge_provider_factory is _UNSET
                else judge_provider_factory
            )
            scoring_jobs = ScoringJobService(service.store, provider_factory=factory)
        #: None 表示显式关闭 Judge 领取；默认是与其他 Worker 状态共存的持久服务。
        self.scoring_jobs = scoring_jobs
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
        from motte_harness.session import recover_runtime_sessions

        store = self.service.store
        session_findings = recover_runtime_sessions(include_terminal=True)
        interrupted = {"preparing", "running", "collecting", "scoring"}
        for run in store.runs.list():
            from motte_sdk.commands import recover_interactive_sessions

            recover_interactive_sessions(self.service, run['id'])
            attempts = store.attempts.list_for_run(run["id"])
            runtime_sessions = [item for item in session_findings if item.get("run_id") == run["id"]]
            session_attempt_ids = {item.get("attempt_id") for item in runtime_sessions}
            prepared = [item for item in attempts if item.get("status") == "prepared"]
            for attempt in prepared:
                if attempt["id"] in session_attempt_ids or any(
                    not item.get("attempt_id") and item.get("case_id") == attempt.get("case_id")
                    for item in runtime_sessions
                ):
                    # A prepared session can have spawned just before a crash.
                    # Convert the CaseAttempt to uncertain; never replay it.
                    store.attempts.transition(
                        attempt["id"], expected_revision=attempt["revision"],
                        expected_status="prepared", status="dispatching",
                    )
                    continue
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
            attempts = store.attempts.list_for_run(run["id"])
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
            if runtime_sessions:
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
                        "details": {"attempt_ids": attempt_ids, "runtime_sessions": runtime_sessions},
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
        if self.scoring_jobs is not None:
            # Judge 作业的恢复与 Run 恢复同锁同轮：prepared（无发送证据）回队列，
            # dispatching（可能已发出）标不确定且绝不自动重发。
            for job_id in self.scoring_jobs.recover_interrupted():
                self.reporter.emit("judge_job_recovered", job_id=job_id)
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
        # 领取顺序：先至多一个 Judge 作业，再一个 Run。显式 run_id 时只领取该 Run。
        judge_result = None
        if run_id is None and self.scoring_jobs is not None:
            judge_result = self._claim_judge_job()
        claimed = self.dispatcher.claim(run_id)
        if claimed is None:
            # 没有 Run 可领时，Judge 作业的结果就是这一轮做的工作（--once 依赖它）。
            return judge_result
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

    def _claim_judge_job(self) -> dict[str, Any] | None:
        """领取并执行至多一个 Judge 作业；失败只上报，不让整个循环停摆。"""
        try:
            outcome = self.scoring_jobs.claim_and_run()
        except Exception as error:  # noqa: BLE001 - 作业仍留在持久状态，由恢复兜底
            self.reporter.emit(
                "judge_job_failed",
                error_class=type(error).__name__,
                message=str(error)[:300],
            )
            return None
        if outcome is None:
            return None
        self.reporter.emit(
            "judge_job_finished",
            job_id=outcome.get("job_id"),
            status=outcome.get("status"),
            billed_calls=outcome.get("billed_calls"),
            publish_outcome=outcome.get("publish_outcome"),
        )
        return outcome

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
