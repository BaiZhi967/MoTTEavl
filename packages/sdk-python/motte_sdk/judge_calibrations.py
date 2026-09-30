"""Immutable calibration lifecycle, compiled into the existing bounded Worker queue.

Import/review/preflight never construct providers. Submit atomically publishes one
execution and its bounded children. Replay reads that durable group before resource
resolution, so a changed connection or price cannot recompile or rebill a request.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Callable

from motte_contracts.evaluation import observation_evidence_hash
from motte_contracts.identity import canonical_sha256
from motte_eval.calibration_records import (
    CalibrationAllowance, CalibrationExecution, CalibrationImport, CalibrationRef,
    CalibrationRunRequest, CalibrationVersion, HumanReviewInput,
    calibration_execution_id, review_calibration_version,
)
from motte_eval.judge import (
    JudgeBudgetError, JudgeCandidateInput, JudgeCandidateRef, JudgeError, JudgeInputError,
    JudgeNotAuthorised, build_judge_spec, build_pairwise_input, parse_evidence_token, price_view,
    scan_candidate_content,
)
from motte_eval.rubrics import policy_for
from motte_storage.calibrations import CalibrationConflict

from .scoring_jobs import (
    CalibrationPlanEntry, ScoringJobRequest, ScoringJobService,
    freeze_judge_provider_snapshot,
)

__all__ = ["CalibrationRunRequest", "JudgeCalibrationService", "compile_calibration_execution"]


def _request_fingerprint(ref: CalibrationRef, request: CalibrationRunRequest) -> str:
    return canonical_sha256({"version": ref.model_dump(mode="json"),
                             "request": request.model_dump(mode="json")})


def _aggregate_allowance(
    records: list[dict], version: CalibrationVersion, request: CalibrationRunRequest,
) -> CalibrationAllowance:
    allowance = CalibrationAllowance.from_plans([plan for row in records for plan in row["plans"]])
    auth = request.authorisation
    if auth is None or not auth.authorised:
        raise JudgeNotAuthorised("calibration requires explicit aggregate authorisation")
    budget = version.spec.budget
    total = allowance.max_prompt_tokens + allowance.max_completion_tokens
    reasons = []
    if allowance.max_calls > min(auth.max_calls, budget.max_calls):
        reasons.append("call allowance exceeded")
    if auth.max_total_tokens is not None and total > auth.max_total_tokens:
        reasons.append("authorisation token allowance exceeded")
    if budget.max_total_tokens and total > budget.max_total_tokens:
        reasons.append("spec token allowance exceeded")
    for key in ("max_prompt_tokens", "max_completion_tokens"):
        ceiling = getattr(budget, key)
        if ceiling and getattr(allowance, key) > ceiling:
            reasons.append(f"spec {key} allowance exceeded")
    for cap in (auth.hard_cost_cap_usd, budget.hard_cost_cap_usd):
        if cap is not None and (allowance.max_cost_usd is None or allowance.max_cost_usd > cap):
            reasons.append("cost allowance exceeded or unknown")
    if reasons:
        raise JudgeBudgetError("calibration aggregate budget is not executable: " + "; ".join(reasons))
    return allowance


def _single_observation(sample: Any, execution_id: str) -> dict[str, Any]:
    """Freeze a calibration-owned observation without creating or borrowing a Run."""
    namespace = f"calibration:{execution_id}"
    raw = deepcopy(sample.observation or {})
    raw.update(
        schema_version=1, run_id=namespace, case_id=sample.sample_id,
        observation_id="calobs-" + canonical_sha256({
            "execution_id": execution_id, "sample_id": sample.sample_id,
        })[7:],
    )
    raw.pop("attempt_id", None)
    raw.setdefault("final_output", sample.candidate_output)
    raw.setdefault("termination", {"reason": "final_answer"})
    raw.setdefault("coverage", {"complete": True})
    raw.setdefault("event_refs", [])
    raw.setdefault("artifact_refs", [])
    raw.setdefault("tool_calls", [])
    raw.setdefault("workspace", {"before": [], "after": []})
    for name in ("event_refs", "artifact_refs"):
        raw[name] = [{**ref, "run_id": namespace} for ref in raw[name]]
    raw.pop("evidence_hash", None)
    raw["evidence_hash"] = observation_evidence_hash(raw)
    return raw


def _preflight(execution: CalibrationExecution, records: list[dict]) -> dict[str, Any]:
    # Receipt times and the optional expected hash do not enter the preflight digest.
    # IDs are derived before planning; no plan/content hash cycle exists.
    report = {
        "schema_version": 1, "execution_id": execution.execution_id,
        "version": execution.version.reference.model_dump(mode="json"),
        "judge_spec_sha256": execution.spec.spec_sha256,
        "provider_snapshot_sha256": execution.provider_snapshot["snapshot_sha256"],
        "policy": execution.policy.model_dump(mode="json"),
        "plan_sha256": execution.plan_sha256,
        "sample_count": len(execution.version.calibration.samples),
        "max_calls": len(execution.plan),
        "child_call_counts": [len(row["plans"]) for row in records],
        "allowance": execution.allowance.model_dump(mode="json") if execution.allowance else None,
        "authorisation": deepcopy(records[0]["authorisation"]),
        "mode": execution.spec.mode,
        "authorised": True, "budget_executable": True, "executed": False,
        "qualification_status": "not_run",
    }
    return {**report, "preflight_sha256": canonical_sha256(report)}


def compile_calibration_execution(
    version: CalibrationVersion, request: CalibrationRunRequest, resources: Any,
    scoring_jobs: ScoringJobService,
) -> tuple[CalibrationExecution, list[dict]]:
    """Resolve once, freeze exact child reservations, check one cap, and write nothing."""
    version = CalibrationVersion.model_validate(version.model_dump(mode="json"))
    request = CalibrationRunRequest.model_validate(request.model_dump(mode="json"))
    published = deepcopy(request.spec_request)
    snapshot = freeze_judge_provider_snapshot(
        model_resource_id=published.pop("model"), resources=resources,
        price_table_version=request.price_table_version,
    )
    price = price_view(snapshot.price_table)
    published["budget"].update(
        price_known=bool(price["known"]),
        price_table_version=price["version"] if price["known"] else None,
    )
    published["criteria"] = published.get("criteria") or None
    spec = build_judge_spec(**published, model=snapshot.model, mode=version.spec.mode)
    if spec != version.spec:
        raise JudgeInputError("calibration request must match the full pinned JudgeSpec")
    if spec.calibration_version not in (None, version.calibration.version):
        raise JudgeInputError("JudgeSpec calibration version does not match the selected version")
    samples = sorted(version.calibration.samples, key=lambda sample: sample.sample_id)
    if not samples:
        raise JudgeInputError("calibration requires a nonempty sample set")
    for sample in samples:
        if ((sample.rubric_id, sample.rubric_version, sample.model, sample.judge_spec_sha256)
                != (spec.rubric_id, spec.rubric_version, spec.model, spec.spec_sha256)
                or sample.calibration_version not in (None, version.calibration.version)):
            raise JudgeInputError("sample rubric/model/spec/version differs from the pinned calibration")
    execution_id = calibration_execution_id(request.request_key)
    intents = []
    pairs, observations = {}, {}
    for sample in samples:
        sample_id = sample.sample_id
        order = []
        if spec.mode == "pairwise":
            sides = []
            for candidate in sorted(version.pairs[sample_id].candidates,
                                    key=lambda candidate: candidate.candidate_id):
                for token in candidate.evidence_allowlist:
                    try:
                        parse_evidence_token(token, f"calibration:{execution_id}")
                    except ValueError as error:
                        raise JudgeInputError(f"invalid calibration evidence: {token}") from error
                sides.append(JudgeCandidateInput(
                    candidate=JudgeCandidateRef(
                        candidate_id=candidate.candidate_id, owner_kind="calibration",
                        calibration_job_id=execution_id, sample_id=sample_id,
                    ), content=candidate.content, evidence_allowlist=list(candidate.evidence_allowlist),
                    injection_flags=scan_candidate_content(candidate.content),
                    calibration_version=version.calibration.version,
                ))
            pairs[sample_id] = build_pairwise_input(
                task_ref=sample_id, candidate_a=sides[0], candidate_b=sides[1],
            )
            order = list(pairs[sample_id].presentation_order)
        else:
            observations[sample_id] = _single_observation(sample, execution_id)
        for kind, repeat, candidate_order in [
            ("single", 0, order), ("repeat", 1, order),
            *([("order_forward", 0, order), ("order_reverse", 0, list(reversed(order)))]
              if spec.mode == "pairwise" else []),
        ]:
            intents.append(CalibrationPlanEntry(sample_id=sample_id, call_kind=kind,
                                                repeat_index=repeat, candidate_order=candidate_order))
    # Never relax max_calls_per_job, even if a caller configures a looser policy.
    width = min(32, scoring_jobs.policy.max_calls_per_job)
    records = []
    for ordinal, start in enumerate(range(0, len(intents), width), 1):
        entries = intents[start:start + width]
        sample_ids = dict.fromkeys(entry.sample_id for entry in entries)
        child_key = f"{execution_id}/child/{ordinal:04d}"
        identity = canonical_sha256({"execution_id": execution_id, "child": ordinal})[7:]
        child = ScoringJobRequest(
            request_key=child_key, judge_spec=spec, mode=spec.mode,
            calibration_job_id=execution_id, sample_ids={key: key for key in sample_ids},
            observations={key: observations[key] for key in sample_ids} if observations else {},
            pairwise_pairs=[pairs[key] for key in sample_ids] if pairs else [],
            calibration_plan=entries, authorisation=request.authorisation,
            publish_policy="allow_non_scored", price_table=snapshot.price_table,
            provider_snapshot=snapshot,
        )
        records.append(scoring_jobs.compile_record(
            child, job_id="caljob-" + identity, reserved_pass_id="calpass-" + identity,
        ))
    allowance = _aggregate_allowance(records, version, request)
    execution = CalibrationExecution.seal({
        "request_key": request.request_key,
        "request_fingerprint": _request_fingerprint(version.reference, request),
        "version": version, "spec": spec,
        "provider_snapshot": snapshot.model_dump(mode="json"),
        "policy": policy_for(spec.rubric_id, spec.rubric_version),
        "plan": [{**plan, "child_job_id": row["job_id"]} for row in records for plan in row["plans"]],
        "child_job_ids": [row["job_id"] for row in records], "allowance": allowance,
        "recorded_at": scoring_jobs._clock(),
    })
    expected = request.expected_preflight_sha256
    if expected is not None and expected != _preflight(execution, records)["preflight_sha256"]:
        raise CalibrationConflict("calibration preflight changed; review the current frozen plan")
    return execution, records


class JudgeCalibrationService:
    def __init__(self, store: Any, resources: Any, *, scoring_jobs: ScoringJobService,
                 clock: Callable[[], str] | None = None) -> None:
        if scoring_jobs.store is not store:
            raise ValueError("calibration and scoring jobs must use the same store")
        self.store = store
        self.resources = resources
        self.scoring_jobs = scoring_jobs
        self.repository = store.calibrations
        self._clock = clock or (lambda: datetime.now(UTC).isoformat())

    def import_version(self, version: CalibrationImport | CalibrationVersion) -> CalibrationVersion:
        if not isinstance(version, CalibrationVersion):
            version = CalibrationImport.model_validate(
                version.model_dump(mode="json") if isinstance(version, CalibrationImport) else version,
            ).to_version()
        return self.repository.put_version(version)

    def review(self, ref: CalibrationRef, *, new_version: str,
               reviews: list[HumanReviewInput]) -> CalibrationVersion:
        parent = self.get_version(ref)
        child, records = review_calibration_version(
            parent, new_version=new_version, reviews=reviews, recorded_at=self._clock(),
        )
        self.repository.put_review(records, child)
        return self.get_version(child.reference)

    def get_version(self, ref: CalibrationRef) -> CalibrationVersion:
        version = self.repository.get_version(ref)
        if version is None:
            raise KeyError(ref)
        return version

    def list_versions(self, calibration_id: str) -> list[CalibrationVersion]:
        return self.repository.list_versions(calibration_id)

    def get_execution(self, execution_id: str) -> CalibrationExecution:
        result = self.repository.get_execution(execution_id)
        if result is None:
            raise KeyError(execution_id)
        return result

    def list_executions(self, calibration_id: str) -> list[CalibrationExecution]:
        return self.repository.list_executions(calibration_id)

    def preflight(self, ref: CalibrationRef, request: CalibrationRunRequest) -> dict:
        execution, records = compile_calibration_execution(
            self.get_version(ref), request, self.resources, self.scoring_jobs,
        )
        return _preflight(execution, records)

    def submit(self, ref: CalibrationRef, request: CalibrationRunRequest) -> CalibrationExecution:
        request = CalibrationRunRequest.model_validate(request.model_dump(mode="json"))
        ref = CalibrationRef.model_validate(ref.model_dump(mode="json"))
        existing = self.repository.get_execution_by_key(request.request_key)
        if existing is not None:
            if existing.request_fingerprint != _request_fingerprint(ref, request):
                raise CalibrationConflict("calibration request fingerprint conflict")
            return existing
        execution, records = compile_calibration_execution(
            self.get_version(ref), request, self.resources, self.scoring_jobs,
        )
        if self.scoring_jobs.provider_factory is None:
            raise JudgeError("no judge provider factory is configured; execution would be unexecutable")
        execution = execution.model_copy(update={"recorded_at": self._clock()})
        saved, _ = self.repository.submit_execution(execution, records)
        return saved
