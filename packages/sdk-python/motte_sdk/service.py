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
        self._next_id = 1

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
        for case in cases:
            result = self.provider(case) if self.provider is not None else {"case_id": case}
            results.append({"case_id": case, "result": result})
        run.update({"status": "completed", "cases": results})
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

    def events(self, run_id: str) -> list[dict[str, Any]]:
        return deepcopy(self._events.get(run_id, []))

    def _emit(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        events = self._events.setdefault(run_id, [])
        events.append({"run_id": run_id, "seq": len(events) + 1, "type": event_type, **payload})
