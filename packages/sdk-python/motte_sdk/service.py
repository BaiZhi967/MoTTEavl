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
    from motte_storage.repositories import SQLiteRepository

    path = Path(db_path if db_path is not None else os.environ.get("MOTTE_DB_PATH", "var/runs.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return RunService(SQLiteRepository(path))


class RunService:
    """Shared run lifecycle used by API, CLI, and worker entry points."""

    TERMINAL = {"completed", "failed", "cancelled", "unsupported", "profile_stale"}

    def __init__(self, repository: Any, provider: Callable[[str], Any] | None = None) -> None:
        self.repository = repository
        self.provider = provider
        self._events: dict[str, list[dict[str, Any]]] = {}
        existing = repository.list()
        ids = [int(item["id"].split("-")[-1]) for item in existing if str(item.get("id", "")).startswith("run-")]
        self._next_id = max(ids, default=0) + 1

    def create_run(
        self,
        scenario_version: str,
        manifest: dict[str, Any],
        case_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        run_id = f"run-{self._next_id}"
        self._next_id += 1
        run = {
            "id": run_id,
            "scenario_version": scenario_version,
            "status": "queued",
            "manifest": deepcopy(manifest),
            "case_ids": list(case_ids),
        }
        self.repository.put(run_id, run)
        self._emit(run_id, "queued", {"status": "queued"})
        return deepcopy(run)

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self.repository.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def execute(
        self,
        run_id: str,
        case_ids: Iterable[str] | None = None,
        provider: Callable[[str], Any] | None = None,
        expectations: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run["status"] == "completed":
            return run
        if run["status"] in self.TERMINAL:
            raise ValueError("run is terminal")
        invoke = provider if provider is not None else self.provider
        ids = list(case_ids) if case_ids is not None else list(run.get("case_ids") or [])
        if ids and not run.get("case_ids"):
            run["case_ids"] = ids
            self.repository.put(run_id, run)
        if run["status"] == "queued":
            run = self._transition(run_id, "preparing")
        if run["status"] != "running":
            run = self._transition(run_id, "running")
        results = list(run.get("cases") or [])
        done = {entry["case_id"] for entry in results}
        try:
            for case_id in ids:
                if case_id in done:
                    continue
                if self.get_run(run_id)["status"] == "cancelled":
                    return self.get_run(run_id)
                result = invoke(case_id) if invoke is not None else {"case_id": case_id}
                expected = self._expected_for(invoke, expectations, case_id)
                entry = {"case_id": case_id, "result": result}
                if expected is not None:
                    entry["expected"] = expected
                results.append(entry)
                run = self.get_run(run_id)
                run["cases"] = results
                self.repository.put(run_id, run)
                self._emit(run_id, "model_response", {"case_id": case_id, "result": result})
        except Exception as error:
            return self._fail(run_id, error)
        if self.get_run(run_id)["status"] == "cancelled":
            return self.get_run(run_id)
        run = self._transition(run_id, "collecting", cases=results)
        run = self._transition(run_id, "scoring")
        scores = self._score_results(run_id, results, emit_events=True)
        return self._transition(run_id, "completed", cases=results, scores=scores)

    def cancel(self, run_id: str, reason: str | None = None) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run["status"] in self.TERMINAL:
            return run
        run["status"] = "cancelled"
        payload: dict[str, Any] = {"status": "cancelled"}
        if reason is not None:
            run["cancellation"] = {"reason": reason}
            payload["reason"] = reason
        self.repository.put(run_id, run)
        self._emit(run_id, "cancelled", payload)
        return deepcopy(run)

    def rescore(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run["status"] != "completed":
            raise ValueError("only completed runs can be rescored")
        scores = self._score_results(run_id, run.get("cases") or [], emit_events=False)
        run["scores"] = scores
        run["rescored"] = True
        self.repository.put(run_id, run)
        self._emit(run_id, "rescored", {"status": run["status"], "scores": deepcopy(scores)})
        return deepcopy(run)

    def mark_unsupported(self, run_id: str, code: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run["status"] in self.TERMINAL:
            return run
        run.update({"status": "unsupported", "error": {"code": code}})
        self.repository.put(run_id, run)
        self._emit(run_id, "unsupported", {"status": "unsupported", "error": run["error"]})
        return deepcopy(run)

    def mark_profile_stale(self, run_id: str, reason: str | None = None) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run["status"] in self.TERMINAL:
            return run
        if run["status"] != "queued":
            raise ValueError("profile staleness is detected before execution")
        error: dict[str, Any] = {"code": "PROFILE_STALE"}
        if reason is not None:
            error["message"] = reason
        run.update({"status": "profile_stale", "error": error})
        self.repository.put(run_id, run)
        self._emit(run_id, "profile_stale", {"status": "profile_stale", "error": error})
        return deepcopy(run)

    def retry(self, run_id: str) -> dict[str, Any]:
        parent = self.get_run(run_id)
        if parent["status"] not in RETRYABLE:
            raise ValueError("only failed, cancelled, unsupported, or profile_stale runs can be retried")
        child = self.create_run(parent["scenario_version"], parent.get("manifest", {}), parent.get("case_ids") or [])
        child["parent_run_id"] = run_id
        self.repository.put(child["id"], child)
        return deepcopy(child)

    def events(self, run_id: str) -> list[dict[str, Any]]:
        events = self._events.get(run_id)
        if events is None and hasattr(self.repository, "keys"):
            events = [self.repository.get(key) for key in self.repository.keys(f"event:{run_id}:")]
            events = [event for event in events if event is not None]
        return deepcopy(events or [])

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
        run = self.get_run(run_id)
        run.update({"status": "failed", "error": {"type": type(error).__name__, "message": str(error)}})
        self.repository.put(run_id, run)
        self._emit(run_id, "failed", {"status": "failed", "error": run["error"]})
        return deepcopy(run)

    def _transition(self, run_id: str, status: str, **fields: Any) -> dict[str, Any]:
        run = self.get_run(run_id)
        if status not in TRANSITIONS.get(run["status"], set()):
            raise ValueError(f"invalid transition: {run['status']} -> {status}")
        run["status"] = status
        run.update(fields)
        self.repository.put(run_id, run)
        self._emit(run_id, status, {"status": status})
        return deepcopy(run)

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
        if run_id not in self._events and hasattr(self.repository, "keys"):
            existing = [self.repository.get(key) for key in self.repository.keys(f"event:{run_id}:")]
            self._events[run_id] = [event for event in existing if event is not None]
        events = self._events.setdefault(run_id, [])
        event = {"run_id": run_id, "seq": len(events) + 1, "type": event_type, **payload}
        events.append(event)
        if hasattr(self.repository, "put"):
            self.repository.put(f"event:{run_id}:{event['seq']:08d}", event)
