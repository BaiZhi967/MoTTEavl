"""Small real-path coding witnesses for the separately guarded Go live harness.

Nothing runs on import. The caller owns live authorisation, credential resolution,
the HTTP endpoint/model allowlist, request counter, deadline and redaction. These
functions only receive a non-secret provider configuration and an isolated root.
They never inspect credentials and never execute model-generated Python.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import UTC, datetime
import json
import io
import os
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from apps.worker.motte_worker.reporting import WorkerReporter
from apps.worker.motte_worker.runtime import WorkerLoop
from motte_sdk import MotteClient
from motte_storage.resource_store import SQLiteResourceStore
from motte_storage.run_store import SQLiteRunStore


CALL_CAPS = {"check_api_model": 1, "check_queued_direct": 1,
             "check_native_agent": 6, "check_scenario_multiturn": 4}
MODEL_ID = "integration-bunny"
SOLUTION = "def add(a, b):\n    return a + b\n"
CODING_PROMPT = (
    "Fix this Python function: def add(a, b): return a - b. "
    "Reply with only the corrected return statement, using spaces around +."
)


def _require(condition, code):
    if not condition:
        raise RuntimeError(code)


def _json(response, status=200):
    _require(response.status_code == status, f"api_status_{response.status_code}")
    return response.json()


@contextmanager
def _environment(ctx, label):
    root = Path(ctx.root) / label
    root.mkdir(parents=True, exist_ok=True)
    changes = {
        "MOTTE_DB_PATH": str(root / "runs.db"),
        "MOTTE_STORAGE": "sqlite",
        "MOTTE_DATASET_DIR": str(root / "datasets"),
        "MOTTE_SKILL_CONTENT_ROOT": str(root / "skill-content"),
        "ARTIFACT_ROOT": str(root / "artifacts"),
        "MOTTE_AGENT_WORKSPACE_ROOT": str(root / "workspaces"),
        "MOTTE_SCENARIO_FIXTURE_ROOT": str(root / "fixtures"),
    }
    previous = {key: os.environ.get(key) for key in changes}
    os.environ.update(changes)
    try:
        path = root / "runs.db"
        yield path
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _app(path):
    # main.py creates a default app at import; import only inside _environment.
    from apps.api.app.main import create_app

    return create_app(SQLiteRunStore(path), SQLiteResourceStore(path))


def _seed(client, ctx):
    config = ctx.provider_config()
    connection_keys = ("kind", "base_url", "credentials", "api_key_env",
                       "request_path", "timeout", "max_retries")
    connection = {key: config[key] for key in connection_keys if key in config}
    connection.update(name="integration-go", timeout=20, max_retries=0)
    response = client.post("/api/v1/providers", json=connection)
    _require(response.status_code in (200, 201), "provider_create_failed")
    response = client.post("/api/v1/models", json={
        "id": MODEL_ID, "provider": "integration-go", "model": config["model"],
        "capabilities": {"text": True}, "supports_tools": True,
        "max_output_tokens": 512, "identity_policy": "require_match",
        "parameters": {"max_output_tokens": 512},
    })
    _require(response.status_code in (200, 201), "model_create_failed")
    _json(client.post(f"/api/v1/models/{MODEL_ID}/publish"))


class _ApplicationTransport(httpx.BaseTransport):
    """Drive the actual ASGI API through TestClient's public request method."""

    def __init__(self, client):
        self.client = client

    def handle_request(self, request):
        response = self.client.request(request.method, str(request.url),
                                       headers=request.headers, content=request.read())
        return httpx.Response(response.status_code, headers=response.headers,
                              content=response.content, request=request)


def _worker(app, run_id):
    result = WorkerLoop(app.state.run_service,
                        reporter=WorkerReporter(enabled=False)).claim_and_execute(run_id)
    _require(result is not None and result["status"] == "completed", "worker_not_completed")
    return result


