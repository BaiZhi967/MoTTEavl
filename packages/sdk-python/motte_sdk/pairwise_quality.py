"""Read-only subject pairwise quality from fixed plans and raw Invocation bytes.

Preferences never become Boolean accuracy. This producer uses exact rational
credits and gives each planned pair equal weight, irrespective of repeats. No
provider, mutable model resource, current Pass or saved attempt is consulted.
"""
from __future__ import annotations

from collections import Counter
from fractions import Fraction
import hashlib
from typing import Any

from motte_contracts.identity import canonical_sha256
from motte_contracts.pairwise_quality import PairwiseQualitySnapshot, PairwiseRoleBinding
from motte_eval.judge import JudgeAuthorisation, JudgeInputError, JudgePairwiseInput, JudgeSpec, parse_pairwise_output
from motte_eval.comparison import pairwise_comparison_eligibility  # re-export public pure helper
from motte_eval.rubrics import Rubric, get_rubric
from motte_storage.scoring_jobs import scoring_jobs_for

from .calibration_ledger import FROZEN_JOB_FIELDS, _call_evidence
from .scoring_jobs import JudgeProviderSnapshot, ScoringJobService


def _source_errors(store, source, job, compiler):
    """Validate frozen identities without re-resolving attempts or live resources."""
    from motte_eval import judge as judge_module

    errors = []
    spec = JudgeSpec.model_validate(job["judge_spec"])
    if spec.prompt_sha256 != "sha256:" + hashlib.sha256(judge_module.JUDGE_PROMPT_TEMPLATE.encode("utf-8")).hexdigest():
        errors.append("pinned_prompt_mismatch")
    snapshot = JudgeProviderSnapshot.model_validate(job["provider_snapshot"])
    rubric = Rubric.model_validate(get_rubric(spec.rubric_id, spec.rubric_version).model_dump(mode="json"))
    if rubric.content_sha256 != spec.rubric_sha256:
        errors.append("pinned_rubric_mismatch")
    compiler._verify_frozen_job_roles(job)
    if (source.get("source") != "judge" or source.get("purpose") != "judge"
            or job.get("purpose") != "judge" or job.get("mode") != "pairwise" or spec.mode != "pairwise"
            or source.get("run_id") != job["run_id"] or source.get("id") != job.get("reserved_pass_id")
            or source.get("job_id") != job.get("job_id")):
        errors.append("subject_pass_job_identity_mismatch")
    run = store.runs.get(job["run_id"])
    if run is None or any(role["case_id"] not in (run.get("case_ids") or []) for role in job["pairwise_roles"]):
        errors.append("subject_run_case_ownership_mismatch")
    if (snapshot.model != spec.model or job.get("judge_spec_sha256") != spec.spec_sha256
            or job.get("budget") != spec.budget.model_dump(mode="json")
            or job.get("price_table") != snapshot.price_table
            or job.get("calibration_version") != spec.calibration_version):
        errors.append("frozen_spec_provider_identity_mismatch")
    if (source.get("scorer_id") != "judge:" + spec.judge_profile_id
            or source.get("scorer_version") != spec.evaluator_version
            or source.get("pairwise_roles") != job["pairwise_roles"]
            or source.get("roles_sha256") != job["roles_sha256"]
            or source.get("qualification_binding") != job.get("qualification_binding")):
        errors.append("selected_pass_instrument_or_roles_mismatch")
    judge = source.get("judge") or {}
    identity = compiler._snapshot_identity(job)
    expected_judge = {
        **spec.as_summary(), "input_digests": job.get("input_digests") or {},
        "publish_policy": job["publish_policy"], "owner": job["owner"],
        "source_pass_id": job.get("source_pass_id"),
        "input_selector": spec.input_selector.model_dump(mode="json"),
        "missing_evidence_policy": spec.missing_evidence_policy,
        "provider_snapshot_sha256": identity.get("provider_snapshot_sha256"),
        "model_resource_id": identity.get("model_resource_id"),
        "provider_connection": identity.get("provider_connection"),
        "provider_adapter": identity.get("provider_adapter"), "endpoint": identity.get("provider_endpoint"),
        "calls": [{key: plan[key] for key in (
            "call_id", "case_id", "repeat_index", "presentation_order", "input_sha256",
            "role_binding", "roles_sha256",
        )} for plan in job["plans"]],
    }
    if any(judge.get(key) != value for key, value in expected_judge.items()):
        errors.append("selected_pass_frozen_judge_mismatch")
    receipt = job.get("receipt") or {}
    for key, expected in {"job_id": job["job_id"], "scoring_pass_id": source["id"],
                          "run_id": job["run_id"], "owner": job["owner"],
                          "fingerprint": job["fingerprint"], "judge_spec_sha256": spec.spec_sha256,
                          "roles_sha256": job["roles_sha256"], "mode": "pairwise"}.items():
        if receipt.get(key) != expected:
            errors.append("subject_publication_receipt_mismatch")
            break
    if job.get("status") != "completed" or (job.get("cancellation") or {}).get("requested"):
        errors.append("subject_job_not_completed")
    if compiler._freeze_plans(spec, {}, job["plans"], job["price_table"]) != job["plans"]:
        errors.append("frozen_plan_reservation_mismatch")
    allowance = compiler._allowance(job["plans"])
    auth = JudgeAuthorisation.model_validate(job["authorisation"])
    total = allowance["max_prompt_tokens"] + allowance["max_completion_tokens"]
    if (job.get("allowance") != allowance or not auth.authorised
            or allowance["max_calls"] > min(auth.max_calls, spec.budget.max_calls, 32)
            or (auth.max_total_tokens is not None and total > auth.max_total_tokens)
            or (spec.budget.max_total_tokens and total > spec.budget.max_total_tokens)
            or any(getattr(spec.budget, key) and allowance[key] > getattr(spec.budget, key)
                   for key in ("max_prompt_tokens", "max_completion_tokens"))
            or any(cap is not None and (allowance["max_cost_usd"] is None or allowance["max_cost_usd"] > cap)
                   for cap in (auth.hard_cost_cap_usd, spec.budget.hard_cost_cap_usd))):
        errors.append("frozen_plan_allowance_mismatch")
    return spec, errors


