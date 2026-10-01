"""Software-only saved subject evidence; no paid calls or human acceptance claims."""
from copy import deepcopy
from fractions import Fraction
from functools import partial
import importlib
import importlib.util
import json
from uuid import uuid4

import pytest

from apps.worker.motte_worker.runtime import WorkerLoop
from motte_contracts.evaluation import observation_evidence_hash
from motte_contracts.identity import canonical_sha256
from motte_eval.calibration import ManualRevisionRequest, build_manual_revision
from motte_provider.openai_compatible import OpenAICompatibleProvider
from motte_provider.pricing import parse_price_table
from motte_sdk.comparisons import ComparisonService
from motte_sdk.scoring_jobs import JudgeProviderSnapshot, ScoringJobService, build_judge_submission
from motte_sdk.service import RunService
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore
from tests.integration.test_judge_worker_flow import seed_resources
from tests.sdk.test_calibration_ledger import ScriptedCalibrationTransport
from tests.sdk.test_judge_calibrations import run_request
from tests.sdk.test_m6_comparison_service import append_pass, make_run, score_row
from tests.storage.conftest import isolated_pg_database  # noqa: F401


def quality_module():
    assert importlib.util.find_spec("motte_sdk.pairwise_quality") is not None, (
        "trusted pairwise quality reconstruction is missing"
    )
    return importlib.import_module("motte_sdk.pairwise_quality")


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def quality_store(request, tmp_path):
    if request.param == "postgres":
        from motte_storage.factory import create_run_store
        from motte_storage.migrations import upgrade
        dsn = request.getfixturevalue("isolated_pg_database")
        upgrade(dsn)
        reopen = partial(create_run_store, storage="postgres", dsn=dsn)
        return reopen(), reopen
    if request.param == "sqlite":
        reopen = partial(SQLiteRunStore, tmp_path / "quality.db")
        return reopen(), reopen
    store = InMemoryRunStore()
    return store, lambda: store


