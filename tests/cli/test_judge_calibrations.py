"""Local/server command parity uses actual service/HTTP API, no private shortcuts."""
import json
import pytest
from motte_cli import main as cli
from motte_sdk import MotteClient
from motte_sdk.service import RunService
from tests.sdk.sync_asgi import SyncASGITransport
from tests.judge_calibration_fixtures import environment


@pytest.mark.parametrize("mode", ["local", "server"])
def test_cli_import_review_preflight_submit_get(tmp_path, monkeypatch, capsys, mode):
    env = environment()
    monkeypatch.setattr(cli, "_service", lambda args: RunService(env["store"]))
    monkeypatch.setattr(cli, "_resources", lambda args: env["resources"])
    monkeypatch.setattr(cli.remote, "build_client", lambda args: MotteClient(
        "http://testserver", transport=SyncASGITransport(env["app"])))
    def invoke(command, body=None, *flags):
        args = ["judge-calibration", command, "--mode", mode]
        if mode == "server":
            args += ["--api-url", "http://testserver"]
        if body is not None:
            path = tmp_path / "input.json"
            path.write_text(json.dumps(body))
            args += ["--file", str(path)]
        code = cli.main([*args, *flags])
        captured = capsys.readouterr()
        assert code == 0, captured.err
        return json.loads(captured.out)
    parent = invoke("import", env["import"])
    version = invoke("review", {"new_version": "reviewed", "expected_parent_sha256": parent["content_sha256"],
                               "reviews": env["reviews"]}, "--id", env["id"], "--version", "imported")
    body = {"content_sha256": version["content_sha256"], "request": env["request"]}
    preview = invoke("preflight", body, "--id", env["id"], "--version", "reviewed")
    assert preview["max_calls"] == 120
    job = invoke("submit", body, "--id", env["id"], "--version", "reviewed")
    assert invoke("get", None, "--id", env["id"], "--job", job["execution_id"]) == job
    assert env["factory"].calls == env["provider"].calls == []


@pytest.mark.parametrize("mode", ["local", "server"])
def test_cli_typed_drift_error_and_missing_request_key(tmp_path, monkeypatch, capsys, mode):
    from tests.judge_calibration_fixtures import execute_body, import_review
    env = environment()
    _, version, _ = import_review(env)
    monkeypatch.setattr(cli, "_service", lambda args: RunService(env["store"]))
    monkeypatch.setattr(cli, "_resources", lambda args: env["resources"])
    monkeypatch.setattr(cli.remote, "build_client", lambda args: MotteClient(
        "http://testserver", transport=SyncASGITransport(env["app"])))
    flags = ["--mode", mode] + (["--api-url", "http://testserver"] if mode == "server" else [])
    body = execute_body(env, version)
    body["content_sha256"] = "sha256:" + "0" * 64
    path = tmp_path / "request.json"
    path.write_text(json.dumps(body))
    command = ["judge-calibration", "submit", "--file", str(path), "--id", env["id"], "--version", "reviewed", *flags]
    assert cli.main(command) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "CALIBRATION_CONFLICT"
    body = execute_body(env, version)
    body["request"].pop("request_key")
    path.write_text(json.dumps(body))
    assert cli.main(command) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "CALIBRATION_CONTRACT_INVALID"
    assert env["store"].calibrations.list_executions(env["id"]) == []
    assert env["factory"].calls == env["provider"].calls == []


