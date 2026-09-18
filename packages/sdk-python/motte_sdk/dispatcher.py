"""Single authority for claiming and executing durable Runs."""
from __future__ import annotations

from typing import Any

from motte_provider.capabilities import UnsupportedParameterError
from motte_storage.integrity import RunConflictError

from .execution_backends import (
    ExecutionBackendError,
    build_execution_handle,
    legacy_execution,
)
from .service import RunService


class RunDispatcher:
    def __init__(self, service: RunService) -> None:
        self.service = service

    @staticmethod
    def _ready_for_claim(run: dict[str, Any]) -> bool:
        manifest = run.get("manifest") or {}
        execution = manifest.get("execution") or {}
        if execution.get("backend_id") != "replay":
            return True
        provider = manifest.get("provider") or {}
        fixture = provider.get("fixture") if isinstance(provider, dict) else None
        if fixture is None:
            fixture = manifest.get("replay_fixture")
        return (
            isinstance(fixture, dict)
            and bool(fixture)
            and all(
                isinstance(case_id, str) and case_id
                and isinstance(item, dict) and "output" in item
                for case_id, item in fixture.items()
            )
            and all(case_id in fixture for case_id in run.get("case_ids") or fixture)
        )

    def claim(self, run_id: str | None = None) -> dict[str, Any] | None:
        if run_id is not None:
            candidate = self.service.store.runs.get(run_id)
            if candidate is not None and candidate.get("status") == "queued" and not self._ready_for_claim(candidate):
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
            if not isinstance(manifest.get("execution"), dict):
                dispatched = {**claimed, "manifest": legacy_execution(claimed)}
            handle = build_execution_handle(dispatched)
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
        return self.service.execute(run_id, provider=handle.invoke)

    def dispatch(self, run_id: str | None = None) -> dict[str, Any] | None:
        claimed = self.claim(run_id)
        return self.execute_claimed(claimed) if claimed is not None else None
