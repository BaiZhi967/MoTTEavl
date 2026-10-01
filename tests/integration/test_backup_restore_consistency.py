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
        "trace_archive_receipts": 0,
        "scoring_passes": sum(
            len(store.scoring_passes.list_for_run(run["id"])) for run in store.runs.list()
        ),
        "baselines": 0,
        "gate_results": 0,
        "statistical_reports": 0,
        "calibration_records": 0,
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
def test_consistent_backup_postgres_manifest(tmp_path, isolated_pg_database):
    from motte_storage.factory import create_run_store

    dsn = isolated_pg_database
    store = create_run_store(dsn=dsn, storage="postgres", migrate=True)
    manifest = consistent_backup_postgres(dsn, tmp_path / "backups", store=store)
    assert manifest["status"] == "complete"
    assert manifest["backend"] == "postgres"
    dump = tmp_path / "backups" / manifest["database"]["snapshot"]
    assert dump.is_file() and dump.stat().st_size > 0
    assert maintenance._sha256_file(dump) == manifest["database"]["sha256"]
    assert maintenance_status(store)["active"] is False


def test_backup_does_not_release_a_new_operation_in_finally(tmp_path, monkeypatch):
    store = SQLiteRunStore(tmp_path / "runs.db")
    original_end = maintenance.end_maintenance
    next_lease = {}

    def end_and_start_next(store, *, owner):
        result = original_end(store, owner=owner)
        if not next_lease:
            next_lease.update(maintenance.begin_maintenance(store, reason="gc"))
        return result

    monkeypatch.setattr(maintenance, "end_maintenance", end_and_start_next)
    try:
        manifest = consistent_backup(store, tmp_path / "backups")
        assert manifest["status"] == "complete"
        assert maintenance_status(store)["active"] is True
    finally:
        if next_lease:
            original_end(store, owner=next_lease["owner"])


def test_backup_resolves_digest_only_evidence_and_restores_it(tmp_path):
    store, root, run_id = _store_with_referenced_artifacts(tmp_path)
    artifact = ArtifactStore(root).put_bytes("legacy/digest-only.bin", b"digest-only-evidence")
    store.case_runs.upsert({
        "run_id": run_id, "case_id": "digest-only",
        "artifacts": [{"sha256": artifact.sha256}],
    })
    backups = tmp_path / "backups"
    manifest = consistent_backup(store, backups, artifacts_root=root)
    assert artifact.id in {entry["path"] for entry in manifest["artifacts"]["files"]}
    restore_staging(backups, tmp_path / "staging")
    assert (tmp_path / "staging" / "artifacts" / artifact.id).read_bytes() == b"digest-only-evidence"


def test_backup_missing_digest_only_evidence_is_incomplete(tmp_path):
    store, root, run_id = _store_with_referenced_artifacts(tmp_path)
    store.case_runs.upsert({
        "run_id": run_id, "case_id": "missing-digest", "artifacts": [{"sha256": "0" * 64}],
    })
    with pytest.raises(BackupIncomplete) as error:
        consistent_backup(store, tmp_path / "backups", artifacts_root=root)
    assert "sha256:" + "0" * 64 in error.value.missing
    assert error.value.manifest["status"] == "incomplete"


def test_backup_with_references_requires_artifact_root(tmp_path):
    store, _root, _run_id = _store_with_referenced_artifacts(tmp_path)
    with pytest.raises(BackupIncomplete) as error:
        consistent_backup(store, tmp_path / "backups")
    assert "nested/dir/report.json" in error.value.missing
    assert error.value.manifest["status"] == "incomplete"


