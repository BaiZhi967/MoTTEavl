"""Offline C-Eval assembly and counted transport; no models/private datasets."""

from copy import deepcopy
import importlib
import json
from types import SimpleNamespace

import pytest
import requests
from pydantic import ValidationError

from motte_contracts.experiment import ExperimentSpec
from motte_sdk.benchmark_catalog import dataset_to_payload, prepare_external_dataset
from motte_sdk.experiments import ExperimentError, ExperimentService, _expand_matrix
from motte_sdk.service import RunService
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore

PROFILE = "motte-ceval-oc042-bounded@1"


def suite_spec():
    return {
        "experiment_id": "ceval-matrix",
        "version": "1",
        "task_ref": {"suite": "ceval-external", "scenario_version": "ceval-external@1"},
        "factors": {"model_profile": ["model-a", "model-b"]},
        "repeats": 2,
        "selected_case_keys": ["logic-2", "math-1"],
        "suite_config": {
            "kind": "ceval",
            "dataset_revision": "offline-revision-1",
            "scope": "custom-subset",
            "split": "val",
            "few_shot": 1,
            "few_shot_split": "dev",
            "seed": 7,
            "execution_profile": PROFILE,
        },
        "budget_policy": {"max_total_calls": 16},
        "created_by": "tester",
        "reason": "scripted offline fixtures",
    }


def seed_ceval(store, resources, revision="offline-revision-1"):
    rows = [
        {
            "id": case,
            "subject": subject,
            "question": case + "?",
            "A": "one",
            "B": "two",
            "C": "three",
            "D": "four",
            "answer": "B",
            "split": split,
        }
        for case, subject, split in [
            ("logic-1", "logic", "val"),
            ("logic-2", "logic", "val"),
            ("math-1", "advanced_mathematics", "val"),
            ("demo-1", "logic", "dev"),
        ]
    ]
    dataset = prepare_external_dataset(
        files={"data.jsonl": "\n".join(json.dumps(row) for row in rows).encode()},
        dataset_revision=revision,
    )
    store.benchmark_datasets.put_immutable(dataset_to_payload(dataset))
    for model in ("model-a", "model-b"):
        resources.models.put(
            {
                "id": model,
                "provider": "openai",
                "model": "gpt-4" if model == "model-a" else "gpt-4-b",
                "lifecycle": "published",
                "context_window": 8192,
                "parameters": {"temperature": 0.0, "max_output_tokens": 17},
            }
        )
    return dataset


def environment():
    store, resources = InMemoryRunStore(), InMemoryResourceStore()
    dataset = seed_ceval(store, resources)
    return (
        store,
        resources,
        ExperimentService(store, RunService(store), resources=resources),
        dataset,
    )


def assert_empty(store):
    assert store.experiments.list_specs() == []
    assert store.experiments.list_cells("ceval-matrix") == []
    assert store.runs.list() == []


