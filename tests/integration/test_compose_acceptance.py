"""Offline safety tests for the real-Docker acceptance controller.

Fake command boundaries check orchestration and cleanup, never count as Docker
acceptance. CI runs scripts/compose_acceptance.py with the real engine separately.
"""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
PROJECT = "m8-accept-" + "a" * 32
IMAGE_HEALTHCHECK = ('python -c "import urllib.request; '
    "urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)\"")


def controller():
    path = ROOT / "scripts/compose_acceptance.py"
    assert path.is_file(), "real Compose acceptance controller is missing"
    spec = importlib.util.spec_from_file_location("compose_acceptance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def base_model():
    image = {"image": "motteavl:local", "build": {"context": "/archive"},
             "networks": {"default": None}}
    app = {**image, "volumes": [{"type": "bind", "source": "/archive/var/artifacts",
                                "target": "/workspace/var/artifacts"}],
           "environment": {"MOTTE_STORAGE": "postgres", "ARTIFACT_ROOT": "/workspace/var/artifacts",
                           "DATABASE_URL": "postgresql://motteavl:motteavl@postgres:5432/motteavl"},
           "depends_on": {"migrate": {"condition": "service_completed_successfully"}}}
    return {"name": "infra", "services": {
        "app-image": {**image, "profiles": ["build"]},
        "migrate": {**image, "command": ["alembic", "upgrade", "head"],
                    "healthcheck": {"disable": True},
                    "depends_on": {"postgres": {"condition": "service_healthy"}}},
        "api": {**app, "command": ["python", "-m", "uvicorn"],
                "ports": [{"target": 8000, "published": "8000", "host_ip": "127.0.0.1"}]},
        "worker": {**deepcopy(app), "command": ["python", "-m", "apps.worker.motte_worker"],
                   "healthcheck": {"disable": True}},
        "postgres": {"image": "postgres:16-alpine", "networks": {"default": None},
                     "volumes": [{"type": "volume", "source": "postgres-data",
                                  "target": "/var/lib/postgresql/data"}],
                     "ports": [{"target": 5432, "published": "5432"}],
                     "healthcheck": {"test": ["CMD", "pg_isready"]}},
        "redis": {"image": "redis:7-alpine"},
        "otel-collector": {"image": "otel/opentelemetry-collector:0.109.0"},
    }, "volumes": {"postgres-data": {"name": "infra_postgres-data"}},
        "networks": {"default": {"name": "infra_default"}}}


def test_image_health_probe_preserves_standalone_api_contract():
    instructions = [line.strip()
                    for line in (ROOT / "Dockerfile").read_text().splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
    assert [line for line in instructions if line.startswith("HEALTHCHECK ")] == [
        "HEALTHCHECK CMD " + IMAGE_HEALTHCHECK]


def test_shipped_non_http_roles_disable_inherited_api_probe():
    services = yaml.safe_load((ROOT / "infra/docker-compose.yml").read_text())["services"]
    assert "healthcheck" not in services["api"]  # Inherit the unchanged image probe.
    for name in ("worker", "migrate"):
        assert services[name].get("healthcheck") == {"disable": True}
    assert services["postgres"]["healthcheck"]["test"] == [
        "CMD-SHELL", "pg_isready -U motteavl -d motteavl"]


def test_acceptance_preserves_shipped_service_health_semantics():
    services = yaml.safe_load((ROOT / "infra/docker-compose.yml").read_text())["services"]
    model = base_model()
    # Copy role exceptions from shipped configuration, never add a test-only override.
    for name in controller().SERVICES:
        model["services"][name].pop("healthcheck", None)
        if "healthcheck" in services[name]:
            model["services"][name]["healthcheck"] = deepcopy(services[name]["healthcheck"])
    isolated = controller().isolate_model(model, PROJECT)
    assert "healthcheck" not in isolated["services"]["api"]
    for name in ("worker", "migrate"):
        assert isolated["services"][name].get("healthcheck") == {"disable": True}
    for name in controller().SERVICES:
        assert isolated["services"][name].get("healthcheck") == services[name].get("healthcheck")


def test_isolation_retains_commands_dependencies_and_health_but_owns_every_resource():
    mod = controller()
    original = base_model()
    before = deepcopy(original)
    isolated = mod.isolate_model(original, PROJECT)
    assert original == before
    assert set(isolated["services"]) == {"app-image", "migrate", "api", "worker", "postgres"}
    for name, service in isolated["services"].items():
        for field in ("command", "depends_on", "healthcheck", "environment"):
            assert service.get(field) == original["services"][name].get(field)
        assert not any(v["type"] == "bind" for v in service.get("volumes", []))
        for port in service.get("ports", []):
            assert port["host_ip"] == "127.0.0.1" and port["published"] == "0"
    assert isolated["services"]["api"]["image"] == f"{PROJECT}:local"
    assert isolated["services"]["worker"]["volumes"] == [
        {"type": "volume", "source": "artifacts", "target": "/workspace/var/artifacts"}]
    assert isolated["volumes"] == {"postgres-data": {}, "artifacts": {}}
    assert isolated["networks"] == {"default": {"internal": True}, "api-loopback": {}}
    assert isolated["services"]["postgres"].get("ports", []) == []


@pytest.mark.parametrize("project", ["default", "motteavl", "m8-accept-", "../m8-accept-x"])
def test_unowned_project_names_are_rejected(project):
    with pytest.raises(ValueError, match="project"):
        controller().isolate_model(base_model(), project)


@pytest.mark.parametrize("field,value", [
    ("privileged", True), ("network_mode", "host"), ("secrets", ["host-token"]),
    ("env_file", ["/home/user/.env"]), ("devices", ["/dev/sda"]),
    ("volumes", [{"type": "bind", "source": "/var/run/docker.sock", "target": "/socket"}]),
])
def test_unexpected_host_access_is_rejected_before_creating_resources(field, value):
    model = base_model()
    model["services"]["worker"][field] = value
    with pytest.raises(ValueError, match="unsafe|unexpected"):
        controller().isolate_model(model, PROJECT)


def test_host_secrets_remote_daemon_and_dotenv_are_not_inherited(tmp_path):
    environment = controller().clean_environment(tmp_path, {
        "PATH": "/usr/bin", "HOME": "/home/user", "DOCKER_HOST": "tcp://production:2376",
        "DOCKER_CONTEXT": "production", "DOCKER_CONFIG": "/home/user/.docker",
        "COMPOSE_FILE": "/production.yml", "COMPOSE_PROJECT_NAME": "prod",
        "DATABASE_URL": "secret", "OPENAI_API_KEY": "secret", "HTTP_PROXY": "secret",
    })
    assert environment == {"PATH": "/usr/bin", "HOME": str(tmp_path),
                           "DOCKER_CONFIG": str(tmp_path / "docker"),
                           "COMPOSE_DISABLE_ENV_FILE": "1", "COMPOSE_ANSI": "never"}


def test_http_rejects_non_loopback_and_redirects():
    mod = controller()
    for base in ["http://example.com:8000", "http://localhost:8000", "https://127.0.0.1:8000"]:
        with pytest.raises(ValueError, match="loopback"):
            mod.API(base)
    assert mod.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "http://evil") is None


def test_report_requires_exact_pass_mixed_outcomes_and_no_provider_cost():
    mod = controller()
    report = {"run_id": "run-a", "status": "completed", "scoring_pass_id": "pass-a",
              "summary": {"cases": 2, "scored": 2, "passed": 1, "failed": 1},
              "cost": {"total": None}, "scores": [
                  {"case_id": "accept-pass", "passed": True},
                  {"case_id": "accept-fail", "passed": False}]}
    mod.validate_report(report, "run-a", "pass-a")
    for field, value in [("scoring_pass_id", "wrong"), ("cost", {"total": 1}),
                         ("scores", []), ("summary", {"cases": 0})]:
        with pytest.raises(RuntimeError):
            mod.validate_report({**report, field: value}, "run-a", "pass-a")


def worker_lifecycle_probe(tmp_path, *, rows=None, emulate_wait_failure=True):
    """Script only external command/HTTP boundaries; run the real controller stages."""
    from types import SimpleNamespace
    mod = controller()
    probe = mod.Acceptance(ROOT, tmp_path / "receipt", project=PROJECT)
    report = {"run_id": "run-a", "status": "completed", "scoring_pass_id": "pass-a",
              "summary": {"cases": 2, "scored": 2, "passed": 1, "failed": 1},
              "cost": {"total": None}, "scores": [
                  {"case_id": "accept-pass", "passed": True},
                  {"case_id": "accept-fail", "passed": False}]}
    calls, artifacts = [], []
    rows = [{"Service": "worker", "State": "running", "ExitCode": 0}] if rows is None else rows
    def compose(*args, **kwargs):
        calls.append(args)
        if emulate_wait_failure and "up" in args and "--wait" in args and "worker" in args:
            raise RuntimeError("container worker has no healthcheck configured")
        output = json.dumps(rows) if args == ("ps", "--all", "--format", "json", "worker") else ""
        return subprocess.CompletedProcess(args, 0, output, "")
    def request(path, body=None, **kwargs):
        if path == "/api/v1/runs" and body is not None:
            return {"id": "run-a", "status": "queued"}
        if path == "/api/v1/runs/run-a":
            return {"status": "completed", "current_scoring_pass_id": "pass-a",
                    "manifest": {"execution": {"backend_id": "replay"}}}
        if path == "/api/v1/runs/run-a/report?scoring_pass_id=pass-a":
            return deepcopy(report)
        if path == "/api/v1/runs/run-a/scoring-passes":
            return {"total": 1, "items": [{"id": "pass-a"}]}
        if path == "/health":
            return {"status": "ok"}
        pytest.fail(f"unexpected request: {path}")
    probe.compose = compose
    probe.api = SimpleNamespace(request=request)
    probe.connect_api = lambda: probe.api
    probe.artifact = lambda service, write=False: artifacts.append((service, write))
    probe.sql = lambda _: json.dumps({"runs": 1, "passes": 1})
    probe.run_id, probe.pass_id, probe.report = "run-a", "pass-a", report
    return probe, calls, artifacts


@pytest.mark.parametrize("stage", ["exercise", "restart"])
def test_non_http_worker_never_uses_compose_health_wait(tmp_path, stage):
    probe, calls, artifacts = worker_lifecycle_probe(tmp_path)
    getattr(probe, stage)()
    worker_up = [call for call in calls if call[0] == "up" and "worker" in call]
    assert len(worker_up) == 1 and "--wait" not in worker_up[0]
    assert ("ps", "--all", "--format", "json", "worker") in calls
    assert ("worker", False) in artifacts and ("api", stage == "exercise") in artifacts
    if stage == "restart":
        assert "--force-recreate" in worker_up[0]
        assert any(call[0] == "up" and "api" in call and "--wait" in call for call in calls)
        assert any(call[0] == "up" and "postgres" in call and "--wait" in call for call in calls)
        assert "postgres-restart-container-recreate-fixed-pass-persistence" in probe.receipt["checks"]
    else:
        assert "worker-completed-fixed-pass-and-shared-artifact" in probe.receipt["checks"]
        assert (probe.output / "fixed-pass-report.json").is_file()


@pytest.mark.parametrize("stage", ["exercise", "restart"])
@pytest.mark.parametrize("rows", [[], [{"Service": "worker", "State": "exited", "ExitCode": 0}],
    [{"Service": "worker", "State": "exited", "ExitCode": 1}],
    [{"Service": "worker", "State": "restarting", "ExitCode": 0}],
    [{"Service": "worker", "State": "running"}],
    [{"Service": "worker", "State": "running", "ExitCode": 0}] * 2])
def test_worker_process_state_cannot_be_replaced_by_a_completed_report(tmp_path, stage, rows):
    probe, _, artifacts = worker_lifecycle_probe(tmp_path, rows=rows, emulate_wait_failure=False)
    with pytest.raises(RuntimeError, match="worker.*running"):
        getattr(probe, stage)()
    assert ("worker", False) not in artifacts
    assert not any("persistence" in check or "worker-completed" in check for check in probe.receipt["checks"])


def test_worker_exit_during_queued_replay_is_detected_before_completion(tmp_path, monkeypatch):
    probe, _, artifacts = worker_lifecycle_probe(tmp_path)
    compose, request = probe.compose, probe.api.request
    states = iter(["running", "exited"])
    statuses = iter(["queued", "completed"])
    def changing_compose(*args, **kwargs):
        result = compose(*args, **kwargs)
        if args == ("ps", "--all", "--format", "json", "worker"):
            result.stdout = json.dumps([{"Service": "worker", "State": next(states), "ExitCode": 0}])
        return result
    def changing_request(path, *args, **kwargs):
        result = request(path, *args, **kwargs)
        if path == "/api/v1/runs/run-a":
            result["status"] = next(statuses)
        return result
    probe.compose, probe.api.request = changing_compose, changing_request
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="worker.*running"):
        probe.exercise()
    assert ("worker", False) not in artifacts
    assert next(statuses) == "completed"  # A stale completed report cannot mask process exit.