def test_postgres_backup_rejects_unrelated_barrier_store(tmp_path, monkeypatch):
    store = SQLiteRunStore(tmp_path / "unrelated.db")
    monkeypatch.setattr(maintenance.shutil, "which", lambda name: "/unused/pg_dump")

    def forbid_dump(*args, **kwargs):
        raise AssertionError("dump must not run with an unrelated store barrier")

    monkeypatch.setattr(maintenance.subprocess, "run", forbid_dump)
    with pytest.raises(BackupUnsupported, match="same PostgreSQL"):
        consistent_backup_postgres("postgresql://localhost/source", tmp_path / "backups", store=store)
    assert maintenance_status(store)["active"] is False


def test_backup_rejects_conflicting_hashes_bound_to_the_same_path(tmp_path):
    store, root, run_id = _store_with_referenced_artifacts(tmp_path)
    artifacts = ArtifactStore(root)
    current = artifacts.put_bytes("shared.bin", b"new-content")
    legacy = artifacts.put_bytes("legacy-copy.bin", b"old-content")
    store.case_runs.upsert({
        "run_id": run_id, "case_id": "conflicting-bound-hashes",
        "artifact_refs": [{"id": current.id, "sha256": current.sha256},
                          {"id": current.id, "sha256": legacy.sha256}],
    })
    with pytest.raises(ValueError, match="conflicting"):
        consistent_backup(store, tmp_path / "backups", artifacts_root=root)
    assert maintenance_status(store)["active"] is False
    assert not list((tmp_path / "backups").glob("manifest-*.json"))


def test_reference_hash_conflict_normalizes_prefix_without_losing_path_binding():
    from motte_storage.artifact_refs import ArtifactReferenceConflict, collect_artifact_refs

    refs, hashes = {}, set()
    collect_artifact_refs({"artifacts": [{"id": "shared.bin", "sha256": "a" * 64},
                                        {"id": "shared.bin", "sha256": "sha256:" + "a" * 64}]},
                          refs, hashes=hashes)
    assert refs == {"shared.bin": "a" * 64}
    assert hashes == {"a" * 64}
    with pytest.raises(ArtifactReferenceConflict) as error:
        collect_artifact_refs({"artifact_id": "shared.bin", "sha256": "b" * 64}, refs, hashes=hashes)
    assert error.value.artifact_id == "shared.bin"
    assert error.value.hashes == {"a" * 64, "b" * 64}
    assert hashes == {"a" * 64}


def test_backup_blocks_live_tombstone_append_after_database_snapshot(tmp_path, monkeypatch):
    import sqlite3

    store, root, _run_id = _store_with_referenced_artifacts(tmp_path)
    tombstones = platform_for(store).tombstones
    original_snapshot = maintenance._snapshot_sqlite

    def snapshot_then_contend(source, destination):
        original_snapshot(source, destination)
        with pytest.raises(sqlite3.IntegrityError, match="maintenance"):
            tombstones.append([{"gc_run_id": "late", "artifact_id": "nested/dir/report.json",
                                "status": "deleted", "reason": "import_rollback"}])

    monkeypatch.setattr(maintenance, "_snapshot_sqlite", snapshot_then_contend)
    manifest = consistent_backup(store, tmp_path / "backups", artifacts_root=root, reason="gc")
    assert manifest["status"] == "complete"
    assert len(manifest["artifacts"]["files"]) == 2
    assert tombstones.list() == []