def test_ceval_standalone_parity_and_initial_factor_repeat_bound():
    from motte_sdk.benchmark_catalog import prepare_external_run_inputs
    from motte_sdk.experiment_assemblers import assemble_experiment_cell

    store, resources, service, dataset = environment()
    spec = ExperimentSpec.model_validate(suite_spec())
    assignment, _ = _expand_matrix(spec)[0]
    assembled = assemble_experiment_cell(spec, assignment, resources=resources, store=store)
    standalone = prepare_external_run_inputs(
        dataset,
        benchmark_id="ceval",
        model_id="model-a",
        model_record=resources.models.get("model-a"),
        scope="custom-subset",
        split="val",
        few_shot=1,
        few_shot_split="dev",
        seed=7,
        case_ids=["logic-2", "math-1"],
        execution_profile=PROFILE,
    )
    assert assembled.manifest == standalone["manifest"]
    assert assembled.case_ids == tuple(standalone["case_ids"])
    assert assembled.call_bound.max_calls == 4
    assert assembled.call_bound.controlled_retries == {
        "runner": 0,
        "provider_transport": 1,
        "operator": 0,
    }
    preview = service.preview(suite_spec())
    assert preview["cell_count"] == 4 and preview["max_potential_calls"] == 16
    assert len({cell["cell_id"] for cell in preview["cells"]}) == 4
    assert_empty(store)


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown-profile",
        "old-profile",
        "missing-revision",
        "latest",
        "nonmodel-factor",
        "low-budget",
        "retry-claim",
    ],
)
def test_ceval_unsupported_requests_write_nothing(mutation):
    store, _, service, _ = environment()
    payload = suite_spec()
    if mutation == "unknown-profile":
        payload["suite_config"]["execution_profile"] = "unknown@1"
    if mutation == "old-profile":
        payload["suite_config"]["execution_profile"] = "bounded-opencompass@1"
    if mutation == "missing-revision":
        payload["suite_config"]["dataset_revision"] = "missing"
    if mutation == "latest":
        payload["suite_config"]["dataset_revision"] = "latest"
    if mutation == "nonmodel-factor":
        payload["factors"]["reasoning_level"] = ["high"]
    if mutation == "low-budget":
        payload["budget_policy"]["max_total_calls"] = 15
    if mutation == "retry-claim":
        payload["suite_config"]["retry_policy"] = {"provider_transport": 0}
    for operation in (service.preview, service.create):
        if mutation == "low-budget" and operation == service.preview:
            assert service.preview(payload)["violations"][0]["code"] == "BUDGET_EXCEEDED"
        else:
            with pytest.raises((ExperimentError, ValidationError)):
                operation(payload)
        assert_empty(store)


def test_ceval_exact_revision_freezes_replay_without_current_resource_lookup(monkeypatch):
    store, resources, service, _ = environment()
    lookups = []
    original = store.benchmark_datasets.get
    monkeypatch.setattr(
        store.benchmark_datasets, "get", lambda *keys: (lookups.append(keys), original(*keys))[1]
    )
    monkeypatch.setattr(
        store.benchmark_datasets, "latest", lambda *a: pytest.fail("latest forbidden")
    )
    payload = suite_spec()
    preview = service.preview(payload)
    seed_ceval(store, resources, revision="newer-revision")
    assert service.preview(payload)["preview_hash"] == preview["preview_hash"]
    result = service.create(payload, expected_preview_hash=preview["preview_hash"])
    assert result["allocated"] == 4 and result["failed"] == []
    assert set(lookups) == {("ceval", "offline-revision-1")}
    frozen = deepcopy(store.experiments.list_cells("ceval-matrix", "1"))
    for repo in (resources.models, store.benchmark_datasets):
        monkeypatch.setattr(
            repo, "get", lambda *a: pytest.fail("replay must not resolve resources")
        )
    monkeypatch.setattr(
        "motte_sdk.benchmark_catalog.prepare_external_run_inputs",
        lambda *a, **kw: pytest.fail("replay must not prepare"),
    )
    replay = ExperimentService(store, RunService(store), resources=None)
    assert replay.allocate("ceval-matrix", "1")["allocated"] == 0
    retry = replay.retry_cell(frozen[0]["cell_id"], reason="explicit new Run")
    assert retry["run_id"] != frozen[0]["run_id"]
    assert len(store.runs.list()) == 5
    assert store.runs.get(retry["run_id"])["manifest"] == frozen[0]["prepared_run"]["manifest"]


def _bounded():
    # A RED assertion, rather than an import error, before the new implementation exists.
    from motte_benchmark.opencompass import config

    assert hasattr(config, "bounded_generate"), "fixed counted transport not implemented"
    return config.bounded_generate