def test_local_subject_pairwise_matches_http_collision_and_frozen_replay(tmp_path, monkeypatch, capsys):
    from copy import deepcopy
    from tests.sdk.test_calibration_gate_binding import save_attempts
    from motte_storage.scoring_jobs import scoring_jobs_for
    env = environment()
    run_id, pair = save_attempts(env["store"])
    monkeypatch.setattr(cli, "_service", lambda args: RunService(env["store"]))
    monkeypatch.setattr(cli, "_resources", lambda args: env["resources"])
    body = {"request_key": "judge-preflight", "run_id": run_id, "mode": "pairwise", "pairwise_refs": [pair],
            "spec": env["request"]["spec_request"], "price_table_version": "price-1",
            "authorisation": {key: value for key, value in env["request"]["authorisation"].items()
                              if key not in {"purpose", "authorised_at"}}}
    existing = env["client"].post("/api/v1/judges", json=body)
    assert existing.status_code == 202, existing.text
    preview_body = deepcopy(body)
    preview_body.pop("request_key")
    preview_body["repeats"] = 2
    path = tmp_path / "subject.json"
    path.write_text(json.dumps(preview_body))
    before = scoring_jobs_for(env["store"]).list_by_status()
    assert cli.main(["judge", "preflight", "--spec", "@" + str(path)]) == 0
    preview = json.loads(capsys.readouterr().out)
    remote = env["client"].post("/api/v1/judges/preflight", json=preview_body)
    assert remote.status_code == 200, remote.text
    assert preview["max_calls"] == remote.json()["max_calls"] == 2
    assert scoring_jobs_for(env["store"]).list_by_status() == before
    body["request_key"] = "local-subject"
    path.write_text(json.dumps(body))
    assert cli.main(["judge", "submit", "--spec", "@" + str(path)]) == 0
    saved = json.loads(capsys.readouterr().out)
    monkeypatch.setattr(env["resources"].models, "get", lambda *args: pytest.fail("mutable model lookup"))
    monkeypatch.setattr(env["store"].attempts, "get", lambda *args: pytest.fail("mutable attempt lookup"))
    assert cli.main(["judge", "submit", "--spec", "@" + str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["job_id"] == saved["job_id"]
    replay = env["client"].post("/api/v1/judges", json=body)
    assert replay.status_code == 202 and replay.json()["job_id"] == saved["job_id"]
    body["qualification_id"] = "different-source"
    path.write_text(json.dumps(body))
    assert cli.main(["judge", "submit", "--spec", "@" + str(path)]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "SCORING_JOB_CONFLICT"
    assert env["factory"].calls == env["provider"].calls == []


def test_local_subject_invalid_pair_is_strict_and_does_not_echo_payload(tmp_path, monkeypatch, capsys):
    from tests.sdk.test_calibration_gate_binding import save_attempts
    env = environment()
    run_id, pair = save_attempts(env["store"])
    monkeypatch.setattr(cli, "_service", lambda args: RunService(env["store"]))
    monkeypatch.setattr(cli, "_resources", lambda args: env["resources"])
    secret = "PRIVATE_CANDIDATE_BODY"
    body = {"request_key": "invalid-pair", "run_id": run_id, "mode": "pairwise",
            "pairwise_refs": [{**pair, "candidate_content": secret}], "spec": env["request"]["spec_request"],
            "authorisation": {key: value for key, value in env["request"]["authorisation"].items()
                              if key not in {"purpose", "authorised_at"}}}
    path = tmp_path / "invalid-pair.json"
    path.write_text(json.dumps(body))
    assert cli.main(["judge", "submit", "--spec", "@" + str(path)]) == 2
    error = capsys.readouterr().err
    assert secret not in error
    assert json.loads(error)["error"]["code"] == "JUDGE_CONTRACT_INVALID"
    assert env["factory"].calls == env["provider"].calls == []


@pytest.mark.parametrize("mode", ["local", "server"])
@pytest.mark.parametrize("payload", [None, ["PRIVATE_FILE_CONTENT"], {"value": float("inf")}])
def test_calibration_cli_rejects_nonobject_nonfinite_files_safely(tmp_path, monkeypatch, capsys, mode, payload):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(cli.remote, "build_client", lambda args: pytest.fail("invalid file reached HTTP"))
    args = ["judge-calibration", "import", "--file", str(path), "--mode", mode]
    if mode == "server":
        args += ["--api-url", "http://testserver"]
    assert cli.main(args) == 2
    error = capsys.readouterr().err
    assert json.loads(error)["error"]["code"] == "CALIBRATION_CONTRACT_INVALID"
    assert "PRIVATE_FILE_CONTENT" not in error and "Infinity" not in error


@pytest.mark.parametrize("mode", ["local", "server"])
def test_calibration_unavailable_exit_matches_server(tmp_path, monkeypatch, capsys, mode):
    from apps.api.app.main import create_app
    from motte_sdk import scoring_jobs
    from tests.judge_calibration_fixtures import execute_body, import_review
    env = environment()
    _, version, _ = import_review(env)
    monkeypatch.setattr(cli, "_service", lambda args: RunService(env["store"]))
    monkeypatch.setattr(cli, "_resources", lambda args: env["resources"])
    monkeypatch.setattr(scoring_jobs, "FrozenProviderFactory", lambda: None)
    app = create_app(env["store"], resource_store=env["resources"], judge_provider_factory=None)
    monkeypatch.setattr(cli.remote, "build_client", lambda args: MotteClient(
        "http://testserver", transport=SyncASGITransport(app)))
    path = tmp_path / "unavailable.json"
    path.write_text(json.dumps(execute_body(env, version)))
    flags = ["--mode", mode] + (["--api-url", "http://testserver"] if mode == "server" else [])
    assert cli.main(["judge-calibration", "submit", "--id", env["id"], "--version", "reviewed", "--file", str(path), *flags]) == 3
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "CALIBRATION_UNAVAILABLE"
    assert env["store"].calibrations.list_executions(env["id"]) == []


@pytest.mark.parametrize("mode", ["local", "server"])
@pytest.mark.parametrize("selector", ["calibration_id", "version", "new_version"])
def test_cli_selector_rejection_has_no_writes_or_calls(tmp_path, monkeypatch, capsys, mode, selector):
    from tests.judge_calibration_fixtures import ROOT, UNADDRESSABLE_SELECTORS, selector_import
    env = environment()
    parent = env["client"].post(ROOT, json=env["import"]).json()
    monkeypatch.setattr(cli, "_service", lambda args: RunService(env["store"]))
    monkeypatch.setattr(cli, "_resources", lambda args: env["resources"])
    monkeypatch.setattr(cli.remote, "build_client", lambda args: MotteClient(
        "http://testserver", transport=SyncASGITransport(env["app"])))
    flags = ["--mode", mode] + (["--api-url", "http://testserver"] if mode == "server" else [])
    before = list(env["store"].calibrations.iter_records())
    path = tmp_path / "selector.json"
    for value in UNADDRESSABLE_SELECTORS:
        if selector == "new_version":
            body = {"new_version": value, "expected_parent_sha256": parent["content_sha256"],
                    "reviews": env["reviews"]}
            command = ["review", "--id", env["id"], "--version", "imported"]
        else:
            body, command = selector_import(env, **{selector: value}), ["import"]
        path.write_text(json.dumps(body))
        assert cli.main(["judge-calibration", *command, "--file", str(path), *flags]) == 2, (selector, value)
        assert json.loads(capsys.readouterr().err)["error"]["code"] == "CALIBRATION_CONTRACT_INVALID"
        assert list(env["store"].calibrations.iter_records()) == before
        assert env["factory"].calls == env["provider"].calls == []