def test_report_backup_restore_and_barrier(tmp_path):
    from tests.storage.test_statistical_report_references import put_report
    from tests.sdk.test_statistical_reports import _seed

    store = _seed(SQLiteRunStore(tmp_path / "reports.db"))
    artifacts = tmp_path / "artifacts"
    artifact = ArtifactStore(artifacts).put_bytes("publication/evidence.bin", b"frozen evidence")
    report = put_report(store, runs=("base", "candidate"), passes=("old-base", "old-candidate"),
                        evidence={"nested": {"artifact_id": artifact.id, "sha256": artifact.sha256}})
    before_runs = store.runs.list()
    before_passes = {run["id"]: store.scoring_passes.list_for_run(run["id"]) for run in before_runs}
    backup = tmp_path / "backups"
    manifest = consistent_backup(store, backup, artifacts_root=artifacts)
    assert manifest["counts"]["statistical_reports"] == 1
    assert [item["path"] for item in manifest["artifacts"]["files"]] == [artifact.id]
    restored = restore_staging(backup, tmp_path / "restored")
    reopened = SQLiteRunStore(restored["database"])
    assert restored["counts"]["actual"]["statistical_reports"] == 1
    assert reopened.statistical_reports.get(report["report_id"]) == report
    assert reopened.runs.list() == before_runs
    assert {run["id"]: reopened.scoring_passes.list_for_run(run["id"]) for run in before_runs} == before_passes
    assert (tmp_path / "restored/artifacts" / artifact.id).read_bytes() == b"frozen evidence"
    installed = restore_sqlite(
        backup, tmp_path / "installed.db", artifacts_root=tmp_path / "installed-artifacts",
    )
    installed_store = SQLiteRunStore(installed["database"])
    assert installed_store.statistical_reports.get(report["report_id"]) == report
    assert platform_for(installed_store).meta.get("restored_from_backup") is not None
    assert (tmp_path / "installed-artifacts" / artifact.id).read_bytes() == b"frozen evidence"


@pytest.mark.parametrize("damage", ["missing", "mismatched"])
@pytest.mark.parametrize("boundary", ["backup", "restore"])
def test_report_pinned_artifact_damage_rejects_backup_and_restore(tmp_path, damage, boundary):
    from tests.storage.test_statistical_report_references import put_report

    store = SQLiteRunStore(tmp_path / "reports.db")
    artifacts = tmp_path / "artifacts"
    artifact = ArtifactStore(artifacts).put_bytes("publication/only.bin", b"frozen bytes")
    put_report(store, evidence={"artifact_id": artifact.id, "sha256": artifact.sha256})
    backups = tmp_path / "backups"
    if boundary == "restore":
        manifest = consistent_backup(store, backups, artifacts_root=artifacts)
        target = backups / manifest["artifacts"]["dir"] / artifact.id
        assert target.is_file(), "report-owned artifact must be included in backup"
    else:
        target = artifacts / artifact.id
    if damage == "missing":
        target.unlink()
    else:
        target.write_bytes(b"changed bytes")
    if boundary == "backup":
        with pytest.raises(BackupIncomplete) as failure:
            consistent_backup(store, backups, artifacts_root=artifacts)
        assert failure.value.manifest["status"] == "incomplete"
        assert artifact.id in (failure.value.missing if damage == "missing" else failure.value.mismatched)
    else:
        with pytest.raises(RestoreIncomplete, match="artifact.*(missing|hash mismatch)"):
            restore_staging(backups, tmp_path / "rejected")
    assert maintenance_status(store)["active"] is False


@pytest.mark.parametrize("failure", ["integrity", "list", "get", "conflict"])
def test_report_reader_failure_never_creates_successful_backup(tmp_path, monkeypatch, failure):
    from tests.storage.test_statistical_report_references import put_report, damage_report_reader

    store = SQLiteRunStore(tmp_path / "report-failure.db")
    artifacts = tmp_path / "artifacts"
    artifact = ArtifactStore(artifacts).put_bytes("only.bin", b"must survive")
    put_report(store, evidence={"artifact_id": artifact.id, "sha256": artifact.sha256})
    error = damage_report_reader(store, monkeypatch, failure)
    backups = tmp_path / "backups"
    with pytest.raises(error):
        consistent_backup(store, backups, artifacts_root=artifacts)
    assert not any(json.loads(path.read_text())["status"] == "complete"
                   for path in backups.glob("manifest-*.json"))
    assert (artifacts / artifact.id).read_bytes() == b"must survive"
    assert platform_for(store).tombstones.list() == []
    assert maintenance_status(store)["active"] is False


