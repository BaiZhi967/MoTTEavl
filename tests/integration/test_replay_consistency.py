import json

from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.worker.motte_worker.reporting import WorkerReporter
from apps.worker.motte_worker.runtime import WorkerLoop
from motte_cli.main import main as cli_main
from motte_sdk.replay_run import ReplayProvider
from motte_sdk.service import RunService
from motte_storage.run_store import SQLiteRunStore

FIXTURE = {
    "case-1": {"output": {"n": 1}, "expected": {"n": 1}},
    "case-2": {"output": {"text": "hi"}, "expected": {"text": "bye"}},
}


def _normalize(result, events):
    return {
        "status": result["status"],
        "scores": result["scores"],
        "cases": [
            {"case_id": entry["case_id"], "result": entry["result"], "expected": entry["expected"]}
            for entry in result["cases"]
        ],
        "events": [(event["seq"], event["type"]) for event in events],
    }


def test_replay_trace_and_score_are_identical_across_api_cli_sdk(tmp_path, capsys):
    sdk_service = RunService(SQLiteRunStore(tmp_path / "sdk.db"))
    sdk_run = sdk_service.create_run("replay@1", {}, case_ids=list(FIXTURE))
    sdk_result = sdk_service.execute(sdk_run["id"], provider=ReplayProvider(FIXTURE).invoke)
    sdk = _normalize(sdk_result, sdk_service.events(sdk_run["id"]))

    client = TestClient(create_app(SQLiteRunStore(tmp_path / "api.db")))
    api_run = client.post("/api/v1/runs", json={"scenario_version": "replay@1", "case_ids": list(FIXTURE)}).json()
    queued = client.post(f"/api/v1/runs/{api_run['id']}/replay", json={"cases": FIXTURE})
    assert queued.status_code == 202
    api_result = WorkerLoop(
        client.app.state.run_service, reporter=WorkerReporter(enabled=False)
    ).claim_and_execute(api_run["id"])
    api = _normalize(api_result, client.app.state.run_service.events(api_run["id"]))

    exit_code = cli_main(["replay", "--db", str(tmp_path / "cli.db"), "--fixture", json.dumps(FIXTURE), "--json"])
    assert exit_code == 0
    cli_result = json.loads(capsys.readouterr().out)
    cli = _normalize(cli_result, RunService(SQLiteRunStore(tmp_path / "cli.db")).events(cli_result["id"]))

    assert sdk == api == cli
