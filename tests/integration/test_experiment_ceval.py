"""Offline API/CLI → Worker → scripted Job lifecycle. No real runner acceptance claim."""

from copy import deepcopy
import json
import os
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.worker.motte_worker.reporting import WorkerReporter
from apps.worker.motte_worker.runtime import WorkerLoop
from motte_benchmark import registry
from motte_benchmark.opencompass.adapter import CevalJobAdapter
from motte_cli.main import main
from motte_sdk import MotteClient
from motte_sdk.experiments import ExperimentService
from motte_sdk.service import RunService
from motte_storage.factory import create_run_store
from motte_storage.integrity import RunConflictError
from motte_storage.resource_store import SQLiteResourceStore
from tests.sdk.sync_asgi import SyncASGITransport
from tests.sdk.test_experiment_ceval import PROFILE, seed_ceval, suite_spec


def scripted_adapter(tmp_path):
    script = tmp_path / "offline_runner.py"
    script.write_text("""import json, os
from pathlib import Path
work = Path(os.environ["MOTTE_WORK_DIR"])
config = json.loads((work / "runner-config.json").read_text())
output = work / "outputs" / "predictions" / "model"
output.mkdir(parents=True)
subjects = {}
for case in config["cases"]:
    group = subjects.setdefault(case["subject"], {})
    group[str(len(group))] = {"prediction": "B", "origin_prompt": case["prompt"]}
for subject, predictions in subjects.items():
    (output / ("ceval-" + subject + ".json")).write_text(json.dumps(predictions))
(work / ".motte-job-complete").write_text('{"exit_code":0,"completed":true}')
""")

    class ScriptedLifecycleAdapter(CevalJobAdapter):
        # Test-only transport replacement; this does not attest a real runner.
        def _verify_bounded_runner(self, spec):
            from motte_benchmark.opencompass.execution import validate_execution_config

            validate_execution_config(spec.runner_config)

    return ScriptedLifecycleAdapter(
        argv=[sys.executable, str(script)], default_limits={"max_wall_seconds": 20}
    )


