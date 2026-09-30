import json
import os
import time
from datetime import UTC, datetime, timedelta

import pytest
from motte_sdk.replay_run import ReplayProvider
from motte_sdk.service import RunService
from motte_storage.gc import apply_gc, plan_gc
from motte_storage.maintenance import (
    BackupUnsupported,
    MaintenanceConflict,
    backup_sqlite,
    begin_maintenance,
    cleanup_artifacts,
    end_maintenance,
    maintenance_status,
    restore_sqlite,
)
from motte_storage.run_store import SQLiteRunStore


def _populate(db_path):
    service = RunService(SQLiteRunStore(db_path), provider=ReplayProvider({}).invoke)
    return service.create_run("replay@1", {}, case_ids=["case-1"])


def test_maintenance_barrier_helpers_are_idempotent(tmp_path):
    store = SQLiteRunStore(tmp_path / "runs.db")
    _populate(tmp_path / "runs.db")

    assert maintenance_status(store) == {"active": False, "started_at": None}
    first = begin_maintenance(store)
    assert first["active"] is True
    assert first["started_at"]
    # 同一操作幂等重入：不刷新 started_at
    again = begin_maintenance(store, reason="backup", owner=first["owner"])
    assert again["started_at"] == first["started_at"]
    assert again["owner"] == first["owner"]
    assert maintenance_status(store)["active"] is True

    # GC cannot overlap or steal the backup lease.
    with pytest.raises(MaintenanceConflict):
        begin_maintenance(store, reason="gc")

    ended = end_maintenance(store, owner=first["owner"])
    assert ended["active"] is False
    assert ended["ended_at"]
    end_maintenance(store, owner=first["owner"])  # 幂等：重复解除不报错
    assert maintenance_status(store) == {"active": False, "started_at": None}


def test_backup_and_gc_cannot_overlap(tmp_path):
    store = SQLiteRunStore(tmp_path / "runs.db")
    _populate(tmp_path / "runs.db")
    root = tmp_path / "artifacts"
    root.mkdir()
    old = root / "orphan.bin"
    old.write_bytes(b"x")
    stamp = (datetime.now(UTC) - timedelta(days=365)).timestamp()
    os.utime(old, (stamp, stamp))
    plan = plan_gc(store, root, artifact_ttl_days=1)

    lease = begin_maintenance(store, reason="backup")
    try:
        with pytest.raises(MaintenanceConflict):
            apply_gc(store, root, plan, confirm=True)
        assert old.exists()
    finally:
        end_maintenance(store, owner=lease["owner"])


def test_backup_and_restore_roundtrip_preserves_runs_and_events(tmp_path):
    db = tmp_path / "runs.db"
    run = _populate(db)
    RunService(
        SQLiteRunStore(db), provider=ReplayProvider({"case-1": {"output": "ok"}}).invoke
    ).execute(run["id"])

    backup_dir = tmp_path / "backups"
    manifest = backup_sqlite(db, backup_dir)
    assert manifest["database"]["bytes"] > 0
    assert (backup_dir / manifest["database"]["snapshot"]).exists()

    restored_db = tmp_path / "restored.db"
    restored = restore_sqlite(backup_dir, restored_db)
    assert restored["database"] == str(restored_db)
    service = RunService(SQLiteRunStore(restored_db))
    assert service.get_run(run["id"])["status"] == "completed"
    events = service.events(run["id"])
    assert len(events) == 8
    assert [item["seq"] for item in events] == list(range(1, 9))
    assert any(item["type"] == "scoring_pass_created" for item in events)

    # M7（协议 §9.2）：覆盖已有目标必须显式 confirm_overwrite
    with pytest.raises(FileExistsError):
        restore_sqlite(backup_dir, restored_db)
    restore_sqlite(backup_dir, restored_db, confirm_overwrite=True)
    assert RunService(SQLiteRunStore(restored_db)).get_run(run["id"])["status"] == "completed"


def test_restore_uses_latest_backup(tmp_path):
    db = tmp_path / "runs.db"
    first = _populate(db)
    backup_dir = tmp_path / "backups"
    backup_sqlite(db, backup_dir)

    second = _populate(db)  # 再创建一个 run 后做第二次备份
    time.sleep(0.01)
    backup_sqlite(db, backup_dir)
    assert len(list(backup_dir.glob("manifest-*.json"))) == 2

    restored_db = tmp_path / "restored.db"
    restore_sqlite(backup_dir, restored_db)
    runs = RunService(SQLiteRunStore(restored_db)).store.runs.list()
    assert {run["id"] for run in runs} == {first["id"], second["id"]}