def _reports(client, run_id):
    """SDK and API share the same persisted run; rescore cannot dispatch a model."""
    with MotteClient("http://testserver", transport=_ApplicationTransport(client), retries=0) as sdk:
        before = sdk.get_run(run_id).raw
        _require(before["status"] == "completed", "report_run_not_completed")
        first = sdk.run_report(run_id).raw
        api = _json(client.get(f"/api/v1/runs/{run_id}/report"))
        def facts(report):
            return {key: value for key, value in report.items() if key != "generated_at"}

        _require(facts(first) == facts(api), "sdk_api_report_mismatch")
        prior_pass = before["current_scoring_pass_id"]
        after = sdk.rescore_run(run_id).raw
        _require(after["current_scoring_pass_id"] != prior_pass, "rescore_not_new_pass")
        historical = sdk.run_report(run_id, scoring_pass_id=prior_pass).raw
        _require(facts(historical) == facts(first), "historical_report_changed")
        events = sdk.run_events_snapshot(run_id)
        _require(bool(events.events), "events_missing")
        waited = sdk.wait_for_run(run_id, timeout=5, poll_interval=0.05)
        _require(waited["status"] == "completed", "wait_not_terminal")
        streamed = list(sdk.stream_events(run_id, deadline=5))
        _require(bool(streamed), "sse_events_missing")
        state = streamed[-1][1]
        _require(not state.partial and not state.reconciliation_mismatch
                 and state.run_status == "completed" and state.report is not None,
                 "sse_terminal_reconciliation_failed")
        _require([event["seq"] for event, _ in streamed]
                 == [event["seq"] for event in events.events], "sse_snapshot_mismatch")
        from motte_cli.main import main as cli_main

        captured, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(captured), redirect_stderr(errors):
            code = cli_main(["run-report", run_id, "--mode", "local",
                             "--db", os.environ["MOTTE_DB_PATH"],
                             "--scoring-pass-id", prior_pass])
        _require(code == 0, "cli_report_failed")
        _require(facts(json.loads(captured.getvalue())) == facts(first), "cli_report_mismatch")
    return {"sdk_api_report_equal": True, "historical_report_immutable": True,
            "rescore_new_pass": True, "score_count": len(after.get("scores") or []),
            "wait_terminal": waited["status"], "sse_reconciled": True,
            "sse_event_count": len(streamed), "cli_report_equal": True}


def check_api_model(ctx):
    """One synchronous provider test, with coding content and 16 output tokens."""
    with _environment(ctx, "api-model") as path, TestClient(_app(path)) as client:
        _seed(client, ctx)
        report = _json(client.post(f"/api/v1/models/{MODEL_ID}/test",
                                   json={"prompt": CODING_PROMPT}))
        _require(report.get("ok") is True, "api_model_failed")
        return {"ok": True, "endpoint": "models/test", "output_token_cap": 16,
                "attempts": report.get("attempts"), "usage": report.get("usage")}


def check_queued_direct(ctx):
    """One coding call, durable queue reopen, SDK report/rescore and offline replay."""
    with _environment(ctx, "queued-direct") as path:
        with TestClient(_app(path)) as client:
            _seed(client, ctx)
            run = _json(client.post("/api/v1/runs", json={
                "scenario_version": "direct-llm@1", "case_ids": ["fix-add"],
                "manifest": {"model": MODEL_ID, "provider_transport_policy": "bounded-http@1",
                             "cases": {"fix-add": {"prompt": CODING_PROMPT,
                                                   "expected": "return a + b"}}},
            }), 202)
            _require(run["status"] == "queued", "not_queued")
        app = _app(path)
        with TestClient(app) as client:
            _require(_json(client.get(f"/api/v1/runs/{run['id']}"))["status"] == "queued",
                     "reopen_lost_queue")
            result = _worker(app, run["id"])
            _require(all(score.get("passed") is True for score in result["scores"]),
                     "coding_answer_mismatch")
            evidence = _reports(client, run["id"])
            saved_output = result["cases"][0]["result"]["content"]
            replay = _json(client.post("/api/v1/runs", json={
                "scenario_version": "replay@1", "case_ids": ["fix-add"],
                "manifest": {"replay_fixture": {"fix-add": {
                    "output": saved_output, "expected": "return a + b"}}},
            }), 202)
            replay_result = _worker(app, replay["id"])
            _require(replay_result["scores"][0]["passed"] is True, "replay_failed")
            return {"ok": True, "run_id": run["id"], "reopened_queued": True,
                    "replay_completed": True, "replay_source": "persisted_live_result",
                    **evidence}


