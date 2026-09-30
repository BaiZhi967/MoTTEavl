#!/usr/bin/env python3
"""Real, isolated Compose acceptance. No mocks, providers, or host data in this run.

Run on a disposable Linux Docker host: python scripts/compose_acceptance.py
The separate offline tests prove controller boundaries, not container acceptance.
Only the selected Git commit enters the build context. The source Compose commands,
health checks and migration dependencies are preserved; isolation changes are checked.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from uuid import uuid4


SERVICES = ("app-image", "postgres", "migrate", "api", "worker")
ARTIFACT_PATH = "m8-compose/synthetic.txt"
ARTIFACT_CONTENT = "m8-compose-acceptance: synthetic, no provider calls\n"
DSN = "postgresql://motteavl:motteavl@postgres:5432/motteavl"


def require_project(project: str) -> None:
    if not re.fullmatch(r"m8-accept-[a-f0-9]{32}", project):
        raise ValueError("unsafe project name; only generated acceptance projects are allowed")


def clean_environment(home: Path, source: dict[str, str]) -> dict[str, str]:
    return {"PATH": source.get("PATH", os.defpath), "HOME": str(home),
            "DOCKER_CONFIG": str(home / "docker"),
            "COMPOSE_DISABLE_ENV_FILE": "1", "COMPOSE_ANSI": "never"}


def isolate_model(original: dict, project: str) -> dict:
    """Preserve runtime semantics, reject unexpected privileges, replace only isolation."""
    require_project(project)
    if original.get("secrets") or original.get("configs"):
        raise ValueError("unsafe external Compose configuration")
    model = deepcopy(original)
    model["name"] = project
    model["services"] = {name: model["services"][name] for name in SERVICES}
    allowed = {"image", "build", "profiles", "command", "entrypoint", "environment",
               "depends_on", "working_dir", "volumes", "ports", "healthcheck", "networks"}
    allowed_env = {"DATABASE_URL": DSN, "MOTTE_STORAGE": "postgres",
                   "ARTIFACT_ROOT": "/workspace/var/artifacts", "REDIS_URL": "redis://redis:6379/0",
                   "POSTGRES_USER": "motteavl", "POSTGRES_PASSWORD": "motteavl",
                   "POSTGRES_DB": "motteavl"}
    for name, service in model["services"].items():
        if set(service) - allowed:
            raise ValueError(f"unsafe/unexpected Compose fields on {name}")
        if service.get("entrypoint") is not None:
            raise ValueError(f"unexpected entrypoint on {name}")
        if set(service.get("networks", {"default": None})) != {"default"}:
            raise ValueError(f"unsafe networks on {name}")
        if set(service.get("depends_on", {})) - set(SERVICES):
            raise ValueError(f"unexpected dependency on {name}; extend acceptance deliberately")
        for key, value in service.get("environment", {}).items():
            if allowed_env.get(key) != value or key not in allowed_env:
                raise ValueError(f"unsafe/unexpected environment on {name}: {key}")
        build = service.get("build", {})
        if set(build) - {"context", "dockerfile"}:
            raise ValueError(f"unsafe build settings on {name}")
        volumes = service.get("volumes", [])
        expected_target = {"api": "/workspace/var/artifacts",
                           "worker": "/workspace/var/artifacts",
                           "postgres": "/var/lib/postgresql/data"}.get(name)
        if expected_target:
            if len(volumes) != 1 or volumes[0].get("target") != expected_target:
                raise ValueError(f"unexpected volumes on {name}")
            service["volumes"] = [{"type": "volume", "source": (
                "postgres-data" if name == "postgres" else "artifacts"),
                "target": expected_target}]
        elif volumes:
            raise ValueError(f"unsafe volumes on {name}")
        # API is the only published service. PostgreSQL stays private to this project.
        service.pop("ports", None)
        if name == "api":
            service["ports"] = [{"target": 8000, "published": "0", "host_ip": "127.0.0.1",
                                 "protocol": "tcp"}]
        if name != "postgres":
            service["image"] = f"{project}:local"
    # The worker has no external egress. Docker does not publish ports on internal-only
    # bridges, so the API also gets a project-owned bridge with a loopback-only port.
    # No real provider/model configuration or credentials enter either container.
    model["networks"] = {"default": {"internal": True}, "api-loopback": {}}
    model["services"]["api"]["networks"] = {"default": None, "api-loopback": None}
    model["volumes"] = {"postgres-data": {}, "artifacts": {}}
    return model


def validate_build_context(model: dict, archive: Path) -> None:
    for name in ("app-image", "migrate", "api", "worker"):
        build = model["services"][name].get("build", {})
        if (build.get("context") != str(archive)
                or build.get("dockerfile", "Dockerfile") != "Dockerfile"):
            raise ValueError(f"{name} build must use only the exact Git archive/Dockerfile")


def validate_owned_model(model: dict, project: str) -> None:
    """Check effective config and retry journal before any resources can be changed."""
    require_project(project)
    def invalid():
        raise ValueError("unsafe acceptance resource ownership/configuration")
    if (model.get("name") != project or set(model) - {"name", "services", "volumes", "networks"}
            or set(model.get("services", {})) != set(SERVICES)
            or set(model.get("volumes", {})) != {"postgres-data", "artifacts"}
            or set(model.get("networks", {})) != {"default", "api-loopback"}):
        invalid()
    for name, volume in model["volumes"].items():
        if set(volume) - {"name"} or volume.get("name", f"{project}_{name}") != f"{project}_{name}":
            invalid()
    for name, network in model["networks"].items():
        if (set(network) - {"name", "internal", "ipam"}
                or network.get("name", f"{project}_{name}") != f"{project}_{name}"
                or network.get("internal", False) is not (name == "default")
                or network.get("ipam", {})):
            invalid()
    for name, service in model["services"].items():
        expected_image = "postgres:16-alpine" if name == "postgres" else f"{project}:local"
        expected_networks = {"default", "api-loopback"} if name == "api" else {"default"}
        if service["image"] != expected_image or set(service.get("networks", {})) != expected_networks:
            invalid()
        ports = service.get("ports", [])
        if name == "api":
            if (len(ports) != 1 or ports[0].get("host_ip") != "127.0.0.1"
                    or ports[0].get("target") != 8000 or str(ports[0].get("published")) != "0"
                    or ports[0].get("protocol", "tcp") != "tcp"):
                invalid()
        elif ports:
            invalid()
        for volume in service.get("volumes", []):
            expected_source = "postgres-data" if name == "postgres" else "artifacts"
            if volume.get("type") != "volume" or volume.get("source") != expected_source:
                invalid()
    # Reuse the source safety checks (env/build/command privileges/dependencies).
    # The API's second isolated-job bridge is the only deliberate network difference.
    source_shape = deepcopy(model)
    source_shape["services"]["api"]["networks"] = {"default": None}
    try:
        isolate_model(source_shape, project)
    except ValueError as error:
        raise ValueError("unsafe acceptance resource ownership/configuration") from error


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, base: str) -> None:
        if not re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]{0,4}", base):
            raise ValueError("API must use a numeric loopback address and dynamic HTTP port")
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path: str, body: dict | None = None, status: int = 200):
        request = urllib.request.Request(
            self.base + path, data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST",
        )
        with self.opener.open(request, timeout=10) as response:
            if response.status != status:
                raise RuntimeError(f"unexpected API status {response.status}: {path}")
            return json.load(response)


def synthetic_request(project: str) -> dict:
    require_project(project)
    return {"scenario_version": "replay@1", "case_ids": ["accept-pass", "accept-fail"],
            "manifest": {"replay_fixture": {
                "accept-pass": {"output": "synthetic-ok", "expected": "synthetic-ok"},
                "accept-fail": {"output": "synthetic-no", "expected": "synthetic-ok"}}},
            "request_key": project}


def validate_report(report: dict, run_id: str, pass_id: str) -> None:
    expected = {"cases": 2, "scored": 2, "passed": 1, "failed": 1}
    actual = report.get("summary", {})
    outcomes = {row["case_id"]: row.get("passed") for row in report.get("scores", [])}
    if (report.get("run_id") != run_id or report.get("status") != "completed"
            or report.get("scoring_pass_id") != pass_id
            or any(actual.get(key) != value for key, value in expected.items())
            or report.get("cost", {}).get("total", "missing") is not None
            or outcomes != {"accept-pass": True, "accept-fail": False}):
        raise RuntimeError("fixed scoring Pass report did not match the synthetic fixture")


class Acceptance:
    def __init__(self, root: Path, output: Path, *, project: str | None = None) -> None:
        self.root = root.resolve()
        self.output = output.resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.project = project or "m8-accept-" + uuid4().hex
        require_project(self.project)
        self.home = self.output / "isolated-home"
        (self.home / "docker").mkdir(parents=True, exist_ok=True)
        self.environment = clean_environment(self.home, os.environ)
        self.env_file = self.output / "empty.env"
        self.env_file.write_text("")
        self.compose_file = self.output / "compose.json"
        self.owned = False
        self.receipt = {"schema_version": 1, "project": self.project, "status": "running",
                        "started_at": datetime.now(UTC).isoformat(), "cleanup": "not_needed",
                        "provider_calls": 0, "fixture": "synthetic-replay-only", "checks": []}
        self.temporary = None

    def save(self) -> None:
        self.receipt["owned"] = self.owned
        payload = json.dumps(self.receipt, indent=2) + "\n"
        temporary = None
        try:
            # Publish a complete sibling atomically: interruption must never
            # truncate the last ownership journal needed by independent cleanup.
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.output,
                prefix=".receipt-", suffix=".tmp", delete=False,
            ) as journal:
                temporary = Path(journal.name)
                journal.write(payload)
                journal.flush()
                os.fsync(journal.fileno())
            os.replace(temporary, self.output / "receipt.json")
        except BaseException as error:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    error.add_note(f"temporary receipt cleanup also failed: {cleanup_error}")
            raise

    def command(self, args: list[str], *, timeout: int = 180) -> subprocess.CompletedProcess:
        # Explicit local socket/config prevent inherited contexts, remote hosts or credentials.
        argv = ["docker", "--config", str(self.home / "docker"),
                "--host", "unix:///var/run/docker.sock", *args]
        with (self.output / "commands.log").open("a") as log:
            log.write("$ " + " ".join(argv) + "\n")
            try:
                result = subprocess.run(argv, cwd=self.root, env=self.environment, text=True,
                                        capture_output=True, timeout=timeout, check=False)
            except (OSError, subprocess.TimeoutExpired) as error:
                log.write(str(error) + "\n")
                raise RuntimeError(f"Docker command unavailable or timed out: {args[:2]}") from error
            log.write(result.stdout + result.stderr + "\n")
        if result.returncode:
            raise RuntimeError(f"Docker command failed ({result.returncode}): {args}; "
                               f"{result.stderr[-2000:]}")
        return result

    def compose(self, *args: str, **kwargs) -> subprocess.CompletedProcess:
        return self.command(["compose", "--project-name", self.project, "--env-file",
                             str(self.env_file), "-f", str(self.compose_file), *args], **kwargs)

    def resources(self) -> dict[str, list[str]]:
        label = "label=com.docker.compose.project=" + self.project
        return {kind: self.command([kind, "ls", "-q",
                                   *(["--all"] if kind == "container" else []),
                                   "--filter", label]).stdout.split()
                for kind in ("container", "volume", "network")}

    def prepare(self) -> None:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        self.receipt["tested_sha"] = sha
        # A missing daemon is a failing prerequisite; this script has no skip mode.
        version = self.command(["version", "--format", "{{json .}}"])
        self.receipt["docker_version"] = json.loads(version.stdout)
        self.receipt["compose_version"] = self.command(["compose", "version", "--short"]).stdout.strip()
        if any(self.resources().values()):
            raise RuntimeError("generated project unexpectedly already owns resources")
        if self.command(["image", "ls", "-q", f"{self.project}:local"]).stdout.strip():
            raise RuntimeError("generated image tag already exists")
        self.temporary = tempfile.TemporaryDirectory(prefix=self.project + "-")
        archive_root = Path(self.temporary.name)
        archive = archive_root / "source.tar"
        subprocess.run(["git", "archive", "--format=tar", "--output", str(archive), sha],
                       cwd=self.root, check=True, timeout=60)
        context = archive_root / "source"
        context.mkdir()
        with tarfile.open(archive) as source:
            source.extractall(context, filter="data")
        config = self.command(["compose", "--project-name", self.project, "--env-file",
                               str(self.env_file), "--profile", "build", "-f",
                               str(context / "infra/docker-compose.yml"), "config", "--no-env-resolution",
                               "--no-interpolate", "--format", "json"])
        model = isolate_model(json.loads(config.stdout), self.project)
        validate_build_context(model, context)
        self.compose_file.write_text(json.dumps(model, indent=2) + "\n")
        effective = json.loads(self.compose("--profile", "build", "config", "--format", "json").stdout)
        validate_owned_model(effective, self.project)
        for name in SERVICES:
            for field in ("command", "healthcheck", "depends_on", "environment"):
                if effective["services"][name].get(field) != model["services"][name].get(field):
                    raise RuntimeError(f"effective Compose changed {name}.{field}")
        (self.output / "effective-compose.json").write_text(json.dumps(effective, indent=2) + "\n")
        # Journal ownership before any mutating Docker operation, including build.
        self.owned = True
        self.save()

    def build(self) -> None:
        self.compose("--profile", "build", "build", "app-image", timeout=600)
        self.compose("pull", "postgres", timeout=180)
        self.receipt["image"] = json.loads(self.command([
            "image", "inspect", f"{self.project}:local", "--format",
            '{{json .Id}}']).stdout)
        self.receipt["image_healthcheck"] = json.loads(self.command([
            "image", "inspect", f"{self.project}:local", "--format",
            '{{json .Config.Healthcheck}}']).stdout)

    def startup(self) -> None:
        # No worker yet: API must acknowledge queued state before any execution.
        self.compose("up", "-d", "--no-build", "--pull", "never", "api")
        self.compose("up", "-d", "--no-deps", "--no-build", "--pull", "never",
                     "--wait", "--wait-timeout", "150", "api", "postgres")
        migrations = self.compose("ps", "--all", "--format", "json", "migrate").stdout
        self.receipt["migration_container"] = self.parse_ps(migrations)
        if not self.receipt["migration_container"] or any(
            row.get("State") != "exited" or row.get("ExitCode") != 0
            for row in self.receipt["migration_container"]
        ):
            raise RuntimeError("shipped migration service did not exit successfully")
        self.receipt["alembic_revision"] = self.sql("SELECT version_num FROM alembic_version;")
        if not self.receipt["alembic_revision"]:
            raise RuntimeError("migration revision missing")

    @staticmethod
    def parse_ps(raw: str) -> list[dict]:
        if raw.lstrip().startswith("["):
            return json.loads(raw)
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def connect_api(self) -> API:
        address = self.compose("port", "api", "8000").stdout.strip()
        return API("http://" + address)

    def health(self) -> None:
        self.api = self.connect_api()
        if self.api.request("/health") != {"status": "ok"}:
            raise RuntimeError("API health failed")
        if self.api.request("/api/v1/runs")["total"] != 0:
            raise RuntimeError("acceptance database was not empty")
        self.receipt["checks"].append("empty-database-api-health")

    def sql(self, query: str) -> str:
        return self.compose("exec", "-T", "postgres", "psql", "-U", "motteavl", "-d",
                            "motteavl", "-At", "-v", "ON_ERROR_STOP=1", "-c", query).stdout.strip()

    def artifact(self, service: str, *, write: bool = False) -> None:
        content = repr(ARTIFACT_CONTENT.encode())
        code = ("import os; from pathlib import Path; "
                "from motte_storage.artifacts import ArtifactStore; "
                "root=Path(os.environ['ARTIFACT_ROOT']); ")
        if write:
            code += f"ArtifactStore(root).put_bytes({ARTIFACT_PATH!r}, {content}); "
        code += (f"assert (root / {ARTIFACT_PATH!r}).read_bytes() == {content}; "
                 "print('synthetic-artifact-ok')")
        result = self.compose("exec", "-T", service, "python", "-c", code)
        if result.stdout.strip() != "synthetic-artifact-ok":
            raise RuntimeError("shared artifact persistence failed")

    def require_worker_running(self) -> None:
        """Verify process state only; the Worker intentionally has no HTTP probe."""
        rows = self.parse_ps(self.compose("ps", "--all", "--format", "json", "worker").stdout)
        self.receipt["worker_container"] = rows
        if (len(rows) != 1 or rows[0].get("Service") != "worker"
                or rows[0].get("State") != "running" or rows[0].get("ExitCode") != 0):
            raise RuntimeError(f"worker is not running with a clean exit state: {rows}")

    def exercise(self) -> None:
        body = synthetic_request(self.project)
        created = self.api.request("/api/v1/runs", body, status=202)
        if created["status"] != "queued":
            raise RuntimeError("API did not create a queued run")
        self.run_id = created["id"]
        self.receipt["run_id"] = self.run_id
        self.receipt["checks"].append("api-created-queued-replay")
        self.artifact("api", write=True)
        # Compose 2.38.2 --wait rejects an explicitly disabled image healthcheck.
        # Check the Worker's process state and actual work instead of HTTP health.
        self.compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", "worker")
        deadline = time.monotonic() + 90
        while True:
            self.require_worker_running()
            run = self.api.request(f"/api/v1/runs/{self.run_id}")
            if run["status"] == "completed":
                break
            if run["status"] in {"failed", "cancelled", "needs_review"} or time.monotonic() >= deadline:
                raise RuntimeError(f"worker failed to complete replay: {run['status']}")
            time.sleep(1)
        if run["manifest"]["execution"]["backend_id"] != "replay":
            raise RuntimeError("unexpected execution backend")
        self.pass_id = run["current_scoring_pass_id"]
        self.receipt["scoring_pass_id"] = self.pass_id
        self.report = self.api.request(
            f"/api/v1/runs/{self.run_id}/report?scoring_pass_id={self.pass_id}")
        validate_report(self.report, self.run_id, self.pass_id)
        passes = self.api.request(f"/api/v1/runs/{self.run_id}/scoring-passes")
        if passes["total"] != 1 or passes["items"][0]["id"] != self.pass_id:
            raise RuntimeError("unexpected persisted scoring passes")
        counts = json.loads(self.sql("SELECT json_build_object('runs', (SELECT count(*) FROM runs), "
                                     "'passes', (SELECT count(*) FROM scoring_passes));"))
        if counts != {"runs": 1, "passes": 1}:
            raise RuntimeError("PostgreSQL does not contain the API run and scoring pass")
        self.receipt["postgres_counts"] = counts
        self.artifact("worker")
        self.receipt["artifact_sha256"] = hashlib.sha256(ARTIFACT_CONTENT.encode()).hexdigest()
        self.receipt["checks"].append("worker-completed-fixed-pass-and-shared-artifact")
        (self.output / "fixed-pass-report.json").write_text(json.dumps(self.report, indent=2) + "\n")

    def restart(self) -> None:
        # Recreate rather than merely restart: state must survive container replacement.
        self.compose("stop", "worker", "api")
        self.compose("restart", "postgres")
        self.compose("up", "-d", "--no-deps", "--no-build", "--pull", "never",
                     "--wait", "--wait-timeout", "150", "postgres")
        self.compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate",
                     "--wait", "--wait-timeout", "150", "api")
        self.compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", "worker")
        self.require_worker_running()
        self.api = self.connect_api()
        if self.api.request("/health") != {"status": "ok"}:
            raise RuntimeError("API failed health after restart")
        report = self.api.request(f"/api/v1/runs/{self.run_id}/report?scoring_pass_id={self.pass_id}")
        validate_report(report, self.run_id, self.pass_id)
        def stable(row):
            return {key: value for key, value in row.items() if key != "generated_at"}
        if stable(report) != stable(self.report):
            raise RuntimeError("fixed Pass changed across database/container restart")
        self.artifact("api")
        self.artifact("worker")
        self.receipt["checks"].append("postgres-restart-container-recreate-fixed-pass-persistence")

    def cleanup(self) -> None:
        if not self.owned:
            return
        errors = []
        # Diagnostic failures must not suppress cleanup. Never prune shared host resources.
        for args in [("ps", "--all", "--format", "json"), ("logs", "--no-color", "--timestamps")]:
            try:
                self.compose(*args, timeout=60)
            except Exception as error:
                self.receipt.setdefault("diagnostic_errors", []).append(str(error))
        def remove_image():
            if self.command(["image", "ls", "-q", f"{self.project}:local"]).stdout.strip():
                self.command(["image", "rm", f"{self.project}:local"])
        for action in [lambda: self.compose("down", "--volumes", "--remove-orphans", "--timeout", "20"),
                       remove_image]:
            try:
                action()
            except Exception as error:
                errors.append(str(error))
        try:
            remaining = self.resources()
            self.receipt["remaining_resources"] = remaining
            if any(remaining.values()):
                errors.append("owned Compose resources remain after cleanup")
        except Exception as error:
            errors.append(str(error))
        self.receipt["cleanup"] = "failed" if errors else "passed"
        if errors:
            self.receipt["cleanup_errors"] = errors
            self.receipt["status"] = "failed"

    def run(self) -> int:
        self.save()
        try:
            for name in ("prepare", "build", "startup", "health", "exercise", "restart"):
                self.receipt["stage"] = name
                self.save()
                getattr(self, name)()
                self.save()
            self.receipt["status"] = "passed"
        except (Exception, KeyboardInterrupt) as error:
            self.receipt["status"] = "failed"
            self.receipt["error"] = f"{type(error).__name__}: {error}"
        finally:
            self.cleanup()
            self.receipt["finished_at"] = datetime.now(UTC).isoformat()
            self.save()
            if self.temporary is not None:
                self.temporary.cleanup()
        print(json.dumps(self.receipt, indent=2))
        return 0 if self.receipt["status"] == "passed" else 1


def cleanup_previous(root: Path, output: Path) -> int:
    """Retry cleanup after cancellation/timeouts; never start or build anything."""
    path = output / "receipt.json"
    if not path.exists():
        return 0
    receipt = json.loads(path.read_text())
    if not receipt.get("owned"):
        return 0
    project = receipt["project"]
    model = json.loads((output / "compose.json").read_text())
    validate_owned_model(model, project)
    probe = Acceptance(root, output, project=project)
    probe.receipt = receipt
    probe.owned = True
    if receipt["status"] == "running":
        receipt["status"] = "failed"
        receipt["error"] = "controller interrupted before completion; independent cleanup executed"
    probe.cleanup()
    probe.save()
    print(json.dumps(probe.receipt, indent=2))
    return 0 if probe.receipt["cleanup"] == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("var/m8-compose-acceptance"))
    parser.add_argument("--cleanup-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.cleanup_only:
        return cleanup_previous(root, args.output)
    def interrupted(signum, frame):
        raise InterruptedError(f"acceptance interrupted by signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    return Acceptance(root, args.output).run()


if __name__ == "__main__":
    raise SystemExit(main())