@pytest.mark.parametrize("failure_stage", ["build", "startup", "health", "exercise", "restart"])
def test_failure_always_collects_logs_and_removes_only_owned_resources(tmp_path, failure_stage):
    mod = controller()
    calls = []
    probe = mod.Acceptance(ROOT, tmp_path / "receipt", project=PROJECT)
    probe.prepare = lambda: None
    probe.compose_file = tmp_path / "compose.json"
    probe.compose_file.write_text("{}")
    probe.owned = True
    def command(args, **kwargs):
        calls.append(args)
        output = "image-id" if args[:2] == ["image", "ls"] else ""
        return subprocess.CompletedProcess(args, 0, output, "")
    probe.command = command
    def stage(name):
        def run():
            if name == failure_stage:
                raise RuntimeError("injected " + name)
        return run
    probe.build = stage("build")
    probe.startup = stage("startup")
    probe.health = stage("health")
    probe.exercise = stage("exercise")
    probe.restart = stage("restart")
    assert probe.run() == 1
    assert any("logs" in call for call in calls)
    down = next(call for call in calls if "down" in call)
    assert down[:4] == ["compose", "--project-name", PROJECT, "--env-file"]
    assert "--volumes" in down and "--remove-orphans" in down
    assert not any("prune" in call for call in calls)
    assert ["image", "rm", f"{PROJECT}:local"] in calls
    receipt = json.loads((tmp_path / "receipt/receipt.json").read_text())
    assert receipt["status"] == "failed" and receipt["cleanup"] == "passed"
    assert failure_stage in receipt["error"]


