"""Explicit subject roles, software-only Workers and immutable historical replay."""

from copy import deepcopy
from functools import partial
import json

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.worker.motte_worker.runtime import WorkerLoop
from motte_contracts.identity import canonical_sha256
from motte_eval.judge import JudgeInputError, judge_job_fingerprint
from motte_sdk.scoring_jobs import ScoringJobService, SubjectPairReference, build_judge_submission
from motte_sdk.service import RunService
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore
from motte_storage.scoring_jobs import ScoringJobConflict
from tests.integration.test_judge_worker_flow import (
    RecordingFactory,
    ScriptedProvider,
    seed_resources,
)
from tests.sdk.test_calibration_gate_binding import save_attempts
from tests.sdk.test_judge_calibrations import run_request
from tests.storage.conftest import isolated_pg_database  # noqa: F401


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def role_env(request, tmp_path):
    if request.param == "postgres":
        from motte_storage.factory import create_run_store
        from motte_storage.migrations import upgrade

        dsn = request.getfixturevalue("isolated_pg_database")
        upgrade(dsn)
        reopen = partial(create_run_store, storage="postgres", dsn=dsn)
        store = reopen()
    elif request.param == "sqlite":
        reopen = partial(SQLiteRunStore, tmp_path / "roles.db")
        store = reopen()
    else:
        store = InMemoryRunStore()

        def reopen():
            return store

    resources = InMemoryResourceStore()
    seed_resources(resources)
    run_id, ref = save_attempts(store, count=3)
    ref["challenger_attempt_id"] = ref["candidate_b_attempt_id"]
    specification = run_request()
    answer = json.dumps(
        {
            "winner": "B",
            "criteria": [
                {
                    "criterion_id": key,
                    "preference": "B",
                    "reason": "software-only",
                    "evidence": ["event:1", "event:2"],
                }
                for key in ("task_completion", "constraint_adherence", "evidence_grounding")
            ],
        }
    )
    provider = ScriptedProvider([answer])
    factory = RecordingFactory(provider)
    service = ScoringJobService(store, provider_factory=factory)
    body = {
        "run_id": run_id,
        "mode": "pairwise",
        "request_key": "explicit-role",
        "spec": specification.spec_request,
        "pairwise_refs": [ref],
        "authorisation": specification.authorisation.model_dump(
            mode="json", exclude={"purpose", "authorised_at"}
        ),
        "price_table_version": "price-1",
    }
    client = TestClient(create_app(store, resource_store=resources, judge_provider_factory=factory))
    return store, reopen, resources, provider, factory, service, body, client


def fixed_request(env, **changes):
    store, _, resources, _, _, _, body, _ = env
    args = {
        "store": store,
        "resources": resources,
        "run_id": body["run_id"],
        "request_key": body["request_key"],
        "mode": body["mode"],
        "spec_request": body["spec"],
        "pairwise_refs": body["pairwise_refs"],
        "authorisation": body["authorisation"],
        "price_table_version": "price-1",
    }
    args.update(changes)
    return build_judge_submission(**args)