@pytest.mark.parametrize("create_via", ["api", "local-cli", "server-cli"])
def test_ceval_two_models_worker_scoring_recovery_retry_and_cancel(
    tmp_path, monkeypatch, capsys, create_via
):
    path = tmp_path / "ceval.db"
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("OPENAI_API_KEY", "offline-only-synthetic")
    monkeypatch.setenv("MOTTE_JOB_WORK_ROOT", str(tmp_path / "jobs"))
    store, resources = create_run_store(str(path)), SQLiteResourceStore(str(path))
    seed_ceval(store, resources)
    adapter = scripted_adapter(tmp_path)
    monkeypatch.setitem(registry._FACTORIES, "ceval-opencompass", lambda: adapter)
    app = create_app(store, resources)
    client = TestClient(app)
    monkeypatch.setattr(
        "motte_cli.remote.build_client",
        lambda args: MotteClient(
            "http://testserver",
            transport=SyncASGITransport(app),
            retries=0,
        ),
    )
    payload = suite_spec()
    modes = {
        "local-cli": ["--mode", "local", "--db", str(path)],
        "server-cli": ["--mode", "server", "--api-url", "http://testserver"],
    }

    def cli(command, mode="local-cli", args=()):
        result = main(["experiment", command, *modes[mode], "--spec", json.dumps(payload), *args])
        captured = capsys.readouterr()
        assert result == 0, captured.err
        return json.loads(captured.out)

    response = client.post("/api/v1/experiments/preview", json=payload)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert cli("preview") == cli("preview", "server-cli") == preview
    assert preview["cell_count"] == 4 and preview["max_potential_calls"] == 16
    assert store.runs.list() == [] and adapter.start_calls == []
    if create_via == "api":
        response = client.post(
            "/api/v1/experiments", json={**payload, "_preview_hash": preview["preview_hash"]}
        )
        assert response.status_code == 202, response.text
        created = response.json()
    else:
        created = cli("create", create_via, ("--preview-hash", preview["preview_hash"]))
    assert created["allocated"] == 4 and created["failed"] == []
    cells = deepcopy(store.experiments.list_cells("ceval-matrix", "1"))
    for repo in (resources.models, store.benchmark_datasets):
        monkeypatch.setattr(
            repo, "get", lambda *a: pytest.fail("frozen recovery must not query resources")
        )
    monkeypatch.setattr(
        "motte_sdk.benchmark_catalog.prepare_external_run_inputs",
        lambda *a, **kw: pytest.fail("frozen recovery must not rebuild inputs"),
    )
    restarted = create_run_store(str(path))
    runs = RunService(restarted)
    service = ExperimentService(restarted, runs, resources=None)
    assert service.allocate("ceval-matrix", "1")["allocated"] == 0
    worker = WorkerLoop(runs, reporter=WorkerReporter(enabled=False))
    for cell in cells:
        finished = worker.claim_and_execute(cell["run_id"])
        assert finished["status"] == "completed", finished
        (job,) = restarted.external_jobs.jobs_for_run(cell["run_id"])
        assert job["status"] == "settled"
        view = runs.get_run(cell["run_id"])
        assert len(view["scores"]) == 2 and all(score["passed"] for score in view["scores"])
        assert view["current_scoring_pass_id"]
        assert job["checkpoint"]["cursor"]["parser_version"] == "ceval-opencompass-parser@2"
        assert cell["prepared_run"]["call_bound"]["execution_boundary"] == PROFILE
        with pytest.raises(RunConflictError):
            worker.claim_and_execute(cell["run_id"])
    assert len(adapter.start_calls) == 4
    assert service.allocate("ceval-matrix", "1")["allocated"] == 0
    assert len(adapter.start_calls) == 4
    # Recovery of a known Job must not even try to attest/start this deliberately
    # unusable adapter; it imports the same frozen records only.
    from motte_sdk.external_jobs import DurableExternalJobRunner, ExternalJobSupervisor
    from motte_storage.artifacts import ArtifactStore

    recovery_adapter = CevalJobAdapter(argv=["/bin/false"])
    recovery = DurableExternalJobRunner(
        ExternalJobSupervisor(recovery_adapter, poll_interval_seconds=0.01),
        restarted.external_jobs,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        work_root=tmp_path / "jobs",
        parser_version="ceval-opencompass-parser@2",
    )
    recovery.bind_service(runs)
    recovered = recovery(restarted.runs.get(cells[0]["run_id"]))
    assert recovered["recovered_from"] == "job-store"
    assert recovered["import"]["imported"] == 0
    assert recovery_adapter.start_calls == []
    assert len(restarted.external_jobs.jobs_for_run(cells[0]["run_id"])) == 1
    retry = service.retry_cell(cells[0]["cell_id"], reason="new explicitly authorized Run")
    assert worker.claim_and_execute(retry["run_id"])["status"] == "completed"
    (old_job,) = restarted.external_jobs.jobs_for_run(cells[0]["run_id"])
    (new_job,) = restarted.external_jobs.jobs_for_run(retry["run_id"])
    assert new_job["job_id"] != old_job["job_id"]
    assert new_job["launch_token"] != old_job["launch_token"]
    assert len(adapter.start_calls) == 5 and len(restarted.runs.list()) == 5
    # The original 16-call matrix count excludes this fifth Run's own 4-call bound.
    assert (
        restarted.experiments.get_cell(cells[0]["cell_id"])["prepared_run"]["call_bound"][
            "max_calls"
        ]
        == 4
    )
    cancelled_retry = service.retry_cell(cells[1]["cell_id"], reason="cancel before dispatch")
    runs.cancel(cancelled_retry["run_id"], reason="offline cancellation")
    with pytest.raises(RunConflictError):
        worker.claim_and_execute(cancelled_retry["run_id"])
    assert restarted.external_jobs.jobs_for_run(cancelled_retry["run_id"]) == []
    assert len(adapter.start_calls) == 5