def test_backup_includes_artifacts_snapshot(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "a.txt").write_text("x", encoding="utf-8")
    db = tmp_path / "runs.db"
    _populate(db)

    manifest = backup_sqlite(db, tmp_path / "backups", artifacts_root=artifacts)
    assert manifest["artifacts"]["files"] == 1

    (artifacts / "a.txt").unlink()
    # 恢复覆盖现有 db 与 artifacts 目录：需显式 confirm_overwrite（协议 §9.2）
    with pytest.raises(FileExistsError):
        restore_sqlite(tmp_path / "backups", db, artifacts_root=artifacts)
    restore_sqlite(tmp_path / "backups", db, artifacts_root=artifacts, confirm_overwrite=True)
    assert (artifacts / "a.txt").read_text() == "x"


def test_cleanup_artifacts_defaults_to_dry_run(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    old = root / "old.json"
    recent = root / "recent.json"
    old.write_text("x" * 100, encoding="utf-8")
    recent.write_text("y", encoding="utf-8")
    cutoff = time.time() - 86400 * 30
    os.utime(old, (cutoff, cutoff))

    report = cleanup_artifacts(root, older_than_days=7)
    assert report["dry_run"] is True
    assert [item["name"] for item in report["deleted"]] == ["old.json"]
    assert report["freed_bytes"] == 100
    assert old.exists() and recent.exists()  # dry-run 不删

    with pytest.raises(BackupUnsupported, match="motte gc"):
        cleanup_artifacts(root, older_than_days=7, dry_run=False)
    assert old.exists() and recent.exists()


def test_cleanup_reports_empty_root(tmp_path):
    report = cleanup_artifacts(tmp_path / "missing", older_than_days=1)
    assert report == {"dry_run": True, "deleted": [], "kept": 0, "freed_bytes": 0}


def test_backup_manifest_is_valid_json(tmp_path):
    db = tmp_path / "runs.db"
    _populate(db)
    manifest = backup_sqlite(db, tmp_path / "backups")
    path = tmp_path / "backups"
    stored = json.loads(sorted(path.glob("manifest-*.json"))[-1].read_text(encoding="utf-8"))
    assert stored == manifest
    assert "created_at" in stored


def _maintenance_contender(db_path, started, result):
    store = SQLiteRunStore(db_path)
    started.set()
    try:
        lease = begin_maintenance(store, reason="backup")
    except MaintenanceConflict:
        result.put("conflict")
    else:
        end_maintenance(store, owner=lease["owner"])
        result.put("acquired")


def test_same_reason_cannot_claim_another_operation_lease(tmp_path):
    store = SQLiteRunStore(tmp_path / "runs.db")
    other = SQLiteRunStore(tmp_path / "runs.db")
    first = begin_maintenance(store)
    try:
        for contender in (store, other):
            with pytest.raises(MaintenanceConflict):
                begin_maintenance(contender, reason="backup")
        with pytest.raises(MaintenanceConflict):
            end_maintenance(other)
        assert maintenance_status(store)["active"] is True
    finally:
        end_maintenance(store, owner=first["owner"])


def test_cross_process_same_reason_cannot_release_active_lease(tmp_path):
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    db = tmp_path / "runs.db"
    store = SQLiteRunStore(db)
    lease = begin_maintenance(store)
    started, result = context.Event(), context.Queue()
    process = context.Process(target=_maintenance_contender, args=(str(db), started, result))
    try:
        process.start()
        assert started.wait(15)
        assert result.get(timeout=15) == "conflict"
        process.join(15)
        assert process.exitcode == 0
        assert maintenance_status(store)["active"] is True
    finally:
        if process.is_alive():
            process.terminate()
            process.join()
        end_maintenance(store, owner=lease["owner"])


def test_maintenance_bars_direct_repository_and_pin_mutations(tmp_path):
    import sqlite3

    store = SQLiteRunStore(tmp_path / "runs.db")
    run = _populate(tmp_path / "runs.db")
    lease = begin_maintenance(store)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="maintenance"):
            store.runs.save({**run, "status": "needs_review"})
        with pytest.raises(sqlite3.IntegrityError, match="maintenance"):
            store.case_runs.upsert({"run_id": run["id"], "case_id": "new-ref",
                                   "artifact_refs": [{"id": "late.bin"}]})
        with pytest.raises(sqlite3.IntegrityError, match="maintenance"):
            store.baseline_store.put({
                "baseline_id": "late-pin", "entries": [{"cell_key": None, "ref": {
                    "run_id": run["id"], "scoring_pass_id": "p1",
                    "report_schema": "run-report@2", "evidence_hash": "sha256:x",
                }}], "comparison_policy_hash": "sha256:y", "created_by": "test",
                "reason": "late pin", "created_at": "2026-09-30T00:00:00Z",
                "metrics": {},
            })
        assert store.runs.get(run["id"])["status"] == "queued"
    finally:
        end_maintenance(store, owner=lease["owner"])
    store.runs.save({**run, "status": "needs_review"})
    assert store.runs.get(run["id"])["status"] == "needs_review"