def reconstruct_pairwise_quality(store: Any, scoring_pass_id: str) -> PairwiseQualitySnapshot:
    """Reconstruct the selected Pass only; incomplete evidence stays unavailable."""
    # Local import avoids a projection/reconstruction module cycle.
    from .comparisons import _selected_pass_source

    source, lineage_error = _selected_pass_source(store, scoring_pass_id)
    selected = store.scoring_passes.get(scoring_pass_id)
    reasons = []
    fatal = []
    if lineage_error:
        fatal.append("selected_pairwise_source_unverifiable")
    if source is None:
        source = {}
    if not isinstance(selected, dict):
        selected = {}
    if selected.get("source") == "manual_revision":
        fatal.append("manual_pairwise_quality_unsupported")
    if (source.get("judge") or {}).get("mode") != "pairwise":
        fatal.append("selected_source_not_pairwise")
    repository = getattr(store, "scoring_jobs", None) or scoring_jobs_for(store)
    job = repository.get(source["job_id"]) if source.get("job_id") else None
    if not isinstance(job, dict):
        job = None
        fatal.extend(("subject_job_unavailable", "plan_digest_unavailable", "ledger_digest_unavailable"))
    plans = job.get("plans") if job is not None else None
    if not isinstance(plans, list):
        plans = []
        if job is not None:
            fatal.append("invalid_pairwise_plan")
    frozen_plans = plans
    plans = list(plans)
    # The selected Pass independently pins calls too. A deleted Job/plan cannot
    # erase known planned identities and accidentally improve coverage.
    source_calls = (source.get("judge") or {}).get("calls") or []
    if isinstance(source_calls, list):
        job_call_ids = {plan.get("call_id") for plan in plans if isinstance(plan, dict)
                        and isinstance(plan.get("call_id"), str)}
        for call in source_calls:
            if not isinstance(call, dict) or not isinstance(call.get("call_id"), str):
                fatal.append("invalid_selected_pass_call_identity")
                continue
            if call["call_id"] not in job_call_ids:
                role = call.get("role_binding") or {}
                pair_id = role.get("pair_id") if isinstance(role, dict) else None
                plans.append({**call, "pair_id": pair_id, "mode": "pairwise"})
                fatal.append("planned_call_missing_from_subject_job")
    # Plan identities, never returned/scored rows, determine the denominator.
    pair_plans = {}
    for ordinal, plan in enumerate(plans, 1):
        pair_id = plan.get("pair_id") if isinstance(plan, dict) else None
        if not isinstance(pair_id, str) or not pair_id:
            # Unknown pair identity is not one new pair per repeated call.
            # Keep only independently saved identities, never fabricate names.
            fatal.append("planned_pair_identities_unavailable")
            continue
        pair_plans.setdefault(pair_id, []).append((ordinal, plan))
    if not plans:
        fatal.append("empty_pairwise_plan")
    roles = []
    try:
        roles = [PairwiseRoleBinding.model_validate(raw) for raw in (job if job is not None else source).get("pairwise_roles") or []]
        if (len({role.pair_id for role in roles}) != len(roles)
                or len({role.case_id for role in roles}) != len(roles)
                or len({candidate for role in roles for candidate in (
                    role.challenger_candidate_id, role.reference_candidate_id)}) != 2 * len(roles)
                or len({attempt for role in roles for attempt in (
                    role.challenger_attempt_id, role.reference_attempt_id)}) != 2 * len(roles)):
            raise ValueError("reused role identities")
    except (ValueError, TypeError, KeyError):
        roles = []
        fatal.append("invalid_pairwise_roles")
    if set(pair_plans) != {role.pair_id for role in roles}:
        fatal.append("missing_pairwise_roles")
    roles = [role for role in roles if role.pair_id in pair_plans]
    if not roles:
        fatal.append("missing_pairwise_roles")
    by_pair = {role.pair_id: role for role in roles}
    roles_sha256 = canonical_sha256([role.model_dump(mode="json") for role in roles])
    plan_sha256 = canonical_sha256(frozen_plans) if job is not None else None
    compiler = ScoringJobService(store, jobs=object())
    spec = None
    if job is not None and plans and roles:
        try:
            spec, errors = _source_errors(store, source, job, compiler)
            fatal.extend(errors)
        except (ValueError, TypeError, KeyError, AttributeError, JudgeInputError) as error:
            fatal.append("frozen_subject_evidence_invalid:" + type(error).__name__)
    raw_calls = (job or {}).get("calls") or []
    raw_views = {
        "job": store.invocations.list_for_job(job["job_id"]) if job is not None else [],
        "run": store.invocations.list_for_run(source["run_id"]) if source.get("run_id") else [],
    }
    views, invalid_views = {}, {}
    for name, raw in raw_views.items():
        if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
            fatal.append("invalid_run_invocation_ledger_shape" if name == "run"
                         else "invalid_invocation_ledger_shape")
            # An unclassifiable Run-view row might belong to this selected Job.
            # Keep its bytes in the diagnostic digest and deny all pair values.
            invalid_views[name] = raw if not isinstance(raw, list) else [
                row for row in raw if not isinstance(row, dict)
            ]
        views[name] = [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []
    invocations = views["job"]
    run_invocations = [row for row in views["run"]
                       if row.get("job_id") == (job or {}).get("job_id")
                       or row.get("scoring_pass_id") == source.get("id")]
    if Counter(canonical_sha256(row) for row in invocations) != Counter(canonical_sha256(row) for row in run_invocations):
        fatal.append("invocation_ownership_views_disagree")
    ledger_sha256 = canonical_sha256({
        **({"invalid_invocation_views": invalid_views} if invalid_views else {}),
        "job": {key: job.get(key) for key in (*FROZEN_JOB_FIELDS, "pairwise_roles", "roles_sha256",
            "qualification_binding", "submission_input", "receipt", "status", "cancellation", "calls")},
        "invocations": sorted(invocations, key=canonical_sha256),
        "run_invocations": sorted(run_invocations, key=canonical_sha256),
        "selected_pass": {key: selected.get(key) for key in (
            "id", "run_id", "source", "job_id", "judge", "pairwise_roles", "roles_sha256", "manual_revision")},
        "source_pass": {key: source.get(key) for key in (
            "id", "run_id", "source", "job_id", "judge", "pairwise_roles", "roles_sha256")},
    }) if job is not None else None
    calls = raw_calls if isinstance(raw_calls, list) else []
    if (not isinstance(raw_calls, list) or any(not isinstance(row, dict)
            or not isinstance(row.get("call_id"), str) or not isinstance(row.get("invocation_id"), str)
            for row in calls)):
        fatal.append("invalid_call_ledger_shape")
        calls = [row for row in calls if isinstance(row, dict)
                 and isinstance(row.get("call_id"), str) and isinstance(row.get("invocation_id"), str)]
    if any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
           or (row.get("result_summary") is not None and not isinstance(row["result_summary"], dict))
           for row in invocations):
        fatal.append("invalid_invocation_ledger_shape")
        invocations = [row for row in invocations if isinstance(row, dict)
                       and isinstance(row.get("id"), str)
                       and (row.get("result_summary") is None or isinstance(row["result_summary"], dict))]
    call_counts = Counter(call.get("call_id") for call in calls)
    inv_counts = Counter(row.get("id") for row in invocations)
    used_invocations = Counter(call.get("invocation_id") for call in calls)
    response_counts = Counter(
        response_id for row in invocations
        if isinstance(response_id := (row.get("result_summary") or {}).get("response_id"), str)
    )
    planned_ids = Counter(plan.get("call_id") if isinstance(plan.get("call_id"), str) else None
                          for plan in plans if isinstance(plan, dict))
    if None in planned_ids or any(count != 1 for count in planned_ids.values()):
        fatal.append("duplicate_planned_call_identity")
    if set(call_counts) - set(planned_ids):
        fatal.append("unplanned_call_in_ledger")
    expected_invocations = set()
    pair_values = {}
    settled = 0
    for pair_id, entries in pair_plans.items():
        credits = []
        for ordinal, plan in entries:
            errors = []
            call_id = plan.get("call_id") if isinstance(plan, dict) else None
            try:
                expected_id = compiler._invocation_record(job, plan)["id"]
                expected_invocations.add(expected_id)
                call = next((row for row in calls if row.get("call_id") == call_id), None)
                invocation = next((row for row in invocations if row.get("id") == expected_id), None)
                errors = _call_evidence(compiler, job, plan, call, invocation, ordinal=ordinal)
                if call_counts[call_id] != 1 or inv_counts[expected_id] != 1 or used_invocations[expected_id] != 1:
                    errors.append("call/invocation references must be unique and unreused")
                if call is not None and any(call.get(key) != plan.get(key) for key in ("role_binding", "roles_sha256")):
                    errors.append("call role binding mismatch")
                result = (invocation or {}).get("result_summary") or {}
                if isinstance(result.get("response_id"), str) and response_counts[result["response_id"]] != 1:
                    errors.append("provider response identity was reused")
                if type(result.get("attempts")) is not int or result.get("attempts") != 1:
                    errors.append("provider attempt count must be exactly integer one")
                if not errors:
                    settled += 1
                    if not fatal and spec is not None:
                        raw = result["raw_response"]
                        parsed = parse_pairwise_output(spec, raw["content"], JudgePairwiseInput.model_validate(plan["pair"]),
                                                       response_ref=raw["response_id"])
                        if (parsed.status != "ok" or len(parsed.judgements) != len(spec.criteria)
                                or {item.criterion_id for item in parsed.judgements} != set(spec.criteria)
                                or any(item.outcome != "scored" for item in parsed.judgements)):
                            errors.append("non_scored_pairwise_output:" + parsed.status)
                        elif parsed.winner_position == "tie" and parsed.winner_candidate_id is None:
                            credits.append(Fraction(1, 2))
                        elif parsed.winner_candidate_id == by_pair[pair_id].challenger_candidate_id:
                            credits.append(Fraction(1))
                        elif parsed.winner_candidate_id == by_pair[pair_id].reference_candidate_id:
                            credits.append(Fraction(0))
                        else:
                            errors.append("unknown_pairwise_winner")
            except (ValueError, TypeError, KeyError, AttributeError, JudgeInputError):
                errors.append("invalid_call_or_invocation_envelope")
            reasons.extend(f"{pair_id}/{call_id}: {error}" for error in errors)
        pair_values[pair_id] = sum(credits, Fraction()) / len(entries) if len(credits) == len(entries) else None
    if set(inv_counts) - expected_invocations:
        fatal.append("unplanned_invocation_in_ledger")
    reasons = sorted(set([*fatal, *reasons]))
    # Global source failures cannot leave apparently validated pair values.
    if fatal:
        pair_values = {pair_id: None for pair_id in pair_values}
    valid = sum(value is not None for value in pair_values.values())
    value = sum(pair_values.values(), Fraction()) / len(pair_values) if pair_values and not reasons else None
    return PairwiseQualitySnapshot.seal({
        "source_pass_id": scoring_pass_id, "job_id": job["job_id"] if job is not None else None,
        "plan_sha256": plan_sha256, "ledger_sha256": ledger_sha256, "roles_sha256": roles_sha256,
        "roles": roles, "planned_pairs": len(pair_plans), "valid_pairs": valid,
        "planned_calls": len(plans) if pair_plans else 0,
        "settled_calls": min(settled, len(plans)) if pair_plans else 0,
        "pair_values": {key: float(value) if value is not None else None for key, value in pair_values.items()},
        "value": float(value) if value is not None else None,
        "coverage": valid / len(pair_plans) if pair_plans else None, "reasons": tuple(reasons),
    })