# Reuse the guarded fixture generator with a separate identity for staging.
@pytest.fixture
def report_restore_database():
    from tests.storage.conftest import isolated_pg_database
    yield from isolated_pg_database.__wrapped__()


@pytest.mark.parametrize("rewritten_manifest", [False, True])
def test_report_postgres_backup_restore_closure(
    tmp_path, isolated_pg_database, report_restore_database, rewritten_manifest,
):
    import shutil
    from motte_storage.postgres import create_postgres_run_store
    from motte_storage.migrations import upgrade
    from tests.storage.test_statistical_report_references import put_report
    from tests.sdk.test_statistical_reports import _seed

    if not shutil.which("pg_dump") or not shutil.which("pg_restore"):
        pytest.skip("pg_dump and pg_restore are required for the disposable PG round trip")
    dsn = isolated_pg_database
    # Backups traverse the current unified evidence closure, not only 0016.
    upgrade(dsn)
    store = _seed(create_postgres_run_store(dsn))
    artifacts = tmp_path / "artifacts"
    artifact = ArtifactStore(artifacts).put_bytes("report/evidence.bin", b"postgres evidence")
    report = put_report(store, runs=("base", "candidate"), passes=("old-base", "old-candidate"),
                        evidence={"artifact_id": artifact.id, "sha256": artifact.sha256})
    runs = store.runs.list()
    passes = {run["id"]: store.scoring_passes.list_for_run(run["id"]) for run in runs}
    backups = tmp_path / "backups"
    manifest = consistent_backup_postgres(dsn, backups, store=store, artifacts_root=artifacts)
    assert manifest["counts"]["statistical_reports"] == 1
    assert [entry["path"] for entry in manifest["artifacts"]["files"]] == [artifact.id]
    assert report_restore_database != dsn
    if rewritten_manifest:
        copied = backups / manifest["artifacts"]["dir"] / artifact.id
        copied.write_bytes(b"rewritten PostgreSQL evidence")
        manifest["artifacts"]["files"][0].update(
            sha256=maintenance._sha256_file(copied), bytes=copied.stat().st_size,
        )
        manifest["manifest_sha256"] = maintenance._manifest_content_sha256(manifest)
        next(backups.glob("manifest-*.json")).write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(RestoreIncomplete, match="reference.*hash mismatch"):
            maintenance.restore_postgres_staging(
                backups, report_restore_database, tmp_path / "rejected-artifacts",
            )
        rejected = create_postgres_run_store(report_restore_database)
        assert platform_for(rejected).meta.get("restored_from_backup") is not None
        assert store.statistical_reports.get(report["report_id"]) == report
        assert (artifacts / artifact.id).read_bytes() == b"postgres evidence"
        return
    restored = maintenance.restore_postgres_staging(
        backups, report_restore_database, tmp_path / "restored-artifacts",
    )
    reopened = create_postgres_run_store(report_restore_database)
    assert restored["counts"]["actual"]["statistical_reports"] == 1
    assert reopened.statistical_reports.get(report["report_id"]) == report
    assert reopened.runs.list() == runs
    assert {run["id"]: reopened.scoring_passes.list_for_run(run["id"]) for run in runs} == passes
    assert (tmp_path / "restored-artifacts" / artifact.id).read_bytes() == b"postgres evidence"