def legacy_record(service, request):
    """Explicit pre-Task7 producer formula; bypass no public admission path."""
    request = request.model_copy(update={"pairwise_roles": None, "submission_input": None})
    inputs, plans = service._prepare_inputs(request)
    plans = service._freeze_plans(request.judge_spec, inputs, plans, request.price_table)
    for plan in plans:
        plan.pop("role_binding", None)
        plan.pop("roles_sha256", None)
    spec = request.judge_spec
    fingerprint = judge_job_fingerprint(
        owner_ref="run:" + request.run_id,
        source_pass_id=request.source_pass_id,
        observation_digests=sorted(plan["input_sha256"] for plan in plans),
        case_ids=sorted(plan["case_id"] for plan in plans),
        spec=spec,
        mode="pairwise",
        publish_policy=request.publish_policy,
        repeats=request.repeats,
        presentation_order=request.presentation_orders[0] if request.presentation_orders else None,
        calibration_version=spec.calibration_version,
        plans=plans,
    )
    if request.provider_snapshot is not None:
        fingerprint = canonical_sha256(
            {
                "job_fingerprint": fingerprint,
                "provider_snapshot_sha256": request.provider_snapshot.snapshot_sha256,
            }
        )
    record = {
        "schema_version": 1,
        "job_id": "historical-job",
        "request_key": request.request_key,
        "fingerprint": fingerprint,
        "owner": request.owner,
        "owner_kind": "subject",
        "owner_ref": "run:" + request.run_id,
        "run_id": request.run_id,
        "reserved_pass_id": "historical-pass",
        "status": "queued",
        "revision": 1,
        "mode": "pairwise",
        "publish_policy": request.publish_policy,
        "repeats": request.repeats,
        "presentation_orders": request.presentation_orders,
        "judge_spec": spec.model_dump(mode="json"),
        "judge_spec_sha256": spec.spec_sha256,
        "budget": spec.budget.model_dump(mode="json"),
        "authorisation": request.authorisation.model_dump(mode="json"),
        "source_pass_id": None,
        "provider_snapshot": request.provider_snapshot.model_dump(mode="json"),
        "price_table": request.price_table,
        "plans": plans,
        "inputs": {},
        "input_digests": {},
        "allowance": service._allowance(plans),
        "calls": [],
        "created_at": "2026-09-30T00:00:00Z",
        "cost_total_usd": 0.0,
        "calibration_version": spec.calibration_version,
        "purpose": "judge",
    }
    service.jobs.submit(record)
    return request, service.jobs.get(record["job_id"])


@pytest.mark.parametrize(
    "bad",
    ["missing", "foreign", "same", "number", "duplicate", "reversed", "second_pair", "unselected"],
)
def test_invalid_new_public_roles_refuse_before_state(role_env, bad):
    store, _, _, provider, factory, service, body, client = role_env
    body = deepcopy(body)
    ref = body["pairwise_refs"][0]
    if bad == "missing":
        ref.pop("challenger_attempt_id")
    elif bad == "foreign":
        ref["challenger_attempt_id"] = "other-attempt"
    elif bad == "number":
        ref["challenger_attempt_id"] = 12
    elif bad == "same":
        ref["candidate_b_attempt_id"] = ref["candidate_a_attempt_id"]
    elif bad == "unselected":
        ref["case_id"] = "unselected"
    else:
        second = deepcopy(ref)
        if bad == "reversed":
            second["candidate_a_attempt_id"], second["candidate_b_attempt_id"] = (
                second["candidate_b_attempt_id"],
                second["candidate_a_attempt_id"],
            )
        elif bad == "second_pair":
            second["candidate_b_attempt_id"] = body["run_id"] + "-attempt-3"
            second["challenger_attempt_id"] = second["candidate_b_attempt_id"]
        body["pairwise_refs"].append(second)
    before = (store.runs.list(), service.jobs.list_by_status())
    for path in ("/api/v1/judges/preflight", "/api/v1/judges"):
        submitted = (
            body
            if path.endswith("judges")
            else {k: v for k, v in body.items() if k != "request_key"}
        )
        response = client.post(path, json=submitted)
        assert response.status_code == 422, response.text
    assert (store.runs.list(), service.jobs.list_by_status()) == before
    assert store.invocations.list_for_run(body["run_id"]) == []
    assert provider.calls == factory.calls == []


