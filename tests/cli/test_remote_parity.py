"""M7 CLI local/server 模式 parity 测试（协议 sdk-and-migration.md frozen@1 §3，A04）。

驱动**真实 API 应用**（apps.api.app.main.create_app）经 tests.sdk.sync_asgi 的
SyncASGITransport 注入到 CLI 的 client 工厂（monkeypatch motte_cli.remote.build_client），
因此 server 模式断言的是完整 HTTP 栈行为，不是 mock。

覆盖：
- A04：server 模式传输失败 → 非零退出 + stderr JSON code=TRANSPORT + 零本地副作用
  （本地 DB 未创建、motte_provider 未被触碰）；
- local/server 同输入同错误码（SCENARIO_NOT_FOUND / RUN_NOT_FOUND）与成功 JSON 可解析；
- run create + wait + events(JSONL) + report 快乐路径（replay fixture，Worker 角色
  由测试扮演——CLI 本身全程 server 模式）；
- run-wait 超时 → TIMEOUT/退出 2，Run 保持 queued，**零 cancel 调用**；
- --db + server → MODE_MISMATCH/退出 2；配置优先级（参数 > 环境变量 > 默认 local）；
- request_key 幂等 parity：server 与 local 同键同 body → 同一 run id；
- 运维命令（backup --consistent / restore --staging / restore-guard / maintenance /
  gc / import）local-only 行为与 --mode server 拒绝。
"""
from __future__ import annotations

import json
import sys

import httpx
import pytest
from motte_storage.run_store import SQLiteRunStore
from motte_sdk import MotteClient

from apps.api.app.main import create_app
from motte_cli.main import main
from tests.migration.conftest import build_legacy_export
from tests.sdk.sync_asgi import SyncASGITransport

BASE_URL = "http://testserver"
SERVER_ARGS = ("--mode", "server", "--api-url", BASE_URL)


def run_cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def stderr_error(err: str) -> dict:
    return json.loads(err)["error"]


def replay_spec(**overrides) -> dict:
    spec = {
        "scenario_version": "replay@1",
        "manifest": {
            "replay_fixture": {"case-1": {"output": "ok", "expected": "ok"}},
        },
        "case_ids": ["case-1"],
    }
    spec.update(overrides)
    return spec


