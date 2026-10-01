"""Final-review boundary regressions. Synthetic data and loopback HTTP only."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import sys
import threading

import pytest


@pytest.fixture
def endpoint():
    posts = []
    responses = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.respond()

        def do_GET(self):
            self.respond()

        def respond(self):
            posts.append(self.path)
            status, headers, body = responses.pop(0) if responses else (404, {}, {"error": {"code": "NOT_FOUND", "message": "synthetic"}})
            data = json.dumps(body).encode()
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", posts, responses
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("kind", ["scenario", "skill"])
@pytest.mark.parametrize("redirect", [307, 308])
@pytest.mark.parametrize("retries", [0, 1])
def test_workflow_transport_bound_counts_actual_sends(endpoint, tmp_path, monkeypatch, kind, redirect, retries):
    from tests.sdk.test_experiment_scenario_skill import environment, suite_spec
    from apps.worker.motte_worker.runtime import WorkerLoop
    from apps.worker.motte_worker.reporting import WorkerReporter
    from motte_sdk.service import RunService

    url, posts, responses = endpoint
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("MOTTE_SCENARIO_FIXTURE_ROOT", str(tmp_path / "fixtures"))
    monkeypatch.setenv("OPENAI_API_KEY", "local-synthetic-only")
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    store, resources, service = environment()
    resources.providers.put({"name": "local", "kind": "openai_compatible", "base_url": url + "/v1",
                             "max_retries": retries})
    factors = {"model_profile": ["model-a"]}
    if kind == "skill":
        factors["skill_version"] = ["no-skill", "cancel-guard@1", "cancel-guard@2"]
    spec = suite_spec(kind, factors=factors, repeats=1, budget_policy={"max_total_calls": len(factors.get("skill_version", [1])) * (1 + retries)})
    spec["suite_config"].update(execution_budget={"max_steps": 1}, cases=["case-1"])
    preview = service.preview(spec)
    assert preview["violations"] == []
    created = service.create(spec, expected_preview_hash=preview["preview_hash"])
    assert created["failed"] == []
    for cell in created["cells"]:
        before = len(posts)
        responses.extend([(redirect, {"Location": "/resolved"}, {}), (200, {}, {
            "choices": [{"message": {"content": '{"action":"final","answer":"done"}'}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })])
        WorkerLoop(RunService(store), reporter=WorkerReporter(enabled=False)).claim_and_execute(cell["run_id"])
        assert len(posts) - before == 1
        assert len(posts) - before <= 1 + retries
        assert "/resolved" not in posts
        responses.clear()
        retry = service.retry_cell(cell["cell_id"], reason="software-only explicit retry")
        child = store.runs.get(retry["run_id"])
        assert child["manifest"]["provider_transport_policy"] == "bounded-http@1"
        assert child["requested_manifest"]["provider_transport_policy"] == "bounded-http@1"
    assert len(posts) <= preview["max_potential_calls"]


def test_six_package_remote_commands_dispatch_without_runtime(endpoint):
    url, posts, responses = endpoint
    from motte_sdk.client import _EXPECTED_API_VERSION
    for _ in range(2):
        responses.extend([(200, {}, {"api_version": _EXPECTED_API_VERSION}),
                          (404, {}, {"error": {"code": "NOT_FOUND", "message": "synthetic"}})])
    script = '''
import importlib.abc, sys
class SixOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'motte_provider', 'motte_agent', 'motte_harness', 'motte_benchmark', 'motte_scenario', 'motte_skill', 'motte_sandbox'}:
            raise ModuleNotFoundError('runtime package unavailable: ' + fullname)
sys.meta_path.insert(0, SixOnly())
from motte_sdk.comparisons import ComparisonService
from motte_cli.main import main
for argv in [['compare', '--baseline', 'a', '--candidate', 'b'], ['statistical-report', 'get', 'unknown']]:
    assert main(argv + ['--mode', 'server', '--api-url', sys.argv[1]]) == 2
'''
    result = subprocess.run([sys.executable, "-c", script, url], capture_output=True, text=True,
                            env={key: value for key, value in os.environ.items() if key.lower() != "all_proxy"})
    assert result.returncode == 0, result.stderr
    assert any("comparisons" in path for path in posts), posts
    assert any("statistical-reports/unknown" in path for path in posts), posts


@pytest.mark.parametrize("snapshot", [True, False])
@pytest.mark.parametrize("empty", [False, True])
def test_local_run_events_warns_after_real_retention(tmp_path, monkeypatch, capsys, snapshot, empty):
    from motte_cli.main import main
    from motte_storage.run_store import SQLiteRunStore
    from motte_storage.trace_retention import apply_trace_retention, plan_trace_retention
    from motte_storage.trace_retention_models import TraceRetentionConfig

    now = datetime(2026, 9, 30, tzinfo=UTC)
    database, root = tmp_path / "runs.db", tmp_path / "artifacts"
    root.mkdir()
    store = SQLiteRunStore(database)
    monkeypatch.setattr("motte_storage.trace_retention_models.utc_now", lambda: now - timedelta(days=20))
    store.runs.create({"id": "r", "status": "completed", "scenario_version": "replay@1", "manifest": {}, "case_ids": []})
    for _ in range(4):
        store.events.append({"run_id": "r", "type": "note"})
    config = TraceRetentionConfig(enabled=True, retention_days=7)
    monkeypatch.setattr("motte_storage.trace_retention_models.utc_now", lambda: now)
    apply_trace_retention(store, root, plan_trace_retention(store, config=config), config=config, confirm=True)
    if empty:
        # Query beyond the retained suffix: the authoritative history is still partial.
        after = 4
    else:
        after = 0
    argv = ["run-events", "r", "--db", str(database), "--mode", "local", "--after", str(after)]
    assert main(argv + (["--snapshot"] if snapshot else [])) == 0
    output = capsys.readouterr()
    assert "TRACE_HISTORY_PARTIAL" in output.err
    assert '"partial": true' in output.err
    assert '"trimmed_through": 3' in output.err
    rows = json.loads(output.out) if snapshot else [json.loads(row) for row in output.out.splitlines()]
    assert rows == ([] if empty else [{"run_id": "r", "seq": 4, "type": "note"}])


@pytest.mark.parametrize("suffix", ["/preview", ""])
def test_invalid_typed_suite_is_422_with_zero_objects(suffix):
    from fastapi.testclient import TestClient
    from apps.api.app.main import create_app
    from tests.sdk.test_experiment_scenario_skill import environment, suite_spec, assert_empty

    store, resources, _ = environment()
    client = TestClient(create_app(store, resource_store=resources), raise_server_exceptions=False)
    payload = suite_spec("scenario")
    payload["suite_config"]["arbitrary_manifest"] = {}
    response = client.post("/api/v1/experiments" + suffix, json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "EXPERIMENT_INVALID"
    assert_empty(store)


def test_experiment_openapi_exposes_typed_request():
    from apps.api.app.main import create_app
    schemas = create_app().openapi()["components"]["schemas"]
    assert "ScenarioExperimentConfig" in schemas
    assert "SkillExperimentConfig" in schemas
    assert "CevalExperimentConfig" in schemas


@pytest.mark.parametrize("typed", [True, False])
@pytest.mark.parametrize("wrong", [True, False])
def test_pairwise_nested_reference_normalized_before_binding(typed, wrong):
    from tests.evaluators.test_pairwise_quality_gate import snapshot, full_evaluate
    from motte_contracts.comparison import RunReportRef
    candidate = snapshot()
    ref = RunReportRef(run_id="wrong" if wrong else "run-1", scoring_pass_id="pass-1", report_schema="report-pairwise-v1", evidence_hash="sha256:" + "1" * 64)
    candidate["ref"] = ref if typed else ref.model_dump()
    assert full_evaluate(candidate).decision.value == ("insufficient_evidence" if wrong else "pass")


@pytest.mark.parametrize("reported", ["different-unqualified-model", None])
def test_calibration_requires_actual_model_identity(tmp_path, reported):
    from tests.sdk.test_calibration_ledger import completed_environment
    from apps.worker.motte_worker.runtime import WorkerLoop
    from motte_sdk.service import RunService
    service, scoring, provider, _, _, execution = completed_environment(tmp_path, execute=False)
    original = provider.transport.post_json_detailed
    def substituted(path, body):
        outcome = original(path, body)
        if reported is None:
            outcome.response_body.pop("model")
        else:
            outcome.response_body["model"] = reported
        return outcome
    provider.transport.post_json_detailed = substituted
    worker = WorkerLoop(RunService(service.store), scoring_jobs=scoring)
    for _ in execution.child_job_ids:
        assert worker.claim_and_execute()["status"] == "completed"
    report = service.publish_report(execution.execution_id)
    assert not report.report.qualified
    assert not report.report.gate_eligible


@pytest.mark.parametrize("redirect", [307, 308])
@pytest.mark.parametrize("bounded", [True, False])
def test_http_retry_composition_keeps_redirects_inside_send_budget(endpoint, redirect, bounded):
    from motte_provider.transport import HTTPTransport
    from motte_provider.errors import ProviderHTTPError

    url, posts, responses = endpoint
    responses.extend([(429, {"Retry-After": "0"}, {}),
                      (redirect, {"Location": "/resolved"}, {}), (200, {}, {"ok": True})])
    transport = HTTPTransport(url, max_retries=1, follow_redirects=not bounded,
                              default_headers={"Idempotency-Key": "software-only"})
    if bounded:
        with pytest.raises(ProviderHTTPError) as raised:
            transport.post_json_detailed("/model", {})
        assert raised.value.outcome.attempts == 2
        assert posts == ["/model", "/model"]
    else:
        assert transport.post_json_detailed("/model", {}).response_body == {"ok": True}
        assert posts == ["/model", "/model", "/resolved"]


@pytest.mark.parametrize("snapshot", [True, False])
@pytest.mark.parametrize("empty", [True, False])
def test_server_run_events_partial_is_sticky_across_pages(endpoint, capsys, monkeypatch, snapshot, empty):
    from motte_cli.main import main
    from motte_sdk.client import _EXPECTED_API_VERSION

    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    url, posts, responses = endpoint
    responses.append((200, {}, {"api_version": _EXPECTED_API_VERSION, "features": {"events_snapshot": True}}))
    first = [] if empty else [{"run_id": "r", "seq": 4, "type": "note"}]
    responses.append((200, {}, {"events": first, "partial": True, "trimmed_through": 3,
                                "has_more": not empty, "next_after": 4, "last_seq": None if empty else 4}))
    if not empty:
        responses.append((200, {}, {"events": [{"run_id": "r", "seq": 5, "type": "note"}], "partial": False,
                                    "has_more": False, "last_seq": 5}))
    assert main(["run-events", "r", "--mode", "server", "--api-url", url] + (["--snapshot"] if snapshot else [])) == 0
    output = capsys.readouterr()
    assert output.err.count("TRACE_HISTORY_PARTIAL") == 1
    metadata = json.loads(output.err)
    assert metadata["partial"] and metadata["trimmed_through"] == 3
    rows = json.loads(output.out) if snapshot else [json.loads(row) for row in output.out.splitlines()]
    assert [row["seq"] for row in rows] == ([] if empty else [4, 5])
    if not empty:
        assert "after=4" in posts[-1]


@pytest.mark.parametrize("alias", ["request_key", "_request_key", "both"])
def test_typed_experiment_transport_controls_do_not_change_spec_or_preview(alias):
    from fastapi.testclient import TestClient
    from apps.api.app.main import create_app
    from tests.sdk.test_experiment_scenario_skill import environment, suite_spec

    store, resources, service = environment()
    client = TestClient(create_app(store, resource_store=resources))
    payload = suite_spec("scenario")
    plain = service.preview(payload)
    controls = {"_preview_hash": plain["preview_hash"], alias: "same-key"} if alias != "both" else {
        "_preview_hash": plain["preview_hash"], "_request_key": "same-key", "request_key": "ignored-key",
    }
    preview = client.post("/api/v1/experiments/preview", json={**payload, **controls})
    assert preview.status_code == 200, preview.text
    assert preview.json()["preview_hash"] == plain["preview_hash"]
    response = client.post("/api/v1/experiments", json={**payload, **controls})
    assert response.status_code == 202, response.text
    saved = store.experiments.list_specs()[0]
    assert not {"request_key", "_request_key", "_preview_hash", "preview_hash"}.intersection(saved)
    replay = client.post("/api/v1/experiments", json={**payload, "request_key": "same-key"})
    assert replay.status_code == 202, replay.text
    assert replay.json()["cells"] == response.json()["cells"]
    changed = client.post("/api/v1/experiments", json={**payload, "experiment_id": "changed", "request_key": "same-key"})
    assert changed.status_code == 409


@pytest.mark.parametrize("mode", ["local", "server"])
@pytest.mark.parametrize("command", ["preview", "create"])
def test_cli_typed_invalid_suite_is_safe_and_zero_write(monkeypatch, capsys, mode, command):
    from fastapi.testclient import TestClient
    from apps.api.app.main import create_app
    from motte_cli.main import main
    from motte_sdk import MotteClient
    from tests.sdk.test_experiment_scenario_skill import environment, suite_spec, assert_empty
    import importlib

    module = importlib.import_module("motte_cli.main")
    store, resources, service = environment()
    payload = suite_spec("scenario")
    payload["suite_config"]["arbitrary_manifest"] = {}
    monkeypatch.setattr(module, "_experiment_service", lambda args: service)
    http = TestClient(create_app(store, resource_store=resources))
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    monkeypatch.setattr(module.remote, "build_client", lambda args: MotteClient("http://testserver", transport=http._transport))
    assert main(["experiment", command, "--spec", json.dumps(payload), "--mode", mode] + (["--api-url", "http://testserver"] if mode == "server" else [])) == 2
    output = capsys.readouterr()
    assert "EXPERIMENT_INVALID" in output.out + output.err
    assert_empty(store)


def test_unbounded_legacy_manifest_does_not_gain_transport_identity():
    from motte_contracts.run import ResolvedManifest
    manifest = ResolvedManifest(execution={"backend_id": "scenario", "backend_version": "1"})
    assert "provider_transport_policy" not in manifest.model_dump()


@pytest.mark.parametrize("suite", ["agent-tasks", "direct-llm", "gsm8k"])
@pytest.mark.parametrize("redirect", [307, 308])
def test_all_admitted_native_experiments_enforce_transport_policy(endpoint, tmp_path, monkeypatch, suite, redirect):
    from apps.worker.motte_worker.runtime import WorkerLoop
    from apps.worker.motte_worker.reporting import WorkerReporter
    from motte_sdk.service import RunService
    from motte_sdk.resolve import prepare_run
    from tests.sdk.test_m8_native_agent_experiments import native_agent_environment, native_agent_spec
    from tests.sdk.test_m6_experiments import make_service, spec_payload

    url, posts, responses = endpoint
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("OPENAI_API_KEY", "local-synthetic-only")
    if suite == "agent-tasks":
        store, resources, service = native_agent_environment()
        spec = native_agent_spec(factors={"model_profile": ["agent-a"]}, selected_case_keys=["task-1"])
    else:
        store, _, service = make_service()
        resources = service.resources
        spec = spec_payload(factors={"model_profile": ["model-a"]}, repeats=1)
        if suite == "gsm8k":
            from motte_sdk.benchmark import import_benchmark_split
            raw = "\n".join(json.dumps({"question": f"Q{i}", "answer": f"work\n#### {i}"}) for i in range(25)).encode()
            scenario = import_benchmark_split(raw, name="synthetic-gsm", version="1", revision="synthetic", license_id="internal-sample", scope="smoke", resources=resources, synthetic=True)["scenario"]
            spec.update(task_ref={"suite": suite, "scenario_version": scenario}, controlled_conditions={"max_output_tokens": 1024})
        _, cases = prepare_run(spec["task_ref"]["scenario_version"], {"model": "model-a"}, [], resources)
        spec["selected_case_keys"] = cases[:1]
    resources.providers.put({**resources.providers.get("local"), "base_url": url + "/v1"})
    preview = service.preview(spec)
    assert preview["violations"] == []
    created = service.create(spec, expected_preview_hash=preview["preview_hash"])
    assert created["failed"] == []
    responses.extend([(redirect, {"Location": "/resolved"}, {}), (200, {}, {
        "choices": [{"message": {"content": '{"action":"final","answer":"done"}'}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })])
    run = store.runs.get(created["cells"][0]["run_id"])
    WorkerLoop(RunService(store), reporter=WorkerReporter(enabled=False)).claim_and_execute(run["id"])
    assert posts and "/resolved" not in posts
    assert len(posts) <= preview["max_potential_calls"]
    assert run["manifest"]["provider_transport_policy"] == "bounded-http@1"
    retry = service.retry_cell(created["cells"][0]["cell_id"], reason="software-only explicit retry")
    child = store.runs.get(retry["run_id"])
    assert child["manifest"]["provider_transport_policy"] == "bounded-http@1"
    assert child["requested_manifest"]["provider_transport_policy"] == "bounded-http@1"