def test_roles_cannot_follow_presentation_or_lexical_order(role_env):
    store, _, _, provider, factory, service, body, _ = role_env
    request = fixed_request(role_env)
    pair = request.pairwise_pairs[0]
    forward = service.compile_record(request, job_id="forward", reserved_pass_id="p-forward")
    reverse = service.compile_record(
        request.model_copy(update={"presentation_orders": [pair.swapped_order]}),
        job_id="reverse",
        reserved_pass_id="p-reverse",
    )
    role = forward["pairwise_roles"][0]
    assert role["challenger_attempt_id"] == body["pairwise_refs"][0]["candidate_b_attempt_id"]
    assert role["challenger_candidate_id"] == pair.candidate_b.candidate.candidate_id
    assert role["reference_candidate_id"] == pair.candidate_a.candidate.candidate_id
    assert forward["pairwise_roles"] == reverse["pairwise_roles"]
    assert (
        forward["roles_sha256"]
        == reverse["roles_sha256"]
        == canonical_sha256(forward["pairwise_roles"])
    )
    assert forward["fingerprint"] != reverse["fingerprint"]
    assert forward["plans"][0]["role_binding"] == reverse["plans"][0]["role_binding"] == role
    assert role["candidate_input_sha256"] == {
        side.candidate.candidate_id: canonical_sha256(side.model_dump(mode="json"))
        for side in (pair.candidate_a, pair.candidate_b)
    }
    flipped = deepcopy(body["pairwise_refs"])
    flipped[0]["challenger_attempt_id"] = flipped[0]["candidate_a_attempt_id"]
    other = service.compile_record(
        fixed_request(role_env, pairwise_refs=flipped),
        job_id="flipped",
        reserved_pass_id="p-flipped",
    )
    assert other["roles_sha256"] != forward["roles_sha256"]
    assert other["fingerprint"] != forward["fingerprint"]
    assert provider.calls == factory.calls == []


def test_worker_restart_keeps_roles_and_completed_replay_is_zero_call(role_env, monkeypatch):
    store, reopen, resources, provider, factory, service, body, client = role_env
    request = fixed_request(role_env)
    job = service.submit(request)
    saved = service.jobs.get(job["job_id"])
    reopened = reopen()
    monkeypatch.setattr(
        reopened.attempts, "get", lambda *a: pytest.fail("restart reread mutable attempts")
    )
    monkeypatch.setattr(
        resources.models, "get", lambda *a: pytest.fail("replay reread mutable resources")
    )
    resumed = ScoringJobService(reopened, provider_factory=factory)
    result = WorkerLoop(RunService(reopened), scoring_jobs=resumed).claim_and_execute()
    assert result["status"] == "completed"
    record = reopened.scoring_passes.get(saved["reserved_pass_id"])
    assert record["pairwise_roles"] == saved["pairwise_roles"]
    assert record["roles_sha256"] == saved["roles_sha256"]
    invocation = reopened.invocations.list_for_job(job["job_id"])[0]
    assert invocation["request_summary"]["role_binding"] == saved["pairwise_roles"][0]
    assert invocation["request_summary"]["roles_sha256"] == saved["roles_sha256"]
    assert all(
        row["passed"] is None
        for row in record["scores"]
        if row["metric_id"] == "pairwise_preference"
    )
    before = (
        resumed.jobs.get(job["job_id"]),
        reopened.scoring_passes.list_for_run(body["run_id"]),
        len(provider.calls),
    )
    client = TestClient(
        create_app(reopened, resource_store=resources, judge_provider_factory=factory)
    )
    replay = client.post("/api/v1/judges", json=body)
    assert replay.status_code == 202, replay.text
    assert replay.json()["reused"] and replay.json()["job_id"] == job["job_id"]
    assert (
        resumed.jobs.get(job["job_id"]),
        reopened.scoring_passes.list_for_run(body["run_id"]),
        len(provider.calls),
    ) == before
    assert len(provider.calls) == 1


def test_new_internal_roleless_or_forged_requests_refuse(role_env):
    _, _, _, provider, factory, service, _, _ = role_env
    request = fixed_request(role_env)
    for bad in (
        request.model_copy(update={"pairwise_roles": None}),
        request.model_copy(
            update={
                "pairwise_pairs": [
                    request.pairwise_pairs[0].model_copy(
                        update={
                            "candidate_b": request.pairwise_pairs[0].candidate_b.model_copy(
                                update={"content": "forged"}
                            )
                        }
                    )
                ]
            }
        ),
    ):
        with pytest.raises((ValueError, JudgeInputError)):
            service.submit(bad)
    assert service.jobs.list_by_status() == []
    assert provider.calls == factory.calls == []