def completed_quality(store, credits=((1,),), *, reverse_roles=False, reverse_presentation=False,
                      non_scored=None):
    """Server-owned irregular plans are fixtures, not a new public runner feature.

    Plan shape, role/input digests, reservation and fingerprint are fixed before
    persistence. The real Worker and frozen adapter settle actual Invocation
    transactions through an in-memory transport; no network request is made.
    """
    run_id = "software-quality-" + uuid4().hex
    cases = [f"case-{index}" for index in range(len(credits))]
    make_run(store, run_id, case_ids=cases)
    refs = []
    for case_id in cases:
        attempts = []
        for index in (1, 2):
            attempt_id = f"{run_id}-{case_id}-{index}"
            row = store.attempts.begin({"id": attempt_id, "run_id": run_id,
                                       "case_id": case_id, "attempt_no": index})
            row = store.attempts.transition(attempt_id, expected_revision=row["revision"],
                                            expected_status="prepared", status="dispatching")
            observation = {
                "observation_id": "obs-" + attempt_id, "run_id": run_id, "case_id": case_id,
                "attempt_id": attempt_id, "final_output": f"Software-only candidate {index}",
                "termination": {"reason": "final_answer"}, "artifact_refs": [],
                "event_refs": [{"kind": "event", "run_id": run_id, "locator": str(index)}],
                "coverage": {"complete": True},
            }
            observation["evidence_hash"] = observation_evidence_hash(observation)
            store.attempts.complete(attempt_id, expected_revision=row["revision"],
                                    changes={"result": {"frozen_observation": observation}})
            attempts.append(attempt_id)
        refs.append({"case_id": case_id, "candidate_a_attempt_id": attempts[0],
                     "candidate_b_attempt_id": attempts[1],
                     "challenger_attempt_id": attempts[1 if reverse_roles else 0]})
    resources = InMemoryResourceStore()
    seed_resources(resources)
    specification = run_request()
    request = build_judge_submission(
        store=store, resources=resources, run_id=run_id, request_key=run_id,
        mode="pairwise", pairwise_refs=refs, spec_request=specification.spec_request,
        authorisation=specification.authorisation, price_table_version="price-1",
        publish_policy="allow_non_scored", repeats=max(map(len, credits)),
    )
    service = ScoringJobService(store)
    job = service.compile_record(request, job_id="job-" + run_id, reserved_pass_id="pass-" + run_id)
    plans = [plan for plan in job["plans"]
             if plan["repeat_index"] < len(credits[cases.index(plan["case_id"])])]
    for index, plan in enumerate(plans, 1):
        plan["call_id"] = f"call-{index}"
        if reverse_presentation:
            order = list(reversed(plan["presentation_order"]))
            plan["pair"]["presentation_order"] = order
            plan["pair"]["input_sha256"] = canonical_sha256({
                key: value for key, value in plan["pair"].items() if key != "input_sha256"
            })
            plan.update(presentation_order=order, input_sha256=plan["pair"]["input_sha256"])
    job["plans"] = service._freeze_plans(request.judge_spec, {}, plans, request.price_table)
    job["allowance"] = service._allowance(job["plans"])
    job["fingerprint"] = service._fingerprint(request, job["plans"], job["pairwise_roles"])
    responses = []
    for plan in job["plans"]:
        # Credits describe fixed candidate 1, independent of the challenged role.
        credit = credits[cases.index(plan["case_id"])][plan["repeat_index"]]
        winner_id = "attempt:" + refs[cases.index(plan["case_id"])][
            "candidate_a_attempt_id" if credit == 1 else "candidate_b_attempt_id"]
        winner = "tie" if credit == .5 else ("A" if plan["presentation_order"][0] == winner_id else "B")
        payload = {"winner": winner, "criteria": [
            {"criterion_id": key, "preference": winner, "reason": "software fixture",
             "evidence": ["event:1", "event:2"]} for key in request.judge_spec.criteria
        ]}
        if non_scored == "missing_criterion":
            payload["criteria"].pop()
        elif non_scored == "missing_evidence":
            for criterion in payload["criteria"]:
                criterion["evidence"] = []
        elif non_scored == "refused":
            payload = {"refused": True}
        responses.append("not-json" if non_scored == "malformed" else json.dumps(payload))
    snapshot = JudgeProviderSnapshot.model_validate(job["provider_snapshot"])
    transport = ScriptedCalibrationTransport(responses)
    provider = OpenAICompatibleProvider(
        transport, snapshot.model, parameters=snapshot.parameters,
        max_output_tokens=snapshot.max_output_tokens, price_table=parse_price_table(snapshot.price_table),
        identity_policy=snapshot.identity_policy or "report_only", identity_aliases=snapshot.identity_aliases,
        identity_alias_version=snapshot.identity_alias_version,
    )
    service.provider_factory = lambda frozen: provider
    service.jobs.submit(job)
    assert WorkerLoop(RunService(store), scoring_jobs=service).claim_and_execute()["status"] == "completed"
    job = service.jobs.get(job["job_id"])
    return service, job, transport


def test_equal_pair_weight_despite_unequal_repeats(quality_store):
    module = quality_module()
    store, _ = quality_store
    for credits in (((1,), (.5,), (0,)), ((1, 1, 1, 1), (0,)), ((1, 0, 0), (1, 1))):
        _, job, transport = completed_quality(store, credits)
        before = len(transport.calls)
        result = module.reconstruct_pairwise_quality(store, job["reserved_pass_id"])
        means = [sum(map(Fraction, pair)) / len(pair) for pair in credits]
        assert result.value == float(sum(means) / len(means)), result.reasons
        assert list(result.pair_values.values()) == list(map(float, means))
        assert result.planned_pairs == result.valid_pairs == len(credits)
        assert result.planned_calls == result.settled_calls == sum(map(len, credits))
        assert result.coverage == 1 and not result.reasons
        assert len(transport.calls) == before
        assert all(row.get("passed") is None for row in store.score_sets.list_for_pass(job["reserved_pass_id"]))


def test_challenger_and_presentation_orientation(quality_store):
    module = quality_module()
    store, _ = quality_store
    values = []
    for reverse_roles, reverse_presentation in ((False, False), (True, False), (False, True)):
        _, job, _ = completed_quality(store, ((1, 1, 0),), reverse_roles=reverse_roles,
                                      reverse_presentation=reverse_presentation)
        values.append(module.reconstruct_pairwise_quality(store, job["reserved_pass_id"]).value)
    assert values == [float(Fraction(2, 3)), float(Fraction(1, 3)), float(Fraction(2, 3))]