def _model(attempts):
    return SimpleNamespace(
        retry=attempts,
        keys=["synthetic"],
        orgs=None,
        proxy_url=None,
        url="http://127.0.0.1:9/v1/chat/completions",
        path="gpt-4",
        logprobs=False,
        top_logprobs=None,
        extra_body={"top_p": 0.8},
        max_seq_len=8192,
        mode="none",
        get_token_len=lambda value: len(value),
        _preprocess_messages=lambda value, cap, *_: ([{"role": "user", "content": value}], cap),
        wait=lambda: None,
    )


@pytest.mark.parametrize("attempts", [1, 3])
@pytest.mark.parametrize(
    "failure", ["503", "429", "connection", "json", "rate-limit", "307", "308"]
)
def test_counted_transport_consumes_every_failure_before_send(monkeypatch, attempts, failure):
    bounded = _bounded()
    from motte_benchmark.opencompass.config import TransportBudgetExhausted

    sends = []

    def send(self, request, **kwargs):
        sends.append((request, kwargs, self.max_retries))
        assert len(sends) <= attempts, "unbounded transport regression"
        if failure == "connection":
            raise requests.ConnectionError("offline")
        response = requests.Response()
        response.request = request
        response.status_code = int(failure) if failure.isdigit() else 200
        response.headers["Location"] = "http://127.0.0.1:1/forbidden-redirect"
        response._content = (
            b"invalid-json" if failure == "json" else b'{"error":{"code":"rate_limit_exceeded"}}'
        )
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    with pytest.raises(TransportBudgetExhausted):
        bounded(_model(attempts), "prompt", 17, 0.0)
    assert len(sends) == attempts
    assert all(item[0].url == _model(attempts).url for item in sends)
    assert all(item[1]["timeout"] == (10, 60) and item[1]["proxies"] == {} for item in sends)
    assert all(item[2].total == 0 for item in sends)


def test_counted_transport_mixed_errors_success_and_payload(monkeypatch):
    bounded = _bounded()
    sends = []

    def send(self, request, **kwargs):
        sends.append(json.loads(request.body))
        response = requests.Response()
        response.status_code = 503 if len(sends) == 1 else 200
        response._content = b'{"choices":[{"message":{"content":" B "}}]}'
        return response

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1/no-proxy")
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    assert bounded(_model(3), "frozen prompt", 17, 0.0) == "B"
    assert len(sends) == 2
    assert sends[0] == sends[1]
    assert sends[0]["n"] == 1 and sends[0]["messages"][0]["content"] == "frozen prompt"


@pytest.mark.parametrize("attempts", [True, -1, 0, "2", None])
def test_counted_transport_invalid_budget_sends_nothing(monkeypatch, attempts):
    bounded = _bounded()
    monkeypatch.setattr(
        requests.adapters.HTTPAdapter, "send", lambda *a, **k: pytest.fail("must not send")
    )
    with pytest.raises(ValueError):
        bounded(_model(attempts), "prompt", 17, 0.0)


def test_execution_profile_rejects_retry_declarations_and_unknown_profiles():
    from motte_sdk.benchmark_catalog import prepare_external_run_inputs

    store, resources, _, dataset = environment()
    for retry in (
        {"provider_transport": True},
        {"provider_transport": -1},
        {"provider_transport": "1"},
        {"provider_transport": 2},
        {"runner": 1},
        {"operator": 1},
        {"unknown": 0},
    ):
        with pytest.raises(ValueError, match="PROFILE|BUDGET"):
            prepare_external_run_inputs(
                dataset,
                benchmark_id="ceval",
                model_id="model-a",
                model_record=resources.models.get("model-a"),
                execution_profile=PROFILE,
                retry_policy=retry,
            )
        assert_empty(store)