class CountingTransport(httpx.BaseTransport):
    """包装真实 ASGI transport，按 (method, path) 计数（断言依据）。"""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self.inner = inner
        self.calls: list[tuple[str, str]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        return self.inner.handle_request(request)

    def count(self, method: str | None = None, path_contains: str | None = None) -> int:
        return sum(
            1 for m, p in self.calls
            if (method is None or m == method)
            and (path_contains is None or path_contains in p)
        )


class FailingTransport(httpx.BaseTransport):
    """任何请求都连接失败（A04：远端不可达）。"""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")


@pytest.fixture()
def server_env(tmp_path, monkeypatch):
    """真实 API app + SyncASGITransport + 计数；CLI 经 build_client 注入。"""
    db_path = tmp_path / "server-app.db"
    store = SQLiteRunStore(db_path)
    app = create_app(store=store)
    transport = CountingTransport(SyncASGITransport(app))

    def fake_build_client(args):
        return MotteClient(
            BASE_URL, transport=transport, retries=0,
            poll_interval=0.05, backoff_base=0.01,
        )

    monkeypatch.setattr("motte_cli.remote.build_client", fake_build_client)
    monkeypatch.setenv("MOTTE_API_URL", BASE_URL)
    yield {"db": db_path, "store": store, "transport": transport}
    transport.inner.close()


@pytest.fixture()
def block_provider(monkeypatch):
    """sys.modules 哨兵：任何 `import motte_provider` 立即 ImportError。"""
    monkeypatch.setitem(sys.modules, "motte_provider", None)


# ------------------------------------------------------------- A04：传输失败


def test_transport_failure_exits_nonzero_with_transport_code(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / "never-created.db"
    monkeypatch.setenv("MOTTE_DB_PATH", str(db_path))

    def fake_build_client(args):
        return MotteClient(BASE_URL, transport=FailingTransport(), retries=0)

    monkeypatch.setattr("motte_cli.remote.build_client", fake_build_client)
    code, out, err = run_cli(capsys, "run-list", *SERVER_ARGS)
    assert code != 0
    assert out == ""
    assert stderr_error(err)["code"] == "TRANSPORT"
    # 零本地副作用：server 模式从不打开本地 DB。
    assert not db_path.exists()


def test_transport_failure_never_touches_provider_or_local_store(
        tmp_path, capsys, monkeypatch, block_provider):
    monkeypatch.setenv("MOTTE_DB_PATH", str(tmp_path / "never.db"))

    def fake_build_client(args):
        return MotteClient(BASE_URL, transport=FailingTransport(), retries=0)

    monkeypatch.setattr("motte_cli.remote.build_client", fake_build_client)
    for argv in (
        ("run", "--spec", json.dumps(replay_spec()), *SERVER_ARGS),
        ("compare", "--baseline", "run-a", "--candidate", "run-b", *SERVER_ARGS),
        ("regression", "--baseline", "run-a", "--candidate", "run-b", *SERVER_ARGS),
    ):
        code, _, err = run_cli(capsys, *argv)
        assert code == 2, (argv, err)
        assert stderr_error(err)["code"] == "TRANSPORT"
    assert not (tmp_path / "never.db").exists()


def test_server_mode_with_db_is_mode_mismatch(tmp_path, capsys):
    db_path = tmp_path / "nope.db"
    code, out, err = run_cli(
        capsys, "run-list", *SERVER_ARGS, "--db", str(db_path))
    assert code == 2
    assert out == ""
    assert stderr_error(err)["code"] == "MODE_MISMATCH"
    assert not db_path.exists()


def test_server_mode_without_api_url_fails(capsys, monkeypatch):
    monkeypatch.delenv("MOTTE_API_URL", raising=False)
    code, _, err = run_cli(capsys, "run-list", "--mode", "server")
    assert code == 2
    assert stderr_error(err)["code"] == "MODE_CONFIG_MISSING"


# ------------------------------------------------- local/server 错误码 parity


def test_scenario_not_found_parity_local_vs_server(tmp_path, capsys, server_env):
    local_db = tmp_path / "local-empty.db"
    SQLiteRunStore(local_db)  # 建空库
    spec = json.dumps(replay_spec(scenario_version="nope@1"))

    code, _, err = run_cli(capsys, "run", "--spec", spec, "--db", str(local_db))
    assert code == 2
    local_error = stderr_error(err)
    assert local_error["code"] == "SCENARIO_NOT_FOUND"

    code, out, err = run_cli(capsys, "run", "--spec", spec, *SERVER_ARGS)
    assert code == 2
    remote_error = stderr_error(err)
    assert remote_error["code"] == "SCENARIO_NOT_FOUND"
    # 同错误键集（协议 §3：local/server 同输入产出同错误码与同 JSON 键集）。
    assert set(remote_error) >= {"code", "message"}


def test_run_not_found_parity_local_vs_server(tmp_path, capsys, server_env):
    local_db = tmp_path / "local-empty.db"
    SQLiteRunStore(local_db)

    code, _, err = run_cli(capsys, "run-get", "run-nope", "--db", str(local_db))
    assert code == 2
    assert stderr_error(err)["code"] == "RUN_NOT_FOUND"

    code, _, err = run_cli(capsys, "run-get", "run-nope", *SERVER_ARGS)
    assert code == 2
    assert stderr_error(err)["code"] == "RUN_NOT_FOUND"


def test_success_json_parseable_in_both_modes(tmp_path, capsys, server_env):
    local_db = tmp_path / "local-seed.db"
    store = SQLiteRunStore(local_db)
    store.runs.create({
        "id": "run-seed", "schema_version": 2, "revision": 1,
        "scenario_version": "replay@1", "status": "completed",
        "manifest": {}, "requested_manifest": {}, "case_ids": [],
    }, event={"run_id": "run-seed", "type": "queued", "status": "completed"})
    server_env["store"].runs.create({
        "id": "run-seed", "schema_version": 2, "revision": 1,
        "scenario_version": "replay@1", "status": "completed",
        "manifest": {}, "requested_manifest": {}, "case_ids": [],
    }, event={"run_id": "run-seed", "type": "queued", "status": "completed"})

    code, out, _ = run_cli(capsys, "run-get", "run-seed", "--db", str(local_db))
    assert code == 0
    assert json.loads(out)["id"] == "run-seed"
    code, out, _ = run_cli(capsys, "run-get", "run-seed", *SERVER_ARGS)
    assert code == 0
    assert json.loads(out)["id"] == "run-seed"

    code, out, _ = run_cli(capsys, "run-list", "--db", str(local_db))
    assert code == 0 and json.loads(out)["total"] == 1
    code, out, _ = run_cli(capsys, "run-list", *SERVER_ARGS)
    assert code == 0 and json.loads(out)["total"] == 1


# ------------------------------------------------------- 快乐路径 + wait 语义


def _dispatch_to_completion(store, run_id: str) -> None:
    """测试扮演 Worker：本地执行 replay Run 到终态（CLI 全程不经此路径）。"""
    from motte_sdk.dispatcher import RunDispatcher
    from motte_sdk.service import RunService

    RunDispatcher(RunService(store)).dispatch(run_id)


def test_server_run_create_wait_events_report_happy_path(capsys, server_env):
    spec = json.dumps(replay_spec())
    code, out, err = run_cli(capsys, "run", "--spec", spec, *SERVER_ARGS)
    assert code == 0, err
    created = json.loads(out)
    run_id = created["id"]
    assert created["status"] == "queued"

    _dispatch_to_completion(server_env["store"], run_id)

    code, out, err = run_cli(
        capsys, "run-wait", run_id, "--timeout", "5", "--poll", "0.05", *SERVER_ARGS)
    assert code == 0, err
    waited = json.loads(out)
    assert waited["status"] == "completed"

    code, out, err = run_cli(capsys, "run-events", run_id, *SERVER_ARGS)
    assert code == 0, err
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines, "run-events 默认输出 JSONL（每行一条事件）"
    events = [json.loads(line) for line in lines]
    seqs = [event["seq"] for event in events]
    assert seqs == sorted(seqs)

    code, out, err = run_cli(capsys, "run-events", run_id, "--snapshot", *SERVER_ARGS)
    assert code == 0, err
    assert isinstance(json.loads(out), list)

    code, out, err = run_cli(capsys, "run-report", run_id, *SERVER_ARGS)
    assert code == 0, err
    report = json.loads(out)
    assert report["run_id"] == run_id
    assert report["scoring_pass_id"]


def test_run_wait_timeout_never_cancels_the_run(capsys, server_env):
    code, out, err = run_cli(capsys, "run", "--spec", json.dumps(replay_spec()), *SERVER_ARGS)
    assert code == 0, err
    run_id = json.loads(out)["id"]

    code, out, err = run_cli(
        capsys, "run-wait", run_id, "--timeout", "0.3", "--poll", "0.05", *SERVER_ARGS)
    assert code == 2
    assert stderr_error(err)["code"] == "TIMEOUT"
    # Run 未被取消：仍是 queued（超时只停止等待，协议 §1.4）。
    code, out, _ = run_cli(capsys, "run-get", run_id, *SERVER_ARGS)
    assert json.loads(out)["status"] == "queued"
    # 零 cancel 调用。
    assert server_env["transport"].count("POST", "/cancel") == 0


# ------------------------------------------------------- 配置优先级 / 幂等 parity


def test_cli_argument_overrides_env_mode(tmp_path, capsys, server_env, monkeypatch):
    monkeypatch.setenv("MOTTE_CLI_MODE", "server")
    # 显式 --mode local 压过环境变量：本地直连（无 HTTP 调用）。
    local_db = tmp_path / "local-empty.db"
    SQLiteRunStore(local_db)
    before = len(server_env["transport"].calls)
    code, _, err = run_cli(capsys, "run-get", "run-nope", "--mode", "local",
                           "--db", str(local_db))
    assert code == 2
    assert stderr_error(err)["code"] == "RUN_NOT_FOUND"
    assert len(server_env["transport"].calls) == before


def test_env_mode_server_routes_to_remote(tmp_path, capsys, server_env, monkeypatch):
    monkeypatch.setenv("MOTTE_CLI_MODE", "server")
    code, _, err = run_cli(capsys, "run-get", "run-nope")
    assert code == 2
    assert stderr_error(err)["code"] == "RUN_NOT_FOUND"
    assert server_env["transport"].count("GET", "/api/v1/runs/run-nope") >= 1


def test_request_key_idempotency_parity(tmp_path, capsys, server_env):
    spec = json.dumps(replay_spec())

    # server：同键同 body → 同一 run id（idempotent_replay 可见）。
    code, out, err = run_cli(
        capsys, "run", "--spec", spec, *SERVER_ARGS, "--request-key", "pay-1")
    assert code == 0, err
    first = json.loads(out)
    code, out, err = run_cli(
        capsys, "run", "--spec", spec, *SERVER_ARGS, "--request-key", "pay-1")
    assert code == 0, err
    replayed = json.loads(out)
    assert replayed["id"] == first["id"]
    assert replayed.get("idempotent_replay") is True

    # local：同一 helper 语义（canonical hash + 注册表 + 确定性 run id）。
    local_db = tmp_path / "local-idem.db"
    SQLiteRunStore(local_db)
    code, out, err = run_cli(
        capsys, "run", "--spec", spec, "--db", str(local_db), "--request-key", "pay-1")
    assert code == 0, err
    local_first = json.loads(out)
    code, out, err = run_cli(
        capsys, "run", "--spec", spec, "--db", str(local_db), "--request-key", "pay-1")
    assert code == 0, err
    local_replay = json.loads(out)
    assert local_replay["id"] == local_first["id"]
    assert local_replay.get("idempotent_replay") is True
    # local/server 的确定性 run id 同构（同一派生函数）。
    assert local_first["id"] == first["id"]


# ------------------------------------------------------------- 运维命令（local-only）


def test_local_only_commands_reject_server_mode(capsys, monkeypatch):
    monkeypatch.setenv("MOTTE_API_URL", BASE_URL)
    for argv in (
        ("backup", "--target", "unused", "--mode", "server"),
        ("restore", "--source", "unused", "--mode", "server"),
        ("gc", "plan", "--mode", "server", "--artifacts-root", "unused"),
        ("maintenance", "status", "--mode", "server"),
        ("restore-guard", "status", "--mode", "server"),
    ):
        code, _, err = run_cli(capsys, *argv)
        assert code == 2, (argv, err)
        assert stderr_error(err)["code"] == "LOCAL_ONLY_COMMAND"


def test_backup_consistent_restore_staging_and_guard_roundtrip(tmp_path, capsys):
    db_path = tmp_path / "ops.db"
    store = SQLiteRunStore(db_path)
    store.runs.create({
        "id": "run-ops", "schema_version": 2, "revision": 1,
        "scenario_version": "replay@1", "status": "completed",
        "manifest": {}, "requested_manifest": {}, "case_ids": [],
    }, event={"run_id": "run-ops", "type": "queued", "status": "completed"})

    backup_dir = tmp_path / "backup"
    code, out, err = run_cli(
        capsys, "backup", "--consistent", "--target", str(backup_dir), "--db", str(db_path))
    assert code == 0, err
    manifest = json.loads(out)
    assert manifest["manifest_version"] == 2
    assert manifest["status"] == "complete"
    assert manifest["counts"]["runs"] == 1

    staging = tmp_path / "staging"
    code, out, err = run_cli(
        capsys, "restore", "--source", str(backup_dir), "--staging", str(staging),
        "--db", str(db_path))
    assert code == 0, err
    restored = json.loads(out)
    assert restored["restore_guard"] == "active"

    code, out, err = run_cli(
        capsys, "restore-guard", "status", "--db", str(staging / "runs.db"))
    assert code == 0, err
    assert json.loads(out)["active"] is True

    code, _, err = run_cli(
        capsys, "restore-guard", "clear", "--db", str(staging / "runs.db"))
    assert code == 2
    assert stderr_error(err)["code"] == "CONFIRM_REQUIRED"

    code, out, err = run_cli(
        capsys, "restore-guard", "clear", "--yes", "--db", str(staging / "runs.db"))
    assert code == 0, err
    assert json.loads(out)["restore_guard"] == "cleared"


def test_maintenance_begin_status_end(tmp_path, capsys):
    db_path = tmp_path / "maint.db"
    SQLiteRunStore(db_path)
    code, out, err = run_cli(
        capsys, "maintenance", "begin", "--reason", "cli test", "--db", str(db_path))
    assert code == 0, err
    assert json.loads(out)["active"] is True

    code, out, err = run_cli(capsys, "maintenance", "status", "--db", str(db_path))
    assert code == 0, err
    assert json.loads(out)["active"] is True

    code, out, err = run_cli(capsys, "maintenance", "end", "--db", str(db_path))
    assert code == 0, err
    assert json.loads(out)["active"] is False


def test_gc_plan_dry_run_and_apply_requires_confirm(tmp_path, capsys):
    db_path = tmp_path / "gc.db"
    SQLiteRunStore(db_path)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    stale = artifacts / "stale.bin"
    stale.write_bytes(b"stale")
    # mtime 拨回过去，越过 TTL。
    import os
    old = stale.stat().st_mtime - 200 * 86400
    os.utime(stale, (old, old))

    code, out, err = run_cli(
        capsys, "gc", "plan", "--db", str(db_path),
        "--artifacts-root", str(artifacts), "--ttl-days", "90")
    assert code == 0, err
    plan = json.loads(out)
    assert plan["summary"]["deletable_count"] == 1
    assert plan["deletable"][0]["artifact_id"] == "stale.bin"

    code, _, err = run_cli(
        capsys, "gc", "apply", "--db", str(db_path), "--artifacts-root", str(artifacts))
    assert code == 2
    assert stderr_error(err)["code"] == "CONFIRM_REQUIRED"
    assert stale.exists()  # 拒绝时不删

    code, out, err = run_cli(
        capsys, "gc", "apply", "--confirm", "--db", str(db_path),
        "--artifacts-root", str(artifacts))
    assert code == 0, err
    assert json.loads(out)["deleted"] == 1
    assert not stale.exists()


def test_import_plan_apply_and_rollback(tmp_path, capsys):
    db_path = tmp_path / "import.db"
    SQLiteRunStore(db_path)
    artifacts_root = tmp_path / "art-root"
    artifacts_root.mkdir()
    source = build_legacy_export(tmp_path / "legacy-export", with_in_flight=False)

    code, out, err = run_cli(
        capsys, "import", "plan", "--source", str(source), "--db", str(db_path))
    assert code == 0, err
    plan = json.loads(out)
    assert plan["import_id"] == "imp-accept-0001"
    assert plan["created"] == 0  # dry-run 零创建

    code, out, err = run_cli(
        capsys, "import", "apply", "--source", str(source),
        "--artifacts-root", str(artifacts_root), "--db", str(db_path))
    assert code == 0, err
    applied = json.loads(out)
    assert applied["created"] > 0
    assert applied["pending"] == 0

    code, _, err = run_cli(
        capsys, "import", "rollback", "--import-id", "imp-accept-0001",
        "--db", str(db_path))
    assert code == 2
    assert stderr_error(err)["code"] == "CONFIRM_REQUIRED"

    code, out, err = run_cli(
        capsys, "import", "rollback", "--import-id", "imp-accept-0001", "--confirm",
        "--operator", "cli-tester", "--artifacts-root", str(artifacts_root),
        "--db", str(db_path))
    assert code == 0, err
    rollback = json.loads(out)
    assert len(rollback["deactivated_runs"]) >= 1


def test_backup_legacy_flagless_still_works(tmp_path, capsys):
    db_path = tmp_path / "legacy-backup.db"
    SQLiteRunStore(db_path)
    target = tmp_path / "legacy-backup"
    code, out, err = run_cli(
        capsys, "backup", "--target", str(target), "--db", str(db_path))
    assert code == 0, err
    manifest = json.loads(out)
    assert "database" in manifest and manifest["artifacts"] is None
