from __future__ import annotations

from collections.abc import Callable, Iterable
from copy import deepcopy
from pathlib import Path
from time import perf_counter
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
    """API、CLI、Worker 共用的服务构造入口；存储后端经 motte_storage 工厂选择。"""
    from motte_storage.factory import create_run_store

    return RunService(create_run_store(db_path))


class RunService:
    """Shared run lifecycle used by API, CLI, and worker entry points."""

    TERMINAL = {"completed", "failed", "cancelled", "unsupported", "profile_stale"}

    def __init__(
        self,
        store: Any,
        provider: Callable[[str], Any] | None = None,
        *,
        event_observer: Callable[[dict[str, Any]], None] | None = None,
        progress_observer: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self._event_observers = [event_observer] if event_observer is not None else []
        self._progress_observers = [progress_observer] if progress_observer is not None else []

    def add_event_observer(self, observer: Callable[[dict[str, Any]], None]) -> None:
        self._event_observers.append(observer)

    def add_progress_observer(self, observer: Callable[[dict[str, Any]], None]) -> None:
        self._progress_observers.append(observer)

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
        if run.get("manifest", {}).get("benchmark_provenance"):
            if case_ids is not None and list(case_ids) != run["case_ids"]:
                raise ValueError("benchmark selected case ids are immutable")
            return self._execute_benchmark(run, invoke)
        ids = list(case_ids) if case_ids is not None else list(run.get("case_ids") or [])
        if ids and not run.get("case_ids"):
            run["case_ids"] = ids
            self.store.runs.save(run)
        if run["status"] == "queued":
            self._transition(run_id, "preparing")
        elif run["status"] == "preparing":
            # Worker 抢占已把状态置为 preparing（无事件）；这里幂等补齐事件，保证三入口 Trace 一致。
            if not any(event["type"] == "preparing" for event in self.store.events.list_for_run(run_id)):
                self._emit(run_id, "preparing", {"status": "preparing"})
        if self._load(run_id)["status"] != "running":
            self._transition(run_id, "running")
        done = {row["case_id"] for row in self.store.case_runs.list_for_run(run_id)}
        total = len(ids)
        try:
            for ordinal, case_id in enumerate(ids, 1):
                if case_id in done:
                    self._notify_progress({
                        "event": "case_skipped", "run_id": run_id, "case_id": case_id,
                        "ordinal": ordinal, "total": total, "reason": "already_persisted",
                    })
                    continue
                if self._load(run_id)["status"] == "cancelled":
                    return self.get_run(run_id)
                self._notify_progress({
                    "event": "case_started", "run_id": run_id, "case_id": case_id,
                    "ordinal": ordinal, "total": total,
                })
                started = perf_counter()
                try:
                    result = invoke(case_id) if invoke is not None else {"case_id": case_id}
                except Exception as error:
                    self._notify_progress({
                        "event": "case_finished", "run_id": run_id, "case_id": case_id,
                        "ordinal": ordinal, "total": total,
                        "duration_ms": round((perf_counter() - started) * 1000, 3),
                        "outcome": "call_failed", "error_class": getattr(error, "error_class", None)
                        or type(error).__name__,
                    })
                    raise
                expected = self._expected_for(invoke, expectations, case_id)
                entry: dict[str, Any] = {"run_id": run_id, "case_id": case_id, "result": result}
                if expected is not None:
                    entry["expected"] = expected
                self.store.case_runs.upsert(entry)
                self._emit(run_id, "model_response", {"case_id": case_id, "result": result})
                self._notify_progress({
                    "event": "case_finished", "run_id": run_id, "case_id": case_id,
                    "ordinal": ordinal, "total": total,
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                    "outcome": "responded", **self._result_summary(result),
                })
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
        if run.get("manifest", {}).get("benchmark_provenance"):
            self._finish_unattempted(run_id)
        return self._view(run_id)

    def rescore(self, run_id: str) -> dict[str, Any]:
        run = self._load(run_id)
        benchmark_terminal = run.get("manifest", {}).get("benchmark_provenance") and run["status"] in self.TERMINAL
        if run["status"] != "completed" and not benchmark_terminal:
            raise ValueError("only completed runs or terminal benchmarks can be rescored")
        scores = self._score_results(run_id, self.store.case_runs.list_for_run(run_id), emit_events=False)
        self.store.scores.replace_for_run(run_id, scores)
        run["rescored"] = True
        self.store.runs.save(run)
        self._emit(run_id, "rescored", {"status": run["status"], "scores": deepcopy(scores)})
        return self._view(run_id)

    def mark_unsupported(self, run_id: str, code: str, message: str | None = None) -> dict[str, Any]:
        run = self._load(run_id)
        if run["status"] in self.TERMINAL:
            return self._view(run_id)
        error: dict[str, Any] = {"code": code}
        if message is not None:
            error["message"] = message
        run.update({"status": "unsupported", "error": error})
        self.store.runs.save(run)
        self._emit(run_id, "unsupported", {"status": "unsupported", "error": run["error"]})
        if run.get("manifest", {}).get("benchmark_provenance"):
            self._finish_unattempted(run_id)
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
        run = self._load(run_id)
        if run.get("manifest", {}).get("benchmark_provenance"):
            from motte_sdk.suites import managed_scores

            scores = managed_scores(run, results)
            if emit_events:
                for score in scores:
                    self._emit(run_id, "score", score)
            return scores
        scores: list[dict[str, Any]] = []
        for entry in results:
            if "expected" in entry:
                passed = self._comparable(entry["result"]) == entry["expected"]
                scores.append({"case_id": entry["case_id"], "passed": passed})
                if emit_events:
                    self._emit(run_id, "score", {"case_id": entry["case_id"], "passed": passed})
        return scores

    def _finish_unattempted(self, run_id):
        run = self._load(run_id)
        done = {r["case_id"] for r in self.store.case_runs.list_for_run(run_id)}
        for case_id in run["case_ids"]:
            if case_id not in done:
                self.store.case_runs.upsert({"run_id": run_id, "case_id": case_id,
                                            "outcome": "not_attempted", "result": None})
        self.store.scores.replace_for_run(run_id, self._score_results(
            run_id, self.store.case_runs.list_for_run(run_id), False))

    def _execute_benchmark(self, run, invoke):
        from motte_eval.execution import CONTINUE_ERROR_CLASSES
        from motte_provider.errors import classify_exception

        run_id = run["id"]
        if run["status"] == "queued":
            self._transition(run_id, "preparing")
        if self._load(run_id)["status"] != "running":
            self._transition(run_id, "running")
        rows = self.store.case_runs.list_for_run(run_id)
        done = {row["case_id"] for row in rows}
        # A durable stop marker in the failed case also survives a crash before
        # final run status/scores are persisted.
        stop = any(row.get("stop_run") for row in rows)
        total = len(run["case_ids"])
        for ordinal, case_id in enumerate(run["case_ids"], 1):
            if case_id in done:
                self._notify_progress({
                    "event": "case_skipped", "run_id": run_id, "case_id": case_id,
                    "ordinal": ordinal, "total": total, "reason": "already_persisted",
                })
                continue
            if self._load(run_id)["status"] == "cancelled":
                return self.get_run(run_id)
            if stop:
                self.store.case_runs.upsert({"run_id": run_id, "case_id": case_id,
                                            "outcome": "not_attempted", "result": None})
                self._notify_progress({
                    "event": "case_skipped", "run_id": run_id, "case_id": case_id,
                    "ordinal": ordinal, "total": total, "reason": "run_stopped",
                })
                continue
            self._notify_progress({
                "event": "case_started", "run_id": run_id, "case_id": case_id,
                "ordinal": ordinal, "total": total,
            })
            started = perf_counter()
            try:
                if invoke is None:
                    raise ValueError("benchmark requires a provider")
                result = invoke(case_id)
            except Exception as error:
                result = deepcopy(getattr(error, "evidence", None)) or {
                    "error": {"class": classify_exception(error), "message": str(error)}}
            error = result.get("error") if isinstance(result, dict) else None
            stop = bool(error and error.get("class") not in CONTINUE_ERROR_CLASSES)
            self.store.case_runs.upsert({"run_id": run_id, "case_id": case_id,
                                        "result": result, "stop_run": stop})
            self._emit(run_id, "case_call_failed" if error else "model_response",
                       {"case_id": case_id, "result": result})
            self._notify_progress({
                "event": "case_finished", "run_id": run_id, "case_id": case_id,
                "ordinal": ordinal, "total": total,
                "duration_ms": round((perf_counter() - started) * 1000, 3),
                "outcome": "call_failed" if error else "responded",
                **self._result_summary(result),
            })
        if self._load(run_id)["status"] == "cancelled":
            return self.get_run(run_id)
        self._transition(run_id, "collecting")
        self._transition(run_id, "scoring")
        rows = self.store.case_runs.list_for_run(run_id)
        self.store.scores.replace_for_run(run_id, self._score_results(run_id, rows, True))
        errors = [row["result"]["error"] for row in rows
                  if isinstance(row.get("result"), dict) and row["result"].get("error")]
        if errors:
            current = self._load(run_id)
            current["error"] = errors[0]
            self.store.runs.save(current)
        return self._transition(run_id, "failed" if errors else "completed")

    @staticmethod
    def _comparable(result: Any) -> Any:
        """envelope 形状的结果取 content 参与比较；其余按原值。"""
        if isinstance(result, dict) and "content" in result:
            return result["content"]
        return result

    def _fail(self, run_id: str, error: Exception) -> dict[str, Any]:
        run = self._load(run_id)
        error_payload: dict[str, Any] = {"type": type(error).__name__, "message": str(error)}
        error_class = getattr(error, "error_class", None)
        if error_class is not None:
            error_payload["class"] = error_class
        evidence = getattr(error, "evidence", None)
        if evidence is not None:
            error_payload["evidence"] = evidence
        run.update({"status": "failed", "error": error_payload})
        self.store.runs.save(run)
        self._emit(run_id, "failed", {"status": "failed", "error": error_payload})
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

    @staticmethod
    def _result_summary(result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {}
        summary: dict[str, Any] = {}
        metering = result.get("metering")
        if isinstance(metering, dict):
            for key in ("latency_ms", "attempts", "retry_count", "error_class"):
                value = metering.get(key)
                if isinstance(value, (int, float, str)) or value is None:
                    summary[key] = value
        usage = result.get("usage")
        if isinstance(usage, dict):
            for source, target in (("prompt_tokens", "prompt_tokens"),
                                   ("completion_tokens", "completion_tokens"),
                                   ("total_tokens", "total_tokens")):
                value = usage.get(source)
                if isinstance(value, int) and not isinstance(value, bool):
                    summary[target] = value
        cost = result.get("cost")
        if isinstance(cost, dict):
            if isinstance(cost.get("total"), (int, float)) and not isinstance(cost.get("total"), bool):
                summary["cost_total"] = cost["total"]
            if isinstance(cost.get("price_table_version"), str):
                summary["price_table_version"] = cost["price_table_version"]
        error = result.get("error")
        if isinstance(error, dict) and isinstance(error.get("class"), str):
            summary["error_class"] = error["class"]
        return summary

    def _notify_progress(self, progress: dict[str, Any]) -> None:
        for observer in tuple(self._progress_observers):
            try:
                observer(deepcopy(progress))
            except Exception:
                pass

    def _emit(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        stored = self.store.events.append({"run_id": run_id, "type": event_type, **payload})
        for observer in tuple(self._event_observers):
            try:
                observer(deepcopy(stored))
            except Exception:
                pass
