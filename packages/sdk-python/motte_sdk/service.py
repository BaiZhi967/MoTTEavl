from __future__ import annotations

from collections.abc import Callable, Iterable
from copy import deepcopy
from typing import Any


class RunService:
    """Shared run lifecycle used by API, CLI, and worker entry points."""

    TERMINAL = {"completed", "failed", "cancelled", "unsupported"}

    def __init__(self, repository: Any, provider: Callable[[str], Any] | None = None) -> None:
        self.repository = repository
        self.provider = provider
        self._events: dict[str, list[dict[str, Any]]] = {}
        existing = repository.list()
        ids = [int(item["id"].split("-")[-1]) for item in existing if str(item.get("id", "")).startswith("run-")]
        self._next_id = max(ids, default=0) + 1

    def create_run(self, scenario_version: str, manifest: dict[str, Any]) -> dict[str, Any]:
        run_id = f"run-{self._next_id}"
        self._next_id += 1
        run = {
            "id": run_id,
            "scenario_version": scenario_version,
            "status": "queued",
            "manifest": deepcopy(manifest),
        }
        self.repository.put(run_id, run)
        self._emit(run_id, "queued", {"status": "queued"})
        return deepcopy(run)

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self.repository.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def execute(self, run_id: str, cases: Iterable[str]) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run["status"] in self.TERMINAL:
            if run["status"] == "completed":
                return run
            raise ValueError("run is terminal")
        run["status"] = "running"
        self.repository.put(run_id, run)
        self._emit(run_id, "running", {"status": "running"})
        results = []
        scores = []
        try:
            for case in cases:
                result = self.provider(case) if self.provider is not None else {"case_id": case}
                self._emit(run_id, "model_response", {"case_id": case, "result": result})
                results.append({"case_id": case, "result": result})
                fixture = getattr(self.provider, "__self__", None)
                expected = getattr(fixture, "fixture", {}).get(case, {}).get("expected")
                if expected is not None:
                    passed = result == expected
                    scores.append({"case_id": case, "passed": passed})
                    self._emit(run_id, "score", {"case_id": case, "passed": passed})
        except Exception as error:
            run.update({"status": "failed", "error": {"type": type(error).__name__, "message": str(error)}})
            self.repository.put(run_id, run)
            self._emit(run_id, "failed", {"status": "failed", "error": run["error"]})
            return deepcopy(run)
        run.update({"status": "completed", "cases": results, "scores": scores})
        self.repository.put(run_id, run)
        self._emit(run_id, "completed", {"status": "completed"})
        return deepcopy(run)

    def cancel(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run["status"] in self.TERMINAL:
            return run
        run["status"] = "cancelled"
        self.repository.put(run_id, run)
        self._emit(run_id, "cancelled", {"status": "cancelled"})
        return deepcopy(run)

    def rescore(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run["status"] != "completed":
            raise ValueError("only completed runs can be rescored")
        run["rescored"] = True
        self.repository.put(run_id, run)
        self._emit(run_id, "rescored", {"status": run["status"]})
        return deepcopy(run)

    def mark_unsupported(self, run_id: str, code: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run["status"] in self.TERMINAL:
            return run
        run.update({"status": "unsupported", "error": {"code": code}})
        self.repository.put(run_id, run)
        self._emit(run_id, "unsupported", {"status": "unsupported", "error": run["error"]})
        return deepcopy(run)

    def retry(self, run_id: str) -> dict[str, Any]:
        parent = self.get_run(run_id)
        if parent["status"] not in {"failed", "cancelled", "unsupported"}:
            raise ValueError("only failed, cancelled, or unsupported runs can be retried")
        child = self.create_run(parent["scenario_version"], parent.get("manifest", {}))
        child["parent_run_id"] = run_id
        self.repository.put(child["id"], child)
        return deepcopy(child)

    def events(self, run_id: str) -> list[dict[str, Any]]:
        events = self._events.get(run_id)
        if events is None and hasattr(self.repository, "keys"):
            events = [self.repository.get(key) for key in self.repository.keys(f"event:{run_id}:")]
            events = [event for event in events if event is not None]
        return deepcopy(events or [])

    def _emit(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        if run_id not in self._events and hasattr(self.repository, "keys"):
            existing = [self.repository.get(key) for key in self.repository.keys(f"event:{run_id}:")]
            self._events[run_id] = [event for event in existing if event is not None]
        events = self._events.setdefault(run_id, [])
        event = {"run_id": run_id, "seq": len(events) + 1, "type": event_type, **payload}
        events.append(event)
        if hasattr(self.repository, "put"):
            self.repository.put(f"event:{run_id}:{event['seq']:08d}", event)