def check_native_agent(ctx):
    """At most six model steps; synthetic file read/write, never code execution."""
    with _environment(ctx, "native-agent") as path:
        with TestClient(_app(path)) as client:
            _seed(client, ctx)
            imported = _json(client.post("/api/v1/agent-tasks/import", json={
                "name": "integration-add", "content": json.dumps([{
                    "case_id": "fix-add", "input": (
                        "Read solution.py, fix add so it adds a and b, and write the corrected "
                        "function back to solution.py. Preserve the function name and arguments. "
                        "Use the read_file and write_file tools. Do not run the code."),
                    "fixture": {"solution.py": "def add(a, b):\n    return a - b\n"},
                    "expected": {"files": {"solution.py": {
                        "mode": "contains", "expected": "return"}}},
                }]),
            }), 201)
            request = {"scenario": imported["scenario"], "model": MODEL_ID,
                       "mode": "native-tool", "budget": {
                           "max_steps": 6, "max_tool_calls": 4, "wall_time_sec": 60,
                           "per_call_timeout_sec": 20}}
            dry = _json(client.post("/api/v1/agent-tasks/runs/dry-run", json=request))
            _require(dry["selected_cases"] == 1, "unexpected_case_count")
            run = _json(client.post("/api/v1/agent-tasks/runs", json=request), 202)
        app = _app(path)
        with TestClient(app) as client:
            _worker(app, run["id"])
            prefix = f"/api/v1/runs/{run['id']}/cases/fix-add"
            detail = _json(client.get(prefix + "/agent"))
            _require(detail["agent"]["termination_reason"] == "final_answer",
                     "agent_not_final")
            artifact = _json(client.get(prefix + "/artifacts/content",
                                         params={"path": "solution.py"}))
            _require(artifact.get("sha256_matches") is True, "artifact_hash_mismatch")
            try:
                ast_match = ast.dump(ast.parse(artifact["content"])) == ast.dump(ast.parse(SOLUTION))
            except (SyntaxError, TypeError):
                ast_match = False
            _require(ast_match, "artifact_ast_mismatch")
            tools = sorted({event.get("tool") or event.get("tool_name")
                            for event in detail["events"] if event.get("type") == "tool_call"})
            _require({"read_file", "write_file"}.issubset(tools), "required_tools_missing")
            invocations = _json(client.get(f"/api/v1/runs/{run['id']}/invocations"))
            _require(invocations["total"] > 0, "invocations_missing")
            return {"ok": True, "run_id": run["id"], "artifact_ast_match": True,
                    "tools": tools, "invocations": invocations["total"],
                    **_reports(client, run["id"])}


def check_scenario_multiturn(ctx):
    """Two coding turns on one built-in Target session, no fixture or business actions."""
    with _environment(ctx, "scenario") as path:
        with TestClient(_app(path)) as client:
            _seed(client, ctx)
            _json(client.post("/api/v1/workflows", json={
                "workflow_id": "integration-code-review", "version": "1",
                "published_at": datetime.now(UTC).isoformat(),
                "target_requirements": {"multi_turn": True, "min_turns": 2},
                "steps": [{"step_id": "review", "kind": "send_message",
                           "message": CODING_PROMPT},
                          {"step_id": "recall", "kind": "send_message", "message": (
                              "For the same function from the previous turn, repeat only "
                              "the corrected return statement, using spaces around +.")}],
                "limits": {"max_total_steps": 4, "max_turns": 2, "wall_time_sec": 60},
            }), 201)
            _json(client.post("/api/v1/scenarios", json={
                "name": "integration-code-review", "version": "1", "evaluator": {
                    "evaluator_id": "workflow-assertions", "version": "1", "config": {
                        "metrics": [{"metric_id": "correct-add", "kind": "response-policy",
                                     "source": "final_output", "require": [
                                         {"text": "return a + b", "min_count": 1}]}]}}}), 201)
            budget = {"max_steps": 4, "max_tool_calls": 1, "wall_time_sec": 60,
                      "per_call_timeout_sec": 20, "max_output_tokens": 512}
            run = _json(client.post("/api/v1/runs", json={
                "scenario_version": "integration-code-review@1", "case_ids": ["fix-add"],
                "manifest": {"workflow": "integration-code-review@1", "model": MODEL_ID,
                             "agent": "builtin-agent@1", "budget": budget,
                             "agent_config": {"mode": "native-tool", "budget": budget},
                             "provider_transport_policy": "bounded-http@1"},
            }), 202)
        app = _app(path)
        with TestClient(app) as client:
            result = _worker(app, run["id"])
            _require(result["scores"] and all(s.get("passed") is True for s in result["scores"]),
                     "scenario_score_failed")
            steps = _json(client.get(f"/api/v1/runs/{run['id']}/steps"))
            sends = [step for step in steps["steps"] if step["kind"] == "send_message"]
            _require(not steps["unknown"] and len(sends) == 2
                     and all(step["status"] == "succeeded" for step in sends),
                     "scenario_step_evidence_missing")
            return {"ok": True, "run_id": run["id"], "turns": len(sends),
                    "persisted_steps": [step["step_id"] for step in sends],
                    **_reports(client, run["id"])}