@pytest.mark.parametrize("non_scored", [None, "missing_criterion", "missing_evidence", "refused", "malformed"])
def test_tie_is_half_but_non_scored_is_missing(quality_store, non_scored):
    module = quality_module()
    store, _ = quality_store
    _, job, _ = completed_quality(store, ((.5,),), non_scored=non_scored)
    result = module.reconstruct_pairwise_quality(store, job["reserved_pass_id"])
    assert result.planned_pairs == result.planned_calls == result.settled_calls == 1
    assert result.value == (.5 if non_scored is None else None)
    assert result.valid_pairs == (1 if non_scored is None else 0)
    if non_scored:
        assert any(non_scored in reason for reason in result.reasons)


@pytest.mark.parametrize("damage", ["missing_call", "missing_invocation", "duplicate_call", "extra_invocation",
                                   "failed", "indeterminate", "wrong_order", "wrong_role", "provider",
                                   "spec", "roles", "pass_calls", "reservation", "response_reuse", "empty"])
def test_partial_plan_preserves_denominator(damage):
    module = quality_module()
    store = InMemoryRunStore()
    service, job, _ = completed_quality(store, ((1,), (.5,), (0,)))
    pass_id = job["reserved_pass_id"]
    before = ComparisonService(store).report_ref(job["run_id"], scoring_pass_id=pass_id)
    raw = service.jobs._rows[job["job_id"]]
    call = raw["calls"][0]
    invocation = store.invocations._rows[call["invocation_id"]]
    if damage == "missing_call":
        raw["calls"].pop(0)
    elif damage == "missing_invocation":
        store.invocations._rows.pop(invocation["id"])
    elif damage == "duplicate_call":
        raw["calls"].append(deepcopy(call))
    elif damage == "extra_invocation":
        extra = deepcopy(invocation)
        extra["id"] = "extra-invocation"
        store.invocations._rows[extra["id"]] = extra
    elif damage in {"failed", "indeterminate"}:
        call["outcome"] = invocation["outcome"] = damage
    elif damage == "wrong_order":
        invocation["request_summary"]["presentation_order"].reverse()
    elif damage == "wrong_role":
        call["role_binding"]["challenger_attempt_id"] = "foreign"
    elif damage == "provider":
        for raw_response in (call["raw_response"], invocation["result_summary"]["raw_response"]):
            raw_response["provider"] = "wrong-provider"
        invocation["result_summary"]["provider"] = "wrong-provider"
    elif damage == "spec":
        raw["judge_spec"]["model"] = "forged-model"
    elif damage == "roles":
        raw.pop("pairwise_roles")
    elif damage == "pass_calls":
        store.scoring_passes._passes[pass_id]["judge"]["calls"].pop()
    elif damage == "reservation":
        raw["plans"][0]["reservation"]["completion_tokens"] += 1
    elif damage == "response_reuse":
        other = raw["calls"][1]["raw_response"]["response_id"]
        call["raw_response"]["response_id"] = other
        invocation["result_summary"]["response_id"] = other
        invocation["result_summary"]["raw_response"]["response_id"] = other
    elif damage == "empty":
        raw["plans"] = []
        raw["pairwise_roles"] = []
        store.scoring_passes._passes[pass_id]["judge"]["calls"] = []
        store.scoring_passes._passes[pass_id]["pairwise_roles"] = []
    result = module.reconstruct_pairwise_quality(store, pass_id)
    assert result.value is None and result.reasons
    assert result.planned_pairs == (0 if damage == "empty" else 3)
    assert result.planned_calls == (0 if damage == "empty" else 3)
    if damage in {"missing_call", "missing_invocation", "duplicate_call", "failed", "indeterminate",
                  "wrong_order", "wrong_role", "provider"}:
        assert result.valid_pairs == 2 and result.coverage == 2 / 3
    assert ComparisonService(store).report_ref(job["run_id"], scoring_pass_id=pass_id) != before