@pytest.mark.parametrize("failure", ["mixed", "503", "429", "json", "rate-limit", "redirect"])
def test_ceval_pinned_runner_localhost_total_posts_match_proven_bound(
    tmp_path, monkeypatch, failure
):
    """Actual installed bridge/config/CLI/partitioner/inferencer; localhost only."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from motte_sdk.benchmark_catalog import prepare_external_run_inputs
    from motte_sdk.dispatcher import RunDispatcher

    python = os.environ.get("MOTTE_OC042_PYTHON")
    if not python:
        pytest.skip("not_run: set MOTTE_OC042_PYTHON to the official isolated 0.4.2 runner")
    store, resources = (
        create_run_store(str(tmp_path / "real.db")),
        SQLiteResourceStore(str(tmp_path / "real.db")),
    )
    dataset = seed_ceval(store, resources)
    inputs = prepare_external_run_inputs(
        dataset,
        benchmark_id="ceval",
        model_id="model-a",
        model_record=resources.models.get("model-a"),
        case_ids=["logic-2", "math-1"],
        few_shot=1,
        seed=7,
        execution_profile=PROFILE,
        credentials={
            "api_key": {"ref": "env:MOTTE_CEVAL_TEST_KEY"},
            "base_url": {"ref": "env:MOTTE_CEVAL_TEST_URL"},
            "offline_hub": {"ref": "env:HF_HUB_OFFLINE"},
            "offline_datasets": {"ref": "env:HF_DATASETS_OFFLINE"},
        },
    )
    posts, counts = [], {}

    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            prompt = data["messages"][0]["content"]
            posts.append((self.path, data))
            counts[prompt] = counts.get(prompt, 0) + 1
            assert self.path == "/v1/chat/completions", "redirect must never be followed"
            status = 503
            body = b'{"error":{"code":"rate_limit_exceeded"}}'
            if failure == "mixed" and counts[prompt] == 2:
                status, body = 200, b'{"choices":[{"message":{"content":"B"}}]}'
            elif failure == "429":
                status = 429
            elif failure == "json":
                status, body = 200, b"malformed-json"
            elif failure == "rate-limit":
                status = 200
            elif failure == "redirect":
                status = 307
            self.send_response(status)
            if status == 307:
                self.send_header("Location", "/forbidden-redirect")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("MOTTE_JOB_WORK_ROOT", str(tmp_path / "jobs"))
    monkeypatch.delenv("MOTTE_RUNNER_PYTHON", raising=False)
    monkeypatch.delenv("MOTTE_RUNNER_ROOT", raising=False)
    adapter = CevalJobAdapter(
        argv=[str(Path(python).parent / "opencompass-entry")],
        extra_env={
            "MOTTE_CEVAL_TEST_KEY": "offline-only-synthetic",
            "MOTTE_CEVAL_TEST_URL": f"http://127.0.0.1:{server.server_port}/v1",
            "HF_HUB_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "PYTHONPATH": "",
        },
        default_limits={"max_wall_seconds": 120},
    )
    monkeypatch.setitem(registry._FACTORIES, "ceval-opencompass", lambda: adapter)
    service = RunService(store)
    run = service.create_run(inputs["scenario_version"], inputs["manifest"], inputs["case_ids"])
    try:
        finished = RunDispatcher(service).dispatch(run["id"])
        with pytest.raises(RunConflictError):
            RunDispatcher(service).dispatch(run["id"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    (job,) = store.external_jobs.jobs_for_run(run["id"])
    assert len(adapter.start_calls) == 1
    assert len(posts) <= 4 and set(counts.values()) == {2}, json.dumps(job, ensure_ascii=False)
    assert all(body["n"] == 1 for _, body in posts)
    if failure == "mixed":
        assert finished["status"] == "completed", json.dumps(job, ensure_ascii=False)
        assert len(posts) == 4 and len(counts) == 2
        assert all(score["passed"] for score in service.get_run(run["id"])["scores"])
    else:
        assert finished["status"] == "failed"
        assert len(posts) == 2  # one exhausted case terminates the sole inference task
    assert service.get_run(run["id"])["status"] == finished["status"]


@pytest.mark.parametrize(
    "mode", ["legacy-env-proxy", "legacy-explicit-proxy", "bounded", "unknown-profile", "bad-retry"]
)
def test_runtime_transport_selection_preserves_legacy_and_fails_closed(mode):
    """Real upstream model and Requests, with all sends intercepted before any socket.

    Removing the verified-profile dispatch restores the reviewed regression:
    legacy non-loopback HTTP and proxy/redirect handling fail before the first send.
    """
    import subprocess

    python = os.environ.get("MOTTE_OC042_PYTHON")
    if not python:
        pytest.skip("not_run: requires the pinned runner for legacy transport compatibility")
    script = r'''
import os
from pathlib import Path
import sys
import requests

# Test the current source against genuine pinned dependencies, including in RED
# before the corrected bridge has been installed into the isolated runner.
sys.path.insert(0, str(Path(sys.argv[1]) / "packages/benchmark-runtime"))
from motte_benchmark.opencompass.execution import (
    PROFILE_ID, TransportBudgetExhausted, freeze_execution_profile,
)
from motte_benchmark.opencompass.upstream import RuntimeOpenAI

mode = sys.argv[2]
legacy = mode.startswith("legacy-")
os.environ["MOTTE_COMPAT_TEST_KEY"] = "synthetic-offline-only"
for name in list(os.environ):
    if name.lower().endswith("_proxy"):
        os.environ.pop(name)
os.environ["HTTP_PROXY"] = "http://environment-proxy.invalid:8080"
sends = []
def send(self, request, **kwargs):
    sends.append((request, kwargs))
    assert len(sends) <= 2, "transport must not issue an unplanned send"
    response = requests.Response()
    response.request = request
    response.url = request.url
    response.status_code = 200 if legacy and len(sends) == 2 else 307
    response.headers["Location"] = "/redirected"
    response._content = b'{"choices":[{"message":{"content":" B "}}]}'
    return response
requests.adapters.HTTPAdapter.send = send

kwargs = dict(path="gpt-4", retry=2, key_env="MOTTE_COMPAT_TEST_KEY",
    openai_api_base=("http://gateway.invalid/v1/chat/completions" if legacy
                     else "http://127.0.0.1:9/v1/chat/completions"))
if mode == "legacy-explicit-proxy":
    kwargs["openai_proxy_url"] = "http://explicit-proxy.invalid:8080"
if not legacy:
    kwargs["execution_profile"] = freeze_execution_profile(PROFILE_ID)
if mode == "unknown-profile":
    kwargs["execution_profile"]["profile_id"] = "unknown@1"
if mode == "bad-retry":
    kwargs["retry"] = 3
if mode in ("unknown-profile", "bad-retry"):
    try:
        RuntimeOpenAI(**kwargs)
    except ValueError:
        assert sends == []
    else:
        raise AssertionError("invalid profile must fail closed, never use legacy transport")
else:
    model = RuntimeOpenAI(**kwargs)
    model._preprocess_messages = lambda prompt, cap, *_: ([{"role":"user", "content":prompt}], cap)
    model.wait = lambda: None
    if legacy:
        assert model._generate("prompt", 17, 0.0) == "B"
        assert [item[0].url for item in sends] == [kwargs["openai_api_base"], "http://gateway.invalid/redirected"]
        proxy = kwargs.get("openai_proxy_url", os.environ["HTTP_PROXY"])
        assert all(item[1]["proxies"]["http"] == proxy for item in sends)
        assert all(item[1]["timeout"] is None for item in sends)
    else:
        try:
            model._generate("prompt", 17, 0.0)
        except TransportBudgetExhausted:
            assert [item[0].url for item in sends] == [kwargs["openai_api_base"]] * 2
            assert all(item[1]["proxies"] == {} for item in sends)
            assert all(item[1]["timeout"] == (10, 60) for item in sends)
        else:
            raise AssertionError("bounded transport must not follow legacy redirects")
print("transport-selection:", mode, "passed")
'''
    result = subprocess.run(
        [python, "-c", script, str(Path(__file__).resolve().parents[2]), mode],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"transport-selection: {mode} passed" in result.stdout


def test_pinned_upstream_actual_retry_body_is_unbounded_on_failures():
    """Genuine installed 0.4.2 body; a sentinel prevents its historical infinite loop."""
    import subprocess

    python = os.environ.get("MOTTE_OC042_PYTHON")
    if not python:
        pytest.skip("not_run: requires genuine pinned upstream OpenAI._generate")
    script = r"""
