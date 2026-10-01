"""Uncalibrated Judge evidence cannot qualify either public Gate entry point."""
import pytest
from motte_sdk.comparisons import ComparisonService
from tests.api.test_judge_flow import environment, run_worker, submit_body, POLICY_FOR_ONE_CASE
from tests.sdk.test_m6_comparison_service import gate_policy_payload


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def pairwise_backend(request, tmp_path):
    """Saved software candidates; submission/preview never execute a provider."""
    from functools import partial
    from fastapi.testclient import TestClient
    from apps.api.app.main import create_app
    from motte_storage.factory import create_run_store
    from motte_storage.resource_store import InMemoryResourceStore
    from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore
    from tests.api.test_judge_flow import RecordingFactory, ScriptedProvider
    from tests.integration.test_judge_worker_flow import seed_resources
    from tests.sdk.test_calibration_gate_binding import save_attempts
    from tests.sdk.test_judge_calibrations import run_request

    if request.param == "postgres":
        from motte_storage.migrations import upgrade
        dsn = request.getfixturevalue("isolated_pg_database")
        upgrade(dsn)
        reopen = partial(create_run_store, storage="postgres", dsn=dsn)
        store = reopen()
    elif request.param == "sqlite":
        reopen = partial(SQLiteRunStore, tmp_path / "pairwise.db")
        store = reopen()
    else:
        store = InMemoryRunStore()
        def reopen():
            return store
    resources = InMemoryResourceStore()
    seed_resources(resources)
    run_id, ref = save_attempts(store)
    provider = ScriptedProvider()
    factory = RecordingFactory(provider)
    specification = run_request()
    body = {
        "request_key": "saved-pairwise", "run_id": run_id, "mode": "pairwise",
        "spec": specification.spec_request, "pairwise_refs": [ref],
        "authorisation": specification.authorisation.model_dump(
            mode="json", exclude={"purpose", "authorised_at"}),
        "price_table_version": "price-1",
    }
    client = TestClient(create_app(store, resource_store=resources, judge_provider_factory=factory))
    return store, reopen, resources, provider, factory, body, client


@pytest.mark.parametrize("qualified", [False, True])
def test_pairwise_replay_uses_durable_job_on_each_backend(
    pairwise_backend, monkeypatch, tmp_path, qualified,
):
    from copy import deepcopy
    from fastapi.testclient import TestClient
    from apps.api.app.main import create_app
    from motte_storage.scoring_jobs import scoring_jobs_for

    store, reopen, resources, provider, factory, body, client = pairwise_backend
    if qualified:
        from tests.sdk.test_calibration_ledger import completed_environment
        env = completed_environment(tmp_path, store=store)
        calibration, scoring, provider, _, specification, execution = env
        calibration.publish_report(execution.execution_id)
        binding = next(row["binding"] for row in store.calibrations.iter_records()
                       if "qualification" in row)
        resources, factory = calibration.resources, scoring.provider_factory
        body = {**body, "spec": specification.spec_request,
                "qualification_id": binding["qualification_id"]}
        client = TestClient(create_app(store, resource_store=resources, judge_provider_factory=factory))
    calls_before = (len(provider.calls), len(factory.calls))
    created = client.post("/api/v1/judges", json=body)
    assert created.status_code == 202, created.text
    job_id = created.json()["job_id"]
    before = scoring_jobs_for(store).get(job_id)
    jobs_before = scoring_jobs_for(store).list_by_status()
    if qualified:
        assert before["qualification_binding"] == binding
    runs_before = store.runs.list()
    reopened = reopen()
    client = TestClient(create_app(reopened, resource_store=resources, judge_provider_factory=factory))
    monkeypatch.setattr(resources.models, "get", lambda *args: pytest.fail("mutable model lookup"))
    monkeypatch.setattr(reopened.attempts, "get", lambda *args: pytest.fail("mutable attempt lookup"))
    replay = client.post("/api/v1/judges", json=body)
    assert replay.status_code == 202, replay.text
    assert replay.json()["reused"] and replay.json()["job_id"] == job_id
    changed = deepcopy(body)
    changed["qualification_id"] = "different-source"
    conflict = client.post("/api/v1/judges", json=changed)
    assert conflict.status_code == 409, conflict.text
    bypass = client.post("/api/v1/judges", json={**body, "_lookup_existing": False})
    assert bypass.status_code == 422, bypass.text
    jobs = scoring_jobs_for(reopened)
    assert jobs.get(job_id) == before
    assert jobs.list_by_status() == jobs_before
    assert reopened.runs.list() == runs_before
    assert reopened.scoring_passes.get(before["reserved_pass_id"]) is None
    assert reopened.invocations.list_for_run(body["run_id"]) == []
    assert (len(provider.calls), len(factory.calls)) == calls_before
    if not qualified:
        assert provider.calls == factory.calls == []