def test_manual_and_cross_run_metric_limits(quality_store):
    module = quality_module()
    store, _ = quality_store
    service, job, _ = completed_quality(store, ((1,),))
    _, other, _ = completed_quality(store, ((0,),))
    pass_id = job["reserved_pass_id"]
    record = store.scoring_passes.get(pass_id)
    revision = build_manual_revision(record, ManualRevisionRequest(
        run_id=job["run_id"], source_pass_id=pass_id, actor="software-test", reason="software-only revision",
        expected_current_pass_id=pass_id, changes=[{
            "case_id": row["case_id"], "metric_id": row["metric_id"], "passed": True, "value": 1.0,
        } for row in record["scores"]]), revision_id="manual-" + pass_id,
        created_at=record["created_at"], current_pass_id=pass_id)
    revised = revision["pass"]
    store.scoring_passes.append(revised, revision["scores"])
    manual = module.reconstruct_pairwise_quality(store, revised["id"])
    assert manual.value is None and "manual_pairwise_quality_unsupported" in manual.reasons
    assert manual.source_pass_id == revised["id"] and manual.planned_pairs == 1
    comparisons = ComparisonService(store)
    baseline = module.reconstruct_pairwise_quality(store, pass_id)
    candidate = module.reconstruct_pairwise_quality(store, other["reserved_pass_id"])
    eligibility = module.pairwise_comparison_eligibility(baseline, candidate)
    assert eligibility["eligible"] is False
    assert eligibility["reason"] == "pairwise_baseline_comparison_unsupported"
    compared = comparisons.compare(job["run_id"], other["run_id"], allowed_factors=("model",),
                                   baseline_pass_id=pass_id, candidate_pass_id=other["reserved_pass_id"])
    assert compared.metric_eligibility["pairwise_challenger_score@1"] is False
    assert "pairwise_baseline_comparison_unsupported" in compared.metric_reasons
    assert baseline.value == 1 and candidate.value == 0
    for selected in (pass_id, revised["id"]):
        assert comparisons.case_outcomes(job["run_id"], selected) == {"case-0": None}
        for k in (1, 3):
            statistics = comparisons.paired_statistics(job["run_id"], job["run_id"], allowed_factors=(),
                baseline_pass_id=selected, candidate_pass_id=selected, k=k)
            assert not statistics["applicable"] and statistics["statistics"] is None
            assert statistics["reason"] == "pairwise_statistical_inference_unsupported"


def test_restart_and_current_drift_keep_selected_metric(quality_store, monkeypatch):
    module = quality_module()
    store, reopen = quality_store
    _, job, transport = completed_quality(store, ((1,), (.5,), (0,)))
    selected = job["reserved_pass_id"]
    before = module.reconstruct_pairwise_quality(store, selected)
    ref = ComparisonService(store).report_ref(job["run_id"], scoring_pass_id=selected)
    append_pass(store, job["run_id"], "new-" + selected, [score_row("case-0", passed=True)])
    run = store.runs.get(job["run_id"])
    store.runs.update({**run, "current_scoring_pass_id": "new-" + selected}, expected_revision=run["revision"])
    reopened = reopen()
    monkeypatch.setattr(reopened.attempts, "get", lambda *a: pytest.fail("read mutable attempt"))
    assert module.reconstruct_pairwise_quality(reopened, selected) == before
    assert ComparisonService(reopened).report_ref(job["run_id"], scoring_pass_id=selected) == ref
    assert len(transport.calls) == 3


@pytest.mark.parametrize("damage", ["call_envelope", "call_list", "invocation_envelope", "duplicate_plan",
                                   "input_bytes", "role_reuse", "job_owner", "job_status", "no_job"])
def test_invalid_source_shapes_fail_closed(damage, monkeypatch):
    module = quality_module()
    store = InMemoryRunStore()
    service, job, _ = completed_quality(store, ((1,), (0,)))
    raw = service.jobs._rows[job["job_id"]]
    if damage == "call_envelope":
        raw["calls"][0] = None
    elif damage == "call_list":
        raw["calls"] = "corrupt"
    elif damage == "invocation_envelope":
        original = store.invocations.list_for_job
        monkeypatch.setattr(store.invocations, "list_for_job", lambda key: [*original(key), None])
    elif damage == "duplicate_plan":
        raw["plans"].append(deepcopy(raw["plans"][0]))
    elif damage == "input_bytes":
        raw["plans"][0]["pair"]["candidate_a"]["content"] = "forged saved content"
    elif damage == "role_reuse":
        raw["pairwise_roles"][1] = deepcopy(raw["pairwise_roles"][0])
    elif damage == "job_owner":
        raw["owner"]["kind"] = "calibration"
    elif damage == "job_status":
        raw["status"] = "indeterminate"
    elif damage == "no_job":
        service.jobs._rows.pop(job["job_id"])
    result = module.reconstruct_pairwise_quality(store, job["reserved_pass_id"])
    assert result.value is None and result.reasons
    assert result.planned_pairs == 2


