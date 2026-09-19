"""Single authority for claiming and executing durable Runs."""
from __future__ import annotations

from typing import Any

from motte_provider.capabilities import UnsupportedParameterError
from motte_storage.integrity import RunConflictError

from .execution_backends import (
    ExecutionBackendError,
    build_execution_handle,
    legacy_execution,
    resolve_replay_case_ids,
)
from .service import RunService


class RunDispatcher:
    def __init__(self, service: RunService) -> None:
        self.service = service

    @staticmethod
    def _ready_for_claim(run: dict[str, Any]) -> bool:
        manifest = run.get("manifest") or {}
        try:
            projected = legacy_execution(run) if not isinstance(manifest.get("execution"), dict) else manifest
        except ExecutionBackendError:
            return True
        execution = projected.get("execution") or {}
        if execution.get("backend_id") != "replay":
            return True
        provider = projected.get("provider") or {}
        has_fixture = (
            isinstance(provider, dict) and "fixture" in provider
        ) or "replay_fixture" in projected
        if not has_fixture:
            return False
        try:
            selected = resolve_replay_case_ids(projected, run.get("case_ids"))
        except ExecutionBackendError:
            # Invalid persisted fixture should be claimed so the dispatcher can
            # record a terminal configuration error instead of waiting forever.
            return True
        return bool(selected)


    def claim(self, run_id: str | None = None) -> dict[str, Any] | None:
        if run_id is not None:
            candidate = self.service.store.runs.get(run_id)
            if candidate is not None and candidate.get("status") == "queued":
                if not self._ready_for_claim(candidate):
                    return None
            return self.service.store.runs.claim(run_id)
        for candidate in self.service.store.runs.list():
            if candidate.get("status") != "queued" or not self._ready_for_claim(candidate):
                continue
            try:
                return self.service.store.runs.claim(candidate["id"])
            except RunConflictError:
                continue
        return None

    def execute_claimed(self, claimed: dict[str, Any]) -> dict[str, Any]:
        if claimed.get("status") != "preparing":
            raise ValueError("dispatcher requires an atomically claimed preparing run")
        run_id = claimed["id"]
        manifest = claimed.get("manifest") or {}
        try:
            dispatched = claimed
            selected_case_ids = list(claimed.get("case_ids") or [])
            if not isinstance(manifest.get("execution"), dict):
                dispatched = {**claimed, "manifest": legacy_execution(claimed)}
                projected_manifest = dispatched["manifest"]
                if projected_manifest.get("execution", {}).get("backend_id") == "replay":
                    selected_case_ids = resolve_replay_case_ids(
                        projected_manifest, selected_case_ids
                    )
            if dispatched.get("manifest", {}).get("execution", {}).get("backend_id") == "replay":
                selected_case_ids = resolve_replay_case_ids(
                    dispatched["manifest"], selected_case_ids
                )
            handle = build_execution_handle(dispatched)
            attach = getattr(handle, "attach", None)
            if attach is not None:
                attach(self.service, run_id)
            if getattr(handle, "execution_mode", "sample") == "job":
                # job 模式：整 Run 只启动一个外部 Job，不逐题 invoke（M2-G01）。
                job_entry = getattr(handle, "run_job", None)
                if job_entry is None:
                    return self.service.mark_unsupported(
                        run_id,
                        "EXTERNAL_JOB_ENTRY_MISSING",
                        message="job execution backend did not provide a run_job hook",
                    )
                return self.service.execute_external_job(run_id, dispatched, job_entry)
        except UnsupportedParameterError as error:
            return self.service.mark_unsupported(
                run_id, "UNSUPPORTED_PARAMETER", message=str(error)
            )
        except ExecutionBackendError as error:
            return self.service.mark_unsupported(run_id, error.code, message=str(error))
        except ValueError as error:
            return self.service.mark_unsupported(
                run_id, "PROVIDER_CONFIG_INVALID", message=str(error)
            )
        return self.service.execute(
            run_id,
            case_ids=selected_case_ids,
            provider=handle.invoke,
        )

    def dispatch(self, run_id: str | None = None) -> dict[str, Any] | None:
        claimed = self.claim(run_id)
        return self.execute_claimed(claimed) if claimed is not None else None