def test_maintenance_refuses_active_executor(tmp_path):
    from motte_sdk.execution_lock import worker_execution_lock

    db = tmp_path / "runs.db"
    store = SQLiteRunStore(db)
    with worker_execution_lock(db):
        with pytest.raises(MaintenanceConflict, match="executor"):
            begin_maintenance(store)
    assert maintenance_status(store)["active"] is False
    lease = begin_maintenance(store)
    end_maintenance(store, owner=lease["owner"])


def _artifact_contender(root, result):
    from motte_storage.artifacts import ArtifactStore

    try:
        ArtifactStore(root).put_bytes("evidence.bin", b"changed")
    except RuntimeError:
        result.put("conflict")
    else:
        result.put("written")


def test_maintenance_bars_cross_process_artifact_overwrite(tmp_path):
    import multiprocessing
    from motte_storage.artifacts import ArtifactStore

    context = multiprocessing.get_context("spawn")
    store = SQLiteRunStore(tmp_path / "runs.db")
    root = tmp_path / "artifacts"
    artifacts = ArtifactStore(root)
    artifacts.put_bytes("evidence.bin", b"original")
    lease = begin_maintenance(store, artifacts_root=root)
    result = context.Queue()
    process = context.Process(target=_artifact_contender, args=(str(root), result))
    try:
        process.start()
        assert result.get(timeout=15) == "conflict"
        process.join(15)
        assert process.exitcode == 0
        assert artifacts.read_bytes("evidence.bin") == b"original"
    finally:
        if process.is_alive():
            process.terminate()
            process.join()
        end_maintenance(store, owner=lease["owner"])
    artifacts.put_bytes("evidence.bin", b"after")
    assert artifacts.read_bytes("evidence.bin") == b"after"


def test_reentry_cannot_claim_a_different_artifact_root(tmp_path):
    store = SQLiteRunStore(tmp_path / "runs.db")
    lease = begin_maintenance(store, artifacts_root=tmp_path / "a")
    try:
        with pytest.raises(MaintenanceConflict, match="artifact root"):
            begin_maintenance(store, owner=lease["owner"], artifacts_root=tmp_path / "b")
    finally:
        end_maintenance(store, owner=lease["owner"])


def _abandon_maintenance(db_path, root, result):
    store = SQLiteRunStore(db_path)
    lease = begin_maintenance(store, artifacts_root=root)
    result.send(lease["owner"])
    result.close()
    os._exit(0)


def test_crashed_owner_stays_fail_closed_until_explicit_recovery(tmp_path):
    import multiprocessing
    import sqlite3
    from motte_storage.artifacts import ArtifactStore

    context = multiprocessing.get_context("spawn")
    db, root = tmp_path / "runs.db", tmp_path / "artifacts"
    store = SQLiteRunStore(db)
    run = _populate(db)
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_abandon_maintenance, args=(str(db), str(root), child))
    process.start()
    try:
        assert parent.poll(15)
        owner = parent.recv()
        process.join(15)
        assert process.exitcode == 0
        with pytest.raises(MaintenanceConflict):
            begin_maintenance(store)
        with pytest.raises(sqlite3.IntegrityError, match="maintenance"):
            store.runs.save({**run, "status": "needs_review"})
        with pytest.raises(MaintenanceConflict):
            end_maintenance(store, owner="not-the-owner")
        end_maintenance(store, owner=owner)
        assert maintenance_status(store)["active"] is False
        lease = begin_maintenance(store, artifacts_root=root)
        end_maintenance(store, owner=lease["owner"])
        ArtifactStore(root).put_bytes("after-recovery", b"ok")
    finally:
        if process.is_alive():
            process.terminate()
            process.join()
        parent.close()
        child.close()


def _race_maintenance(db_path, ready, start, release, result):
    store = SQLiteRunStore(db_path)
    ready.put(True)
    assert start.wait(15)
    try:
        lease = begin_maintenance(store)
    except MaintenanceConflict:
        result.put("conflict")
    else:
        result.put("acquired")
        assert release.wait(15)
        end_maintenance(store, owner=lease["owner"])


