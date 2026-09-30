"""Authoritative, read-only calibration reconstruction from frozen inputs and calls.

Computed Job results, qualification flags and registries are projections, never
evidence. Publication holds the same outer guard as reconstruction, then performs
the repository's atomic report/qualification insert. No provider is constructed.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import math
from typing import Any

from motte_contracts.identity import canonical_sha256
from motte_eval.calibration import (
    CalibrationCall, build_calibration_report, pairwise_calibration_statistics,
    qualification_record, qualify_judge,
)
from motte_eval.calibration_records import (
    CalibrationAllowance, CalibrationExecution, CalibrationQualificationSource,
    CalibrationReportRecord, PairwiseCalibrationCall, PairwiseLabel, QualificationBinding,
)
from motte_eval.judge import (
    JudgeAuthorisation, JudgeCandidateInput, JudgeCandidateRef, JudgeInputBundle,
    JudgePairwiseInput, build_pairwise_input, parse_judge_output, parse_pairwise_output,
    scan_candidate_content,
)
from motte_eval.rubrics import Rubric, get_rubric, policy_for
from motte_storage.calibrations import (
    CalibrationConflict, CalibrationCorrupt, calibration_publication_guard,
)
from motte_storage.scoring_jobs import scoring_jobs_for

from .judge_calibrations import _single_observation
from .scoring_jobs import (
    CalibrationPlanEntry, JudgeProviderSnapshot, ScoringJobRequest, ScoringJobService,
)

TERMINAL = frozenset({"completed", "failed", "cancelled", "indeterminate"})
FROZEN_JOB_FIELDS = (
    "job_id", "request_key", "fingerprint", "owner", "owner_kind", "owner_ref", "run_id",
    "source_pass_id", "reserved_pass_id", "mode", "publish_policy", "repeats",
    "presentation_orders", "judge_spec", "judge_spec_sha256", "budget", "authorisation",
    "calibration_version", "inputs", "plans", "allowance", "input_digests", "price_table",
    "provider_snapshot", "price_coverage", "purpose",
)


def _corrupt(message: str) -> CalibrationCorrupt:
    return CalibrationCorrupt("CALIBRATION_SOURCE_CORRUPT: " + message)


def _load_execution(store, execution_id):
    try:
        execution = store.calibrations.get_execution(execution_id)
        if execution is None:
            raise _corrupt("declared execution is missing")
        return CalibrationExecution.model_validate(execution.model_dump(mode="json"))
    except (ValueError, TypeError, KeyError) as error:
        raise _corrupt("execution/version/review/child source is missing or inconsistent") from error


def _expected_children(store, execution, jobs):
    """Recompile only frozen version/spec/provider data, never mutable resources.

    Child widths are part of the frozen partition and may be stricter than 32.
    Every other plan field is independently derived in canonical sample order.
    """
    version, spec = execution.version, execution.spec
    # ID/version labels cannot authorize changed parser/prompt semantics. Rehash
    # the actual registered content, including unchecked in-process model edits,
    # before the compiler or parser can read it. The source binding derives this
    # same pinned digest from the revalidated execution spec.
    rubric = Rubric.model_validate(get_rubric(spec.rubric_id, spec.rubric_version).model_dump(mode="json"))
    if (rubric.rubric_id, rubric.version, rubric.content_sha256) != (
        spec.rubric_id, spec.rubric_version, spec.rubric_sha256,
    ):
        raise _corrupt("pinned rubric no longer matches the rubric definition")
    snapshot = JudgeProviderSnapshot.model_validate(execution.provider_snapshot)
    if execution.policy != policy_for(spec.rubric_id, spec.rubric_version):
        raise _corrupt("pinned policy no longer matches the policy definition")
    intents, pairs, observations = [], {}, {}
    for sample in sorted(version.calibration.samples, key=lambda item: item.sample_id):
        sample_id = sample.sample_id
        order = []
        if spec.mode == "pairwise":
            sides = [JudgeCandidateInput(
                candidate=JudgeCandidateRef(candidate_id=side.candidate_id, owner_kind="calibration",
                                            calibration_job_id=execution.execution_id, sample_id=sample_id),
                content=side.content, evidence_allowlist=list(side.evidence_allowlist),
                injection_flags=scan_candidate_content(side.content),
                calibration_version=version.calibration.version,
            ) for side in sorted(version.pairs[sample_id].candidates, key=lambda side: side.candidate_id)]
            pairs[sample_id] = build_pairwise_input(task_ref=sample_id, candidate_a=sides[0], candidate_b=sides[1])
            order = list(pairs[sample_id].presentation_order)
        else:
            observations[sample_id] = _single_observation(sample, execution.execution_id)
        for kind, repeat, presentation in [
            ("single", 0, order), ("repeat", 1, order),
            *([("order_forward", 0, order), ("order_reverse", 0, list(reversed(order)))]
              if spec.mode == "pairwise" else []),
        ]:
            intents.append(CalibrationPlanEntry(sample_id=sample_id, call_kind=kind,
                                                repeat_index=repeat, candidate_order=presentation))
    if len(intents) != len(execution.plan):
        raise _corrupt("execution plan does not cover the required sample calls")
    auth = JudgeAuthorisation.model_validate(jobs[0].get("authorisation"))
    # No jobs repository, provider or mutable resource is used by compile_record.
    compiler = ScoringJobService(store, jobs=object(), clock=lambda: execution.recorded_at)
    cursor, expected = 0, []
    for ordinal, (job_id, job) in enumerate(zip(execution.child_job_ids, jobs, strict=True), 1):
        size = sum(plan.get("child_job_id") == job_id for plan in execution.plan)
        if not 1 <= size <= 32:
            raise _corrupt("invalid bounded child partition")
        segment = execution.plan[cursor:cursor + size]
        if any(plan.get("child_job_id") != job_id for plan in segment):
            raise _corrupt("child plan partition is not ordered and contiguous")
        entries = intents[cursor:cursor + size]
        cursor += size
        sample_ids = dict.fromkeys(entry.sample_id for entry in entries)
        identity = canonical_sha256({"execution_id": execution.execution_id, "child": ordinal})[7:]
        request = ScoringJobRequest(
            request_key=f"{execution.execution_id}/child/{ordinal:04d}", judge_spec=spec, mode=spec.mode,
            calibration_job_id=execution.execution_id, sample_ids={key: key for key in sample_ids},
            observations={key: observations[key] for key in sample_ids} if observations else {},
            pairwise_pairs=[pairs[key] for key in sample_ids] if pairs else [],
            calibration_plan=entries, authorisation=auth, publish_policy="allow_non_scored",
            provider_snapshot=snapshot, price_table=snapshot.price_table,
        )
        record = compiler.compile_record(request, job_id="caljob-" + identity,
                                         reserved_pass_id="calpass-" + identity)
        if record["job_id"] != job_id or record["plans"] != [
            {key: value for key, value in plan.items() if key != "child_job_id"} for plan in segment
        ]:
            raise _corrupt("execution plan differs from its frozen source inputs")
        expected.append(record)
    allowance = CalibrationAllowance.from_plans([plan for row in expected for plan in row["plans"]])
    total = allowance.max_prompt_tokens + allowance.max_completion_tokens
    budget = spec.budget
    if (not auth.authorised or allowance.max_calls > min(auth.max_calls, budget.max_calls)
            or (auth.max_total_tokens is not None and total > auth.max_total_tokens)
            or (budget.max_total_tokens and total > budget.max_total_tokens)
            or any(getattr(budget, key) and getattr(allowance, key) > getattr(budget, key)
                   for key in ("max_prompt_tokens", "max_completion_tokens"))
            or any(cap is not None and (allowance.max_cost_usd is None or allowance.max_cost_usd > cap)
                   for cap in (auth.hard_cost_cap_usd, budget.hard_cost_cap_usd))):
        raise _corrupt("aggregate frozen reservations exceed authorization/spec budgets")
    return compiler, expected, allowance


def _money(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _call_evidence(compiler, expected_job, plan, call, invocation, *, ordinal):
    """Validate both independent persisted sides of a call before parsing its bytes."""
    reasons = []
    expected_invocation = compiler._invocation_record(expected_job, plan)
    if call is None or invocation is None:
        return ["planned call or invocation is missing"]
    for field in ("call_id", "case_id", "mode", "input_sha256", "reservation"):
        if call.get(field) != plan[field]:
            reasons.append("call " + field + " mismatch")
    for field in ("candidate_ids", "presentation_order"):
        if (call.get(field) or []) != (plan.get(field) or []):
            reasons.append("call " + field + " mismatch")
    if call.get("repeat_index") != plan["repeat_index"] or call.get("ordinal") != ordinal:
        reasons.append("call repeat/ordinal mismatch")
    if call.get("invocation_id") != expected_invocation["id"]:
        reasons.append("call invocation identity mismatch")
    for field in ("id", "run_id", "case_id", "kind", "purpose", "job_id", "model", "request_summary"):
        if invocation.get(field) != expected_invocation[field]:
            reasons.append("invocation " + field + " mismatch")
    for field, value in expected_invocation["owner"].items():
        if (invocation.get("owner") or {}).get(field) != value:
            reasons.append("invocation owner mismatch")
    if (invocation.get("owner") or {}).get("run_id") != expected_invocation["run_id"]:
        reasons.append("invocation owner namespace mismatch")
    if invocation.get("scoring_pass_id") not in (None, expected_job["reserved_pass_id"]):
        reasons.append("invocation belongs to another scoring pass")
    if (call.get("status") != "settled" or invocation.get("status") != "settled"
            or call.get("outcome") != "succeeded" or invocation.get("outcome") != "succeeded"):
        reasons.append("call is not a completed successful invocation")
    result = invocation.get("result_summary") or {}
    for field in ("raw_response", "cost_usd", "usage", "price_table_version"):
        if call.get(field) != result.get(field):
            reasons.append("settled response " + field + " mismatch")
    raw = result.get("raw_response") or {}
    # provider_config maps frozen adapter_id to provider kind, and the existing
    # BaseHTTPProvider envelope emits that kind exactly. identity_aliases concern
    # reported *model* identities; they never alias the response's provider kind.
    expected_provider = expected_job["provider_snapshot"].get("adapter_id")
    actual_provider = raw.get("provider")
    if (not isinstance(expected_provider, str) or not expected_provider.strip()
            or not isinstance(actual_provider, str) or not actual_provider.strip()
            or actual_provider != expected_provider):
        reasons.append("response provider differs from the frozen adapter identity")
    if (not isinstance(raw.get("content"), str) or not isinstance(raw.get("response_id"), str)
            or not raw.get("response_id") or result.get("response_id") != raw.get("response_id")
            or result.get("provider") != raw.get("provider")
            or result.get("model") != expected_job["judge_spec"]["model"]):
        reasons.append("raw response identity is missing or inconsistent")
    # report_only permits execution, never qualification. Recompute identity
    # solely from persisted response evidence and the immutable versioned map.
    from motte_provider.identity import assess_identity, validate_identity_config

    snapshot = expected_job["provider_snapshot"]
    transport = snapshot.get("transport") or {}
    if (transport.get("follow_redirects") is not False
            or type(transport.get("max_retries")) is not int
            or transport["max_retries"] != 0):
        # Historical attempts=1 cannot prove that urllib sent only one POST.
        # Preserve old records, but never upgrade absent/coerced proof.
        reasons.append("frozen transport does not prove one HTTP send per Judge call")
    try:
        policy, aliases, version = validate_identity_config(
            snapshot.get("identity_policy") or "report_only",
            snapshot.get("identity_aliases"), snapshot.get("identity_alias_version"),
        )
        resolved, decision, evidence, allowed = assess_identity(
            expected_job["judge_spec"]["model"], raw.get("reported_model"),
            policy=policy, aliases=aliases, alias_version=version,
        )
        identity = {
            "requested_model": expected_job["judge_spec"]["model"],
            "reported_model": raw.get("reported_model"),
            "resolved_model_identity": resolved, "identity_evidence": evidence,
            "identity_policy": policy, "identity_policy_result": decision, "policy_passed": allowed,
        }
        if (decision not in {"exact_match", "alias_match"}
                or any(raw.get(key) != value or result.get(key) != value
                       for key, value in identity.items())):
            reasons.append("actual response model identity is missing, mismatched or inconsistent")
    except (ValueError, TypeError):
        reasons.append("frozen model identity policy or alias mapping is invalid")
    if result.get("attempts") != 1 or result.get("unsafe_retry_observed") is not False:
        reasons.append("response does not prove exactly one provider attempt")
    usage = result.get("usage") or {}
    reservation = plan["reservation"]
    for key in ("prompt_tokens", "completion_tokens"):
        amount = usage.get(key)
        if type(amount) is not int or amount < 0 or amount > reservation[key]:
            reasons.append("actual " + key + " missing or exceeds reservation")
    cost = result.get("cost_usd")
    if cost is not None and (not _money(cost) or (reservation["cost_usd"] is not None
                                                and cost > reservation["cost_usd"] + 1e-8)):
        reasons.append("actual cost is invalid or exceeds reservation")
    if cost is not None and result.get("price_table_version") != expected_job["budget"]["price_table_version"]:
        reasons.append("actual cost price version differs from the frozen price")
    return reasons


def reconstruct_calibration_report(store, execution_id: str) -> CalibrationReportRecord:
    """Rebuild from frozen sources and raw Invocation ledger, with zero external calls."""
    execution = _load_execution(store, execution_id)
    repository = getattr(store, "scoring_jobs", None) or scoring_jobs_for(store)
    jobs = [repository.get(job_id) for job_id in execution.child_job_ids]
    if any(job is None for job in jobs):
        raise _corrupt("declared child is missing")
    if any(job.get("status") not in TERMINAL for job in jobs):
        raise CalibrationConflict("calibration report requires every child to be terminal")
    try:
        compiler, expected, allowance = _expected_children(store, execution, jobs)
    except (ValueError, KeyError, TypeError) as error:
        raise _corrupt("frozen child plan/authorization chain cannot be reconstructed") from error
    reasons = []
    if execution.allowance is None:
        reasons.append("aggregate allowance is missing authorization evidence")
    elif execution.allowance != allowance:
        reasons.append("aggregate allowance differs from actual frozen child reservations")
    # Check both ownership indexes. Neither an undeclared child nor a wrong
    # namespace can hide an extra call from the other side of the source chain.
    namespace = "calibration:" + execution.execution_id
    execution_jobs = repository.list_for_run(namespace)
    if Counter(job.get("job_id") for job in execution_jobs) != Counter(execution.child_job_ids):
        reasons.append("execution child multiset differs from the authorized group")
    child_invocations = {
        job["job_id"]: store.invocations.list_for_job(job["job_id"])
        for job in [*execution_jobs, *jobs]
    }
    all_invocations = store.invocations.list_for_run(namespace)
    seen = {row.get("id") for row in all_invocations}
    for rows in child_invocations.values():
        for row in rows:
            if row.get("id") not in seen:
                all_invocations.append(row)
                seen.add(row.get("id"))
            elif row not in all_invocations:
                reasons.append("invocation ownership views disagree")
    expected_invocation_ids = {
        compiler._invocation_record(job, plan)["id"] for job in expected for plan in job["plans"]
    }
    if Counter(row.get("id") for row in all_invocations) != Counter(expected_invocation_ids):
        reasons.append("execution invocation multiset differs from the complete frozen plan")
    inv_counts = Counter(row.get("id") for row in all_invocations)
    all_calls = [call for job in execution_jobs for call in job.get("calls", [])]
    call_counts = Counter(call.get("call_id") for call in all_calls)
    used_invocations = Counter(call.get("invocation_id") for call in all_calls)
    response_counts = Counter(
        (row.get("result_summary") or {}).get("response_id") for row in all_invocations
        if (row.get("result_summary") or {}).get("response_id")
    )
    calls, pairwise_calls, parsed_by_call, observed, ledger_jobs = [], [], {}, {}, []
    for job, expected_job in zip(jobs, expected, strict=True):
        job_errors = []
        for field in FROZEN_JOB_FIELDS:
            if job.get(field) != expected_job[field]:
                job_errors.append("frozen child " + field + " mismatch")
        if job.get("status") != "completed" or (job.get("cancellation") or {}).get("requested"):
            job_errors.append("child is not an uncancelled completed execution")
        actual = job.get("calls") or []
        planned_ids = [plan["call_id"] for plan in expected_job["plans"]]
        if Counter(call.get("call_id") for call in actual) != Counter(planned_ids):
            job_errors.append("actual call multiset differs from the complete frozen plan")
        invocations = child_invocations[job["job_id"]]
        expected_invocation_ids = {compiler._invocation_record(expected_job, plan)["id"]
                                   for plan in expected_job["plans"]}
        if Counter(row.get("id") for row in invocations) != Counter(expected_invocation_ids):
            job_errors.append("actual invocation multiset differs from the complete frozen plan")
        reasons.extend(job["job_id"] + ": " + reason for reason in job_errors)
        for ordinal, plan in enumerate(expected_job["plans"], 1):
            matching = [row for row in actual if row.get("call_id") == plan["call_id"]]
            actual_call = matching[0] if len(matching) == 1 else None
            invocation_id = compiler._invocation_record(expected_job, plan)["id"]
            matching_inv = [row for row in invocations if row.get("id") == invocation_id]
            invocation = matching_inv[0] if len(matching_inv) == 1 else None
            try:
                errors = _call_evidence(compiler, expected_job, plan, actual_call, invocation, ordinal=ordinal)
            except (ValueError, TypeError, KeyError, AttributeError):
                errors = ["invalid call or invocation envelope"]
            if (call_counts[plan["call_id"]] != 1 or inv_counts[invocation_id] != 1
                    or used_invocations[invocation_id] != 1):
                errors.append("call/invocation references must be unique and unreused")
            result = (invocation or {}).get("result_summary") or {}
            if result.get("response_id") and response_counts[result["response_id"]] != 1:
                errors.append("provider response identity was reused")
            reasons.extend(plan["call_id"] + ": " + error for error in errors)
            raw = result.get("raw_response") or {}
            cost = result.get("cost_usd")
            call_data = {
                "call_id": plan["call_id"], "sample_id": plan["sample_id"], "kind": plan["call_kind"],
                "judge_job_id": job["job_id"], "invocation_id": invocation_id,
                "outcome": "succeeded" if not errors else (
                    "failed" if (actual_call or {}).get("outcome") == "failed" else "indeterminate"),
                "cost_usd": cost if _money(cost) else None,
                "price_table_version": result.get("price_table_version"),
                "presentation_order": list(plan.get("presentation_order") or []),
            }
            parsed = None
            if not errors:
                if execution.spec.mode == "pairwise":
                    parsed = parse_pairwise_output(execution.spec, raw["content"],
                                                  JudgePairwiseInput.model_validate(plan["pair"]),
                                                  response_ref=raw["response_id"])
                else:
                    parsed = parse_judge_output(execution.spec, raw["content"],
                                               JudgeInputBundle.model_validate(expected_job["inputs"][plan["case_id"]]),
                                               response_ref=raw["response_id"])
                parsed_by_call[plan["call_id"]] = parsed
                call_data["status"] = parsed.status
                if plan["call_kind"] == "single":
                    observed[plan["sample_id"]] = parsed
            if execution.spec.mode == "pairwise":
                preferences = {
                    item.criterion_id: (PairwiseLabel(kind="tie") if item.preference == "tie"
                                        else PairwiseLabel(kind="candidate", candidate_id=item.preferred_candidate_id))
                    for item in (parsed.judgements if parsed else []) if item.outcome == "scored"
                }
                winner = None
                if parsed is not None and parsed.status == "ok" and set(preferences) == set(execution.spec.criteria):
                    winner = PairwiseLabel(kind="tie") if parsed.winner_position == "tie" else (
                        PairwiseLabel(kind="candidate", candidate_id=parsed.winner_candidate_id)
                    )
                call_data["winner_candidate_id"] = winner.candidate_id if winner else None
                call = CalibrationCall(**call_data)
                pairwise_calls.append(PairwiseCalibrationCall(call=call, preferences=preferences, winner=winner))
            else:
                call_data["criteria"] = {item.criterion_id: item.passed for item in (
                    parsed.judgements if parsed else []) if item.outcome == "scored" and item.passed is not None}
                call = CalibrationCall(**call_data)
            calls.append(call)
        ledger_jobs.append({
            "frozen": {field: job.get(field) for field in FROZEN_JOB_FIELDS},
            "status": job["status"], "cancellation": job.get("cancellation"),
            "calls": deepcopy(actual), "invocations": sorted(invocations, key=lambda row: row["id"]),
        })
    body = build_calibration_report(execution.version.calibration, observed=observed, calls=calls,
                                    policy=execution.policy, generated_at=None)
    confusions = []
    if execution.spec.mode == "pairwise":
        statistics = pairwise_calibration_statistics(execution.version, parsed_by_call, pairwise_calls)
        confusions = statistics.pop("pairwise_confusion")
        body = qualify_judge(body.model_copy(update=statistics))
    known_costs = [call.cost_usd for call in calls if call.cost_usd is not None]
    cost_known = len(known_costs) == len(calls) and bool(calls) and not reasons
    body = body.model_copy(update={"cost": {
        "known": cost_known, "total_usd": round(sum(known_costs), 8) if cost_known else None,
        "known_subtotal_usd": round(sum(known_costs), 8) if known_costs else None,
        "known_subtotal_scope": "matched_planned_calls",
        "priced_calls": len(known_costs), "planned_calls": len(calls),
        "price_table_versions": sorted({call.price_table_version for call in calls if call.price_table_version}),
    }})
    reasons = sorted(set(reasons))
    body = body.model_copy(update={"coverage": {**body.coverage, "ledger": {
        "complete": not reasons, "planned_calls": len(execution.plan),
        "observed_calls": len(all_calls), "observed_invocations": len(all_invocations),
        "allowance": execution.allowance.model_dump(mode="json") if execution.allowance else None,
    }}})
    if reasons:
        body = body.model_copy(update={"qualified": False, "experimental": True, "gate_eligible": False,
                                      "reasons": [*body.reasons, *("ledger: " + reason for reason in reasons)]})
    return CalibrationReportRecord.seal({
        "execution_id": execution.execution_id, "source": execution.source_binding,
        "ledger_sha256": canonical_sha256({
            "execution": execution.identity_payload(), "children": ledger_jobs,
            "execution_invocations": sorted(all_invocations, key=lambda row: row["id"]),
            "undeclared_children": [
                {**{field: job.get(field) for field in FROZEN_JOB_FIELDS},
                 "status": job.get("status"), "calls": job.get("calls")}
                for job in sorted(execution_jobs, key=lambda job: job["job_id"])
                if job["job_id"] not in execution.child_job_ids
            ],
        }),
        "report": body, "pairwise_confusion": confusions, "pairwise_calls": pairwise_calls,
        "recorded_at": execution.recorded_at,
    })


def qualification_source(report: CalibrationReportRecord, *, recorded_at: str):
    """Only a completely reconstructed eligible report may publish qualification."""
    if not report.report.gate_eligible:
        return None
    return CalibrationQualificationSource.seal({
        "qualification": qualification_record(report.report, evaluated_at=recorded_at),
        "binding": {**report.source.model_dump(mode="json"), "report_id": report.report_id,
                    "report_sha256": report.content_sha256, "qualification_id": "pending"},
        "recorded_at": recorded_at,
    })


def verify_qualification_source(store, binding: QualificationBinding) -> dict:
    """Fail closed on missing/corrupt evidence; never consult a registry or latest model."""
    qualification_id = getattr(binding, "qualification_id", None)
    try:
        binding = QualificationBinding.model_validate(
            binding.model_dump(mode="json") if hasattr(binding, "model_dump") else binding,
        )
        qualification_id = binding.qualification_id
        with calibration_publication_guard(store):
            source = store.calibrations.get_qualification(binding.qualification_id)
            if source is None or source.binding != binding:
                raise _corrupt("qualification source/binding is missing or mismatched")
            stored = store.calibrations.get_report(binding.report_id)
            if stored is None:
                raise _corrupt("qualification report is missing")
            rebuilt = reconstruct_calibration_report(store, stored.execution_id)
            expected = qualification_source(rebuilt, recorded_at=source.recorded_at)
            if (rebuilt.content_sha256 != stored.content_sha256 or expected is None
                    or expected.content_sha256 != source.content_sha256 or expected.binding != binding):
                raise _corrupt("qualification differs from reconstructed source evidence")
        return {"gate_eligible": True, "experimental": False, "reason": "verified_calibration_source",
                "qualification_id": qualification_id}
    except Exception as error:  # storage/validation failure must never acquire qualification
        return {"gate_eligible": False, "experimental": True,
                "reason": "calibration_source_unverifiable: " + type(error).__name__,
                "qualification_id": qualification_id}