def test_legacy_internal_replay_does_not_enrich_or_call(role_env, monkeypatch):
    _, reopen, _, provider, factory, service, _, _ = role_env
    request, saved = legacy_record(service, fixed_request(role_env))
    reopened = reopen()
    resumed = ScoringJobService(reopened, provider_factory=None)
    monkeypatch.setattr(
        reopened.attempts, "get", lambda *a: pytest.fail("legacy replay read attempts")
    )
    replay = resumed.submit(request)
    assert replay["reused"] and replay["job_id"] == saved["job_id"]
    assert resumed.jobs.get(saved["job_id"]) == saved
    body, client = role_env[6], role_env[7]
    read = client.get("/api/v1/judges/" + saved["job_id"])
    assert read.status_code == 200, read.text
    assert "pairwise_roles" not in read.json() and "roles_sha256" not in read.json()
    historical_body = deepcopy(body)
    historical_body["pairwise_refs"][0].pop("challenger_attempt_id")
    response = client.post("/api/v1/judges", json=historical_body)
    assert response.status_code == 422, response.text
    assert resumed.jobs.get(saved["job_id"]) == saved
    with pytest.raises(ScoringJobConflict):
        resumed.submit(request.model_copy(update={"repeats": 2}))
    with pytest.raises(ScoringJobConflict):
        resumed.submit(fixed_request(role_env))
    assert provider.calls == factory.calls == []


def test_queued_roleless_worker_fails_deterministically_without_dispatch(role_env):
    store, reopen, _, provider, factory, service, _, _ = role_env
    _, saved = legacy_record(service, fixed_request(role_env))
    reopened = reopen()
    resumed = ScoringJobService(reopened, provider_factory=factory)
    result = WorkerLoop(RunService(reopened), scoring_jobs=resumed).claim_and_execute()
    assert result["status"] == "failed"
    assert result["failure"]["code"] == "PAIRWISE_ROLES_REQUIRED"
    assert result["plans"] == saved["plans"] and result["calls"] == []
    assert reopened.scoring_passes.get(saved["reserved_pass_id"]) is None
    assert provider.calls == factory.calls == []


def test_authoritative_api_schema_requires_explicit_role():
    from apps.api.app.schemas import JudgeSubmitRequest

    schema = JudgeSubmitRequest.model_json_schema()["$defs"]["SubjectPairReference"]
    assert "challenger_attempt_id" in schema["required"]
    ref = {"case_id": "c", "candidate_a_attempt_id": "a", "candidate_b_attempt_id": "b"}
    with pytest.raises(ValueError):
        SubjectPairReference.model_validate(ref)


def settle_saved_call(service, job, plan, content, *, last):
    """Land a synthetic historical response through actual Invocation transactions."""
    job = service.jobs.begin_call(
        job["job_id"],
        expected_revision=job["revision"],
        invocation=service._invocation_record(job, plan),
        call={
            "call_id": plan["call_id"],
            "case_id": plan["case_id"],
            "mode": "pairwise",
            "input_sha256": plan["input_sha256"],
            "presentation_order": plan["presentation_order"],
            "candidate_ids": plan["candidate_ids"],
            "repeat_index": plan["repeat_index"],
            "reservation": plan["reservation"],
        },
    )
    return service.jobs.settle_call(
        job["job_id"],
        expected_revision=job["revision"],
        call_id=plan["call_id"],
        outcome="succeeded",
        result_summary={
            "raw_response": {"content": content, "response_id": "historical-response"},
            "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            "cost_usd": 0.01,
        },
        job_status="settled" if last else "dispatching",
    )