def test_fixed_renderer_contract_and_installed_identity_rejects_drift(tmp_path):
    from motte_benchmark.opencompass.entry import (
        export_subject_files,
        render_opencompass_config_source,
    )
    from motte_sdk.experiment_assemblers import assemble_experiment_cell

    store, resources, _, _ = environment()
    spec = ExperimentSpec.model_validate(suite_spec())
    assignment, _ = _expand_matrix(spec)[0]
    assembled = assemble_experiment_cell(spec, assignment, resources=resources, store=store)
    config = assembled.manifest["external_benchmark"]["runner_config"]
    source = render_opencompass_config_source(config, export_subject_files(tmp_path, config))
    for text in (
        "NumWorkerPartitioner",
        "num_worker=1",
        "num_split=1",
        "strategy='heuristic'",
        "LocalRunner",
        "max_num_workers=1",
        "batch_size=1",
        "GenInferencer",
        "ZeroRetriever",
        "inferencer=dict(type=GenInferencer, batch_size=1)",
        "run_cfg=dict(num_gpus=0, num_procs=1)",
    ):
        assert text in source
    assert "model_dataset_combinations" not in source
    assert config["execution_profile"]["profile_id"] == PROFILE
    for mutate in (
        lambda x: x["retry"].update(runner=1),
        lambda x: x["execution_profile"].update(batch_size=2),
        lambda x: x["execution_profile"]["bridge_sha256"].update({"upstream.py": "forged"}),
        lambda x: x.update(models=[{}, {}]),
    ):
        bad = deepcopy(config)
        mutate(bad)
        with pytest.raises(ValueError, match="PROFILE|CONFIG"):
            render_opencompass_config_source(bad, {})


@pytest.mark.parametrize("changed", ["prompt", "model", "profile", "bound", "scorer", "parser"])
@pytest.mark.parametrize("operation", ["allocate", "retry"])
def test_ceval_frozen_tampering_rejects_before_run_job_writes(changed, operation):
    store, _, service, _ = environment()
    service.create(suite_spec())
    cell = store.experiments.list_cells("ceval-matrix", "1")[0]
    # Test persisted JSON copies, not aliasing mutable assembler output.
    saved = json.loads(json.dumps(cell))
    frozen = saved["prepared_run"]
    config = frozen["manifest"]["external_benchmark"]["runner_config"]
    if changed == "prompt":
        config["cases"][0]["prompt"] = "different prompt"
    if changed == "model":
        config["model"]["model"] = "changed-model"
    if changed == "profile":
        config["execution_profile"]["num_worker"] = 2
    if changed == "bound":
        frozen["call_bound"]["max_calls"] = 1
    if changed == "scorer":
        frozen["manifest"]["evaluation"]["scorer_version"] = "forged"
    if changed == "parser":
        frozen["manifest"]["external_benchmark"]["parser_version"] = "forged"
    # Memory repository returns copies, so deliberately model corrupted persistence.
    for name, value in vars(store.experiments).items():
        if isinstance(value, dict) and cell["cell_id"] in value:
            value[cell["cell_id"]] = saved
            break
    else:
        pytest.fail("test must mutate the persisted Cell, not its read copy")
    before = deepcopy(store.runs.list())
    replay = ExperimentService(store, RunService(store), resources=None)
    with pytest.raises(ExperimentError, match="FROZEN_INPUT_INVALID"):
        if operation == "allocate":
            replay.allocate("ceval-matrix", "1")
        else:
            replay.retry_cell(cell["cell_id"], reason="must reject corrupt replay")
    assert store.runs.list() == before
    assert all(store.external_jobs.jobs_for_run(run["id"]) == [] for run in before)


def test_ceval_preview_model_drift_rejects_before_writes():
    store, resources, service, _ = environment()
    payload = suite_spec()
    preview = service.preview(payload)
    model = resources.models.get("model-a")
    model["parameters"]["temperature"] = 0.5
    resources.models._rows[("model-a",)] = deepcopy(model)  # simulate out-of-band resource drift
    with pytest.raises(ExperimentError, match="PREVIEW_STALE"):
        service.create(payload, expected_preview_hash=preview["preview_hash"])
    assert_empty(store)