from types import SimpleNamespace
from importlib.metadata import version, distribution
from pathlib import Path
from threading import Lock
import ast
import hashlib
import requests
assert version("opencompass") == "0.4.2"
source = Path(distribution("opencompass").locate_file("opencompass/models/openai_api.py")).read_bytes()
assert hashlib.sha1(b"blob " + str(len(source)).encode() + b"\0" + source).hexdigest() == "7b2c2c53943ab41c9f04444c490cfff9b0c66865"
# Execute the exact installed function AST, not a reconstructed retry algorithm.
# This isolates transport logic from Torch/CUDA imports unrelated to its loop.
klass = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef) and node.name == "OpenAI")
method = next(node for node in klass.body if isinstance(node, ast.FunctionDef) and node.name == "_generate")
namespace = {"requests": requests, "Lock": Lock, "PromptType": str, "PromptList": list,
             "json": __import__("json"), "O1_MODEL_LIST": ["o1", "o3"],
             "time": SimpleNamespace(sleep=lambda *_: None)}
exec(compile(ast.Module(body=[method], type_ignores=[]), "genuine-installed-openai-api.py", "exec"), namespace)
upstream_generate = namespace["_generate"]
class Sentinel(Exception): pass
for failure in ("503", "connection", "json", "rate-limit"):
    sends = []
    def post(*args, **kwargs):
        sends.append(1)
        if len(sends) == 3: raise Sentinel()
        if failure == "connection": raise requests.ConnectionError("scripted")
        response = requests.Response()
        response.status_code = 503 if failure == "503" else 200
        response._content = b"malformed" if failure == "json" else b'{"error":{"code":"rate_limit_exceeded"}}'
        return response
    requests.post = post
    model = SimpleNamespace(retry=2, keys=["offline"], invalid_keys=set(), key_ctr=0, orgs=None,
        path="gpt-4", logprobs=False, top_logprobs=None, extra_body=None, proxy_url=None,
        url="http://127.0.0.1:1/never-actually-sent", mode="none", max_seq_len=8192,
        get_token_len=lambda _: 1, wait=lambda: None,
        _preprocess_messages=lambda value, cap, *_: ([{"role":"user","content":value}], cap),
        logger=SimpleNamespace(error=lambda *a: None, warn=lambda *a: None, debug=lambda *a: None))
    try:
        upstream_generate(model, "prompt", 17, 0.0)
    except Sentinel:
        assert len(sends) == 3
    else:
        raise AssertionError("upstream historical counterexample did not reproduce")
print("actual-upstream-counterexample: 4 branches each exceed retry=2")
"""
    result = subprocess.run([python, "-c", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "actual-upstream-counterexample: 4 branches" in result.stdout