def test_missing_daemon_is_a_failure_never_a_skip_or_cleanup_of_unknown_state(tmp_path):
    mod = controller()
    probe = mod.Acceptance(ROOT, tmp_path / "receipt", project=PROJECT)
    calls = []
    def command(args, **kwargs):
        calls.append(args)
        raise RuntimeError("Docker daemon unavailable")
    probe.command = command
    assert probe.run() == 1
    receipt = json.loads((tmp_path / "receipt/receipt.json").read_text())
    assert receipt["status"] == "failed"
    assert receipt["cleanup"] == "not_needed"
    assert not any("up" in call or "down" in call for call in calls)


def test_cleanup_failure_prevents_passing_receipt(tmp_path):
    mod = controller()
    probe = mod.Acceptance(ROOT, tmp_path / "receipt", project=PROJECT)
    probe.prepare = lambda: None
    probe.compose_file = tmp_path / "compose.json"
    probe.compose_file.write_text("{}")
    probe.owned = True
    for method in ("build", "startup", "health", "exercise", "restart"):
        setattr(probe, method, lambda: None)
    def command(args, **kwargs):
        if "down" in args:
            raise RuntimeError("cleanup unavailable")
        return subprocess.CompletedProcess(args, 0, "", "")
    probe.command = command
    assert probe.run() == 1
    receipt = json.loads((tmp_path / "receipt/receipt.json").read_text())
    assert receipt["status"] == "failed" and receipt["cleanup"] == "failed"


