"""Complete offline public lifecycle with reopened durable stores and existing Worker."""
import json
from fastapi.testclient import TestClient
from apps.api.app.main import create_app
from apps.worker.motte_worker.runtime import WorkerLoop
from motte_sdk.scoring_jobs import ScoringJobService
from motte_sdk.service import RunService
from tests.judge_calibration_fixtures import (
    ROOT, attach_scripted_adapter, execute_body, import_review,
)
from tests.judge_calibration_fixtures import lifecycle_env  # noqa: F401
from tests.sdk.test_calibration_gate_binding import save_attempts
from tests.sdk.test_m6_comparison_service import gate_policy_payload


def test_public_lifecycle_restarts_and_exact_qualification(lifecycle_env, monkeypatch, capsys, tmp_path, request):  # noqa: F811
    env = lifecycle_env
    _, version, _ = import_review(env)
    prefix = f"{ROOT}/{env['id']}"
    body = execute_body(env, version)
    preview = env["client"].post(prefix + "/versions/reviewed/preflight", json=body)
    assert preview.status_code == 200, preview.text
    body["request"]["expected_preflight_sha256"] = preview.json()["preflight_sha256"]
    submitted = env["client"].post(prefix + "/versions/reviewed/jobs", json=body)
    assert submitted.status_code == 202, submitted.text
    job = submitted.json()
    assert env["factory"].calls == env["provider"].calls == []
    execution = env["store"].calibrations.get_execution(job["execution_id"])
    provider = attach_scripted_adapter(env, execution)
    for _ in job["child_job_ids"]:
        store = env["reopen"]()
        scoring = ScoringJobService(store, provider_factory=env["factory"])
        assert WorkerLoop(RunService(store), scoring_jobs=scoring).claim_and_execute()["status"] == "completed"
    assert len(provider.calls) == 120
    calibration_factory_calls = len(env["factory"].calls)
    store = env["reopen"]()
    client = TestClient(create_app(store, resource_store=env["resources"], judge_provider_factory=env["factory"]))
    before = list(store.calibrations.iter_records())
    assert client.get(prefix + "/jobs/" + job["execution_id"]).status_code == 200
    assert client.get(prefix + "/jobs/" + job["execution_id"] + "/reports").json()["total"] == 0
    assert list(store.calibrations.iter_records()) == before
    from motte_sdk import MotteClient
    from tests.sdk.sync_asgi import SyncASGITransport
    with MotteClient("http://testserver", transport=SyncASGITransport(client.app)) as sdk:
        published = sdk.publish_judge_calibration_report(env["id"], job["execution_id"])
        report = published.raw
        assert sdk.get_judge_calibration_report(env["id"], published.report_id).raw == report
        assert sdk.get_judge_qualification(env["id"], published.qualification.qualification_id).binding == published.qualification.binding
    assert report["qualification"]["qualification"]["gate_eligible"]
    assert report["report"]["report"]["missing_evidence_rate"] == .2
    binding = report["qualification"]["binding"]
    assert client.post(prefix + "/jobs/" + job["execution_id"] + "/reports").json() == report
    for path in [prefix + "/reports/" + report["report"]["report_id"],
                 prefix + "/qualifications/" + binding["qualification_id"]]:
        assert client.get(path).status_code == 200
        assert client.get(path.replace(env["id"], "other", 1)).status_code == 404
    assert len(provider.calls) == 120
    assert len(env["factory"].calls) == calibration_factory_calls
    run_id, pair = save_attempts(store)
    subject = {"run_id": run_id, "mode": "pairwise", "pairwise_refs": [pair],
               "spec": env["request"]["spec_request"], "request_key": "public-subject",
               "qualification_id": binding["qualification_id"], "price_table_version": "price-1",
               "authorisation": {key: value for key, value in env["request"]["authorisation"].items()
                                 if key not in {"purpose", "authorised_at"}}}
    from motte_cli import main as cli
    monkeypatch.setattr(cli, "_service", lambda args: RunService(store))
    monkeypatch.setattr(cli, "_resources", lambda args: env["resources"])
    subject_file = tmp_path / "subject.json"
    subject_file.write_text(json.dumps(subject))
    assert cli.main(["judge", "submit", "--spec", "@" + str(subject_file)]) == 0
    local_subject = json.loads(capsys.readouterr().out)
    response = client.post("/api/v1/judges", json=subject)
    assert response.json()["job_id"] == local_subject["job_id"]
    assert response.status_code == 202, response.text
    provider.transport.responses.append(json.dumps({"winner": "tie", "criteria": [
        {"criterion_id": key, "preference": "tie", "reason": "software-only", "evidence": ["event:1", "event:2"]}
        for key in execution.spec.criteria]}))
    assert WorkerLoop(RunService(store), scoring_jobs=ScoringJobService(store, provider_factory=env["factory"])).claim_and_execute()["status"] == "completed"
    subject_factory_calls = len(env["factory"].calls)
    pass_id = response.json()["reserved_pass_id"]
    store = env["reopen"]()
    client = TestClient(create_app(store, resource_store=env["resources"], judge_provider_factory=env["factory"]))
    lite = client.post("/api/v1/gates", json={"run_id": run_id, "scoring_pass_id": pass_id,
                                             "policy": {"metric": "accuracy", "threshold": 0}})
    assert lite.status_code == 200, lite.text
    assert not lite.json()["passed"]
    assert next(rule for rule in lite.json()["rules"] if rule["id"] == "judge_qualification")["passed"]
    assert any(rule["id"] == "pairwise_quality_unavailable" for rule in lite.json()["rules"])
    assert client.post("/api/v1/gate-policies", json=gate_policy_payload()).status_code in {200, 201}
    full = client.post("/api/v1/gates/versioned", json={"run_id": run_id, "scoring_pass_id": pass_id,
                                                       "policy_id": "pol-m6", "policy_version": "1"})
    assert full.status_code == 200, full.text
    assert full.json()["decision"] == "insufficient_evidence"
    assert full.json()["judge_qualification"]["candidate"]["binding"] == binding
    assert len(provider.calls) == 121
    # Explicit pairwise quality is separate from the still-unavailable Boolean metric.
    from tests.evaluators.test_pairwise_quality_gate import lite as quality_lite, policy, rule
    from motte_sdk.comparisons import ComparisonService
    service = ComparisonService(store)
    selected_report = service.report_snapshot(run_id, scoring_pass_id=pass_id)
    assert selected_report.pairwise_quality.value == .5
    assert selected_report.pairwise_quality.planned_pairs == 1
    assert selected_report.pairwise_quality.coverage == 1.
    assert selected_report.metric_values["accuracy"] is None
    assert all(row["passed"] is None for row in store.score_sets.list_for_pass(pass_id))
    quality_results = []
    monkeypatch.setattr(cli, "_comparison_service", lambda args: ComparisonService(store))
    monkeypatch.setattr(cli.remote, "build_client", lambda args: MotteClient(
        "http://testserver", transport=SyncASGITransport(client.app)))
    for threshold, decision, code in ((.4, "pass", 0), (.6, "quality_fail", 1)):
        policy_id = "quality-" + str(threshold)
        payload = policy([rule(threshold=threshold)], policy_id=policy_id)
        assert client.post("/api/v1/gate-policies", json=payload).status_code == 201
        lite_quality = client.post("/api/v1/gates", json={"run_id": run_id, "scoring_pass_id": pass_id,
                                                        "policy": quality_lite(threshold=threshold)})
        assert lite_quality.status_code == 200, lite_quality.text
        assert lite_quality.json()["passed"] is (code == 0)
        assert lite_quality.json()["schema"] == "gate-lite@3"
        full_quality = client.post("/api/v1/gates/versioned", json={"run_id": run_id,
            "scoring_pass_id": pass_id, "policy_id": policy_id, "policy_version": "1"})
        assert full_quality.status_code == 200, full_quality.text
        result = full_quality.json()
        assert result["decision"] == decision and result["exit_code"] == code
        quality_results.append(result)
        assert result["judge_qualification"]["candidate"]["binding"] == binding
        with MotteClient("http://testserver", transport=SyncASGITransport(client.app)) as sdk:
            assert sdk.evaluate_gate_versioned(run_id, policy_id, "1", scoring_pass_id=pass_id).raw == result
            assert sdk.get_gate_result(result["gate_result_id"]).raw == {key: value for key, value in result.items() if key != "exit_code"}
        for mode in ("local", "server"):
            flags = ["--mode", mode] + (["--api-url", "http://testserver"] if mode == "server" else [])
            assert cli.main(["gate", "evaluate", "--policy", policy_id+"@1", "--run", run_id,
                             "--pass", pass_id, *flags]) == code
            assert json.loads(capsys.readouterr().out) == result
    assert quality_results[0]["gate_result_id"] != quality_results[1]["gate_result_id"]
    assert len(provider.calls) == 121
    # Manual descendants retain only the exact source qualification, never edited Boolean quality.
    from motte_eval.calibration import ManualRevisionRequest, build_manual_revision
    source = store.scoring_passes.get(pass_id)
    revision = build_manual_revision(source, ManualRevisionRequest(
        run_id=run_id, source_pass_id=pass_id, actor="software-only-operator",
        reason="Protocol fixture; not actual human review", expected_current_pass_id=pass_id,
        changes=[{"case_id": row["case_id"], "metric_id": row["metric_id"], "passed": True, "value": 1.0}
                 for row in source["scores"]]), revision_id="public-manual", created_at=source["created_at"],
        current_pass_id=pass_id)
    store.scoring_passes.append(revision["pass"], revision["scores"])
    manual = client.post("/api/v1/gates", json={"run_id": run_id, "scoring_pass_id": "public-manual",
                                               "policy": {"metric": "accuracy", "threshold": 0}})
    assert manual.status_code == 200, manual.text
    assert not manual.json()["passed"]
    assert next(rule for rule in manual.json()["rules"] if rule["id"] == "judge_qualification")["passed"]
    assert manual.json()["coverage_summary"]["cost"]["per_success_usd"] is None
    full_manual = client.post("/api/v1/gates/versioned", json={"run_id": run_id, "scoring_pass_id": "public-manual",
                                                              "policy_id": "pol-m6", "policy_version": "1"})
    assert full_manual.status_code == 200, full_manual.text
    assert full_manual.json()["decision"] == "insufficient_evidence"
    assert full_manual.json()["judge_qualification"]["candidate"]["binding"] == binding
    manual_quality = client.post("/api/v1/gates/versioned", json={"run_id": run_id,
        "scoring_pass_id": "public-manual", "policy_id": "quality-0.4", "policy_version": "1"})
    assert manual_quality.status_code == 200, manual_quality.text
    assert manual_quality.json()["decision"] == "insufficient_evidence"
    assert "manual_pairwise_quality_unsupported" in json.dumps(manual_quality.json())
    from copy import deepcopy
    drift = deepcopy(subject)
    drift["request_key"] = "changed-spec-subject"
    drift["spec"]["parameters"]["temperature"] = .4
    rejected = client.post("/api/v1/judges", json=drift)
    assert rejected.status_code == 422, rejected.text
    # Both CLI modes read already-published report/qualification sources without a provider call.
    monkeypatch.setattr(cli, "_service", lambda args: RunService(store))
    monkeypatch.setattr(cli.remote, "build_client", lambda args: MotteClient(
        "http://testserver", transport=SyncASGITransport(client.app)))
    for mode in ("local", "server"):
        flags = ["--mode", mode] + (["--api-url", "http://testserver"] if mode == "server" else [])
        assert cli.main(["judge-calibration", "report", "--id", env["id"], "--job", job["execution_id"],
                         "--publish", *flags]) == 0
        assert json.loads(capsys.readouterr().out) == report
        for command, selector, identifier in [
            ("report", "--report", report["report"]["report_id"]),
            ("qualification", "--qualification", binding["qualification_id"]),
        ]:
            assert cli.main(["judge-calibration", command, "--id", env["id"], selector, identifier, *flags]) == 0
            output = json.loads(capsys.readouterr().out)
            assert output == (report if command == "report" else report["qualification"])
    assert len(provider.calls) == 121
    # Exact durable replay does not consult mutable resources after restart.
    monkeypatch.setattr(env["resources"].models, "get", lambda *args: (_ for _ in ()).throw(AssertionError("resource lookup")))
    assert client.post(prefix + "/versions/reviewed/jobs", json=body).status_code == 202
    assert len(provider.calls) == 121

    assert len(env["factory"].calls) == subject_factory_calls

    # Missing one actual subject Invocation cannot reuse an earlier passing ID.
    subject_job_id = response.json()["job_id"]
    list_job, list_run = store.invocations.list_for_job, store.invocations.list_for_run
    with monkeypatch.context() as damaged:
        damaged.setattr(store.invocations, "list_for_job", lambda identity:
                        [] if identity == subject_job_id else list_job(identity))
        damaged.setattr(store.invocations, "list_for_run", lambda identity:
                        [] if identity == run_id else list_run(identity))
        for threshold in (.4, .6):
            result = client.post("/api/v1/gates/versioned", json={"run_id": run_id,
                "scoring_pass_id": pass_id, "policy_id": "quality-"+str(threshold), "policy_version": "1"})
            assert result.status_code == 200, result.text
            assert result.json()["decision"] == "insufficient_evidence" and result.json()["exit_code"] == 5
            assert result.json()["gate_result_id"] not in {row["gate_result_id"] for row in quality_results}
            lite_missing = client.post("/api/v1/gates", json={"run_id": run_id, "scoring_pass_id": pass_id,
                "policy": quality_lite(threshold=threshold)})
            assert lite_missing.status_code == 200 and not lite_missing.json()["passed"]
            assert lite_missing.json()["pairwise_quality"]["planned_pairs"] == 1
            assert lite_missing.json()["pairwise_quality"]["value"] is None
    store = env["reopen"]()
    assert ComparisonService(store).report_snapshot(run_id, scoring_pass_id=pass_id).model_dump() == selected_report.model_dump()
    for row in quality_results:
        assert store.gate_store.get_result(row["gate_result_id"]) == {key: value for key, value in row.items() if key != "exit_code"}
    # Only persistent stores have backup/restore; Memory proves deterministic reopening above.
    from motte_storage.maintenance import (consistent_backup, restore_staging,
        consistent_backup_postgres, restore_postgres_staging)
    if getattr(store, "dsn", None):
        from motte_storage.factory import create_run_store
        target_dsn = request.getfixturevalue("disposable_pg_pair")[1]
        root = tmp_path / "empty-artifacts"
        root.mkdir()
        consistent_backup_postgres(store.dsn, tmp_path / "quality-backup", artifacts_root=root, store=store)
        restore_postgres_staging(tmp_path / "quality-backup", target_dsn, tmp_path / "quality-restored-artifacts")
        recovered = create_run_store(storage="postgres", dsn=target_dsn)
    elif getattr(store.runs, "_path", None):
        from motte_storage.run_store import SQLiteRunStore
        root = tmp_path / "empty-artifacts"
        root.mkdir()
        consistent_backup(store, tmp_path / "quality-backup", artifacts_root=root)
        restored = restore_staging(tmp_path / "quality-backup", tmp_path / "quality-restore")
        recovered = SQLiteRunStore(restored["database"])
    else:
        recovered = store
    assert ComparisonService(recovered).report_snapshot(run_id, scoring_pass_id=pass_id).model_dump() == selected_report.model_dump()
    assert ComparisonService(recovered)._judge_gate_qualification(pass_id)["binding"] == binding
    for row in quality_results:
        assert recovered.gate_store.get_result(row["gate_result_id"]) == {key: value for key, value in row.items() if key != "exit_code"}
    assert len(provider.calls) == 121 and len(env["factory"].calls) == subject_factory_calls


from tests.integration.test_m8_pg_restore import disposable_pg_pair  # noqa: E402, F401