def test_pairwise_preflight_ignores_public_request_key_history(pairwise_backend, monkeypatch):
    from motte_storage.scoring_jobs import scoring_jobs_for

    store, _, _, provider, factory, body, client = pairwise_backend
    preview = {key: value for key, value in body.items() if key != "request_key"}
    preview["repeats"] = 2
    initial = client.post("/api/v1/judges/preflight", json=preview)
    assert initial.status_code == 200, initial.text
    assert initial.json()["max_calls"] == 2
    submitted = client.post("/api/v1/judges", json={**body, "request_key": "judge-preflight"})
    assert submitted.status_code == 202, submitted.text
    jobs = scoring_jobs_for(store)
    before = jobs.list_by_status()
    runs_before = store.runs.list()
    monkeypatch.setattr(type(jobs), "get_by_request_key",
                        lambda *args: pytest.fail("preflight consulted submission replay keys"))
    again = client.post("/api/v1/judges/preflight", json=preview)
    assert again.status_code == 200, again.text
    initial_report, repeated_report = initial.json(), again.json()
    for report in (initial_report, repeated_report):
        report["provider_snapshot"].pop("frozen_at")
    assert repeated_report == initial_report
    assert jobs.list_by_status() == before
    assert store.runs.list() == runs_before
    assert store.invocations.list_for_run(body["run_id"]) == []
    assert provider.calls == factory.calls == []


def judged(tmp_path):
    store, resources, provider, _factory, client = environment(tmp_path)
    client.post("/api/v1/judges", json=submit_body())
    assert run_worker(store, provider).claim_and_execute()["status"] == "completed"
    return store, client


