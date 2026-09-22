"""R4-01/05: safe snapshots and opt-in real 0.4.2/local-HTTP integration.

MOTTE_OC042_PYTHON selects an isolated runner environment, never a provider.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from motte_benchmark.opencompass.adapter import CevalJobAdapter
from motte_benchmark.opencompass.entry import export_subject_files, render_opencompass_config_source
from motte_benchmark.opencompass.parser import parse_opencompass_files
from motte_sdk.benchmark_catalog import prepare_external_dataset, prepare_external_run_inputs


def _production_inputs(few_shot):
    rows = [
        {"id": f"logic-{i}", "subject": "logic", "question": f"TARGET-{i}?",
         "A": "one", "B": "two", "C": "three", "D": "four",
         "answer": "D" if i == 1 else "B",
         "split": "val"} for i in range(1, 4)
    ] + [{"id": "dev-1", "subject": "logic", "question": "EXAMPLE-ONLY?",
          "A": "demo one", "B": "demo two", "C": "demo three", "D": "demo four",
          "answer": "A", "split": "dev"}]
    dataset = prepare_external_dataset(
        files={"data.jsonl": "\n".join(json.dumps(row) for row in rows).encode()},
        dataset_revision="r4-runner-fixture",
    )
    return prepare_external_run_inputs(
        dataset, benchmark_id="ceval", model_id="r4-local",
        model_record={"id": "r4-local", "provider": "openai", "model": "gpt-4",
                      "lifecycle": "published", "context_window": 8192,
                      "parameters": {"temperature": 0.0, "top_p": 0.8,
                                     "max_output_tokens": 17}},
        few_shot=few_shot, case_ids=["logic-3", "logic-1"],
        credentials={"api_key": {"ref": "env:MOTTE_R4_TEST_KEY"},
                     "base_url": {"ref": "env:MOTTE_R4_TEST_URL"}},
    )


def test_r4_snapshot_hashes_exact_evidence_bytes(tmp_path):
    work = tmp_path / "job"
    output = work / "outputs" / "predictions" / "model" / "ceval-logic.json"
    output.parent.mkdir(parents=True)
    data = b'{"0": {"prediction": "B"}}\r\n'
    output.write_bytes(data)
    excluded = work / "outputs" / "configs" / "private.py"
    excluded.parent.mkdir()
    excluded.write_text("not evidence")
    adapter = CevalJobAdapter()
    snap = adapter.snapshot_outputs(SimpleNamespace(work_dir=str(work)))
    assert snap == {"complete": True, "files": {
        "outputs/predictions/model/ceval-logic.json": hashlib.sha256(data).hexdigest(),
    }}


def test_r4_snapshot_rejects_symlink_and_oversize(tmp_path):
    work = tmp_path / "job"
    work.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "secret.json").write_text("secret")
    (work / "outputs").symlink_to(victim, target_is_directory=True)
    adapter = CevalJobAdapter(default_limits={"max_result_bytes": 4})
    handle = SimpleNamespace(work_dir=str(work))
    assert adapter.snapshot_outputs(handle) == {"complete": False, "files": {}}
    (work / "outputs").unlink()
    (work / "runner-config.json").write_text("12345")
    assert adapter.snapshot_outputs(handle) == {"complete": False, "files": {}}


def test_r4_prediction_only_output_does_not_invent_native_accuracy():
    parsed = parse_opencompass_files({
        "stamp/predictions/model/ceval-logic.json": json.dumps({
            "0": {"origin_prompt": "question", "prediction": "B"},
        }),
    }, experiment="stamp")
    assert parsed["samples"][0]["prediction"] == "B"
    assert parsed["samples"][0]["gold"] is None
    assert "ceval_logic/accuracy" not in parsed["native"]
    assert parsed["diagnostic"]["per_subject"] == {}


@pytest.mark.parametrize("benchmark", ["ceval", "cmmlu"])
def test_r4_parser_and_manifest_share_version(benchmark):
    from motte_sdk.benchmark_catalog import benchmark_descriptor

    expected = f"{benchmark}-opencompass-parser@2"
    parsed = parse_opencompass_files({
        f"predictions/model/{benchmark}-logic.json": '{"0": {"prediction": "B"}}',
    }, dataset=benchmark)
    assert parsed["parser_version"] == expected
    assert benchmark_descriptor(benchmark).parser_version == expected


@pytest.mark.parametrize("kind", ["results", "predictions"])
def test_r4_parser_rejects_multiple_models_for_one_subject(kind):
    files = {
        f"stamp/{kind}/{model}/ceval-logic.json": json.dumps({
            "0": {"prediction": answer},
        } if kind == "predictions" else {"accuracy": 50.0})
        for model, answer in [("model-a", "B"), ("model-z", "D")]
    }
    with pytest.raises(ValueError, match="ambiguous-model"):
        parse_opencompass_files(files, experiment="stamp")


def test_r4_parser_rejects_result_prediction_from_different_models():
    with pytest.raises(ValueError, match="ambiguous-model"):
        parse_opencompass_files({
            "stamp/results/model-a/ceval-logic.json": '{"accuracy": 50.0}',
            "stamp/predictions/model-z/ceval-logic.json": '{"0": {"prediction": "B"}}',
        }, experiment="stamp")


@pytest.mark.skipif(os.name == "nt", reason="POSIX sh wrapper contract")
def test_r4_installed_wrapper_uses_and_exports_adjacent_interpreter(tmp_path):
    root = Path(__file__).resolve().parents[2]
    bindir = tmp_path / "runner" / "bin"
    bindir.mkdir(parents=True)
    wrapper = bindir / "opencompass-entry"
    shutil.copy2(root / "scripts/runner/opencompass-entry", wrapper)
    python = bindir / "python"
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$MOTTE_RUNNER_PYTHON" "$@"\n')
    python.chmod(0o755)
    env = {**os.environ, "MOTTE_WORK_DIR": str(tmp_path)}
    env.pop("MOTTE_RUNNER_PYTHON", None)
    env.pop("MOTTE_RUNNER_ROOT", None)
    result = subprocess.run([str(wrapper)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[:3] == [str(python), "-m", "motte_benchmark.opencompass.entry"]


@pytest.mark.parametrize("few_shot", [0, 1])
def test_r4_real_opencompass_production_config_to_scoring(tmp_path, few_shot, monkeypatch):
    python = os.environ.get("MOTTE_OC042_PYTHON")
    if not python:
        pytest.skip("set MOTTE_OC042_PYTHON to the isolated fixed runner")
    inputs = _production_inputs(few_shot)
    config = inputs["manifest"]["external_benchmark"]["runner_config"]
    requests = []

    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers["Authorization"], payload))
            answer = "D" if "TARGET-1?" in payload["messages"][0]["content"] else "B"
            body = json.dumps({"choices": [{"message": {"content": answer}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    secret = "synthetic-r4-only-credential"
    env = {"MOTTE_R4_TEST_KEY": secret,
           "MOTTE_R4_TEST_URL": f"http://127.0.0.1:{server.server_port}/v1",
           "HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1", "PYTHONPATH": ""}
    from motte_benchmark import registry
    from motte_sdk.dispatcher import RunDispatcher
    from motte_sdk.service import RunService
    from motte_storage.external_jobs import SQLiteExternalJobs
    from motte_storage.run_store import SQLiteRunStore

    monkeypatch.setenv("MOTTE_JOB_WORK_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.delenv("MOTTE_RUNNER_PYTHON", raising=False)
    monkeypatch.delenv("MOTTE_RUNNER_ROOT", raising=False)
    adapter = CevalJobAdapter(
        argv=[str(Path(python).parent / "opencompass-entry")], extra_env=env,
        default_limits={"max_wall_seconds": 120},
    )
    monkeypatch.setitem(registry._FACTORIES, "ceval-opencompass", lambda: adapter)
    store = SQLiteRunStore(tmp_path / "runs.db")
    service = RunService(store)
    run = service.create_run(inputs["scenario_version"], inputs["manifest"], inputs["case_ids"])
    try:
        finished = RunDispatcher(service).dispatch(run["id"])
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
    jobs = SQLiteExternalJobs(str(tmp_path / "runs.db")).jobs_for_run(run["id"])
    assert len(jobs) == 1
    job = jobs[0]
    assert finished["status"] == "completed", json.dumps(job, ensure_ascii=False)
    assert job["status"] == "settled"
    assert len(adapter.start_calls) == 1
    work = Path(job["handle"]["work_dir"])
    assert len(requests) == 2
    expected_prompts = {case["prompt"] for case in config["cases"]}
    assert {request[2]["messages"][0]["content"] for request in requests} == expected_prompts
    for path, authorization, payload in requests:
        assert path == "/v1/chat/completions"
        assert authorization == f"Bearer {secret}"
        assert payload["model"] == "gpt-4"
        assert payload["temperature"] == 0.0 and payload["top_p"] == 0.8
        assert payload["max_tokens"] == 17
        assert ("EXAMPLE-ONLY?" in payload["messages"][0]["content"]) == bool(few_shot)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), str(path)
    records, cursor = adapter.collect(SimpleNamespace(work_dir=str(work)), {})
    assert [record.source_case_id for record in records] == ["logic-3", "logic-1"]
    assert [record.output["prediction"] for record in records] == ["B", "D"]
    assert cursor["records_consumed"] == 2
    assert cursor["parser_version"] == inputs["manifest"]["external_benchmark"]["parser_version"]
    assert all(record.output["gold"] is None for record in records)
    scoring_pass = store.scoring_passes.current(run["id"])
    assert scoring_pass["id"] == finished["current_scoring_pass_id"]
    assert all(score["passed"] is True for score in scoring_pass["scores"])
    aggregate = scoring_pass["summary"]["aggregate"]
    assert aggregate["accuracy"] == 1.0 and aggregate["coverage"] == 1.0
    native = scoring_pass["summary"]["external_job_metrics"]["native"]
    assert not any(key.endswith("/accuracy") for key in native)
    assert job["checkpoint"]["evidence"]["complete"] is True
    from apps.api.app.main import create_app
    from fastapi.testclient import TestClient
    from motte_storage.resource_store import InMemoryResourceStore

    with TestClient(create_app(store, InMemoryResourceStore())) as client:
        response = client.get(f"/api/v1/runs/{run['id']}/report")
        assert response.status_code == 200, response.text
        report = response.json()
    assert report["scoring_pass_id"] == scoring_pass["id"]
    assert report["summary"]["aggregate"]["accuracy"] == 1.0
    assert report["summary"]["aggregate"]["selected"] == 2
    (tmp_path / "integration-report.json").write_text(json.dumps(report, indent=2))


def test_r4_real_config_keeps_credentials_as_refs_and_default_endpoint(tmp_path):
    python = os.environ.get("MOTTE_OC042_PYTHON")
    if not python:
        pytest.skip("set MOTTE_OC042_PYTHON to the isolated fixed runner")
    config = _production_inputs(0)["manifest"]["external_benchmark"]["runner_config"]
    config["credentials"].pop("base_url")
    source = render_opencompass_config_source(config, export_subject_files(tmp_path, config))
    cfg = tmp_path / "config.py"
    cfg.write_text(source)
    program = '''
import sys
from mmengine import Config
from opencompass.utils import build_dataset_from_cfg, build_model_from_cfg
cfg = Config.fromfile(sys.argv[1])
model = build_model_from_cfg(cfg.models[0])
assert model.url == 'https://api.openai.com/v1/chat/completions', model.url
assert model.keys == ['synthetic-r4-only-credential']
dataset = build_dataset_from_cfg(cfg.datasets[0])
assert len(dataset.test) == 2
assert 'synthetic-r4-only-credential' not in cfg.pretty_text
assert cfg.datasets[0].abbr == 'ceval-logic'
'''
    env = {**os.environ, "MOTTE_R4_TEST_KEY": "synthetic-r4-only-credential",
           "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "packages/benchmark-runtime")}
    env.pop("OPENAI_BASE_URL", None)
    result = subprocess.run([python, "-c", program, str(cfg)], env=env,
                            cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