def test_simultaneous_processes_have_exactly_one_maintenance_owner(tmp_path):
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    db = tmp_path / "runs.db"
    store = SQLiteRunStore(db)
    ready, result = context.Queue(), context.Queue()
    start, release = context.Event(), context.Event()
    processes = [context.Process(target=_race_maintenance,
                                 args=(str(db), ready, start, release, result)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        assert ready.get(timeout=15) and ready.get(timeout=15)
        start.set()
        assert sorted([result.get(timeout=15), result.get(timeout=15)]) == ["acquired", "conflict"]
        assert maintenance_status(store)["active"] is True
        release.set()
        for process in processes:
            process.join(15)
            assert process.exitcode == 0
        assert maintenance_status(store)["active"] is False
    finally:
        release.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()


def test_owner_release_closes_locks_even_if_flag_was_removed(tmp_path):
    from motte_storage.artifacts import ArtifactStore
    from motte_storage.platform import platform_for

    store = SQLiteRunStore(tmp_path / "runs.db")
    root = tmp_path / "artifacts"
    lease = begin_maintenance(store, artifacts_root=root)
    platform_for(store).meta.delete("maintenance")
    end_maintenance(store, owner=lease["owner"])
    artifact = ArtifactStore(root).put_bytes("after", b"released")
    assert artifact.id == "after"


def test_memory_store_cannot_claim_a_durable_write_barrier():
    from motte_storage.maintenance import BackupUnsupported
    from motte_storage.run_store import InMemoryRunStore

    with pytest.raises(BackupUnsupported, match="persistent"):
        begin_maintenance(InMemoryRunStore())


def test_unrelated_artifact_writers_can_share_the_mutation_gate(tmp_path):
    from motte_storage.artifacts import ArtifactStore
    from motte_storage.operation_locks import artifact_mutation

    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    with artifact_mutation(root):
        assert store.put_bytes("parallel-evidence", b"safe").id == "parallel-evidence"


def test_lazy_scoring_job_cannot_publish_after_barrier_activation(tmp_path):
    import sqlite3
    from motte_storage.scoring_jobs import ScoringJobConflict, scoring_jobs_for
    from tests.storage.test_scoring_jobs import job_record

    store = SQLiteRunStore(tmp_path / "runs.db")
    lease = begin_maintenance(store)
    try:
        jobs = scoring_jobs_for(store)
        with pytest.raises(ScoringJobConflict) as error:
            jobs.submit(job_record(owner={"kind": "calibration", "calibration_job_id": "late"}))
        assert isinstance(error.value.__cause__, sqlite3.IntegrityError)
        assert "maintenance" in str(error.value.__cause__)
        assert jobs.get("sjob-1") is None
    finally:
        end_maintenance(store, owner=lease["owner"])


def test_lazy_resource_repository_cannot_write_after_barrier_activation(tmp_path):
    import sqlite3
    from motte_storage.resource_store import SQLiteResourceStore

    db = tmp_path / "runs.db"
    store = SQLiteRunStore(db)
    lease = begin_maintenance(store)
    try:
        resources = SQLiteResourceStore(db)
        with pytest.raises(sqlite3.IntegrityError, match="maintenance"):
            resources.providers.put({"name": "late-provider"})
        assert resources.providers.get("late-provider") is None
    finally:
        end_maintenance(store, owner=lease["owner"])


@pytest.mark.parametrize("action", ["append", "update", "delete"])
def test_backup_barrier_freezes_reference_bearing_tombstones(tmp_path, action):
    import sqlite3
    from motte_storage.platform import platform_for

    db = tmp_path / "runs.db"
    store = SQLiteRunStore(db)
    tombstones = platform_for(store).tombstones
    tombstones.append([{"gc_run_id": "old", "artifact_id": "archived.bin"}])
    # The reason is an audit label, not permission to alter backup inputs.
    lease = begin_maintenance(store, reason="gc")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="maintenance"):
            if action == "append":
                tombstones.append([{"gc_run_id": "new", "artifact_id": "other.bin"}])
            else:
                query = ("UPDATE motte_gc_tombstones SET payload = '{}'" if action == "update"
                         else "DELETE FROM motte_gc_tombstones")
                with sqlite3.connect(db) as connection:
                    connection.execute(query)
        assert len(tombstones.list()) == 1
    finally:
        end_maintenance(store, owner=lease["owner"])


def test_gc_explicitly_allows_audit_writes_and_cannot_upgrade_reentry(tmp_path):
    from motte_storage.platform import platform_for

    store = SQLiteRunStore(tmp_path / "runs.db")
    lease = begin_maintenance(store, reason="gc", allow_tombstone_writes=True)
    try:
        platform_for(store).tombstones.append([{"gc_run_id": "gc", "artifact_id": "deleted.bin"}])
        assert len(platform_for(store).tombstones.list()) == 1
    finally:
        end_maintenance(store, owner=lease["owner"])
    lease = begin_maintenance(store)
    try:
        with pytest.raises(MaintenanceConflict, match="tombstone"):
            begin_maintenance(store, owner=lease["owner"], allow_tombstone_writes=True)
    finally:
        end_maintenance(store, owner=lease["owner"])


@pytest.mark.parametrize("in_maintenance", [False, True])
def test_legacy_cleanup_apply_preserves_old_referenced_evidence(tmp_path, in_maintenance):
    db, root = tmp_path / "runs.db", tmp_path / "artifacts"
    store = SQLiteRunStore(db)
    run = _populate(db)
    root.mkdir()
    evidence = root / "evidence.bin"
    evidence.write_bytes(b"referenced")
    old = time.time() - 86400 * 365
    os.utime(evidence, (old, old))
    store.runs.save({**run, "artifact_refs": [{"id": evidence.name}]})
    lease = begin_maintenance(store, artifacts_root=root) if in_maintenance else None
    try:
        with pytest.raises(BackupUnsupported, match="motte gc"):
            cleanup_artifacts(root, older_than_days=7, dry_run=False)
        assert evidence.read_bytes() == b"referenced"
        assert store.runs.get(run["id"])["artifact_refs"] == [{"id": evidence.name}]
    finally:
        if lease:
            end_maintenance(store, owner=lease["owner"])


def test_maintenance_blocks_report_repository_and_raw_sql_but_allows_get(tmp_path):
    import sqlite3
    from contextlib import closing
    from motte_contracts.hashing import canonical_json
    from motte_contracts.statistical_reports import statistical_report_id
    from tests.storage.test_statistical_report_references import put_report, report_body

    db = tmp_path / "reports.db"
    store = SQLiteRunStore(db)
    existing = put_report(store)
    body = report_body(evidence={"artifact_id": "late.bin"})
    report_id = statistical_report_id(body)
    lease = begin_maintenance(store)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="maintenance"):
            store.statistical_reports.put(report_id, body)
        with closing(sqlite3.connect(db)) as connection, connection:
            with pytest.raises(sqlite3.IntegrityError, match="maintenance"):
                connection.execute("INSERT INTO statistical_reports VALUES (?, ?, ?)",
                                   (report_id, canonical_json(body), "2026-09-30T00:00:00Z"))
        assert store.statistical_reports.get(existing["report_id"]) == existing
        assert store.statistical_reports.get(report_id) is None
        assert store.statistical_reports.list() == [existing]
    finally:
        end_maintenance(store, owner=lease["owner"])


