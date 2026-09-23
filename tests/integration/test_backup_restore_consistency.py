"""M7 一致备份 / staging 恢复集成测试（协议 §9.1/§9.2，反例 A14/A15/A16）。

- A14：consistent_backup 的窗口内维护屏障激活——API 写入口 503、Worker 拒绝领取；
  备份结束后恢复写入。
- Manifest v2 往返：计数与库实况一致、DB/逐文件 sha256、root 额外文件记 warning。
- A15：备份期缺引用文件 → BackupIncomplete + manifest status=incomplete；
  备份目录被损 → restore_staging 抛 RestoreIncomplete，绝不标成功。
- staging 恢复：全量校验 + restored_from_backup 守卫（Worker 拒绝领取）+
  clear_restore_guard(confirm=True) 后恢复领取；未终态 Run 列出且状态原样
  （A16：不自动执行、不改状态）。
- 覆盖确认：非空 staging 目录 / restore_sqlite 覆盖均需显式确认（协议 §9.2）。
- PG：PATH 无 pg_dump → BackupUnsupported（如实 blocked）；真库用例需 MOTTE_PG_DSN。
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient
from motte_sdk.replay_run import ReplayProvider
from motte_sdk.service import RunService
from motte_storage import maintenance
from motte_storage.artifacts import ArtifactStore
from motte_storage.maintenance import (
    BackupIncomplete,
    BackupUnsupported,
    RestoreIncomplete,
    clear_restore_guard,
    consistent_backup,
    consistent_backup_postgres,
    maintenance_status,
    restore_sqlite,
    restore_staging,
)
from motte_storage.platform import platform_for
from motte_storage.run_store import SQLiteRunStore

from apps.api.app.main import create_app
from apps.worker.motte_worker.runtime import WorkerLoop


def _store_with_referenced_artifacts(tmp_path, *, extra_files=()):
    """SQLite 库 + 两个被 case_run payload 引用的 artifact（含嵌入 sha256）。"""
    db = tmp_path / "runs.db"
    store = SQLiteRunStore(db)
    service = RunService(store, provider=ReplayProvider({}).invoke)
    run = service.create_run("replay@1", {}, case_ids=["case-1"])
    RunService(
        store, provider=ReplayProvider({"case-1": {"output": "ok"}}).invoke
    ).execute(run["id"])

    artifacts_root = tmp_path / "artifacts"
    artifact_store = ArtifactStore(artifacts_root)
    stdout = artifact_store.put_bytes(
        f"replay/{run['id']}/case-1/evidence/stdout/{'a' * 12}", b"stdout-bytes"
    )
    report = artifact_store.put_bytes("nested/dir/report.json", b'{"ok": true}')
    store.case_runs.upsert(
        {
            "run_id": run["id"],
            "case_id": "case-evidence",
            "artifact_refs": [
                {"id": stdout.id, "kind": "file", "uri": stdout.uri, "sha256": stdout.sha256},
                {"id": report.id, "kind": "file", "uri": report.uri, "sha256": report.sha256},
            ],
        }
    )
    for name in extra_files:
        target = artifacts_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("orphan", encoding="utf-8")
    return store, artifacts_root, run["id"]


# ------------------------------------------------------------------- A14


def test_a14_backup_window_holds_barrier_against_api_and_worker(tmp_path, monkeypatch):
    store = SQLiteRunStore(tmp_path / "a14.db")
    client = TestClient(create_app(store=store))
    created = client.post("/api/v1/runs", json={"scenario_version": "replay@1"})
    assert created.status_code == 202
    run_id = created.json()["id"]

    observed = {}
    original = maintenance._snapshot_sqlite

    def probe(source, destination):
        # 备份窗口中段：屏障必须已激活
        observed["status"] = maintenance.maintenance_status(store)
        observed["create"] = client.post("/api/v1/runs", json={"scenario_version": "replay@1"})
        worker = WorkerLoop(RunService(store), execution_lock_held=True, scoring_jobs=None)
        observed["claim"] = worker.run_once()
        observed["run_status"] = store.runs.get(run_id)["status"]
        return original(source, destination)

    monkeypatch.setattr(maintenance, "_snapshot_sqlite", probe)
    manifest = consistent_backup(store, tmp_path / "backups")

    assert observed["status"]["active"] is True
    assert observed["status"]["started_at"]
    assert observed["create"].status_code == 503
    assert observed["create"].json()["error"]["code"] == "MAINTENANCE_MODE"
    assert observed["claim"] is None
    assert observed["run_status"] == "queued"
    assert manifest["status"] == "complete"

    # 备份结束后屏障解除、写入恢复
    assert maintenance_status(store)["active"] is False
    assert client.post("/api/v1/runs", json={"scenario_version": "replay@1"}).status_code == 202


# ------------------------------------------------------------- Manifest v2


def test_manifest_v2_roundtrip_counts_hashes_and_extra_file_warning(tmp_path):
    store, artifacts_root, _run_id = _store_with_referenced_artifacts(
        tmp_path, extra_files=("orphan.log",)
    )
    backups = tmp_path / "backups"
    manifest = consistent_backup(store, backups, artifacts_root=artifacts_root)

    assert manifest["manifest_version"] == 2
    assert manifest["status"] == "complete"
    assert manifest["backend"] == "sqlite"
    assert manifest["alembic_revision"] is None  # SQLite 不走 Alembic
    assert manifest["schema_version"] == maintenance._sqlite_schema_fingerprint()
    assert any("orphan.log" in warning for warning in manifest["warnings"])
    assert manifest["counts"] == {
        "runs": len(store.runs.list()),
        "scoring_passes": sum(
            len(store.scoring_passes.list_for_run(run["id"])) for run in store.runs.list()
        ),
        "baselines": 0,
        "gate_results": 0,
    }
    assert manifest["maintenance"]["started_at"]
    assert manifest["maintenance"]["ended_at"]
    assert maintenance_status(store)["active"] is False

    stored = json.loads(sorted(backups.glob("manifest-*.json"))[-1].read_text(encoding="utf-8"))
    assert stored == manifest
    assert maintenance._manifest_content_sha256(manifest) == manifest["manifest_sha256"]

    snapshot = backups / manifest["database"]["snapshot"]
    assert snapshot.stat().st_size == manifest["database"]["bytes"]
    assert maintenance._sha256_file(snapshot) == manifest["database"]["sha256"]

    files = {entry["path"]: entry for entry in manifest["artifacts"]["files"]}
    assert len(files) == 2
    artifact_dir = backups / manifest["artifacts"]["dir"]
    for entry in files.values():
        copied = artifact_dir / entry["path"]
        assert copied.stat().st_size == entry["bytes"]
        assert maintenance._sha256_file(copied) == entry["sha256"]


# -------------------------------------------------------------------- A15


def test_a15_missing_referenced_artifact_fails_backup_and_restore(tmp_path):
    store, artifacts_root, _run_id = _store_with_referenced_artifacts(tmp_path)
    backups = tmp_path / "backups"

    # 备份期缺引用文件：manifest 落盘为 incomplete 且抛错，绝不标成功
    victim = artifacts_root / "nested" / "dir" / "report.json"
    victim.unlink()
    with pytest.raises(BackupIncomplete) as excinfo:
        consistent_backup(store, backups, artifacts_root=artifacts_root)
    assert "nested/dir/report.json" in excinfo.value.missing
    assert maintenance_status(store)["active"] is False  # 屏障一定解除
    on_disk = json.loads(sorted(backups.glob("manifest-*.json"))[-1].read_text(encoding="utf-8"))
    assert on_disk["status"] == "incomplete"
    assert on_disk["missing"] == ["nested/dir/report.json"]

    # 备份完整、备份目录中被损：restore_staging 拒绝标成功
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_bytes(b'{"ok": true}')
    manifest = consistent_backup(store, backups, artifacts_root=artifacts_root)
    backup_file = backups / manifest["artifacts"]["dir"] / "nested" / "dir" / "report.json"
    backup_file.unlink()
    with pytest.raises(RestoreIncomplete, match="report.json"):
        restore_staging(backups, tmp_path / "staging")


# ----------------------------------------------- staging 恢复 + 守卫（A16）


def test_staging_restore_verifies_and_guards_worker_claims(tmp_path):
    store, artifacts_root, run_id = _store_with_referenced_artifacts(tmp_path)
    client = TestClient(create_app(store=store))
    queued = client.post(
        "/api/v1/runs", json={"scenario_version": "replay@1", "case_ids": ["case-1"]}
    ).json()
    fixture = {"case-1": {"output": {"n": 1}, "expected": {"n": 1}}}
    attached = client.post(f"/api/v1/runs/{queued['id']}/replay", json={"cases": fixture})
    assert attached.status_code == 202
    store.runs.save({**store.runs.get(run_id), "status": "needs_review"})

    backups = tmp_path / "backups"
    manifest = consistent_backup(store, backups, artifacts_root=artifacts_root)
    staging = tmp_path / "staging"
    report = restore_staging(
        backups, staging, expected_manifest_sha256=manifest["manifest_sha256"]
    )

    assert report["restore_guard"] == "active"
    assert report["backup"] == sorted(backups.glob("manifest-*.json"))[-1].name
    assert report["artifacts"]["files_restored"] == 2
    assert report["counts"]["expected"] == manifest["counts"] == report["counts"]["actual"]
    assert (staging / "artifacts" / "nested" / "dir" / "report.json").read_bytes() == b'{"ok": true}'

    staging_store = SQLiteRunStore(staging / "runs.db")
    assert platform_for(staging_store).meta.get("restored_from_backup") == report["backup"]
    # 快照冻结了备份窗口的 maintenance 标志；staging 不得继承"维护中"，
    # 写保护只能来自恢复守卫（否则清守卫后仍 503/拒领）。
    assert maintenance_status(staging_store)["active"] is False

    # A16：未终态 Run 列在报告里、状态原样保留；恢复过程不执行任何东西
    unresolved = {item["id"]: item["status"] for item in report["unresolved_runs"]}
    assert unresolved == {queued["id"]: "queued", run_id: "needs_review"}
    assert staging_store.runs.get(queued["id"])["status"] == "queued"
    assert staging_store.runs.get(run_id)["status"] == "needs_review"

    worker = WorkerLoop(RunService(staging_store), execution_lock_held=True, scoring_jobs=None)
    assert worker.run_once() is None  # 恢复守卫激活：queued 也不领取
    assert staging_store.runs.get(queued["id"])["status"] == "queued"

    with pytest.raises(ValueError):
        clear_restore_guard(staging_store)
    clear_restore_guard(staging_store, confirm=True)
    assert platform_for(staging_store).meta.get("restored_from_backup") is None
    executed = worker.run_once()
    assert executed is not None
    assert staging_store.runs.get(queued["id"])["status"] != "queued"


def test_restore_targets_require_explicit_overwrite_confirmation(tmp_path):
    store, artifacts_root, _run_id = _store_with_referenced_artifacts(tmp_path)
    backups = tmp_path / "backups"
    consistent_backup(store, backups, artifacts_root=artifacts_root)

    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "precious.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        restore_staging(backups, staging)
    assert (staging / "precious.txt").read_text() == "keep"
    report = restore_staging(backups, staging, confirm_overwrite=True)
    assert report["restore_guard"] == "active"
    assert not (staging / "precious.txt").exists()

    # 旧入口 restore_sqlite 覆盖线上目标同样要求显式确认
    with pytest.raises(FileExistsError):
        restore_sqlite(backups, tmp_path / "runs.db", artifacts_root=artifacts_root)
    restore_sqlite(
        backups, tmp_path / "runs.db", artifacts_root=artifacts_root, confirm_overwrite=True
    )


def test_restore_staging_rejects_tampered_manifest(tmp_path):
    store, artifacts_root, _run_id = _store_with_referenced_artifacts(tmp_path)
    backups = tmp_path / "backups"
    manifest = consistent_backup(store, backups, artifacts_root=artifacts_root)
    manifest_path = sorted(backups.glob("manifest-*.json"))[-1]
    tampered = json.loads(manifest_path.read_text(encoding="utf-8"))
    tampered["counts"]["runs"] += 1
    manifest_path.write_text(json.dumps(tampered, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(RestoreIncomplete, match="manifest hash mismatch"):
        restore_staging(backups, tmp_path / "staging")
    # 外部期望哈希对不上也拒绝
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(RestoreIncomplete, match="expected manifest sha256"):
        restore_staging(
            backups, tmp_path / "staging", expected_manifest_sha256="0" * 64
        )


# ----------------------------------------------------------- PostgreSQL


def test_consistent_backup_postgres_reports_blocked_without_pg_dump(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(BackupUnsupported, match="pg_dump"):
        consistent_backup_postgres("postgresql://localhost/motte", tmp_path / "backups")


def test_postgres_staging_restore_rejects_corrupt_dump_before_target_write(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "pg_restore")
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "source.dump").write_bytes(b"damaged-dump")
    manifest = {
        "manifest_version": 2, "status": "complete", "backend": "postgres",
        "database": {"snapshot": "source.dump", "sha256": "0" * 64},
        "artifacts": None, "counts": {}, "alembic_revision": None,
    }
    manifest["manifest_sha256"] = maintenance._manifest_content_sha256(manifest)
    (backups / "manifest-test.json").write_text(json.dumps(manifest), encoding="utf-8")
    target = tmp_path / "staging-artifacts"
    with pytest.raises(RestoreIncomplete, match="database snapshot hash mismatch"):
        maintenance.restore_postgres_staging(
            backups, "postgresql://localhost/unreachable", target,
        )
    assert not target.exists()


def test_postgres_staging_report_database_identity_excludes_credentials():
    label = maintenance._postgres_database_name(
        "postgresql://operator:private-value@localhost/source?dbname=m8_staging"
    )
    assert label == "m8_staging"
    assert "private-value" not in label


@pytest.mark.skipif(
    not os.environ.get("MOTTE_PG_DSN"),
    reason="set MOTTE_PG_DSN to run PostgreSQL integration tests",
)
def test_consistent_backup_postgres_manifest(tmp_path):
    from motte_storage.factory import create_run_store

    dsn = os.environ["MOTTE_PG_DSN"]
    store = create_run_store(dsn=dsn, storage="postgres", migrate=True)
    manifest = consistent_backup_postgres(dsn, tmp_path / "backups", store=store)
    assert manifest["status"] == "complete"
    assert manifest["backend"] == "postgres"
    dump = tmp_path / "backups" / manifest["database"]["snapshot"]
    assert dump.is_file() and dump.stat().st_size > 0
    assert maintenance._sha256_file(dump) == manifest["database"]["sha256"]
    assert maintenance_status(store)["active"] is False
