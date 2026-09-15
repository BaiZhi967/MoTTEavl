"""运维维护：SQLite 备份/恢复 与 artifact TTL 清理（默认 dry-run，安全先行）。

PostgreSQL 的备份/恢复使用 pg_dump / pg_restore，见 docs/operations/backup-restore.md。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def backup_sqlite(
    db_path: str | Path,
    target_dir: str | Path,
    *,
    artifacts_root: str | Path | None = None,
) -> dict[str, Any]:
    """在线备份 SQLite（sqlite3 backup API，对 WAL 安全）+ 可选 artifacts 快照。"""
    db_path = Path(db_path)
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    # 毫秒后缀避免同秒多次备份互相覆盖
    stamp = now.strftime("%Y%m%dT%H%M%S") + f"m{now.microsecond // 1000:03d}Z"
    snapshot = target / f"runs-{stamp}.db"
    if db_path.exists():
        with sqlite3.connect(db_path) as source, sqlite3.connect(snapshot) as destination:
            source.backup(destination)
    else:
        snapshot.touch()
    manifest: dict[str, Any] = {
        "created_at": datetime.now(UTC).isoformat(),
        "database": {
            "source": str(db_path),
            "snapshot": snapshot.name,
            "bytes": snapshot.stat().st_size,
        },
        "artifacts": None,
    }
    if artifacts_root is not None:
        root = Path(artifacts_root)
        if root.exists():
            artifact_snapshot = target / f"artifacts-{stamp}"
            shutil.copytree(root, artifact_snapshot)
            manifest["artifacts"] = {
                "source": str(root),
                "snapshot": artifact_snapshot.name,
                "files": sum(1 for path in artifact_snapshot.rglob("*") if path.is_file()),
            }
    (target / f"manifest-{stamp}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def restore_sqlite(
    backup_dir: str | Path,
    db_path: str | Path,
    *,
    artifacts_root: str | Path | None = None,
) -> dict[str, Any]:
    """从 backup_dir 中最新一次备份恢复数据库与 artifacts（覆盖目标文件）。

    恢复前请停止 API 与 Worker，避免活连接写坏刚恢复的库。
    """
    backup_dir = Path(backup_dir)
    manifests = sorted(backup_dir.glob("manifest-*.json"))
    if not manifests:
        raise FileNotFoundError(f"no backup manifest found in {backup_dir}")
    manifest = json.loads(manifests[-1].read_text(encoding="utf-8"))
    snapshot = backup_dir / manifest["database"]["snapshot"]
    if not snapshot.exists():
        raise FileNotFoundError(f"snapshot missing: {snapshot}")
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(snapshot, db_path)
    restored: dict[str, Any] = {"database": str(db_path), "backup": manifests[-1].name}
    if artifacts_root is not None and manifest.get("artifacts"):
        source = backup_dir / manifest["artifacts"]["snapshot"]
        target = Path(artifacts_root)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        restored["artifacts"] = str(target)
    return restored


def cleanup_artifacts(
    root: str | Path,
    *,
    older_than_days: float,
    dry_run: bool = True,
    now: float | None = None,
) -> dict[str, Any]:
    """按 TTL 清理 artifact 文件；默认 dry-run 只报告不删除。"""
    root = Path(root)
    report: dict[str, Any] = {"dry_run": dry_run, "deleted": [], "kept": 0, "freed_bytes": 0}
    if not root.exists():
        return report
    cutoff = (now if now is not None else time.time()) - older_than_days * 86400
    deleted: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        stat = path.stat()
        if stat.st_mtime < cutoff:
            deleted.append({"name": str(path.relative_to(root)), "bytes": stat.st_size})
            report["freed_bytes"] += stat.st_size
            if not dry_run:
                path.unlink()
        else:
            report["kept"] += 1
    report["deleted"] = deleted
    if not dry_run:
        for directory in sorted((item for item in root.rglob("*") if item.is_dir()), reverse=True):
            if not any(directory.iterdir()):
                directory.rmdir()
    return report