def test_pg_maintenance_blocks_report_repository_and_raw_sql_but_allows_get(isolated_pg_database):
    import psycopg
    from psycopg.conninfo import make_conninfo
    from motte_contracts.hashing import canonical_json
    from motte_contracts.statistical_reports import statistical_report_id
    from motte_storage.postgres import create_postgres_run_store
    from motte_storage.statistical_reports import PgStatisticalReports
    from tests.storage.test_statistical_reports_pg import _upgrade
    from tests.storage.test_statistical_report_references import put_report, report_body

    dsn = isolated_pg_database
    _upgrade(dsn)
    store = create_postgres_run_store(dsn)
    existing = put_report(store)
    body = report_body(evidence={"artifact_id": "late.bin"})
    report_id = statistical_report_id(body)
    # Independent real connections time out while maintenance's SHARE lock is held.
    bounded = make_conninfo(dsn, options="-c lock_timeout=300ms")
    repository = PgStatisticalReports(bounded)
    lease = begin_maintenance(store)
    try:
        with pytest.raises(psycopg.errors.LockNotAvailable):
            repository.put(report_id, body)
        with psycopg.connect(bounded) as connection:
            with pytest.raises(psycopg.errors.LockNotAvailable):
                connection.execute("INSERT INTO statistical_reports VALUES (%s, %s, %s)",
                                   (report_id, canonical_json(body), "2026-09-30T00:00:00Z"))
        assert repository.get(existing["report_id"]) == existing
        assert repository.get(report_id) is None
        assert repository.list() == [existing]
    finally:
        end_maintenance(store, owner=lease["owner"])