@pytest.mark.parametrize("entrypoint", ["staging", "sqlite"])
def test_restore_rechecks_report_pinned_hash_against_rewritten_artifact_manifest(tmp_path, entrypoint):
    """A consistent file inventory cannot override immutable report evidence hashes."""
    from tests.storage.test_statistical_report_references import put_report

    store = SQLiteRunStore(tmp_path / "reports.db")
    artifacts = tmp_path / "artifacts"
    artifact = ArtifactStore(artifacts).put_bytes("report/pinned.bin", b"original pinned bytes")
    put_report(store, evidence={"artifact_id": artifact.id, "sha256": artifact.sha256})
    backups = tmp_path / "backups"
    manifest = consistent_backup(store, backups, artifacts_root=artifacts)
    copied = backups / manifest["artifacts"]["dir"] / artifact.id
    copied.write_bytes(b"different artifact bytes")
    entry = manifest["artifacts"]["files"][0]
    entry.update(sha256=maintenance._sha256_file(copied), bytes=copied.stat().st_size)
    manifest["manifest_sha256"] = maintenance._manifest_content_sha256(manifest)
    path = next(backups.glob("manifest-*.json"))
    path.write_text(json.dumps(manifest), encoding="utf-8")
    if entrypoint == "staging":
        with pytest.raises(RestoreIncomplete, match="reference.*hash mismatch"):
            restore_staging(backups, tmp_path / "rejected")
        assert not (tmp_path / "rejected").exists()
    else:
        destination = tmp_path / "existing.db"
        destination.write_bytes(b"existing target must remain untouched")
        target_artifacts = tmp_path / "existing-artifacts"
        target_artifacts.mkdir()
        (target_artifacts / "precious.bin").write_bytes(b"unchanged")
        with pytest.raises(RestoreIncomplete, match="reference.*hash mismatch"):
            restore_sqlite(backups, destination, artifacts_root=target_artifacts, confirm_overwrite=True)
        assert destination.read_bytes() == b"existing target must remain untouched"
        assert (target_artifacts / "precious.bin").read_bytes() == b"unchanged"
    assert (artifacts / artifact.id).read_bytes() == b"original pinned bytes"


@pytest.mark.parametrize("failure", ["list", "get"])
def test_restore_report_reader_failure_preserves_existing_destination(tmp_path, monkeypatch, failure):
    from tests.storage.test_statistical_report_references import put_report, damage_report_reader

    store = SQLiteRunStore(tmp_path / "reports.db")
    put_report(store)
    backups = tmp_path / "backups"
    consistent_backup(store, backups)
    target = tmp_path / "existing.db"
    target.write_bytes(b"existing destination")
    error = damage_report_reader(store, monkeypatch, failure)
    with pytest.raises(error, match="report read unavailable"):
        restore_sqlite(backups, target, confirm_overwrite=True)
    assert target.read_bytes() == b"existing destination"


def test_report_restore_requires_artifact_destination_before_overwrite(tmp_path):
    from tests.storage.test_statistical_report_references import put_report

    store = SQLiteRunStore(tmp_path / "reports.db")
    artifacts = tmp_path / "artifacts"
    artifact = ArtifactStore(artifacts).put_bytes("pinned.bin", b"must restore together")
    put_report(store, evidence={"artifact_id": artifact.id, "sha256": artifact.sha256})
    backups = tmp_path / "backups"
    consistent_backup(store, backups, artifacts_root=artifacts)
    target = tmp_path / "existing.db"
    target.write_bytes(b"existing destination")
    with pytest.raises(RestoreIncomplete, match="artifacts_root"):
        restore_sqlite(backups, target, confirm_overwrite=True)
    assert target.read_bytes() == b"existing destination"


def test_invalid_report_restore_does_not_replace_existing_staging_store(tmp_path):
    from tests.storage.test_statistical_report_references import put_report

    store = SQLiteRunStore(tmp_path / "source.db")
    put_report(store)
    backups = tmp_path / "backups"
    manifest = consistent_backup(store, backups)
    manifest["counts"]["statistical_reports"] += 1
    manifest["manifest_sha256"] = maintenance._manifest_content_sha256(manifest)
    next(backups.glob("manifest-*.json")).write_text(json.dumps(manifest), encoding="utf-8")
    target = tmp_path / "existing-staging"
    target.mkdir()
    database = target / "runs.db"
    database.write_bytes(b"existing staging database")
    (target / "precious.bin").write_bytes(b"existing staging artifact")
    with pytest.raises(RestoreIncomplete, match="count mismatch for statistical_reports"):
        restore_staging(backups, target, confirm_overwrite=True)
    assert database.read_bytes() == b"existing staging database"
    assert (target / "precious.bin").read_bytes() == b"existing staging artifact"


