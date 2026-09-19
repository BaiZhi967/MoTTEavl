from __future__ import annotations

from collections.abc import Callable, Iterable
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from motte_storage.integrity import RunConflictError, validate_scores

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
    "needs_review",
)

# 状态机迁移表；running/collecting/scoring 允许回到 running，用于 Worker 崩溃后的中断恢复。
TRANSITIONS: dict[str, set[str]] = {
    "queued": {"preparing", "cancelled", "unsupported", "profile_stale"},
    "preparing": {"running", "failed", "cancelled", "unsupported"},
    "running": {"collecting", "failed", "cancelled", "running", "needs_review"},
    "collecting": {"scoring", "failed", "cancelled", "running", "needs_review"},
    "scoring": {"completed", "failed", "cancelled", "running", "needs_review"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
    "unsupported": set(),
    "profile_stale": set(),
    "needs_review": set(),
}

RETRYABLE = {"failed", "cancelled", "unsupported", "profile_stale", "needs_review"}


def build_run_service(db_path: str | Path | None = None) -> RunService:
    """API、CLI、Worker 共用的服务构造入口；存储后端经 motte_storage 工厂选择。"""
    from motte_storage.factory import create_run_store

    return RunService(create_run_store(db_path))


class RunService:
    """Shared run lifecycle used by API, CLI, and worker entry points."""

    TERMINAL = {"completed", "failed", "cancelled", "unsupported", "profile_stale", "needs_review"}

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

    @staticmethod
    def _validate_frozen_benchmark_snapshot(run: dict[str, Any]) -> None:
        manifest = run.get("manifest")
        manifest = manifest if isinstance(manifest, dict) else {}
        provenance = manifest.get("benchmark_provenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        snapshot = manifest.get("benchmark_snapshot")
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        scenario_identity = snapshot.get("scenario")
        dataset_identity = snapshot.get("dataset")
        scenario_identity = scenario_identity if isinstance(scenario_identity, dict) else {}
        dataset_identity = dataset_identity if isinstance(dataset_identity, dict) else {}
        is_direct_v2 = (
            provenance.get("suite") == "direct-llm"
            and str(provenance.get("plugin_version") or "") == "2"
        ) or (
            snapshot.get("schema_version") == 2
            and str(scenario_identity.get("plugin_version") or "") == "2"
            and dataset_identity.get("contract_version") == 2
        )
        if is_direct_v2:
            from .direct_llm_v2 import selected_cases_from_snapshot

            selected_cases_from_snapshot(run)
        elif provenance.get("suite") == "agent-tasks":
            from .agent_tasks import selected_agent_cases_from_snapshot

            selected_agent_cases_from_snapshot(run)

    def create_run(
        self,
        scenario_version: str,
        manifest: dict[str, Any],
        case_ids: Iterable[str] = (),
        *,
        requested_manifest: dict[str, Any] | None = None,
        parent_run_id: str | None = None,
    ) -> dict[str, Any]:
        selected = list(case_ids)
        if len(selected) != len(set(selected)):
            raise ValueError("case_ids must be unique")
        if scenario_version.startswith("replay@") or (
            isinstance(manifest.get("execution"), dict)
            and manifest["execution"].get("backend_id") == "replay"
        ):
            from .execution_backends import resolve_replay_case_ids

            selected = resolve_replay_case_ids(manifest, selected)
        run_id = f"run-{uuid4().hex}"
        now = datetime.now(UTC).isoformat()
        run: dict[str, Any] = {
            "id": run_id,
            "schema_version": 2,
            "revision": 1,
            "scenario_version": scenario_version,
            "status": "queued",
            "requested_manifest": deepcopy(
                manifest if requested_manifest is None else requested_manifest
            ),
            "manifest": deepcopy(manifest),
            "case_ids": selected,
            "created_at": now,
            "updated_at": now,
        }
        if parent_run_id is not None:
            run["parent_run_id"] = parent_run_id
        self._validate_frozen_benchmark_snapshot(run)
        event = {"run_id": run_id, "type": "queued", "status": "queued"}
        self.store.runs.create(run, event=event)
        self._notify_latest_event(run_id)
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
        if run["status"] not in {"queued", "preparing"}:
            raise RunConflictError("run must be atomically claimed before execution")
        invoke = provider if provider is not None else self.provider
        requested_case_ids = list(case_ids) if case_ids is not None else None
        if run.get("manifest", {}).get("benchmark_provenance"):
            self._validate_frozen_benchmark_snapshot(run)
            if requested_case_ids is not None and requested_case_ids != run["case_ids"]:
                raise ValueError("benchmark selected case ids are immutable")
            return self._execute_benchmark(run, invoke)
        if requested_case_ids is not None and run.get("case_ids") and requested_case_ids != run["case_ids"]:
            raise ValueError("selected case ids are immutable")
        ids = requested_case_ids if requested_case_ids is not None else list(run.get("case_ids") or [])
        if ids and not run.get("case_ids"):
            expected_revision = run["revision"]
            run["case_ids"] = ids
            run["updated_at"] = datetime.now(UTC).isoformat()
            self.store.runs.update(run, expected_revision=expected_revision)
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
                cancelled = self._honor_cancellation(run_id)
                if cancelled is not None:
                    return cancelled
                self._notify_progress({
                    "event": "case_started", "run_id": run_id, "case_id": case_id,
                    "ordinal": ordinal, "total": total,
                })
                started = perf_counter()
                try:
                    attempt = self._begin_case_attempt(self._load(run_id), case_id)
                except RunConflictError:
                    cancelled = self._honor_cancellation(run_id)
                    if cancelled is not None:
                        return cancelled
                    raise
                try:
                    if invoke is None:
                        raise ValueError("execution backend did not provide an invoke hook")
                    result = invoke(case_id)
                except Exception as error:
                    evidence = deepcopy(getattr(error, "evidence", None))
                    result = evidence or {"error": {
                        "class": getattr(error, "error_class", None) or type(error).__name__,
                        "message": str(error),
                    }}
                    entry = {"run_id": run_id, "case_id": case_id, "result": result}
                    self._complete_case_attempt(attempt, entry, failed=True)
                    self._notify_progress({
                        "event": "case_finished", "run_id": run_id, "case_id": case_id,
                        "ordinal": ordinal, "total": total,
                        "duration_ms": round((perf_counter() - started) * 1000, 3),
                        "outcome": "call_failed", "error_class": getattr(error, "error_class", None)
                        or type(error).__name__,
                    })
                    cancelled = self._honor_cancellation(run_id)
                    if cancelled is not None:
                        return cancelled
                    raise
                expected = self._expected_for(invoke, expectations, case_id)
                entry = {"run_id": run_id, "case_id": case_id, "result": result}
                from .replay_run import NO_EXPECTATION

                if expected is not NO_EXPECTATION:
                    entry["expected"] = expected
                self._complete_case_attempt(attempt, entry, failed=False)
                self._notify_progress({
                    "event": "case_finished", "run_id": run_id, "case_id": case_id,
                    "ordinal": ordinal, "total": total,
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                    "outcome": "responded", **self._result_summary(result),
                })
        except Exception as error:
            return self._fail_or_quarantine(run_id, error)
        cancelled = self._honor_cancellation(run_id)
        if cancelled is not None:
            return cancelled
        results = self.store.case_runs.list_for_run(run_id)
        self._transition(run_id, "collecting")
        cancelled = self._honor_cancellation(run_id)
        if cancelled is not None:
            return cancelled
        self._transition(run_id, "scoring")
        cancelled = self._honor_cancellation(run_id)
        if cancelled is not None:
            return cancelled
        try:
            scores = self._score_results(run_id, results, emit_events=False)
            cancelled = self._honor_cancellation(run_id)
            if cancelled is not None:
                return cancelled
            self._append_scoring_pass(
                run_id, scores, source="initial", final_status="completed"
            )
        except Exception as error:
            return self._fail_or_quarantine(run_id, error)
        return self._view(run_id)

    def _honor_cancellation(self, run_id: str) -> dict[str, Any] | None:
        run = self._load(run_id)
        cancellation = run.get("cancellation")
        if run["status"] in self.TERMINAL or not isinstance(cancellation, dict):
            return self._view(run_id) if run["status"] == "cancelled" else None
        return self.cancel(run_id, reason=cancellation.get("reason"), _settle=True)

    def cancel(
        self, run_id: str, reason: str | None = None, *, _settle: bool = False
    ) -> dict[str, Any]:
        run = self._load(run_id)
        if run["status"] in self.TERMINAL:
            return self._view(run_id)
        cancellation = {"reason": reason} if reason is not None else {}
        open_attempts = self.store.attempts.list_open(run_id)
        if open_attempts or (
            not _settle and run["status"] in {"running", "collecting", "scoring"}
        ):
            now = datetime.now(UTC).isoformat()
            cancellation["requested_at"] = now
            self.store.runs.update(
                {**run, "cancellation": cancellation, "updated_at": now},
                expected_revision=run["revision"],
                expected_status=run["status"],
                event={
                    "run_id": run_id,
                    "type": "cancellation_requested",
                    "status": run["status"],
                    **({"reason": reason} if reason is not None else {}),
                },
            )
            self._notify_latest_event(run_id)
            return self._view(run_id)
        changes = {"cancellation": cancellation} if cancellation else {}
        if run.get("manifest", {}).get("benchmark_provenance"):
            try:
                self._finish_unattempted(
                    run_id,
                    final_status="cancelled",
                    final_changes=changes,
                    terminal_event_payload={"reason": reason} if reason is not None else {},
                )
            except RunConflictError:
                # A claim won the no-open-attempts race; leave a durable request for
                # the executor to settle after its in-flight attempt closes.
                current = self._load(run_id)
                if current["status"] in self.TERMINAL:
                    return self._view(run_id)
                cancellation["requested_at"] = datetime.now(UTC).isoformat()
                self.store.runs.update(
                    {**current, "cancellation": cancellation, "updated_at": cancellation["requested_at"]},
                    expected_revision=current["revision"], expected_status=current["status"],
                    event={"run_id": run_id, "type": "cancellation_requested", "status": current["status"]},
                )
        else:
            self._transition(
                run_id,
                "cancelled",
                changes=changes,
                event_payload={"reason": reason} if reason is not None else {},
            )
        return self._view(run_id)

    def rescore(self, run_id: str) -> dict[str, Any]:
        run = self._load(run_id)
        self._validate_frozen_benchmark_snapshot(run)
        benchmark_terminal = run.get("manifest", {}).get("benchmark_provenance") and run["status"] in self.TERMINAL
        if run["status"] != "completed" and not benchmark_terminal:
            raise ValueError("only completed runs or terminal benchmarks can be rescored")
        scores = self._score_results(
            run_id, self.store.case_runs.list_for_run(run_id), emit_events=False
        )
        scoring_pass = self._append_scoring_pass(run_id, scores, source="rescore")
        self._emit(run_id, "rescored", {
            "status": run["status"],
            "scoring_pass_id": scoring_pass["id"],
        })
        return self._view(run_id)

    def mark_unsupported(self, run_id: str, code: str, message: str | None = None) -> dict[str, Any]:
        run = self._load(run_id)
        if run["status"] in self.TERMINAL:
            return self._view(run_id)
        error: dict[str, Any] = {"code": code}
        if message is not None:
            error["message"] = message
        if run.get("manifest", {}).get("benchmark_provenance"):
            self._finish_unattempted(
                run_id, final_status="unsupported", final_error=error
            )
        else:
            self._transition(run_id, "unsupported", changes={"error": error})
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
        return self._transition(run_id, "profile_stale", changes={"error": error})

    def retry(
        self, run_id: str, *, refreshed_manifest: dict[str, Any] | None = None,
        case_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        parent = self._load(run_id)
        self._validate_frozen_benchmark_snapshot(parent)
        if parent["status"] not in RETRYABLE:
            raise ValueError(
                "only failed, cancelled, unsupported, profile_stale, or needs_review runs can be retried"
            )
        if parent["status"] == "profile_stale" and refreshed_manifest is None:
            raise ValueError("profile_stale retry requires a freshly resolved manifest")
        open_attempts = self.store.attempts.list_open(run_id)
        if open_attempts and parent["status"] != "needs_review":
            raise ValueError("run has unresolved attempts and must be quarantined before retry")
        manifest = deepcopy(
            refreshed_manifest if refreshed_manifest is not None else parent.get("manifest", {})
        )
        selected = list(case_ids) if case_ids is not None else list(parent.get("case_ids") or [])
        return self.create_run(
            parent["scenario_version"],
            manifest,
            selected,
            requested_manifest=deepcopy(
                parent.get("requested_manifest")
                if parent.get("requested_manifest") is not None
                else parent.get("manifest") or {}
            ),
            parent_run_id=run_id,
        )

    def events(self, run_id: str) -> list[dict[str, Any]]:
        return self.store.events.list_for_run(run_id)

    def emit_run_event(
        self, run_id: str, event_type: str, payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """后端证据通道：追加一条持久 trace 事件并广播（agent 步骤等）。"""
        try:
            stored = self._emit(run_id, event_type, payload or {})
            return stored
        except Exception:  # noqa: BLE001 - 证据通道故障不阻断执行
            return None

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
        scoring_pass = self.store.scoring_passes.current(run_id) if self.store.scoring_passes else None
        if scoring_pass is not None:
            run["scores"] = self.store.score_sets.list_for_pass(scoring_pass["id"])
            run["current_scoring_pass_id"] = scoring_pass["id"]
            run["scoring_pass"] = scoring_pass
        else:
            run["scores"] = self.store.scores.list_for_run(run_id)
        return run

    def _append_scoring_pass(
        self, run_id: str, scores: list[dict[str, Any]], *, source: str,
        final_status: str | None = None, final_error: dict[str, Any] | None = None,
        final_changes: dict[str, Any] | None = None,
        terminal_event_payload: dict[str, Any] | None = None,
        require_no_open_attempts: bool = False,
        skip_aggregate: bool = False,
        allow_aggregate_failure: bool = False,
        case_rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        run = self._load(run_id)
        scores = validate_scores(deepcopy(scores))
        evaluation = (run.get("manifest") or {}).get("evaluation") or {}
        provenance = (run.get("manifest") or {}).get("benchmark_provenance") or {}
        scorer_version = str(
            evaluation.get("scorer_version")
            or provenance.get("scorer_version")
            or next((row.get("scorer_version") for row in scores if row.get("scorer_version")), "generic-v1")
        )
        scorer_id = str(evaluation.get("scorer_id") or provenance.get("scorer") or scorer_version)
        selected_case_ids = set(run.get("case_ids") or [])
        if not selected_case_ids:
            selected_case_ids = {
                row["case_id"] for row in self.store.case_runs.list_for_run(run_id)
                if isinstance(row.get("case_id"), str)
            }
        # Cancellation/recovery may finalize a pass before every selected case has a score;
        # membership is mandatory, complete coverage is intentionally not.
        raw_score_case_ids = [score.get("case_id") for score in scores]
        if any(not isinstance(case_id, str) or not case_id for case_id in raw_score_case_ids):
            raise ValueError("scores require nonempty string case IDs")
        score_case_ids = set(raw_score_case_ids)
        unexpected_case_ids = score_case_ids - selected_case_ids
        if unexpected_case_ids:
            raise ValueError(
                "scores contain case IDs outside the run selection: "
                + ", ".join(sorted(str(case_id) for case_id in unexpected_case_ids))
            )
        if any(score.get("scoring_pass_id") is not None for score in scores):
            raise ValueError("scoring_pass_id is assigned by the scoring pass, not by plugins")
        previous = self.store.scoring_passes.current(run_id)
        manifest_bytes = json.dumps(
            run.get("manifest") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        passed = sum(score.get("passed") is True for score in scores)
        multi_metric = any(score.get("metric_id") for score in scores)
        metric_summary: dict[str, dict[str, int]] | None = None
        if multi_metric:
            metric_summary = {}
            for score in scores:
                bucket = metric_summary.setdefault(str(score["metric_id"]), {
                    "scored": 0, "passed": 0, "failed": 0,
                    "insufficient_evidence": 0, "evaluator_error": 0, "not_applicable": 0,
                })
                status = str(score.get("metric_status") or (
                    "scored" if score.get("passed") is not None else "not_applicable"
                ))
                if status in bucket:
                    bucket[status] += 1
                if status == "scored":
                    if score.get("passed") is True:
                        bucket["passed"] += 1
                    elif score.get("passed") is False:
                        bucket["failed"] += 1
        aggregate = None
        if provenance and not skip_aggregate:
            from .benchmark_plugins import aggregate_with_plugin

            try:
                aggregate = aggregate_with_plugin(run, scores)
                if not isinstance(aggregate, dict):
                    raise ValueError("benchmark aggregate must be an object")
                from motte_contracts.report import ReportSummary

                recognized = {
                    key: value for key, value in aggregate.items()
                    if key in ReportSummary.model_fields and key != "aggregate"
                }
                ReportSummary.model_validate({
                    "cases": recognized.get("cases", len(scores)),
                    "scored": recognized.get("scored", len(scores)),
                    "passed": recognized.get("passed", sum(
                        score.get("passed") is True for score in scores
                    )),
                    "failed": recognized.get("failed", sum(
                        score.get("passed") is False for score in scores
                    )),
                    **recognized,
                })
                selected_count = len(selected_case_ids)
                selected = recognized.get("selected")
                if selected is not None and selected != selected_count:
                    raise ValueError("benchmark aggregate selected count does not match the run")
                outcome_keys = (
                    "correct", "wrong_answer", "no_expectation", "parse_failure",
                    "call_failed", "not_attempted",
                )
                counts = [
                    recognized[key] for key in outcome_keys if recognized.get(key) is not None
                ]
                if any(count > selected_count for count in counts) or sum(counts) > selected_count:
                    raise ValueError("benchmark aggregate outcome counts exceed selected cases")
                attempted = recognized.get("attempted")
                responded = recognized.get("responded")
                not_attempted = recognized.get("not_attempted")
                if attempted is not None and attempted > selected_count:
                    raise ValueError("benchmark aggregate attempted count exceeds selected cases")
                if responded is not None and (
                    responded > selected_count or attempted is not None and responded > attempted
                ):
                    raise ValueError("benchmark aggregate responded count is inconsistent")
                if attempted is not None and not_attempted is not None and (
                    attempted + not_attempted > selected_count
                ):
                    raise ValueError("benchmark aggregate attempt counts exceed selected cases")
            except Exception as error:
                if not allow_aggregate_failure:
                    raise
                aggregate = None
                aggregate_details = {"error_type": type(error).__name__}
                if final_error:
                    final_error = {
                        **final_error,
                        "details": {
                            **(final_error.get("details") or {}),
                            "aggregate_error": aggregate_details,
                        },
                    }
                else:
                    final_error = {
                        "code": "AGGREGATE_UNAVAILABLE",
                        "message": "terminal run aggregate could not be computed",
                        "details": aggregate_details,
                    }
        pass_id = f"pass-{uuid4().hex}"
        record = {
            "id": pass_id,
            "run_id": run_id,
            "scorer_id": scorer_id,
            "scorer_version": scorer_version,
            "created_at": datetime.now(UTC).isoformat(),
            "source": source,
            "source_run_revision": run["revision"],
            "source_snapshot_hash": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
            "previous_pass_id": previous.get("id") if previous else None,
            "summary": {
                "scores": len(scores),
                "passed": passed,
                **({"multi_metric": True, "metrics": metric_summary} if metric_summary else {}),
                **({"aggregate": deepcopy(aggregate)} if aggregate is not None else {}),
            },
            "scores": deepcopy(scores),
        }
        now = datetime.now(UTC).isoformat()
        existing_events = self.store.events.list_for_run(run_id)
        last_seq = existing_events[-1]["seq"] if existing_events else 0
        stored = self.store.scoring_passes.append(
            record,
            scores,
            expected_run_revision=run["revision"],
            expected_run_status=run["status"],
            event={
                "run_id": run_id,
                "type": "scoring_pass_created",
                "scoring_pass_id": pass_id,
                "source": source,
                "scorer_id": scorer_id,
                "scorer_version": scorer_version,
            },
            score_events=([{
                "run_id": run_id,
                "type": "score",
                "scoring_pass_id": pass_id,
                "case_id": score["case_id"],
                **({"metric_id": score["metric_id"]} if score.get("metric_id") else {}),
                **({"passed": score["passed"]} if "passed" in score else {}),
            } for score in scores] if source != "terminal-unattempted" else []),
            final_status=final_status,
            run_changes={
                "updated_at": now,
                **({"finished_at": now} if final_status in self.TERMINAL else {}),
                **deepcopy(final_changes or {}),
                **({"error": deepcopy(final_error)} if final_error is not None else {}),
            },
            terminal_event={
                "run_id": run_id, "type": final_status, "status": final_status,
                **deepcopy(terminal_event_payload or {}),
                **({"error": deepcopy(final_error)} if final_error is not None else {}),
            } if final_status is not None else None,
            require_no_open_attempts=require_no_open_attempts,
            case_rows=case_rows,
        )
        # The append-only pass is authoritative. A compatibility mirror failure must not
        # make an already committed pass look unsuccessful to callers.
        try:
            self.store.scores.replace_for_run(run_id, scores)
        except Exception:
            pass
        for event in self.store.events.list_after(run_id, last_seq):
            self._notify_event(event)
        return stored

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

    def _begin_case_attempt(self, run: dict[str, Any], case_id: str) -> dict[str, Any]:
        previous = [
            item for item in self.store.attempts.list_for_run(run["id"])
            if item.get("case_id") == case_id
        ]
        now = datetime.now(UTC).isoformat()
        backend = ((run.get("manifest") or {}).get("execution") or {}).get("backend_id")
        attempt = self.store.attempts.begin({
            "run_id": run["id"],
            "case_id": case_id,
            "attempt_no": len(previous) + 1,
            "execution_backend_id": backend,
            "idempotency_key": f"{run['id']}:{case_id}:{len(previous) + 1}",
            "prepared_at": now,
        }, expected_run_revision=run["revision"], expected_run_status=run["status"])
        try:
            return self.store.attempts.dispatch(
                attempt["id"],
                expected_revision=attempt["revision"],
                run_id=run["id"],
                expected_run_revision=run["revision"],
                expected_run_status=run["status"],
            )
        except RunConflictError:
            current = self.store.attempts.get(attempt["id"])
            if current is not None and current.get("status") == "prepared":
                self.store.attempts.transition(
                    attempt["id"], expected_revision=current["revision"],
                    expected_status="prepared", status="failed",
                    changes={
                        "finished_at": datetime.now(UTC).isoformat(),
                        "error": {"code": "DISPATCH_ABORTED"},
                    },
                )
            raise

    def _complete_case_attempt(
        self,
        attempt: dict[str, Any],
        case_run: dict[str, Any],
        *,
        failed: bool,
    ) -> None:
        self._complete_open_attempt(attempt, case_run, failed=failed, emit_event=True)

    def _complete_open_attempt(
        self,
        attempt: dict[str, Any],
        case_run: dict[str, Any],
        *,
        failed: bool,
        emit_event: bool,
    ) -> None:
        """完成一个开放的 case attempt 并落盘结果；emit_event=False 用于
        事件已先行发出的补跑/落盘路径（时间线不重复报同一结果）。"""
        result = case_run.get("result")
        event_type = "case_call_failed" if failed else "model_response"
        self.store.attempts.complete(
            attempt["id"],
            expected_revision=attempt["revision"],
            status="failed" if failed else "succeeded",
            changes={
                "finished_at": datetime.now(UTC).isoformat(),
                "result": deepcopy(result),
            },
            case_run=case_run,
            event={
                "run_id": attempt["run_id"],
                "type": event_type,
                "case_id": attempt["case_id"],
                "result": deepcopy(result),
            } if emit_event else None,
        )
        self._notify_latest_event(attempt["run_id"])

    def _finish_unattempted(
        self, run_id: str, *, final_status: str | None = None,
        final_error: dict[str, Any] | None = None,
        final_changes: dict[str, Any] | None = None,
        terminal_event_payload: dict[str, Any] | None = None,
    ) -> None:
        run = self._load(run_id)
        existing_rows = self.store.case_runs.list_for_run(run_id)
        done = {row["case_id"] for row in existing_rows}
        open_case_ids = {
            attempt["case_id"] for attempt in self.store.attempts.list_open(run_id)
        }
        synthetic_rows = [
            {"run_id": run_id, "case_id": case_id, "outcome": "not_attempted", "result": None}
            for case_id in run["case_ids"]
            if case_id not in done and case_id not in open_case_ids
        ]
        rows = existing_rows + synthetic_rows
        scoring_error: Exception | None = None
        try:
            scores = self._score_results(run_id, rows, False)
        except Exception as error:
            scoring_error = error
            scores = [
                {
                    "case_id": row["case_id"],
                    "passed": None,
                    "details": {
                        "code": "SCORING_UNAVAILABLE",
                        "error": str(error),
                    },
                }
                for row in rows
            ]
        effective_error = deepcopy(final_error)
        if scoring_error is not None:
            scoring_details = {"error_type": type(scoring_error).__name__}
            if effective_error:
                effective_error = {
                    **effective_error,
                    "details": {
                        **(effective_error.get("details") or {}),
                        "scoring_error": scoring_details,
                    },
                }
            else:
                effective_error = {
                    "code": "SCORING_UNAVAILABLE",
                    "message": "terminal run could not be fully scored",
                    "details": scoring_details,
                }
        append_args = {
            "source": "terminal-unattempted",
            "final_status": final_status,
            "final_error": effective_error,
            "final_changes": final_changes,
            "terminal_event_payload": terminal_event_payload,
            "require_no_open_attempts": True,
            "allow_aggregate_failure": True,
            "case_rows": synthetic_rows,
        }
        try:
            self._append_scoring_pass(run_id, scores, **append_args)
        except Exception as error:
            if isinstance(error, RunConflictError):
                raise
            # A malformed plugin result must not veto cancellation/unsupported
            # terminalization. Persist a valid unjudged evidence set instead.
            fallback: list[dict[str, Any]] = []
            seen: set[str] = set()
            for row in rows:
                case_id = row.get("case_id")
                if isinstance(case_id, str) and case_id and case_id not in seen:
                    seen.add(case_id)
                    fallback.append({
                        "case_id": case_id,
                        "passed": None,
                        "details": {"code": "SCORING_UNAVAILABLE"},
                    })
            fallback_details = {"error_type": type(error).__name__}
            if effective_error:
                append_args["final_error"] = {
                    **effective_error,
                    "details": {
                        **(effective_error.get("details") or {}),
                        "scoring_error": fallback_details,
                    },
                }
            else:
                append_args["final_error"] = {
                    "code": "SCORING_UNAVAILABLE",
                    "message": "terminal run could not be fully scored",
                    "details": fallback_details,
                }
            self._append_scoring_pass(run_id, fallback, **append_args)

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
        # 瞬时失败（可继续错误类）暂不落盘：attempt 保持开放，收尾前统一补跑一次。
        pending: dict[str, tuple[int, dict[str, Any], dict[str, Any]]] = {}
        for ordinal, case_id in enumerate(run["case_ids"], 1):
            if case_id in done:
                self._notify_progress({
                    "event": "case_skipped", "run_id": run_id, "case_id": case_id,
                    "ordinal": ordinal, "total": total, "reason": "already_persisted",
                })
                continue
            cancelled = self._cancel_checkpoint(run_id, pending)
            if cancelled is not None:
                return cancelled
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
                attempt = self._begin_case_attempt(self._load(run_id), case_id)
            except RunConflictError:
                cancelled = self._cancel_checkpoint(run_id, pending)
                if cancelled is not None:
                    return cancelled
                raise
            try:
                if invoke is None:
                    raise ValueError("benchmark requires an execution backend invoke hook")
                result = invoke(case_id)
            except Exception as error:
                result = deepcopy(getattr(error, "evidence", None)) or {
                    "error": {"class": classify_exception(error), "message": str(error)}}
            error = result.get("error") if isinstance(result, dict) else None
            # agent 后端有文件副作用：dispatch 后结果不确定的整段补跑被禁止，
            # 瞬态失败立即落盘交由操作员显式 retry（M1-G09）。
            allow_second_chance = (
                run.get("manifest", {}).get("execution", {}).get("backend_id") != "builtin-agent"
            )
            if error and error.get("class") in CONTINUE_ERROR_CLASSES and allow_second_chance:
                pending[case_id] = (ordinal, attempt, result)
                self._emit(run_id, "case_call_failed",
                           {"case_id": case_id, "result": deepcopy(result)})
                self._notify_progress({
                    "event": "case_finished", "run_id": run_id, "case_id": case_id,
                    "ordinal": ordinal, "total": total,
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                    "outcome": "call_failed", "reason": "second_chance_pending",
                    **self._result_summary(result),
                })
                continue
            # 走到这里的一定不是瞬时错误；非空 error 即持久停止标记。
            stop = bool(error)
            entry = {"run_id": run_id, "case_id": case_id,
                     "result": result, "stop_run": stop}
            self._complete_case_attempt(attempt, entry, failed=bool(error))
            self._notify_progress({
                "event": "case_finished", "run_id": run_id, "case_id": case_id,
                "ordinal": ordinal, "total": total,
                "duration_ms": round((perf_counter() - started) * 1000, 3),
                "outcome": "call_failed" if error else "responded",
                **self._result_summary(result),
            })
        cancelled = self._cancel_checkpoint(run_id, pending)
        if cancelled is not None:
            return cancelled
        cancelled = self._sweep_transient_failures(run_id, invoke, pending)
        if cancelled is not None:
            return cancelled
        self._transition(run_id, "collecting")
        cancelled = self._honor_cancellation(run_id)
        if cancelled is not None:
            return cancelled
        self._transition(run_id, "scoring")
        cancelled = self._honor_cancellation(run_id)
        if cancelled is not None:
            return cancelled
        rows = self.store.case_runs.list_for_run(run_id)
        errors = [row["result"]["error"] for row in rows
                  if isinstance(row.get("result"), dict) and row["result"].get("error")]
        final_status = "failed" if errors else "completed"
        try:
            scores = self._score_results(run_id, rows, False)
            cancelled = self._honor_cancellation(run_id)
            if cancelled is not None:
                return cancelled
            self._append_scoring_pass(
                run_id,
                scores,
                source="initial",
                final_status=final_status,
                final_error=errors[0] if errors else None,
            )
        except Exception as error:
            return self._fail_or_quarantine(run_id, error)
        return self._view(run_id)

    def _sweep_transient_failures(
        self, run_id: str, invoke: Callable[[str], Any],
        pending: dict[str, tuple[int, dict[str, Any], dict[str, Any]]],
    ) -> dict[str, Any] | None:
        """瞬时失败题（server/network/timeout/rate_limit）收尾前补跑一次。

        网关瞬时 5xx 一类的不稳定不该让整次基准一票否决：主循环全部跑完后，
        对挂在 pending 里的瞬时失败题再调一次；补跑成功则用恢复结果完成该题
        记账（首次错误以 first_attempt_error 嵌入结果留证），仍失败则落盘
        最终错误。存储契约不变：每题一条 case 结果、一个 attempt 记账。
        """
        from motte_provider.errors import classify_exception

        total = len(self._load(run_id)["case_ids"])
        for case_id in list(pending):
            if case_id not in pending:
                continue  # 已因取消被统一落盘
            cancelled = self._cancel_checkpoint(run_id, pending)
            if cancelled is not None:
                return cancelled
            ordinal, attempt, first_result = pending.pop(case_id)
            self._notify_progress({
                "event": "case_started", "run_id": run_id, "case_id": case_id,
                "ordinal": ordinal, "total": total, "reason": "second_chance",
            })
            started = perf_counter()
            try:
                result = invoke(case_id)
            except Exception as error:
                result = deepcopy(getattr(error, "evidence", None)) or {
                    "error": {"class": classify_exception(error), "message": str(error)}}
            error = result.get("error") if isinstance(result, dict) else None
            if not error:
                result = {**result,
                          "first_attempt_error": deepcopy(first_result["error"])}
            entry = {"run_id": run_id, "case_id": case_id,
                     "result": result, "stop_run": bool(error)}
            # 首次失败的 case_call_failed 事件已在主循环发过，这里只在补跑
            # 成功时发 model_response，避免时间线重复报同一失败。
            self._complete_open_attempt(attempt, entry, failed=bool(error),
                                        emit_event=not error)
            self._notify_progress({
                "event": "case_finished", "run_id": run_id, "case_id": case_id,
                "ordinal": ordinal, "total": total,
                "duration_ms": round((perf_counter() - started) * 1000, 3),
                "outcome": "call_failed" if error else "responded",
                "reason": "second_chance",
                **self._result_summary(result),
            })
        return None

    def _cancel_checkpoint(
        self, run_id: str,
        pending: dict[str, tuple[int, dict[str, Any], dict[str, Any]]],
    ) -> dict[str, Any] | None:
        """取消检查点：先把挂起的瞬时失败题按首次错误落盘，取消才能到终态。"""
        if pending and isinstance(self._load(run_id).get("cancellation"), dict):
            self._settle_pending(run_id, pending)
        return self._honor_cancellation(run_id)

    def _settle_pending(
        self, run_id: str,
        pending: dict[str, tuple[int, dict[str, Any], dict[str, Any]]],
    ) -> None:
        """把挂起的瞬时失败题按首次错误完成记账（事件已在主循环发过）。"""
        total = len(self._load(run_id)["case_ids"])
        for case_id in list(pending):
            ordinal, attempt, result = pending.pop(case_id)
            entry = {"run_id": run_id, "case_id": case_id,
                     "result": result, "stop_run": False}
            self._complete_open_attempt(attempt, entry, failed=True, emit_event=False)
            self._notify_progress({
                "event": "case_finished", "run_id": run_id, "case_id": case_id,
                "ordinal": ordinal, "total": total, "reason": "second_chance_aborted",
            })

    @staticmethod
    def _comparable(result: Any) -> Any:
        """envelope 形状的结果取 content 参与比较；其余按原值。"""
        if isinstance(result, dict) and "content" in result:
            return result["content"]
        return result

    def _fail_or_quarantine(self, run_id: str, error: Exception) -> dict[str, Any]:
        uncertain = [
            attempt for attempt in self.store.attempts.list_for_run(run_id)
            if attempt.get("status") in {"dispatching", "indeterminate"}
        ]
        if not uncertain:
            return self._fail(run_id, error)
        run = self._load(run_id)
        attempt_ids = [attempt["id"] for attempt in uncertain]
        now = datetime.now(UTC).isoformat()
        self.store.attempts.quarantine_indeterminate(
            run_id,
            expected_run_revision=run["revision"],
            expected_run_status=run["status"],
            changes={
                "updated_at": now,
                "finished_at": now,
                "error": {
                    "code": "CALL_OUTCOME_INDETERMINATE",
                    "message": "the provider returned but durable case completion failed",
                    "details": {
                        "attempt_ids": attempt_ids,
                        "persistence_error": type(error).__name__,
                    },
                },
            },
            event={
                "run_id": run_id,
                "type": "needs_review",
                "status": "needs_review",
                "attempt_ids": attempt_ids,
            },
        )
        self._notify_latest_event(run_id)
        return self._view(run_id)

    def _fail(self, run_id: str, error: Exception) -> dict[str, Any]:
        run = self._load(run_id)
        error_payload: dict[str, Any] = {"type": type(error).__name__, "message": str(error)}
        error_class = getattr(error, "error_class", None)
        if error_class is not None:
            error_payload["class"] = error_class
        evidence = getattr(error, "evidence", None)
        if evidence is not None:
            error_payload["evidence"] = evidence
        if run["status"] in self.TERMINAL:
            return self._view(run_id)
        return self._transition(run_id, "failed", changes={"error": error_payload})

    def _transition(
        self,
        run_id: str,
        status: str,
        *,
        changes: dict[str, Any] | None = None,
        event_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run = self._load(run_id)
        previous = run["status"]
        if status not in TRANSITIONS.get(previous, set()):
            raise ValueError(f"invalid transition: {previous} -> {status}")
        now = datetime.now(UTC).isoformat()
        patch = {**(changes or {}), "updated_at": now}
        if status == "running" and not run.get("started_at"):
            patch["started_at"] = now
        if status in self.TERMINAL:
            patch["finished_at"] = now
        self.store.runs.transition(
            run_id,
            expected_revision=run["revision"],
            expected_status=previous,
            status=status,
            changes=patch,
            event={
                "run_id": run_id,
                "type": status,
                "status": status,
                **(event_payload if event_payload is not None else (changes or {})),
            },
        )
        self._notify_latest_event(run_id)
        return self._view(run_id)

    @staticmethod
    def _expected_for(
        invoke: Callable[[str], Any] | None,
        expectations: dict[str, Any] | None,
        case_id: str,
    ) -> Any:
        from .replay_run import NO_EXPECTATION

        if expectations is not None:
            return expectations.get(case_id, NO_EXPECTATION)
        receiver = getattr(invoke, "__self__", None)
        expected_for = getattr(invoke, "expected_for", None)
        if not callable(expected_for):
            expected_for = getattr(receiver, "expected_for", None)
        if callable(expected_for):
            value = expected_for(case_id)
            fixture = getattr(invoke, "fixture", None)
            if not isinstance(fixture, dict):
                fixture = getattr(receiver, "fixture", None)
            if value is None and not (
                isinstance(fixture, dict) and "expected" in fixture.get(case_id, {})
            ):
                return NO_EXPECTATION
            return value
        return NO_EXPECTATION

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
        self._notify_event(stored)

    def _notify_latest_event(self, run_id: str) -> None:
        events = self.store.events.list_for_run(run_id)
        if events:
            self._notify_event(events[-1])

    def _notify_event(self, event: dict[str, Any]) -> None:
        for observer in tuple(self._event_observers):
            try:
                observer(deepcopy(event))
            except Exception:
                pass