@pytest.mark.parametrize("all_settled", [False, True])
def test_historical_partial_and_settled_recovery_preserves_ledger(role_env, all_settled):
    _, reopen, _, provider, factory, service, body, _ = role_env
    request, saved = legacy_record(service, fixed_request(role_env, repeats=2))
    job = service.jobs.claim(saved["job_id"])
    job = settle_saved_call(service, job, saved["plans"][0], provider.responses[0], last=False)
    if all_settled:
        job = settle_saved_call(service, job, saved["plans"][1], provider.responses[0], last=True)
    calls_before = deepcopy(job["calls"])
    ledger_before = service.store.invocations.list_for_job(job["job_id"])
    reopened = reopen()
    resumed = ScoringJobService(reopened, provider_factory=None)
    resumed.recover_interrupted()
    if all_settled:
        result = WorkerLoop(RunService(reopened), scoring_jobs=resumed).claim_and_execute()
        assert result["status"] == "completed"
        record = reopened.scoring_passes.get(saved["reserved_pass_id"])
        assert "pairwise_roles" not in record and "roles_sha256" not in record
        from motte_sdk.comparisons import ComparisonService

        assert (
            ComparisonService(reopened).candidate_summary(
                body["run_id"], scoring_pass_id=record["id"]
            )["metric_values"]["accuracy"]
            is None
        )
        before = resumed.jobs.get(job["job_id"])
        assert resumed.submit(request)["reused"]
        assert resumed.jobs.get(job["job_id"]) == before
    else:
        result = resumed.get(job["job_id"])
        assert result["status"] == "indeterminate"
        assert result["role_recovery"]["code"] == "PAIRWISE_ROLES_REQUIRED"
        assert "explicit" in result["role_recovery"]["message"].lower()
        assert WorkerLoop(RunService(reopened), scoring_jobs=resumed).claim_and_execute() is None
        assert reopened.scoring_passes.get(saved["reserved_pass_id"]) is None
    assert resumed.jobs.get(job["job_id"])["calls"] == calls_before
    assert reopened.invocations.list_for_job(job["job_id"]) == ledger_before
    assert provider.calls == factory.calls == []


@pytest.mark.parametrize("settled", [False, True])
def test_invalid_saved_role_digest_never_dispatches_or_publishes(role_env, settled):
    _, reopen, _, provider, factory, service, _, _ = role_env
    request = fixed_request(role_env)
    record = service.compile_record(
        request, job_id="corrupt-role-job", reserved_pass_id="corrupt-role-pass"
    )
    record["roles_sha256"] = "sha256:" + "f" * 64
    service.jobs.submit(record)
    if settled:
        claimed = service.jobs.claim(record["job_id"])
        settle_saved_call(service, claimed, record["plans"][0], provider.responses[0], last=True)
    resumed = ScoringJobService(reopen(), provider_factory=factory)
    result = WorkerLoop(RunService(resumed.store), scoring_jobs=resumed).claim_and_execute()
    assert result["status"] == "failed"
    assert result["failure"]["code"] == "PAIRWISE_ROLES_INVALID"
    assert resumed.store.scoring_passes.get(record["reserved_pass_id"]) is None
    assert provider.calls == factory.calls == []


def test_calibration_pair_restart_does_not_require_subject_roles(role_env):
    _, reopen, _, provider, factory, service, _, _ = role_env
    from motte_eval.judge import JudgeCandidateInput, JudgeCandidateRef, build_pairwise_input
    from motte_sdk.scoring_jobs import ScoringJobRequest

    subject = fixed_request(role_env)
    pair = build_pairwise_input(
        task_ref="software-sample",
        candidate_a=JudgeCandidateInput(
            candidate=JudgeCandidateRef(
                candidate_id="left",
                owner_kind="calibration",
                calibration_job_id="review-fixture",
                sample_id="software-sample",
            ),
            content="left",
            evidence_allowlist=["event:1"],
        ),
        candidate_b=JudgeCandidateInput(
            candidate=JudgeCandidateRef(
                candidate_id="right",
                owner_kind="calibration",
                calibration_job_id="review-fixture",
                sample_id="software-sample",
            ),
            content="right",
            evidence_allowlist=["event:2"],
        ),
    )
    request = ScoringJobRequest(
        request_key="calibration-roles-exempt",
        judge_spec=subject.judge_spec,
        mode="pairwise",
        calibration_job_id="review-fixture",
        sample_ids={"software-sample": "software-sample"},
        pairwise_pairs=[pair],
        authorisation=subject.authorisation,
        price_table=subject.price_table,
        provider_snapshot=subject.provider_snapshot,
    )
    job = service.submit(request)
    assert "pairwise_roles" not in job
    claimed = service.jobs.claim(job["job_id"])
    assert claimed["status"] == "prepared"
    resumed = ScoringJobService(reopen(), provider_factory=factory)
    assert resumed.recover_interrupted() == [job["job_id"]]
    assert resumed.get(job["job_id"])["status"] == "queued"
    assert "role_recovery" not in resumed.get(job["job_id"])
    result = WorkerLoop(RunService(resumed.store), scoring_jobs=resumed).claim_and_execute()
    assert result["status"] == "completed"
    assert len(provider.calls) == 1
    assert result["owner"]["kind"] == "calibration"
    assert resumed.store.runs.get(result["run_id"]) is None
    assert "pairwise_roles" not in resumed.store.scoring_passes.get(job["reserved_pass_id"])


