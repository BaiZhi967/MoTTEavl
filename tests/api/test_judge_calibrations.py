"""Public calibration boundary: strict inputs, ownership, zero-call reads/previews."""
from copy import deepcopy
import json

import pytest

from tests.judge_calibration_fixtures import ROOT, environment, execute_body, import_review
from tests.judge_calibration_fixtures import lifecycle_env  # noqa: F401


def test_public_import_review_preview_submit_and_read_are_zero_call():
    env = environment()
    parent, version, review = import_review(env)
    client, store = env["client"], env["store"]
    prefix = f"{ROOT}/{env['id']}"
    assert parent["calibration"]["samples"][0]["status"] == "candidate"
    assert version["calibration"]["samples"][0]["reviewed_by"] == "software-only-reviewer"
    before = list(store.calibrations.iter_records())
    for path in [ROOT, prefix + "/versions", prefix + "/versions/imported",
                 prefix + "/versions/imported/reviews", prefix + "/versions/reviewed/jobs"]:
        assert client.get(path).status_code == 200
    body = execute_body(env, version)
    preview = client.post(prefix + "/versions/reviewed/preflight", json=body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["child_call_counts"] == [32, 32, 32, 24]
    assert preview.json()["max_calls"] == 120 and preview.json()["executed"] is False
    assert list(store.calibrations.iter_records()) == before
    body["request"]["expected_preflight_sha256"] = preview.json()["preflight_sha256"]
    created = client.post(prefix + "/versions/reviewed/jobs", json=body)
    assert created.status_code == 202, created.text
    job = created.json()
    assert job["worker_command"] == "uv run python -m apps.worker.motte_worker --once"
    assert len(job["child_job_ids"]) == 4 and job["allowance"]["max_calls"] == 120
    assert "plan" not in job and "provider_snapshot" not in job
    assert "Challenger fixture" not in created.text and "raw_response" not in created.text
    assert client.get(prefix + "/jobs/" + job["execution_id"]).json() == job
    replay = client.post(prefix + "/versions/reviewed/jobs", json=body)
    assert replay.json() == job
    assert env["factory"].calls == env["provider"].calls == []
    assert client.post(prefix + "/versions/imported/reviews", json=review).json() == version


def test_nested_owners_and_drift_reject_without_mutation():
    env = environment()
    _, version, review = import_review(env)
    client, store = env["client"], env["store"]
    prefix = f"{ROOT}/{env['id']}"
    body = execute_body(env, version)
    created = client.post(prefix + "/versions/reviewed/jobs", json=body)
    assert created.status_code == 202, created.text
    execution = created.json()["execution_id"]
    before = list(store.calibrations.iter_records())
    for path in [f"{ROOT}/other/jobs/{execution}", f"{ROOT}/other/jobs/{execution}/reports",
                 f"{ROOT}/other/versions/reviewed", f"{ROOT}/other/versions/reviewed/jobs"]:
        assert client.get(path).status_code == 404
    assert client.post(f"{ROOT}/other/jobs/{execution}/reports").status_code == 404
    changed = {**body, "content_sha256": "sha256:" + "0" * 64}
    assert client.post(prefix + "/versions/reviewed/preflight", json=changed).status_code == 409
    changed = deepcopy(body)
    changed["request"]["authorisation"]["actor"] = "different-declared-operator"
    assert client.post(prefix + "/versions/reviewed/jobs", json=changed).status_code == 409
    review["expected_parent_sha256"] = "sha256:" + "0" * 64
    assert client.post(prefix + "/versions/imported/reviews", json=review).status_code == 409
    assert list(store.calibrations.iter_records()) == before
    assert env["factory"].calls == env["provider"].calls == []


@pytest.mark.parametrize("mutation", ["report", "plan", "nonfinite", "actor_claim", "synthetic"])
def test_untrusted_fields_and_numbers_are_safe_typed_errors(mutation):
    env = environment()
    _, version, review = import_review(env)
    prefix = f"{ROOT}/{env['id']}"
    body = execute_body(env, version)
    path = prefix + "/versions/reviewed/jobs"
    secret = "PRIVATE_INPUT_DO_NOT_ECHO"
    if mutation == "report":
        body = {"report": {"qualified": True, "raw_response": secret}}
        path = prefix + "/jobs/unknown/reports"
    elif mutation == "plan":
        body["request"]["plan"] = {secret: "caller authority"}
    elif mutation == "nonfinite":
        body["request"]["spec_request"]["budget"]["hard_cost_cap_usd"] = float("inf")
    elif mutation == "actor_claim":
        review["reviews"][0]["authenticated_user"] = secret
        body, path = review, prefix + "/versions/imported/reviews"
    else:
        other = environment()
        from tests.sdk.test_judge_calibrations import import_data, run_request
        imported, _ = import_data(run_request(), source="synthetic_candidate")
        other["import"] = imported.model_dump(mode="json")
        first = other["client"].post(ROOT, json=other["import"])
        assert first.status_code == 201, first.text
        env = other
        review["expected_parent_sha256"] = first.json()["content_sha256"]
        body, path = review, prefix + "/versions/imported/reviews"
    before = list(env["store"].calibrations.iter_records())
    response = env["client"].post(path, content=json.dumps(body), headers={"Content-Type": "application/json"})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "CALIBRATION_CONTRACT_INVALID"
    assert secret not in response.text and "Infinity" not in response.text
    assert list(env["store"].calibrations.iter_records()) == before
    assert env["factory"].calls == env["provider"].calls == []


def test_openapi_has_runtime_errors_and_no_report_import():
    schema = environment()["app"].openapi()
    path = ROOT + "/{calibration_id}/versions/{version}/jobs"
    assert path in schema["paths"]
    operation = schema["paths"][path]["post"]
    for status in ["404", "409", "422", "503"]:
        assert operation["responses"][status]["content"]["application/json"]["schema"]["$ref"].endswith("CalibrationErrorResponse")
    assert "CalibrationImportRequest" in schema["components"]["schemas"]


def test_unavailable_submit_has_typed_503_and_no_children():
    from apps.api.app.main import create_app
    from fastapi.testclient import TestClient
    from motte_storage.scoring_jobs import scoring_jobs_for
    env = environment()
    _, version, _ = import_review(env)
    before = list(env["store"].calibrations.iter_records())
    client = TestClient(create_app(env["store"], resource_store=env["resources"], judge_provider_factory=None))
    response = client.post(f"{ROOT}/{env['id']}/versions/reviewed/jobs", json=execute_body(env, version))
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "CALIBRATION_UNAVAILABLE"
    assert list(env["store"].calibrations.iter_records()) == before
    assert scoring_jobs_for(env["store"]).list_by_status() == []


def test_declared_actor_never_authenticates_and_other_handlers_are_unchanged():
    from apps.api.app.main import create_app
    from fastapi.testclient import TestClient
    env = environment()
    client = TestClient(create_app(env["store"], resource_store=env["resources"], api_token="fixture-token"))
    denied = client.post(ROOT, json=env["import"])
    assert denied.status_code == 401
    assert list(env["store"].calibrations.iter_records()) == []
    assert client.post(ROOT, json=env["import"], headers={"Authorization": "Bearer fixture-token"}).status_code == 201
    # Lifecycle scoping does not replace the default unrelated validation shape.
    invalid = env["client"].post("/api/v1/runs", json={"unexpected": True})
    assert invalid.status_code == 422 and "detail" in invalid.json()
    assert env["factory"].calls == env["provider"].calls == []


@pytest.mark.parametrize("selector", ["calibration_id", "version", "new_version"])
def test_public_selector_rejection_has_no_durable_writes_or_calls(lifecycle_env, selector):  # noqa: F811
    from motte_storage.scoring_jobs import scoring_jobs_for
    from tests.judge_calibration_fixtures import UNADDRESSABLE_SELECTORS, selector_import
    env = lifecycle_env
    parent = env["client"].post(ROOT, json=env["import"]).json()
    before = list(env["store"].calibrations.iter_records())
    for value in UNADDRESSABLE_SELECTORS:
        if selector == "new_version":
            body = {"new_version": value, "expected_parent_sha256": parent["content_sha256"],
                    "reviews": env["reviews"]}
            path = f"{ROOT}/{env['id']}/versions/imported/reviews"
        else:
            body, path = selector_import(env, **{selector: value}), ROOT
        response = env["client"].post(path, json=body)
        assert response.status_code == 422, (selector, value, response.text)
        assert response.json()["error"]["code"] == "CALIBRATION_CONTRACT_INVALID"
        assert list(env["reopen"]().calibrations.iter_records()) == before
        assert scoring_jobs_for(env["store"]).list_by_status() == []
        assert env["factory"].calls == env["provider"].calls == []


def test_legacy_internal_selectors_and_nonselector_ids_remain_readable():
    from motte_eval.calibration import calibration_content_sha256, sample_content_sha256
    from motte_eval.calibration_records import CalibrationImport, CalibrationVersion
    from motte_sdk.calibration_transport import CalibrationLifecycle
    from motte_sdk.judge_calibrations import JudgeCalibrationService
    from motte_sdk.scoring_jobs import ScoringJobService
    from tests.judge_calibration_fixtures import selector_import
    env = environment()
    service = JudgeCalibrationService(env["store"], env["resources"], scoring_jobs=ScoringJobService(
        env["store"], provider_factory=env["factory"]))
    legacy = service.import_version(CalibrationImport.model_validate(
        selector_import(env, calibration_id="legacy/owner", version="../legacy")))
    public = CalibrationLifecycle(service)
    assert public.version("legacy/owner", "../legacy") == legacy
    assert CalibrationVersion.model_validate(legacy.model_dump(mode="json")) == legacy
    assert env["client"].get(ROOT).json()["items"][0]["calibration_id"] == "legacy/owner"
    # The boundary applies only to public selectors, not sample/candidate identities.
    body = deepcopy(env["import"])
    sample = body["calibration"]["samples"][0]
    old_id, sample["sample_id"] = sample["sample_id"], "sample/./%2F"
    sample["content_sha256"] = sample_content_sha256(sample)
    body["pairs"][sample["sample_id"]] = body["pairs"].pop(old_id)
    body["pairs"][sample["sample_id"]]["candidates"][0]["candidate_id"] = "candidate/../%2F"
    body["calibration"]["content_sha256"] = calibration_content_sha256(body["calibration"])
    created = env["client"].post(ROOT, json=body)
    assert created.status_code == 201, created.text
    saved = created.json()
    assert saved["calibration"] == body["calibration"] and saved["pairs"] == body["pairs"]
    assert env["factory"].calls == env["provider"].calls == []
