"""Bounded code-review witnesses; the caller owns the live HTTP safety guard.

Subject observations are explicitly synthetic code fixtures, never represented as
live agent outputs. Calibration samples remain unreviewed synthetic candidates.
No credentials are inspected here: the normal frozen provider factory resolves
only the caller's selected profile when the Worker actually executes a call.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from apps.worker.motte_worker.reporting import WorkerReporter
from apps.worker.motte_worker.runtime import WorkerLoop
from motte_contracts.evaluation import observation_evidence_hash
from motte_eval.calibration import build_calibration_set, candidate_sample
from motte_eval.calibration_records import (
    CalibrationImport, CalibrationRunRequest, calibration_execution_id,
)
from motte_eval.judge import build_judge_spec
from motte_sdk.judge_calibrations import JudgeCalibrationService
from motte_sdk.scoring_jobs import (
    FrozenProviderFactory, ScoringJobService, build_judge_submission,
    freeze_judge_provider_snapshot,
)
from motte_sdk.service import RunService
from motte_storage.resource_store import SQLiteResourceStore
from motte_storage.run_store import SQLiteRunStore

CALL_CAPS = {"check_judge_single": 1, "check_judge_pairwise": 2,
             "check_judge_calibration": 2}
MODEL_ID = "go-code-review"
CASE_ID = "python-add-review"
CORRECT = "Task: implement Python add(a, b) returning a + b.\nCode: def add(a, b): return a + b"
INCORRECT = "Task: implement Python add(a, b) returning a + b.\nCode: def add(a, b): return a - b"

SAFE_FAILURE_CODES = frozenset("judge_" + code for code in (
    "witness_failure", "output_cap_invalid", "request_timeout_invalid", "worker_wrong_job", "worker_not_completed",
    "unexpected_call_count", "output_not_scored", "invocation_ledger_mismatch",
    "preflight_call_count", "pass_missing", "scores_missing", "pairwise_code_preference_mismatch",
    "code_review_did_not_pass", "idempotent_submit_changed", "report_changed",
    "report_scores_changed", "report_rebilled", "calibration_plan_expanded",
    "calibration_child_count", "calibration_not_honest", "calibration_ledger_incomplete",
    "calibration_report_changed", "calibration_replay_changed", "calibration_rebilled",
    "calibration_repeat_inconsistent", "calibration_code_review_did_not_pass",
    "calibration_repeat_metric_mismatch", "calibration_call_evidence_invalid",
    "calibration_gate_reason_mismatch",
))
SAFE_DIAGNOSTIC_KEYS = frozenset((
    "job_status_code", "planned_calls", "billed_calls", "returned_calls", "settled_calls",
    "parse_failure_count", "non_scored_count", "completion_tokens", "calls_at_output_cap",
    "calibration_call_count", "repeat_measured", "repeat_sample_count", "repeat_stable_count",
    "repeat_unstable_count", "repeat_rate_is_one", "criterion_disagreement_count",
    "criterion_false_count", "criterion_missing_count", "calibration_invalid_call_count",
))
JOB_STATUS_CODES = {name: index for index, name in enumerate((
    "queued", "prepared", "dispatching", "settled", "completed", "failed", "cancelled",
    "indeterminate",
))}


class JudgeWitnessFailure(RuntimeError):
    """Public harness failure with no upstream text or unapproved diagnostic values."""

    def __init__(self, code, diagnostics=None):
        self.code = code if code in SAFE_FAILURE_CODES else "judge_witness_failure"
        self.error_class = self.code
        self.diagnostics = {key: value for key, value in (diagnostics or {}).items()
                            if key in SAFE_DIAGNOSTIC_KEYS and type(value) is int and value >= 0}
        super().__init__(self.code)


def _require(condition, code, diagnostics=None):
    if not condition:
        raise JudgeWitnessFailure("judge_" + code, diagnostics)


def _output_cap(config):
    value = config.get("max_output_tokens", 512)
    _require(type(value) is int and 1 <= value <= 4096, "output_cap_invalid")
    return value


def _job_diagnostics(job):
    """Project only counts; never forward saved failure strings or response bodies."""
    if not isinstance(job, dict):
        return {}
    result = job.get("result") or {}
    calls = job.get("calls") or []
    plans = job.get("plans") or []
    ceilings = {plan.get("call_id"): plan.get("output_tokens") for plan in plans}
    at_cap = 0
    for call in calls:
        tokens = (call.get("usage") or {}).get("completion_tokens")
        ceiling = ceilings.get(call.get("call_id"))
        if type(tokens) is int and type(ceiling) is int and tokens >= ceiling:
            at_cap += 1
    values = {
        "planned_calls": len(plans), "billed_calls": job.get("billed_calls"),
        "returned_calls": job.get("returns"),
        "settled_calls": sum(call.get("status") == "settled" for call in calls),
        "parse_failure_count": len(result.get("parse_failures") or []),
        "non_scored_count": result.get("non_scored"),
        "completion_tokens": (job.get("usage_total") or {}).get("completion_tokens"),
        "calls_at_output_cap": at_cap,
    }
    status = JOB_STATUS_CODES.get(job.get("status"))
    if status is not None:
        values["job_status_code"] = status
    return JudgeWitnessFailure("judge_witness_failure", values).diagnostics


def _setup(ctx, label):
    root = Path(ctx.root) / label
    root.mkdir(parents=True, exist_ok=False)
    path = root / "judge.db"
    resources = SQLiteResourceStore(path)
    config = ctx.provider_config()
    output_cap = _output_cap(config)
    timeout = config.get("timeout",20)
    _require(type(timeout) in (int,float) and 0 < timeout <= 120,"request_timeout_invalid")
    connection = {key: config[key] for key in
                  ("kind", "base_url", "credentials", "api_key_env") if key in config}
    resources.providers.put({**connection, "name": "go-judge", "generation": 1,
                             "enabled": True, "timeout": timeout, "max_retries": 0})
    resources.models.put({
        "id": MODEL_ID, "provider": "go-judge", "model": config["model"],
        "capabilities": {"text": True}, "max_output_tokens": output_cap,
        "parameters": {"max_output_tokens": output_cap}, "identity_policy": "require_match",
        "lifecycle": "published", "generation": 1,
        "published_at": datetime.now(UTC).isoformat(),
    })
    return path, SQLiteRunStore(path), resources


def _calibration_diagnostics(report, criteria):
    """Count report facts without retaining model explanations or dynamic labels."""
    grades = report.calls
    stability = report.repeat_stability
    values = {
        "calibration_call_count": len(grades),
        "repeat_measured": int(stability.get("measured") is True),
        "repeat_sample_count": stability.get("samples"),
        "repeat_stable_count": stability.get("stable"),
        "repeat_unstable_count": len(stability.get("unstable_samples") or []),
        "repeat_rate_is_one": int(stability.get("rate") == 1.0),
        "criterion_disagreement_count": sum(
            len({grade.criteria.get(key) for grade in grades}) > 1 for key in criteria),
        "criterion_false_count": sum(
            grade.criteria.get(key) is False for grade in grades for key in criteria),
        "criterion_missing_count": sum(
            key not in grade.criteria for grade in grades for key in criteria),
        "calibration_invalid_call_count": sum(
            grade.sample_id != CASE_ID or grade.kind not in {"single", "repeat"}
            or grade.status != "ok" or grade.outcome != "succeeded" for grade in grades),
    }
    return JudgeWitnessFailure("judge_witness_failure", values).diagnostics


def _spec_request(calls, output_cap):
    return {"judge_profile_id": "bounded-python-review", "model": MODEL_ID,
            "rubric_id": "answer-quality", "rubric_version": "1",
            "parameters": {"max_output_tokens": output_cap},
            "input_selector": {"fields": ["final_output", "termination", "event_refs"]},
            "budget": {"max_calls": calls, "max_prompt_tokens": 40_000,
                       "max_completion_tokens": output_cap * calls}}


def _authorisation(calls, output_cap):
    return {"authorised": True, "actor": "explicit-bounded-live-smoke", "max_calls": calls,
            "max_total_tokens": 40_000 + output_cap * calls}


def _observation(store, run_id, text, *, attempt_id=None):
    event = store.events.append({"run_id": run_id, "type": "synthetic_code_fixture",
                                       "payload": {"source": "synthetic_fixture", "code": text}})
    raw = {"schema_version": 1, "observation_id": "obs-" + (attempt_id or CASE_ID),
           "run_id": run_id, "case_id": CASE_ID, "final_output": text,
           "termination": {"reason": "final_answer"}, "coverage": {"complete": True},
           "event_refs": [{"kind": "event", "run_id": run_id, "locator": str(event["seq"])}],
           "artifact_refs": [], "tool_calls": [], "workspace": {"before": [], "after": []}}
    if attempt_id:
        raw["attempt_id"] = attempt_id
    raw["evidence_hash"] = observation_evidence_hash(raw)
    return raw


def _synthetic_subject(store, *, pairwise):
    run_id = "synthetic-code-review-input"
    timestamp = datetime.now(UTC).isoformat()
    store.runs.create({
        "id": run_id, "schema_version": 2, "revision": 1, "scenario_version": "replay@1",
        "status": "completed", "case_ids": [CASE_ID], "created_at": timestamp,
        "updated_at": timestamp, "requested_manifest": {},
        "manifest": {"synthetic_fixture": True,
                     "evaluation": {"scorer_id": "deterministic", "scorer_version": "1"}},
    }, event={"run_id": run_id, "type": "synthetic_fixture_import", "status": "completed"})
    if not pairwise:
        raw = _observation(store, run_id, CORRECT)
        store.case_runs.upsert({"run_id": run_id, "case_id": CASE_ID, "outcome": "responded",
                                "result": {"observation": raw}, "expected": {}})
        return run_id, None
    ids = []
    for index, code in enumerate((CORRECT, INCORRECT), 1):
        attempt = store.attempts.begin({"id": f"synthetic-code-{index}", "run_id": run_id,
                                       "case_id": CASE_ID, "attempt_no": index})
        attempt = store.attempts.transition(attempt["id"], expected_revision=attempt["revision"],
                                            expected_status="prepared", status="dispatching")
        raw = _observation(store, run_id, code, attempt_id=attempt["id"])
        store.attempts.complete(attempt["id"], expected_revision=attempt["revision"],
                                changes={"result": {"frozen_observation": raw,
                                                    "source": "synthetic_fixture"}})
        ids.append(attempt["id"])
    return run_id, {"case_id": CASE_ID, "candidate_a_attempt_id": ids[0],
                    "candidate_b_attempt_id": ids[1], "challenger_attempt_id": ids[0]}


def _scoring(store):
    return ScoringJobService(store, provider_factory=FrozenProviderFactory())


def _execute(path, job_id, calls):
    # Reopen the database: queued input and model identity must be durable.
    store = SQLiteRunStore(path)
    scoring = _scoring(store)
    result = WorkerLoop(RunService(store), scoring_jobs=scoring,
                        reporter=WorkerReporter(enabled=False)).claim_and_execute()
    saved = scoring.jobs.get(job_id)
    diagnostics = _job_diagnostics(saved)
    _require(result is not None and result.get("job_id") == job_id, "worker_wrong_job", diagnostics)
    _require(result["status"] == "completed", "worker_not_completed", diagnostics)
    _require(saved["billed_calls"] == calls, "unexpected_call_count", diagnostics)
    _require(not saved["result"]["parse_failures"] and not saved["result"]["non_scored"],
             "output_not_scored", diagnostics)
    invocations = store.invocations.list_for_job(job_id)
    _require(len(invocations) == calls and all(row["purpose"] == "judge" for row in invocations),
             "invocation_ledger_mismatch", diagnostics)
    return store, scoring, saved


def _subject_check(ctx, *, pairwise):
    calls = 2 if pairwise else 1
    label = "judge-pairwise" if pairwise else "judge-single"
    path, store, resources = _setup(ctx, label)
    output_cap = resources.models.get(MODEL_ID)["max_output_tokens"]
    run_id, pair_ref = _synthetic_subject(store, pairwise=pairwise)
    args = {"store": store, "resources": resources, "request_key": label + "-" + uuid4().hex, "run_id": run_id,
            "mode": "pairwise" if pairwise else "single", "spec_request": _spec_request(calls, output_cap),
            "authorisation": _authorisation(calls, output_cap)}
    if pairwise:
        args["pairwise_refs"] = [pair_ref]
        ids = ["attempt:" + pair_ref[key] for key in
               ("candidate_a_attempt_id", "candidate_b_attempt_id")]
        args["presentation_orders"] = [ids, list(reversed(ids))]
    else:
        args["case_ids"] = [CASE_ID]
    request = build_judge_submission(**args)
    scoring = _scoring(store)
    preview = scoring.preflight(request)
    _require(preview["max_calls"] == calls, "preflight_call_count")
    job = scoring.submit(request)
    store, scoring, saved = _execute(path, job["job_id"], calls)
    diagnostics = _job_diagnostics(saved)
    record = store.scoring_passes.get(saved["reserved_pass_id"])
    _require(record is not None, "pass_missing", diagnostics)
    scores = store.score_sets.list_for_pass(record["id"])
    _require(bool(scores) and all(row["metric_status"] == "scored" for row in scores),
             "scores_missing", diagnostics)
    if pairwise:
        values = [row["value"] for row in scores if row["metric_id"] == "pairwise_preference"]
        _require(values == [1.0, 1.0], "pairwise_code_preference_mismatch", diagnostics)
    else:
        _require(all(row["passed"] is True for row in scores), "code_review_did_not_pass", diagnostics)
    before = len(store.invocations.list_for_job(job["job_id"]))
    _require(scoring.submit(request)["job_id"] == job["job_id"], "idempotent_submit_changed", diagnostics)
    reopened = SQLiteRunStore(path)
    _require(reopened.scoring_passes.get(record["id"]) == record, "report_changed", diagnostics)
    _require(reopened.score_sets.list_for_pass(record["id"]) == scores, "report_scores_changed", diagnostics)
    _require(len(reopened.invocations.list_for_job(job["job_id"])) == before, "report_rebilled", diagnostics)
    return {"ok": True, "job_id":job["job_id"], "billed_calls": calls, "mode": request.mode,
            "subject_source": "synthetic_frozen_fixture", "report_immutable": True,
            "idempotent_submit": True, "metric_rows": len(scores)}


def check_judge_single(ctx):
    return _subject_check(ctx, pairwise=False)


def check_judge_pairwise(ctx):
    return _subject_check(ctx, pairwise=True)


def check_judge_calibration(ctx):
    path, store, resources = _setup(ctx, "judge-calibration")
    output_cap = resources.models.get(MODEL_ID)["max_output_tokens"]
    spec_request = _spec_request(2, output_cap)
    snapshot = freeze_judge_provider_snapshot(model_resource_id=MODEL_ID, resources=resources)
    spec = build_judge_spec(**{**spec_request, "model": snapshot.model}, mode="single")
    # Store the synthetic evidence under its eventual calibration owner, so the
    # compiler's namespace normalization cannot create a dangling event reference.
    request_key = "bounded-candidate-calibration-" + uuid4().hex
    namespace = "calibration:" + calibration_execution_id(request_key)
    raw = _observation(store, namespace, CORRECT)
    sample = candidate_sample(CASE_ID, "clear_pass", CORRECT, observation=raw,
        rubric_id=spec.rubric_id, rubric_version=spec.rubric_version, model=spec.model,
        judge_spec_sha256=spec.spec_sha256, labelling_notes="Synthetic code fixture; unreviewed")
    calibration = build_calibration_set("bounded-code-review", "candidate-1",
        rubric_id=spec.rubric_id, rubric_version=spec.rubric_version, model=spec.model,
        samples=[sample], judge_spec_sha256=spec.spec_sha256,
        config={"judge_spec": spec.model_dump(mode="json")})
    service = JudgeCalibrationService(store, resources, scoring_jobs=_scoring(store))
    version = service.import_version(CalibrationImport(calibration=calibration))
    request = CalibrationRunRequest(request_key=request_key,
        spec_request=spec_request, authorisation=_authorisation(2, output_cap))
    preview = service.preflight(version.reference, request)
    _require(preview["max_calls"] == 2 and preview["sample_count"] == 1, "calibration_plan_expanded")
    request = request.model_copy(update={"expected_preflight_sha256": preview["preflight_sha256"]})
    execution = service.submit(version.reference, request)
    _require(len(execution.child_job_ids) == 1, "calibration_child_count")
    store, scoring, saved = _execute(path, execution.child_job_ids[0], 2)
    diagnostics = _job_diagnostics(saved)
    service = JudgeCalibrationService(store, SQLiteResourceStore(path), scoring_jobs=scoring)
    report = service.publish_report(execution.execution_id)
    diagnostics.update(_calibration_diagnostics(report.report, spec.criteria))
    _require(report.report.call_count == 2 and report.report.human_reviewed_count == 0
             and report.report.candidate_only_count == 1 and not report.report.gate_eligible
             and not report.report.qualified and report.report.experimental
             and "calibration is not human-reviewed at the required level" in report.report.reasons
             and bool(report.report.not_run),
             "calibration_not_honest", diagnostics)
    _require(report.report.coverage["ledger"]["complete"], "calibration_ledger_incomplete", diagnostics)
    grades = report.report.calls
    _require(len(grades) == 2 and {grade.kind for grade in grades} == {"single", "repeat"}
             and all(grade.sample_id == CASE_ID and grade.status == "ok"
                     and grade.outcome == "succeeded"
                     and set(grade.criteria) == set(spec.criteria)
                     and all(type(value) is bool for value in grade.criteria.values()) for grade in grades),
             "calibration_call_evidence_invalid", diagnostics)
    # Lifecycle acceptance requires truthful repeat metrics, even when this
    # synthetic witness's model judgements disagree or reject the code.
    by_kind = {grade.kind: grade for grade in grades}
    labels = [{key: by_kind[kind].criteria[key] for key in spec.criteria}
              for kind in ("single", "repeat")]
    stable = labels[0] == labels[1]
    expected_stability = {"measured": True, "samples": 1, "stable": int(stable),
                          "rate": float(stable), "unstable_samples": [] if stable else [CASE_ID]}
    _require(report.report.repeat_stability == expected_stability,
             "calibration_repeat_metric_mismatch", diagnostics)
    instability_reason = any(reason.startswith("repeat stability ") for reason in report.report.reasons)
    _require(instability_reason is (not stable), "calibration_gate_reason_mismatch", diagnostics)
    repeated = service.publish_report(execution.execution_id)
    _require(repeated == report and service.get_report(report.report_id) == report,
             "calibration_report_changed", diagnostics)
    _require(service.submit(version.reference, request) == execution, "calibration_replay_changed", diagnostics)
    _require(len(store.invocations.list_for_job(saved["job_id"])) == 2, "calibration_rebilled", diagnostics)
    return {"ok": True, "execution_id":execution.execution_id,
            "billed_calls": 2, "human_reviewed_count": 0, "candidate_only_count": 1,
            "gate_eligible": False, "qualified": False, "qualification": "blocked_missing_human_review",
            "report_immutable": True, "ledger_complete": True,
            "criterion_labels": labels, "repeat_stable": stable,
            "instability_gate_reason_present": instability_reason,
            "repeat_stability": {key: report.report.repeat_stability[key]
                                 for key in ("measured", "samples", "stable", "rate")},
            "model_quality_passed": all(value for grade in labels for value in grade.values()),
            "diagnostics": diagnostics}