def test_runner_identity_checks_dependencies_and_source_bytes(tmp_path, monkeypatch):
    from motte_benchmark.opencompass import execution

    profile = execution.freeze_execution_profile(PROFILE)
    monkeypatch.setattr(
        execution, "sys", SimpleNamespace(version_info=(3, 10, 20), platform="linux")
    )
    monkeypatch.setattr(execution.metadata, "version", lambda name: "forged-version")
    with pytest.raises(ValueError, match="dependency mismatch"):
        execution.verify_installed_profile(profile)
    monkeypatch.setattr(
        execution.metadata, "version", lambda name: profile["pinned_identity"]["dependencies"][name]
    )
    bad = tmp_path / "bad.py"
    bad.write_text("# different installed implementation\n")
    monkeypatch.setattr(
        execution.metadata, "distribution", lambda name: SimpleNamespace(locate_file=lambda p: bad)
    )
    with pytest.raises(ValueError, match="installed source mismatch"):
        execution.verify_installed_profile(profile)


def test_bridge_rejects_installed_identity_before_cli_or_data_writes(tmp_path, monkeypatch):
    from motte_benchmark.opencompass import entry, execution

    store, _, service, _ = environment()
    service.create(suite_spec())
    config = store.experiments.list_cells("ceval-matrix", "1")[0]["prepared_run"]["manifest"][
        "external_benchmark"
    ]["runner_config"]
    (tmp_path / "runner-config.json").write_text(json.dumps(config))
    monkeypatch.setenv("MOTTE_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(
        execution,
        "verify_installed_profile",
        lambda p: (_ for _ in ()).throw(ValueError("EXECUTION_PROFILE_INVALID")),
    )
    monkeypatch.setattr(
        entry, "_run_cli", lambda *a: pytest.fail("identity failure must not launch CLI")
    )
    assert entry.main([]) == 3
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "opencompass-config.py").exists()


def test_profile_rejects_boolean_substitution_even_with_rehashed_config():
    from motte_benchmark.opencompass.execution import validate_execution_config
    from motte_contracts.hashing import canonical_hash

    store, _, service, _ = environment()
    service.create(suite_spec())
    config = store.experiments.list_cells("ceval-matrix", "1")[0]["prepared_run"]["manifest"][
        "external_benchmark"
    ]["runner_config"]
    config["execution_profile"]["n"] = True
    config["config_hash"] = canonical_hash(
        {key: value for key, value in config.items() if key != "config_hash"}
    )
    with pytest.raises(ValueError, match="PROFILE"):
        validate_execution_config(config)


@pytest.mark.parametrize("failure", ["mixed", "503", "json", "redirect", "connection"])
def test_actual_requests_localhost_sends_bound_concurrently(monkeypatch, failure):
    """Exercise the real Session/HTTPAdapter/urllib3 path, never a paid provider."""
    from concurrent.futures import ThreadPoolExecutor
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import socket
    import threading
    from motte_benchmark.opencompass.execution import bounded_generate, TransportBudgetExhausted

    counts, paths = {}, []
    lock = threading.Lock()

    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            prompt = payload["messages"][0]["content"]
            with lock:
                paths.append(self.path)
                counts[prompt] = counts.get(prompt, 0) + 1
                count = counts[prompt]
            if failure == "connection":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            status, body = 503, b"failure"
            if failure == "mixed" and count == 2:
                status, body = 200, b'{"choices":[{"message":{"content":"B"}}]}'
            elif failure == "redirect":
                status = 308
            elif failure == "json":
                status, body = 200, b"invalid-json"
            self.send_response(status)
            if failure == "redirect":
                self.send_header("Location", "/forbidden")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    model = _model(2)
    model.url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1/blocked-proxy")

    def call(prompt):
        if failure == "mixed":
            assert bounded_generate(model, prompt, 17, 0) == "B"
        else:
            with pytest.raises(TransportBudgetExhausted):
                bounded_generate(model, prompt, 17, 0)

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(call, ["one", "two", "three"]))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert counts == {"one": 2, "two": 2, "three": 2}
    assert paths == ["/v1/chat/completions"] * 6