def test_legacy_roles_and_client_projections_never_supply_quality(quality_store, monkeypatch):
    module = quality_module()
    store, _ = quality_store
    service, job, transport = completed_quality(store, ((0,),))
    before = module.reconstruct_pairwise_quality(store, job["reserved_pass_id"])
    original = service.jobs.get
    changed = deepcopy(job)
    changed["result"] = {"metrics": [{"passed": True, "value": 1}], "pairwise_quality": {"value": 1}}
    monkeypatch.setattr(service.jobs, "get", lambda key: deepcopy(changed) if key == job["job_id"] else original(key))
    assert module.reconstruct_pairwise_quality(store, job["reserved_pass_id"]).value == before.value == 0
    changed.pop("pairwise_roles")
    changed.pop("roles_sha256")
    for plan in changed["plans"]:
        plan.pop("role_binding")
        plan.pop("roles_sha256")
    legacy = module.reconstruct_pairwise_quality(store, job["reserved_pass_id"])
    assert legacy.value is None and legacy.planned_pairs == 1
    assert "missing_pairwise_roles" in legacy.reasons
    assert len(transport.calls) == 1


def test_changed_raw_winner_changes_quality_and_report_identity():
    module = quality_module()
    store = InMemoryRunStore()
    service, job, _ = completed_quality(store, ((1,),))
    pass_id = job["reserved_pass_id"]
    before = ComparisonService(store).report_ref(job["run_id"], scoring_pass_id=pass_id)
    call = service.jobs._rows[job["job_id"]]["calls"][0]
    invocation = store.invocations._rows[call["invocation_id"]]
    payload = json.loads(call["raw_response"]["content"])
    payload["winner"] = "B"
    for criterion in payload["criteria"]:
        criterion["preference"] = "B"
    for raw in (call["raw_response"], invocation["result_summary"]["raw_response"]):
        raw["content"] = json.dumps(payload)
    assert module.reconstruct_pairwise_quality(store, pass_id).value == 0
    assert ComparisonService(store).report_ref(job["run_id"], scoring_pass_id=pass_id) != before


@pytest.mark.parametrize("missing", ["one_plan", "all_plans", "job"])
def test_missing_job_plan_keeps_selected_pass_planned_identities(missing):
    module = quality_module()
    store = InMemoryRunStore()
    service, job, _ = completed_quality(store, ((1,), (.5,), (0,)))
    if missing == "job":
        service.jobs._rows.pop(job["job_id"])
    elif missing == "one_plan":
        service.jobs._rows[job["job_id"]]["plans"].pop()
    else:
        service.jobs._rows[job["job_id"]]["plans"] = []
    result = module.reconstruct_pairwise_quality(store, job["reserved_pass_id"])
    assert result.planned_pairs == result.planned_calls == 3
    assert result.value is None and result.valid_pairs == 0 and result.coverage == 0
    assert "planned_call_missing_from_subject_job" in result.reasons
    if missing == "job":
        assert result.job_id is result.plan_sha256 is result.ledger_sha256 is None
        assert {"subject_job_unavailable", "plan_digest_unavailable", "ledger_digest_unavailable"} <= set(result.reasons)


def test_pairwise_quality_cannot_treat_calibration_job_as_subject():
    module = quality_module()
    store = InMemoryRunStore()
    service, job, _ = completed_quality(store)
    service.jobs._rows[job["job_id"]]["owner"] = {
        "kind": "calibration", "calibration_job_id": "software-calibration",
        "sample_ids": {"case-0": "sample-0"},
    }
    result = module.reconstruct_pairwise_quality(store, job["reserved_pass_id"])
    assert result.value is None and result.valid_pairs == 0


@pytest.mark.parametrize("field,value", [("attempts", True), ("response_id", {"invalid": "identity"})])
def test_invalid_response_identity_and_attempt_count_are_unavailable(field, value):
    module = quality_module()
    store = InMemoryRunStore()
    service, job, _ = completed_quality(store)
    call = service.jobs._rows[job["job_id"]]["calls"][0]
    invocation = store.invocations._rows[call["invocation_id"]]
    invocation["result_summary"][field] = value
    if field == "response_id":
        call["raw_response"][field] = value
        invocation["result_summary"]["raw_response"][field] = value
    result = module.reconstruct_pairwise_quality(store, job["reserved_pass_id"])
    assert result.value is None and result.valid_pairs == 0 and result.planned_pairs == 1