def test_candidate_side_swap_keeps_explicit_roles(role_env):
    _, _, _, provider, factory, service, body, _ = role_env
    first = service.compile_record(
        fixed_request(role_env), job_id="first", reserved_pass_id="first-pass"
    )
    refs = deepcopy(body["pairwise_refs"])
    ref = refs[0]
    ref["candidate_a_attempt_id"], ref["candidate_b_attempt_id"] = (
        ref["candidate_b_attempt_id"],
        ref["candidate_a_attempt_id"],
    )
    swapped = service.compile_record(
        fixed_request(role_env, pairwise_refs=refs),
        job_id="swapped",
        reserved_pass_id="swapped-pass",
    )
    assert first["pairwise_roles"] == swapped["pairwise_roles"]
    assert first["roles_sha256"] == swapped["roles_sha256"]
    assert provider.calls == factory.calls == []


def test_prepared_roleless_recovery_does_not_requeue_dispatch(role_env):
    _, reopen, _, provider, factory, service, _, _ = role_env
    _, saved = legacy_record(service, fixed_request(role_env))
    claimed = service.jobs.claim(saved["job_id"])
    assert claimed["status"] == "prepared"
    resumed = ScoringJobService(reopen(), provider_factory=factory)
    assert resumed.recover_interrupted() == [saved["job_id"]]
    recovered = resumed.get(saved["job_id"])
    assert recovered["status"] == "failed"
    assert (
        recovered["failure"]["code"]
        == recovered["role_recovery"]["code"]
        == "PAIRWISE_ROLES_REQUIRED"
    )
    assert recovered["plans"] == saved["plans"] and recovered["calls"] == []
    assert WorkerLoop(RunService(resumed.store), scoring_jobs=resumed).claim_and_execute() is None
    assert provider.calls == factory.calls == []


@pytest.mark.parametrize("corruption", ["attempt", "run_owner", "role_flip"])
def test_rehashed_saved_role_mutation_cannot_keep_old_job_identity(role_env, corruption):
    _, reopen, _, provider, factory, service, _, _ = role_env
    from motte_eval.judge import build_pairwise_input

    request = fixed_request(role_env)
    record = service.compile_record(
        request, job_id="rehashed-role", reserved_pass_id="rehashed-pass"
    )
    role = record["pairwise_roles"][0]
    if corruption == "attempt":
        role["challenger_attempt_id"] = "unrelated-attempt"
    elif corruption == "role_flip":
        role["challenger_attempt_id"], role["reference_attempt_id"] = (
            role["reference_attempt_id"],
            role["challenger_attempt_id"],
        )
        role["challenger_candidate_id"], role["reference_candidate_id"] = (
            role["reference_candidate_id"],
            role["challenger_candidate_id"],
        )
    else:
        pair = request.pairwise_pairs[0]
        changed_side = pair.candidate_b.model_copy(
            update={
                "candidate": pair.candidate_b.candidate.model_copy(update={"run_id": "foreign-run"})
            }
        )
        changed = build_pairwise_input(
            task_ref=pair.task_ref,
            candidate_a=pair.candidate_a,
            candidate_b=changed_side,
            presentation_order=pair.presentation_order,
        )
        role["pair_id"] = changed.pair_id
        role["candidate_input_sha256"] = {
            side.candidate.candidate_id: canonical_sha256(side.model_dump(mode="json"))
            for side in (changed.candidate_a, changed.candidate_b)
        }
        for plan in record["plans"]:
            plan.update(
                pair_id=changed.pair_id,
                input_sha256=changed.input_sha256,
                pair=changed.model_dump(mode="json"),
            )
    record["roles_sha256"] = canonical_sha256(record["pairwise_roles"])
    for plan in record["plans"]:
        plan.update(role_binding=deepcopy(role), roles_sha256=record["roles_sha256"])
    service.jobs.submit(record)
    resumed = ScoringJobService(reopen(), provider_factory=factory)
    result = WorkerLoop(RunService(resumed.store), scoring_jobs=resumed).claim_and_execute()
    assert result["status"] == "failed"
    assert result["failure"]["code"] == "PAIRWISE_ROLES_INVALID"
    assert resumed.store.scoring_passes.get(record["reserved_pass_id"]) is None
    assert provider.calls == factory.calls == []