def test_lite_gate_retains_case_metrics_but_rejects_uncalibrated_judge(tmp_path):
    store, client = judged(tmp_path)
    response = client.post("/api/v1/gates", json={
        "run_id": "run-1", "policy": {**POLICY_FOR_ONE_CASE, "judge_qualified": True},
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["coverage_summary"]["selected"] == 1
    assert result["coverage_summary"]["metric_values"]["accuracy"] == 1.0
    assert result["passed"] is False
    assert next(rule for rule in result["rules"] if rule["id"] == "judge_qualification")["passed"] is False


@pytest.mark.parametrize("diagnostic", [False, True])
def test_versioned_gate_uncalibrated_judge_never_becomes_pass(tmp_path, diagnostic):
    store, client = judged(tmp_path)
    service = ComparisonService(store)
    service.publish_gate_policy(gate_policy_payload(diagnostic=diagnostic))
    response = client.post("/api/v1/gates/versioned", json={
        "run_id": "run-1", "policy_id": "pol-m6", "policy_version": "1",
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["decision"] == "insufficient_evidence"
    assert result["exit_code"] == 5
    assert any(rule["rule_id"] == "judge_qualification" for rule in result["rule_results"])
    assert store.gate_store.get_result(result["gate_result_id"]) is not None


def test_lite_gate_baseline_ref_and_qualification_use_same_compared_pass(monkeypatch):
    from motte_storage.run_store import InMemoryRunStore
    from tests.sdk.test_m6_comparison_service import make_run, append_pass, score_row
    store = InMemoryRunStore()
    for run_id in ("base", "cand"):
        make_run(store, run_id, case_ids=["c1"])
        append_pass(store, run_id, "old-" + run_id, [score_row("c1", passed=True)])
    service = ComparisonService(store)
    original = service.compare
    def rescore(*args, **kwargs):
        result = original(*args, **kwargs)
        append_pass(store, "base", "new-base", [score_row("c1", passed=False)])
        return result
    monkeypatch.setattr(service, "compare", rescore)
    result = service.evaluate_gate("cand", baseline_run_id="base", policy={
        "metric": "accuracy", "threshold": 1, "required_coverage": 1, "require_comparable": True,
    })
    assert result["report_refs"]["baseline"]["scoring_pass_id"] == "old-base"


def test_http_lite_gate_summary_uses_returned_fixed_pass(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from apps.api.app.main import create_app
    from motte_storage.run_store import InMemoryRunStore
    from tests.sdk.test_m6_comparison_service import make_run, append_pass, score_row
    store = InMemoryRunStore()
    make_run(store, "candidate", case_ids=["c1"])
    append_pass(store, "candidate", "old", [score_row("c1", passed=True)])
    app = create_app(store=store)
    original = app.state.comparisons.evaluate_gate
    def rescore(*args, **kwargs):
        result = original(*args, **kwargs)
        append_pass(store, "candidate", "new", [score_row("c1", passed=False)])
        return result
    monkeypatch.setattr(app.state.comparisons, "evaluate_gate", rescore)
    result = TestClient(app).post("/api/v1/gates", json={
        "run_id": "candidate", "policy": {"metric": "accuracy", "threshold": 1},
    }).json()
    assert result["report_refs"]["candidate"]["scoring_pass_id"] == "old"
    assert result["coverage_summary"]["metric_values"]["accuracy"] == 1.0


def test_cross_run_manual_lineage_cannot_grant_judge_eligibility():
    from motte_storage.run_store import InMemoryRunStore
    from tests.sdk.test_m6_comparison_service import make_run, append_pass, score_row
    store = InMemoryRunStore()
    make_run(store, "foreign", case_ids=["c1"])
    make_run(store, "candidate", case_ids=["c1"])
    append_pass(store, "foreign", "foreign-pass", [score_row("c1", passed=True)])
    store.scoring_passes.append({
        "id": "cross-run-manual", "run_id": "candidate", "source": "manual_revision",
        "scorer_id": "manual-revision", "scorer_version": "1", "source_run_revision": 1,
        "created_at": "2026-09-30T00:00:00Z", "summary": {},
        "manual_revision": {"source_pass_id": "foreign-pass"},
    }, [score_row("c1", passed=True)])
    service = ComparisonService(store)
    assert service._judge_gate_qualification("cross-run-manual")["gate_eligible"] is False
    assert service.evaluate_gate("candidate", policy={"threshold": 1})["passed"] is False


def test_public_pairwise_submission_binds_verified_source_and_keeps_quality_unavailable(tmp_path):
    from fastapi.testclient import TestClient
    from apps.api.app.main import create_app
    from tests.sdk.test_calibration_ledger import completed_environment
    from tests.sdk.test_calibration_gate_binding import save_attempts
    import json
    env = completed_environment(tmp_path, memory=False)
    service, scoring, provider, version, request, execution = env
    service.publish_report(execution.execution_id)
    binding = next(row["binding"] for row in service.store.calibrations.iter_records() if "qualification" in row)
    run_id, ref = save_attempts(service.store)
    client = TestClient(create_app(service.store, resource_store=service.resources,
                                  judge_provider_factory=scoring.provider_factory))
    body = {"run_id": run_id, "request_key": "public-pair", "mode": "pairwise",
            "spec": request.spec_request, "authorisation": request.authorisation.model_dump(mode="json", exclude={"purpose", "authorised_at"}),
            "pairwise_refs": [ref], "qualification_id": binding["qualification_id"], "price_table_version": "price-1"}
    before = len(provider.calls)
    preflight = client.post("/api/v1/judges/preflight", json={k: v for k, v in body.items() if k != "request_key"})
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["max_calls"] == 1
    result = client.post("/api/v1/judges", json=body)
    assert result.status_code == 202, result.text
    assert result.json()["qualification_binding"] == binding
    assert len(provider.calls) == before
    provider.transport.responses.append(json.dumps({"winner": "tie", "criteria": [
        {"criterion_id": key, "preference": "tie", "reason": "software fixture", "evidence": ["event:1", "event:2"]}
        for key in version.spec.criteria]}))
    from apps.worker.motte_worker.runtime import WorkerLoop
    from motte_sdk.service import RunService
    WorkerLoop(RunService(service.store), scoring_jobs=scoring).claim_and_execute()
    pass_id = scoring.jobs.get(result.json()["job_id"])["reserved_pass_id"]
    comparisons = ComparisonService(service.store)
    comparisons.publish_gate_policy(gate_policy_payload())
    lite = client.post("/api/v1/gates", json={"run_id": run_id, "scoring_pass_id": pass_id,
                                           "policy": {"threshold": 0, "judge_qualified": True}})
    assert lite.status_code == 200, lite.text
    assert not lite.json()["passed"] and lite.json()["coverage_summary"]["metric_values"]["accuracy"] is None
    full = client.post("/api/v1/gates/versioned", json={"run_id": run_id, "scoring_pass_id": pass_id,
        "policy_id": "pol-m6", "policy_version": "1"})
    assert full.status_code == 200, full.text
    assert full.json()["decision"] == "insufficient_evidence" and full.json()["exit_code"] == 5
    assert full.json()["judge_qualification"]["candidate"]["binding"] == binding
    assert len(provider.calls) == before + 1
    for forbidden in ({"qualification_binding": binding}, {"qualified": True}, {"owner": {"kind": "subject"}},
                      {"observations": {}}, {"plans": []}):
        rejected = client.post("/api/v1/judges", json={**body, **forbidden})
        assert rejected.status_code == 422, rejected.text
    rejected = client.post("/api/v1/judges", json={**body, "pairwise_refs": [{**ref, "content": "injected"}]})
    assert rejected.status_code == 422


def test_binding_is_documented_and_absent_values_preserve_legacy_views():
    from apps.api.app.schemas import JudgeJobView, ScoringPassView, JudgeSubmitRequest
    for model in (JudgeJobView, ScoringPassView):
        assert "qualification_binding" in model.model_json_schema()["properties"]
    schema = JudgeSubmitRequest.model_json_schema()
    assert "qualification_id" in schema["properties"]
    assert "pairwise_refs" in schema["properties"]
    assert "qualification_binding" not in schema["properties"]
    assert schema["additionalProperties"] is False
    view = ScoringPassView(id="old", run_id="r", scorer_id="x", scorer_version="1")
    assert "qualification_binding" not in view.model_dump()


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_qualification_reference_is_serializable_422(tmp_path, number):
    from fastapi.testclient import TestClient
    import json
    store, resources, provider, factory, existing = environment(tmp_path)
    client = TestClient(existing.app, raise_server_exceptions=False)
    response = client.post("/api/v1/judges", content=json.dumps(submit_body(qualification_id=number)),
                           headers={"content-type": "application/json"})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "JUDGE_CONTRACT_INVALID"
    assert provider.calls == []
    assert store.scoring_jobs is None or not store.scoring_jobs.list_by_status()


def test_judge_openapi_describes_actual_error_envelopes(tmp_path):
    _, _, _, _, client = environment(tmp_path)
    schema = client.get("/openapi.json").json()
    for path in ("/api/v1/judges", "/api/v1/judges/preflight"):
        responses = schema["paths"][path]["post"]["responses"]
        for status in ("409", "422", "503"):
            assert responses[status]["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/JudgeErrorResponse"}