def test_current_prompt_definition_cannot_replace_pinned_instrument(monkeypatch):
    module = quality_module()
    from motte_eval import judge
    store = InMemoryRunStore()
    _, job, _ = completed_quality(store)
    # Equal byte length prevents a token reservation coincidence from proving
    # prompt identity; the frozen prompt digest must still be checked.
    monkeypatch.setattr(judge, "JUDGE_PROMPT_TEMPLATE", judge.JUDGE_PROMPT_TEMPLATE.replace("独立", "无效"))
    result = module.reconstruct_pairwise_quality(store, job["reserved_pass_id"])
    assert result.value is None and "pinned_prompt_mismatch" in result.reasons


def test_roleless_history_without_job_does_not_invent_pair_identities():
    module = quality_module()
    store = InMemoryRunStore()
    service, job, _ = completed_quality(store, ((1, 1, 1),))
    source = store.scoring_passes._passes[job["reserved_pass_id"]]
    source.pop("pairwise_roles")
    source.pop("roles_sha256")
    for call in source["judge"]["calls"]:
        call.pop("role_binding")
        call.pop("roles_sha256")
    service.jobs._rows.pop(job["job_id"])
    result = module.reconstruct_pairwise_quality(store, job["reserved_pass_id"])
    assert result.value is None and result.planned_pairs == 0
    assert result.pair_values == {} and result.coverage is None
    assert "planned_pair_identities_unavailable" in result.reasons


@pytest.mark.parametrize("payload", [None, [], "corrupt", 17, True])
def test_persisted_non_object_invocation_keeps_unavailable_pair_plan(tmp_path, payload):
    """Real SQLite JSON corruption stays a diagnostic result, never a read crash."""
    import sqlite3

    module = quality_module()
    database = tmp_path / "malformed-invocation.sqlite"
    store = SQLiteRunStore(database)
    _, job, transport = completed_quality(store, ((1,), (0,)))
    selected = job["reserved_pass_id"]
    comparisons = ComparisonService(store)
    before = comparisons.report_ref(job["run_id"], scoring_pass_id=selected)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE agent_invocations SET payload=? WHERE id=?",
                           (json.dumps(payload), job["calls"][0]["invocation_id"]))
    assert store.invocations.list_for_run(job["run_id"])[0] == payload
    assert len(store.invocations.list_for_job(job["job_id"])) == 1
    result = module.reconstruct_pairwise_quality(store, selected)
    assert result.value is None and result.valid_pairs == 0
    assert result.planned_pairs == result.planned_calls == 2
    assert "invalid_run_invocation_ledger_shape" in result.reasons
    assert comparisons.report_ref(job["run_id"], scoring_pass_id=selected) != before
    assert comparisons.report_snapshot(job["run_id"], scoring_pass_id=selected).pairwise_quality == result
    assert comparisons.candidate_summary(job["run_id"], scoring_pass_id=selected)["pairwise_quality"] == result.model_dump(mode="json")
    assert len(transport.calls) == 2


@pytest.mark.parametrize("view", ["list_for_job", "list_for_run"])
@pytest.mark.parametrize("payload", [None, "corrupt", {"wrong": "collection"}])
def test_non_list_invocation_view_is_unavailable(view, payload, monkeypatch):
    module = quality_module()
    store = InMemoryRunStore()
    _, job, _ = completed_quality(store, ((1,), (0,)))
    monkeypatch.setattr(store.invocations, view, lambda key: payload)
    result = module.reconstruct_pairwise_quality(store, job["reserved_pass_id"])
    assert result.value is None and result.valid_pairs == 0 and result.planned_pairs == 2
    expected = "invalid_run_invocation_ledger_shape" if view == "list_for_run" else "invalid_invocation_ledger_shape"
    assert expected in result.reasons


def test_invocation_read_operational_error_is_not_swallowed(monkeypatch):
    module = quality_module()
    store = InMemoryRunStore()
    _, job, _ = completed_quality(store)

    def unavailable(run_id):
        raise RuntimeError("software storage read failure")

    monkeypatch.setattr(store.invocations, "list_for_run", unavailable)
    with pytest.raises(RuntimeError, match="software storage read failure"):
        module.reconstruct_pairwise_quality(store, job["reserved_pass_id"])