@pytest.mark.parametrize(
    "fragment",
    [
        "job_digest",
        "null_roles",
        "plan",
        "call",
        "invocation",
        "submission",
        "new_record",
        "fingerprint_only",
    ],
)
@pytest.mark.parametrize("settled", [False, True])
def test_contradictory_role_fragments_never_qualify_as_legacy(role_env, fragment, settled):
    _, reopen, _, provider, factory, service, _, _ = role_env
    base = fixed_request(role_env)
    legacy_request, historical = legacy_record(service, base)
    service.cancel(historical["job_id"], actor="software-only", reason="isolate fragment fixture")
    record = deepcopy(historical)
    record.update(
        job_id="fragment-job", request_key="fragment-key", reserved_pass_id="fragment-pass"
    )
    if fragment in {"new_record", "fingerprint_only"}:
        record = service.compile_record(
            base.model_copy(update={"request_key": record["request_key"]}),
            job_id=record["job_id"],
            reserved_pass_id=record["reserved_pass_id"],
        )
        record.pop("pairwise_roles")
        if fragment == "fingerprint_only":
            record.pop("roles_sha256")
            for plan in record["plans"]:
                plan.pop("role_binding")
                plan.pop("roles_sha256")
            for reference in record["submission_input"]["pairwise_refs"]:
                reference.pop("challenger_attempt_id")
    elif fragment == "job_digest":
        record["roles_sha256"] = None
    elif fragment == "null_roles":
        record["pairwise_roles"] = None
    elif fragment == "plan" and not settled:
        record["plans"][0]["role_binding"] = None
    elif fragment == "submission":
        record["submission_input"] = {
            "pairwise_refs": [{"challenger_attempt_id": "explicit-challenger"}]
        }
    service.jobs.submit(record)
    if settled:
        claimed = service.jobs.claim(record["job_id"])
        landed = settle_saved_call(
            service, claimed, record["plans"][0], provider.responses[0], last=True
        )
    else:
        landed = service.jobs.get(record["job_id"])
    if fragment == "plan" and settled:
        plans = deepcopy(landed["plans"])
        plans[0]["role_binding"] = None
        service.jobs.transition(
            record["job_id"],
            expected_revision=landed["revision"],
            expected_status=landed["status"],
            status=landed["status"],
            allow_same_status=True,
            changes={"plans": plans},
        )
    if fragment == "call":
        calls = deepcopy(landed["calls"])
        if calls:
            calls[0]["roles_sha256"] = None
        else:
            calls = [{"call_id": "fragment-only", "status": "prepared", "roles_sha256": None}]
        service.jobs.transition(
            record["job_id"],
            expected_revision=landed["revision"],
            expected_status=landed["status"],
            status=landed["status"],
            allow_same_status=True,
            changes={"calls": calls},
        )
    if fragment == "invocation":
        invocation = service._invocation_record(record, record["plans"][0])
        invocation.update(
            id="fragment-invocation",
            request_summary={**invocation["request_summary"], "role_binding": None},
        )
        service.store.invocations.create(invocation)
    resumed = ScoringJobService(reopen(), provider_factory=None)
    result = WorkerLoop(RunService(resumed.store), scoring_jobs=resumed).claim_and_execute()
    assert result["status"] == "failed"
    assert result["failure"]["code"] == "PAIRWISE_ROLES_INVALID"
    assert resumed.store.scoring_passes.get(record["reserved_pass_id"]) is None
    # Exact internal replay must not endorse/enrich these contradictory saved records.
    replay = legacy_request.model_copy(update={"request_key": record["request_key"]})
    with pytest.raises(JudgeInputError) as refused:
        resumed.submit(replay)
    assert refused.value.code == "PAIRWISE_ROLES_INVALID"
    assert provider.calls == factory.calls == []