def test_bounded_job_rejects_arbitrary_runner_before_workdir_or_launch(tmp_path):
    from motte_benchmark.opencompass.adapter import CevalJobAdapter
    from motte_sdk.execution_backends import external_job_spec_from_run

    store, _, service, _ = environment()
    service.create(suite_spec())
    run = store.runs.list()[0]
    spec = external_job_spec_from_run(run, work_root=str(tmp_path / "jobs"))
    adapter = CevalJobAdapter(argv=["/bin/echo", "unproved runner"])
    with pytest.raises(ValueError, match="EXECUTION_PROFILE_INVALID"):
        adapter.prepare(spec)
    assert not (tmp_path / "jobs").exists()
    assert adapter.start_calls == []


def test_external_job_retry_identity_equals_actual_transport_policy():
    from motte_sdk.execution_backends import external_job_spec_from_run

    store, _, service, _ = environment()
    service.create(suite_spec())
    spec = external_job_spec_from_run(store.runs.list()[0], work_root="/tmp/unused")
    assert spec.retry_policy.model_dump() == {"runner": 0, "provider_transport": 1, "operator": 0}


def test_bounded_profile_uses_complete_linux_lock_without_changing_existing_pins():
    from pathlib import Path
    from motte_benchmark.opencompass.execution import freeze_execution_profile

    root = Path(__file__).resolve().parents[2]
    original = root / "scripts/runner/opencompass-0.4.2-py310.lock"
    linux = root / "scripts/runner/opencompass-0.4.2-linux-x86_64-py310.lock"
    assert linux.exists(), "Linux transitive dependency closure is missing"

    def parse(file):
        return dict(line.split("==", 1) for line in file.read_text().splitlines() if "==" in line)

    old, completed = parse(original), parse(linux)
    assert all(completed[name] == version for name, version in old.items())
    assert len(old) == 128 and len(completed) == 147
    profile = freeze_execution_profile(PROFILE)
    assert profile["pinned_identity"]["dependencies"] == completed
    assert profile["pinned_identity"]["platform"] == "linux-x86_64"
    assert (
        "opencompass-0.4.2-linux-x86_64-py310.lock"
        in (root / "scripts/runner/install-opencompass").read_text()
    )


def test_ceval_environment_digest_attests_frozen_implementation_not_old_version_label():
    from motte_contracts.hashing import canonical_hash

    store, _, service, _ = environment()
    service.create(suite_spec())
    external = store.runs.list()[0]["manifest"]["external_benchmark"]
    expected = canonical_hash(external["runner_config"]["execution_profile"])
    assert external["environment_digest"] == expected
    assert external["profile"]["environment_digest"] == expected
    assert external["runner_config"]["environment_digest"] == expected


def test_ceval_profile_freezes_default_credential_reference_for_worker_env(monkeypatch):
    from motte_benchmark.env_boundary import (
        declared_env_refs,
        plan_process_env,
        verify_process_env_boundary,
    )

    store, _, service, _ = environment()
    service.create(suite_spec())
    config = store.runs.list()[0]["manifest"]["external_benchmark"]["runner_config"]
    assert config["credentials"]["api_key"] == "env:OPENAI_API_KEY"
    assert "synthetic-default" not in json.dumps(config)
    env, boundary = plan_process_env(
        {"OPENAI_API_KEY": "synthetic-default", "UNRELATED_SECRET": "must-not-forward"},
        declared=declared_env_refs(config),
        injected={},
        identity={},
    )
    verify_process_env_boundary(boundary)
    assert env["OPENAI_API_KEY"] == "synthetic-default"
    assert "UNRELATED_SECRET" not in env
