from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from copy import deepcopy
from pathlib import Path
from typing import Any

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
)

# 状态机迁移表；running/collecting/scoring 允许回到 running，用于 Worker 崩溃后的中断恢复。
TRANSITIONS: dict[str, set[str]] = {
    "queued": {"preparing", "cancelled", "unsupported", "profile_stale"},
    "preparing": {"running", "failed", "cancelled"},
    "running": {"collecting", "failed", "cancelled", "running"},
    "collecting": {"scoring", "failed", "cancelled", "running"},
    "scoring": {"completed", "failed", "cancelled", "running"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
    "unsupported": set(),
    "profile_stale": set(),
}

RETRYABLE = {"failed", "cancelled", "unsupported", "profile_stale"}


def build_run_service(db_path: str | Path | None = None) -> RunService:
    """API、CLI、Worker 共用的服务构造入口（MOTTE_DB_PATH，默认 var/runs.db）。"""
    from motte_storage.run_store import SQLiteRunStore

    path = Path(db_path if db_path is not None else os.environ.get("MOTTE_DB_PATH", "var/runs.db"))
    return RunService(SQLiteRunStore(path))


class RunService:
    """Shared run lifecycle used by API, CLI, and worker entry points."""

    TERMINAL = {"completed", "failed", "cancelled", "unsupported", "profile_stale"}

    def __init__(self, store: Any, provider: Callable[[str], Any] | None = None) -> None:
        self.store = store
        self.provider = provider

    def create_run(
        self,
        scenario_version: str,
        manifest: dict[str, Any],
        case_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        run_id = self.store.runs.next_run_id()
        run = {
            "id": run_id,
            "scenario_version": scenario_version,
            "status": "queued",
            "manifest": deepcopy(manifest),
            "case_ids": list(case_ids),
        }
        self.store.runs.save(run)
        self._emit(run_id, "queued", {"status": "queued"})
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
        invoke = provider if provider is not None else self.provider
        ids = list(case_ids) if case_ids is not None else list(run.get("case_ids") or [])
        if ids and not run.get("case_ids"):
            run["case_ids"] = ids
            self.store.runs.save(run)
        if run["status"] == "queued":
            self._transition(run_id, "preparing")
        if self._load(run_id)["status"] != "running":
            self._transition(run_id, "running")
        done = {row["case_id"] for row in self.store.case_runs.list_for_run(run_id)}
        try:
            for case_id in ids:
                if case_id in done:
                    continue
                if self._load(run_id)["status"] == "cancelled":
                    return self.get_run(run_id)
                result = invoke(case_id) if invoke is not None else {"case_id": case_id}
                expected = self._expected_for(invoke, expectations, case_id)
                entry: dict[str, Any] = {"run_id": run_id, "case_id": case_id, "result": result}
                if expected is not None:
                    entry["expected"] = expected
                self.store.case_runs.upsert(entry)
                self._emit(run_id, "model_response", {"case_id": case_id, "result": result})
        except Exception as error:
            return self._fail(run_id, error)
        if self._load(run_id)["status"] == "cancelled":
            return self.get_run(run_id)
        results = self.store.case_runs.list_for_run(run_id)
        self._transition(run_id, "collecting")
        self._transition(run_id, "scoring")
        scores = self._score_results(run_id, results, emit_events=True)
        self.store.scores.replace_for_run(run_id, scores)
        return self._transition(run_id, "completed")

    def cancel(self, run_id: str, reason: str | None = None) -> dict[str, Any]:
        run = self._load(run_id)
        if run["status"] in self.TERMINAL:
            return self._view(run_id)
        run["status"] = "cancelled"
        payload: dict[str, Any] = {"status": "cancelled"}
        if reason is not None:
            run["cancellation"] = {"reason": reason}
            payload["reason"] = reason
        self.store.runs.save(run)
        self._emit(run_id, "cancelled", payload)
        return self._view(run_id)

    def rescore(self, run_id: str) -> dict[str, Any]:
        run = self._load(run_id)
        if run["status"] != "completed":
            raise ValueError("only completed runs can be rescored")
        scores = self._score_results(run_id, self.store.case_runs.list_for_run(run_id), emit_events=False)
        self.store.scores.replace_for_run(run_id, scores)
        run["rescored"] = True
        self.store.runs.save(run)
        self._emit(run_id, "rescored", {"status": run["status"], "scores": deepcopy(scores)})
        return self._view(run_id)

    def mark_unsupported(self, run_id: str, code: str) -> dict[str, Any]:
        run = self._load(run_id)
        if run["status"] in self.TERMINAL:
            return self._view(run_id)
        run.update({"status": "unsupported", "error": {"code": code}})
        self.store.runs.save(run)
        self._emit(run_id, "unsupported", {"status": "unsupported", "error": run["error"]})
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
        run.update({"status": "profile_stale", "error": error})
        self.store.runs.save(run)
        self._emit(run_id, "profile_stale", {"status": "profile_stale", "error": error})
        return self._view(run_id)

    def retry(self, run_id: str) -> dict[str, Any]:
        parent = self._load(run_id)
        if parent["status"] not in RETRYABLE:
            raise ValueError("only failed, cancelled, unsupported, or profile_stale runs can be retried")
        child = self.create_run(parent["scenario_version"], parent.get("manifest", {}), parent.get("case_ids") or [])
        child["parent_run_id"] = run_id
        self.store.runs.save(child)
        return self._view(child["id"])

    def events(self, run_id: str) -> list[dict[str, Any]]:
        return self.store.events.list_for_run(run_id)

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
        run["scores"] = self.store.scores.list_for_run(run_id)
        return run

    def _score_results(
        self,
        run_id: str,
        results: list[dict[str, Any]],
        emit_events: bool,
    ) -> list[dict[str, Any]]:
        scores: list[dict[str, Any]] = []
        for entry in results:
            if "expected" in entry:
                passed = entry["result"] == entry["expected"]
                scores.append({"case_id": entry["case_id"], "passed": passed})
                if emit_events:
                    self._emit(run_id, "score", {"case_id": entry["case_id"], "passed": passed})
        return scores

    def _fail(self, run_id: str, error: Exception) -> dict[str, Any]:
        run = self._load(run_id)
        run.update({"status": "failed", "error": {"type": type(error).__name__, "message": str(error)}})
        self.store.runs.save(run)
        self._emit(run_id, "failed", {"status": "failed", "error": run["error"]})
        return self._view(run_id)

    def _transition(self, run_id: str, status: str) -> dict[str, Any]:
        run = self._load(run_id)
        if status not in TRANSITIONS.get(run["status"], set()):
            raise ValueError(f"invalid transition: {run['status']} -> {status}")
        run["status"] = status
        self.store.runs.save(run)
        self._emit(run_id, status, {"status": status})
        return self._view(run_id)

    @staticmethod
    def _expected_for(
        invoke: Callable[[str], Any] | None,
        expectations: dict[str, Any] | None,
        case_id: str,
    ) -> Any:
        if expectations is not None:
            return expectations.get(case_id)
        expected_for = getattr(invoke, "expected_for", None)
        if not callable(expected_for):
            receiver = getattr(invoke, "__self__", None)
            expected_for = getattr(receiver, "expected_for", None)
        if callable(expected_for):
            return expected_for(case_id)
        return None

    def _emit(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.store.events.append({"run_id": run_id, "type": event_type, **payload})
