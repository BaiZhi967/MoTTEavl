import json
import os
import time

from motte_sdk.replay_run import ReplayProvider
from motte_sdk.service import RunService
from motte_storage.maintenance import backup_sqlite, cleanup_artifacts, restore_sqlite
from motte_storage.run_store import SQLiteRunStore


def _populate(db_path):
    service = RunService(SQLiteRunStore(db_path), provider=ReplayProvider({}).invoke)
    return service.create_run("replay@1", {}, case_ids=["case-1"])


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
    restore_sqlite(tmp_path / "backups", db, artifacts_root=artifacts)
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

    applied = cleanup_artifacts(root, older_than_days=7, dry_run=False)
    assert applied["dry_run"] is False
    assert not old.exists()
    assert recent.exists()
    assert applied["kept"] == 1


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