def test_ci_runs_real_controller_and_always_uploads_evidence():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "compose-acceptance:" in text
    job = text.split("  compose-acceptance:\n", 1)[1]
    assert "runs-on: ubuntu-latest" in job
    assert "python scripts/compose_acceptance.py" in job
    assert "if: always()" in job and "actions/upload-artifact@v4" in job
    assert "continue-on-error" not in job


def test_api_port_uses_separate_bridge_while_worker_stays_on_internal_network():
    # Docker does not publish ports on an internal-only bridge. Only the API,
    # which queues this replay request, gets the separate loopback-published edge.
    model = controller().isolate_model(base_model(), PROJECT)
    assert model["networks"] == {"default": {"internal": True}, "api-loopback": {}}
    assert model["services"]["api"]["networks"] == {"default": None, "api-loopback": None}
    assert model["services"]["worker"]["networks"] == {"default": None}


def test_resource_checks_include_stopped_containers(tmp_path):
    mod = controller()
    probe = mod.Acceptance(ROOT, tmp_path / "receipt", project=PROJECT)
    calls = []
    def command(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    probe.command = command
    probe.resources()
    assert "--all" in next(args for args in calls if args[0] == "container")


def test_no_daemon_receipt_still_identifies_exact_source_commit(tmp_path):
    mod = controller()
    probe = mod.Acceptance(ROOT, tmp_path / "receipt", project=PROJECT)
    def command(args, **kwargs):
        raise RuntimeError("Docker daemon unavailable")
    probe.command = command
    assert probe.run() == 1
    receipt = json.loads((tmp_path / "receipt/receipt.json").read_text())
    assert receipt["tested_sha"] == subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def test_missing_built_image_does_not_turn_successful_resource_cleanup_into_failure(tmp_path):
    mod = controller()
    probe = mod.Acceptance(ROOT, tmp_path / "receipt", project=PROJECT)
    probe.owned = True
    def command(args, **kwargs):
        assert args[:2] != ["image", "rm"], "no image exists; no removal is needed"
        return subprocess.CompletedProcess(args, 0, "", "")
    probe.command = command
    probe.cleanup()
    assert probe.receipt["cleanup"] == "passed"


def test_replay_fixture_is_accepted_and_reported_by_actual_api_worker(tmp_path):
    from fastapi.testclient import TestClient
    from apps.api.app.main import create_app
    from apps.worker.motte_worker.runtime import WorkerLoop
    from motte_storage.run_store import SQLiteRunStore

    mod = controller()
    app = create_app(SQLiteRunStore(tmp_path / "runs.db"))
    client = TestClient(app)
    body = mod.synthetic_request(PROJECT)
    assert set(body["manifest"]) == {"replay_fixture"}
    created = client.post("/api/v1/runs", json=body)
    assert created.status_code == 202
    assert created.json()["status"] == "queued"
    result = WorkerLoop(app.state.run_service, scoring_jobs=None).claim_and_execute(created.json()["id"])
    assert result["manifest"]["execution"]["backend_id"] == "replay"
    pass_id = result["current_scoring_pass_id"]
    report = client.get(f"/api/v1/runs/{result['id']}/report?scoring_pass_id={pass_id}").json()
    mod.validate_report(report, result["id"], pass_id)


def test_ci_has_independent_always_cleanup_after_controller_timeout():
    text = (ROOT / ".github/workflows/ci.yml").read_text().split("  compose-acceptance:\n", 1)[1]
    assert "--cleanup-only" in text
    assert text.index("--cleanup-only") < text.index("actions/upload-artifact@v4")
    assert text.count("if: always()") >= 2
    assert "timeout-minutes: 15" in text and "timeout-minutes: 25" in text


def test_cleanup_retry_uses_saved_owned_project_without_starting_services(tmp_path, monkeypatch):
    mod = controller()
    output = tmp_path / "receipt"
    probe = mod.Acceptance(ROOT, output, project=PROJECT)
    probe.owned = True
    probe.compose_file.write_text(json.dumps(mod.isolate_model(base_model(), PROJECT)))
    probe.receipt["status"] = "failed"
    probe.save()
    calls = []
    def command(self, args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(mod.Acceptance, "command", command)
    assert mod.cleanup_previous(ROOT, output) == 0
    assert any("down" in args for args in calls)
    assert not any("up" in args or "build" in args or "pull" in args for args in calls)
    receipt = json.loads((output / "receipt.json").read_text())
    assert receipt["status"] == "failed"  # successful cleanup cannot erase the failure
    assert receipt["cleanup"] == "passed" and receipt["project"] == PROJECT


@pytest.mark.parametrize("alteration", ["project", "volume", "service", "network"])
def test_cleanup_retry_rejects_changed_ownership_metadata(tmp_path, monkeypatch, alteration):
    mod = controller()
    output = tmp_path / "receipt"
    probe = mod.Acceptance(ROOT, output, project=PROJECT)
    probe.owned = True
    model = mod.isolate_model(base_model(), PROJECT)
    if alteration == "project":
        model["name"] = "production"
    elif alteration == "volume":
        model["volumes"]["artifacts"] = {"name": "production-artifacts"}
    elif alteration == "service":
        model["services"]["api"]["container_name"] = "production-api"
    else:
        model["networks"]["default"] = {"name": "production-network"}
    probe.compose_file.write_text(json.dumps(model))
    probe.save()
    def command(self, args, **kwargs):
        pytest.fail("unsafe saved config reached Docker")
    monkeypatch.setattr(mod.Acceptance, "command", command)
    with pytest.raises(ValueError, match="ownership"):
        mod.cleanup_previous(ROOT, output)


def test_cleanup_retry_without_receipt_has_nothing_to_remove(tmp_path):
    assert controller().cleanup_previous(ROOT, tmp_path / "absent") == 0


@pytest.mark.parametrize("mutation", ["public-port", "static-port", "bind-mount", "image",
                                      "environment", "internal-network", "privileged"])
def test_effective_compose_configuration_is_verified_before_startup(mutation):
    mod = controller()
    model = mod.isolate_model(base_model(), PROJECT)
    if mutation == "public-port":
        model["services"]["api"]["ports"][0]["host_ip"] = "0.0.0.0"
    elif mutation == "static-port":
        model["services"]["api"]["ports"][0]["published"] = "8000"
    elif mutation == "bind-mount":
        model["services"]["worker"]["volumes"][0]["type"] = "bind"
    elif mutation == "image":
        model["services"]["worker"]["image"] = "unowned:latest"
    elif mutation == "environment":
        model["services"]["worker"]["environment"]["OPENAI_API_KEY"] = "host-secret"
    elif mutation == "internal-network":
        model["networks"]["default"]["internal"] = False
    else:
        model["services"]["worker"]["privileged"] = True
    with pytest.raises(ValueError, match="ownership|unsafe|unexpected"):
        mod.validate_owned_model(model, PROJECT)


def test_source_compose_parse_never_reads_service_env_files_or_interpolates_host_env(tmp_path):
    mod = controller()
    probe = mod.Acceptance(ROOT, tmp_path / "receipt", project=PROJECT)
    def command(args, **kwargs):
        if args[0] == "version":
            output = '{}'
        elif args[:2] == ["compose", "version"]:
            output = '2.40.0'
        elif "config" in args:
            assert "--no-env-resolution" in args
            assert "--no-interpolate" in args
            raise RuntimeError("bounded source parse reached")
        else:
            output = ''
        return subprocess.CompletedProcess(args, 0, output, '')
    probe.command = command
    try:
        with pytest.raises(RuntimeError, match="bounded source parse reached"):
            probe.prepare()
    finally:
        if probe.temporary is not None:
            probe.temporary.cleanup()


@pytest.mark.parametrize("changed", ["context", "dockerfile"])
def test_build_context_cannot_escape_the_exact_git_archive(changed):
    mod = controller()
    model = base_model()
    mod.validate_build_context(model, Path("/archive"))
    model["services"]["worker"]["build"][changed] = (
        "/home/user/private" if changed == "context" else "../../outside/Dockerfile")
    with pytest.raises(ValueError, match="archive"):
        mod.validate_build_context(model, Path("/archive"))


def test_interrupted_owned_receipt_write_preserves_journal_and_drives_exact_cleanup(
    tmp_path, monkeypatch,
):
    import io

    mod = controller()
    output = tmp_path / "receipt"
    probe = mod.Acceptance(ROOT, output, project=PROJECT)
    probe.compose_file.write_text(json.dumps(mod.isolate_model(base_model(), PROJECT)))
    probe.owned = True
    probe.save()
    journal = output / "receipt.json"
    previous = journal.read_bytes()
    original_open = io.open

    class InterruptedWriter:
        def __init__(self, stream):
            self.stream = stream
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def write(self, data):
            self.stream.write(data[:16])
            self.stream.flush()
            raise KeyboardInterrupt("interrupted partial receipt write")

    def interrupted_open(file, mode="r", *args, **kwargs):
        stream = original_open(file, mode, *args, **kwargs)
        return InterruptedWriter(stream) if "w" in mode else stream

    probe.receipt["stage"] = "after-resource-creation"
    with monkeypatch.context() as patch:
        patch.setattr(io, "open", interrupted_open)
        with pytest.raises(KeyboardInterrupt, match="partial receipt"):
            probe.save()
    assert journal.read_bytes() == previous, "the last good ownership journal was destroyed"
    assert json.loads(previous)["owned"] is True
    assert not list(output.glob(".receipt-*.tmp"))
    calls = []
    def command(self, args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(mod.Acceptance, "command", command)
    assert mod.cleanup_previous(ROOT, output) == 0
    down = next(args for args in calls if "down" in args)
    assert down[:4] == ["compose", "--project-name", PROJECT, "--env-file"]
    assert not any("up" in args or "build" in args or "prune" in args for args in calls)
    restored = json.loads(journal.read_text())
    assert restored["project"] == PROJECT and restored["cleanup"] == "passed"


def test_receipt_replacement_is_complete_synced_and_atomic(tmp_path, monkeypatch):
    mod = controller()
    probe = mod.Acceptance(ROOT, tmp_path / "receipt", project=PROJECT)
    probe.owned = True
    probe.save()
    journal = probe.output / "receipt.json"
    previous = journal.read_bytes()
    original_replace, original_fsync = mod.os.replace, mod.os.fsync
    observed = []
    def fsync(fd):
        original_fsync(fd)
        observed.append("synced")
    def replace(source, target):
        assert observed == ["synced"]
        assert Path(source).parent == journal.parent and Path(target) == journal
        assert journal.read_bytes() == previous
        assert json.loads(Path(source).read_text())["stage"] == "new-complete-receipt"
        observed.append("replaced")
        return original_replace(source, target)
    monkeypatch.setattr(mod.os, "fsync", fsync)
    monkeypatch.setattr(mod.os, "replace", replace)
    probe.receipt["stage"] = "new-complete-receipt"
    probe.save()
    assert observed == ["synced", "replaced"]
    assert json.loads(journal.read_text())["stage"] == "new-complete-receipt"
    assert not list(probe.output.glob(".receipt-*.tmp"))


@pytest.mark.parametrize("failure", ["fsync", "replace"])
@pytest.mark.parametrize("unlink_fails", [False, True])
def test_receipt_publication_failure_keeps_previous_journal_and_reports_cleanup_error(
    tmp_path, monkeypatch, failure, unlink_fails,
):
    mod = controller()
    probe = mod.Acceptance(ROOT, tmp_path / "receipt", project=PROJECT)
    probe.owned = True
    probe.save()
    journal = probe.output / "receipt.json"
    previous = journal.read_bytes()
    primary = OSError("injected " + failure + " failure")
    def fail(*args, **kwargs):
        raise primary
    monkeypatch.setattr(mod.os, failure, fail)
    real_unlink = Path.unlink
    def unlink(path, *args, **kwargs):
        if path.name.startswith(".receipt-") and unlink_fails:
            raise OSError("injected temporary cleanup failure")
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    probe.receipt["stage"] = "not-published"
    with pytest.raises(OSError) as raised:
        probe.save()
    assert raised.value is primary
    assert journal.read_bytes() == previous
    assert json.loads(journal.read_text())["owned"] is True
    leftovers = list(probe.output.glob(".receipt-*.tmp"))
    if unlink_fails:
        assert len(leftovers) == 1
        assert any("temporary cleanup failure" in note for note in raised.value.__notes__)
    else:
        assert not leftovers


def test_failed_first_ownership_publication_never_reaches_resource_creation(tmp_path, monkeypatch):
    mod = controller()
    probe = mod.Acceptance(ROOT, tmp_path / "receipt", project=PROJECT)
    original_fsync = mod.os.fsync
    def fsync(fd):
        if probe.owned:
            raise OSError("ownership journal cannot be published")
        original_fsync(fd)
    monkeypatch.setattr(mod.os, "fsync", fsync)
    def prepare():
        probe.owned = True
        probe.save()
    probe.prepare = prepare
    probe.command = lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "", "")
    probe.build = lambda: pytest.fail("resources created without a published ownership journal")
    with pytest.raises(OSError, match="ownership journal"):
        probe.run()
    assert json.loads((probe.output / "receipt.json").read_text())["owned"] is False