@pytest.mark.parametrize("ownership", ["same_report", "independent_reports"])
@pytest.mark.parametrize("boundary", ["backup", "staging", "sqlite"])
def test_digest_only_resolution_cannot_replace_report_path_hash(tmp_path, ownership, boundary):
    from motte_storage.artifact_refs import ArtifactReferenceConflict
    from tests.storage.test_statistical_report_references import put_report

    store = SQLiteRunStore(tmp_path / "source.db")
    artifacts = tmp_path / "artifacts"
    artifact_store = ArtifactStore(artifacts)
    pinned = artifact_store.put_bytes("pinned.bin", b"original pinned A")
    discovered = artifact_store.put_bytes("digest-only.bin", b"digest-only B")
    explicit_ref = {"artifact_id": pinned.id, "sha256": pinned.sha256}
    hash_only_ref = {"kind": "artifact", "sha256": discovered.sha256}
    if ownership == "same_report":
        put_report(store, evidence={"items": [explicit_ref, hash_only_ref]})
    else:
        put_report(store, evidence={"items": [explicit_ref]})
        put_report(store, evidence={"items": [hash_only_ref]})
    before_reports = store.statistical_reports.list()
    backups = tmp_path / "backups"
    manifest = consistent_backup(store, backups, artifacts_root=artifacts)
    assert manifest["status"] == "complete"
    assert {entry["path"] for entry in manifest["artifacts"]["files"]} == {
        pinned.id, discovered.id,
    }
    if boundary == "backup":
        (artifacts / pinned.id).write_bytes(b"digest-only B")
        rejected = tmp_path / "rejected-backup"
        with pytest.raises(ArtifactReferenceConflict) as failure:
            consistent_backup(store, rejected, artifacts_root=artifacts)
        assert not any(json.loads(path.read_text())["status"] == "complete"
                       for path in rejected.glob("manifest-*.json"))
        assert (artifacts / pinned.id).read_bytes() == b"digest-only B"
    else:
        copied = backups / manifest["artifacts"]["dir"] / pinned.id
        copied.write_bytes(b"digest-only B")
        for entry in manifest["artifacts"]["files"]:
            if entry["path"] == pinned.id:
                entry.update(sha256=maintenance._sha256_file(copied), bytes=copied.stat().st_size)
        manifest["manifest_sha256"] = maintenance._manifest_content_sha256(manifest)
        next(backups.glob("manifest-*.json")).write_text(json.dumps(manifest), encoding="utf-8")
        existing = tmp_path / "existing"
        existing.mkdir()
        target_db = existing / "runs.db"
        target_db.write_bytes(b"existing database must survive")
        target_artifacts = existing / "artifacts"
        target_artifacts.mkdir()
        (target_artifacts / "precious.bin").write_bytes(b"existing artifact must survive")
        with pytest.raises(ArtifactReferenceConflict) as failure:
            if boundary == "staging":
                restore_staging(backups, existing, confirm_overwrite=True)
            else:
                restore_sqlite(backups, target_db, artifacts_root=target_artifacts,
                               confirm_overwrite=True)
        assert target_db.read_bytes() == b"existing database must survive"
        assert (target_artifacts / "precious.bin").read_bytes() == b"existing artifact must survive"
        assert (artifacts / pinned.id).read_bytes() == b"original pinned A"
    assert failure.value.artifact_id == pinned.id
    assert failure.value.hashes == {pinned.sha256.removeprefix("sha256:"),
                                    discovered.sha256.removeprefix("sha256:")}
    assert (artifacts / discovered.id).read_bytes() == b"digest-only B"
    assert store.statistical_reports.list() == before_reports
    assert platform_for(store).tombstones.list() == []
    assert maintenance_status(store)["active"] is False
