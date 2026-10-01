"""HTTP SDK uses the real API, retaining the lightweight installed-client boundary."""
from motte_sdk import MotteClient
import pytest
from tests.sdk.sync_asgi import SyncASGITransport
from tests.judge_calibration_fixtures import environment
from tests.judge_calibration_fixtures import ADDRESSABLE_SELECTORS


def test_http_client_lifecycle_methods_are_typed_and_zero_call():
    env = environment()
    with MotteClient("http://testserver", transport=SyncASGITransport(env["app"])) as client:
        assert callable(getattr(client, "import_judge_calibration", None)), "missing lifecycle client"
        parent = client.import_judge_calibration(env["import"])
        assert parent.calibration_id == env["id"] and parent.version == "imported"
        version = client.review_judge_calibration(env["id"], "imported", {
            "new_version": "reviewed", "expected_parent_sha256": parent.content_sha256,
            "reviews": env["reviews"]})
        body = {"content_sha256": version.content_sha256, "request": env["request"]}
        preview = client.preflight_judge_calibration(env["id"], "reviewed", body)
        assert preview.max_calls == 120 and preview.raw["executed"] is False
        job = client.submit_judge_calibration(env["id"], "reviewed", body)
        assert job.allowance["max_calls"] == 120
        assert client.get_judge_calibration_job(env["id"], job.execution_id).raw == job.raw
        assert client.get_judge_calibration_version(env["id"], "reviewed").raw == version.raw
        assert client.list_judge_calibrations().total == 1
        assert client.list_judge_calibration_versions(env["id"]).total == 2
        assert client.list_judge_calibration_reviews(env["id"], "imported").total == 30
        assert client.list_judge_calibration_jobs(env["id"], "reviewed").total == 1
        assert client.list_judge_calibration_reports(env["id"], job.execution_id).total == 0
    assert env["factory"].calls == env["provider"].calls == []


def test_http_sdk_lost_submit_response_replays_only_on_explicit_call():
    import pytest
    from motte_sdk import ConflictError, NotFoundError, ReadTimeout, ValidationError
    from tests.sdk.test_client_contract import CountingTransport, FlakyTransport
    from tests.judge_calibration_fixtures import ROOT, import_review, execute_body
    env = environment()
    _, version, _ = import_review(env)
    body = execute_body(env, version)
    path = f"{ROOT}/{env['id']}/versions/reviewed/jobs"
    counted = CountingTransport(FlakyTransport(SyncASGITransport(env["app"]),
                                              method="POST", path_prefix=path))
    with MotteClient("http://testserver", transport=counted) as client:
        with pytest.raises(ReadTimeout):
            client.submit_judge_calibration(env["id"], "reviewed", body)
        assert counted.count("POST", path) == 1
        assert len(env["store"].calibrations.list_executions(env["id"])) == 1
        replay = client.submit_judge_calibration(env["id"], "reviewed", body)
        assert replay.execution_id == env["store"].calibrations.list_executions(env["id"])[0].execution_id
        assert counted.count("POST", path) == 2
        with pytest.raises(NotFoundError) as missing:
            client.get_judge_calibration_job("different-owner", replay.execution_id)
        assert missing.value.code == "CALIBRATION_NOT_FOUND"
        with pytest.raises(ConflictError):
            client.submit_judge_calibration(env["id"], "reviewed", {**body, "content_sha256": "sha256:" + "0" * 64})
        with pytest.raises(ValidationError):
            client.submit_judge_calibration(env["id"], "reviewed", {**body, "qualified": True})
    assert env["factory"].calls == env["provider"].calls == []


def test_new_client_views_do_not_import_local_execution_stack(tmp_path):
    import os
    import subprocess
    import sys
    code = '''
import sys
import motte_sdk
from motte_sdk.client_types import CalibrationJob, CalibrationReport, CalibrationVersionView
assert hasattr(motte_sdk.MotteClient, 'import_judge_calibration')
for name in ('motte_eval', 'motte_storage', 'motte_provider', 'motte_sdk.calibration_transport'):
    assert name not in sys.modules, name
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path,
                            env=dict(os.environ), text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "var").exists()


@pytest.mark.parametrize("selector", ["calibration_id", "version", "new_version"])
def test_http_sdk_selector_rejections_match_public_api(selector):
    from motte_sdk import ValidationError
    from tests.judge_calibration_fixtures import UNADDRESSABLE_SELECTORS, selector_import
    env = environment()
    with MotteClient("http://testserver", transport=SyncASGITransport(env["app"])) as client:
        parent = client.import_judge_calibration(env["import"])
        before = list(env["store"].calibrations.iter_records())
        for value in UNADDRESSABLE_SELECTORS:
            with pytest.raises(ValidationError) as invalid:
                if selector == "new_version":
                    client.review_judge_calibration(env["id"], "imported", {
                        "new_version": value, "expected_parent_sha256": parent.content_sha256,
                        "reviews": env["reviews"]})
                else:
                    client.import_judge_calibration(selector_import(env, **{selector: value}))
            assert invalid.value.http_status == 422 and invalid.value.code == "CALIBRATION_CONTRACT_INVALID"
            assert list(env["store"].calibrations.iter_records()) == before
    assert env["factory"].calls == env["provider"].calls == []


@pytest.mark.parametrize("name", ADDRESSABLE_SELECTORS)
def test_accepted_selectors_round_trip_losslessly_through_api_and_sdk(name):
    from urllib.parse import quote
    from tests.judge_calibration_fixtures import ROOT, selector_import
    env = environment()
    body = selector_import(env, calibration_id=name, version=name)
    with MotteClient("http://testserver", transport=SyncASGITransport(env["app"])) as client:
        parent = client.import_judge_calibration(body)
        assert parent.raw["calibration"] == body["calibration"]
        assert client.get_judge_calibration_version(name, name).raw == parent.raw
        prefix = f"{ROOT}/{quote(name, safe='')}/versions/"
        assert env["client"].get(prefix + quote(name, safe="")).json() == parent.raw
        reviewed_name = "review-" + name
        reviewed = client.review_judge_calibration(name, name, {
            "new_version": reviewed_name, "expected_parent_sha256": parent.content_sha256,
            "reviews": env["reviews"]})
        assert reviewed.version == reviewed_name
        assert client.get_judge_calibration_version(name, reviewed_name).raw == reviewed.raw
        assert env["client"].get(prefix + quote(reviewed_name, safe="")).json() == reviewed.raw
        assert client.list_judge_calibration_versions(name).total == 2
    assert env["factory"].calls == env["provider"].calls == []


def test_http_sdk_pairwise_gate_preserves_the_distinct_quality_decision():
    from tests.api.test_pairwise_quality_gate import environment as gate_environment
    from tests.evaluators.test_pairwise_quality_gate import policy
    store, api = gate_environment()
    with MotteClient('http://testserver', transport=SyncASGITransport(api.app)) as client:
        assert client.publish_gate_policy(policy())['rules'][0]['threshold'] == .4
        result = client.evaluate_gate_versioned('run', 'explicit', '1', scoring_pass_id='pass')
        assert result.decision == 'insufficient_evidence'
        assert result.extra['exit_code'] == 5
        assert result.raw['candidates'][0]['scoring_pass_id'] == 'pass'
        assert client.get_gate_result(result.raw['gate_result_id']).raw == store.gate_store.get_result(result.raw['gate_result_id'])
