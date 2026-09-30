"""运维维护：SQLite 备份/恢复 与 artifact TTL 清理（默认 dry-run，安全先行）。

PostgreSQL 的备份/恢复使用 pg_dump / pg_restore，见 docs/operations/backup-restore.md。

M7（协议 docs/protocols/sdk-and-migration.md frozen@1 §9.1/§9.2）在此之上提供：

- 维护屏障 ``begin_maintenance`` / ``end_maintenance`` / ``maintenance_status``：
  备份窗口内 API 写入口 503、Worker 拒绝领取（apps/worker 的
  ``_platform_block_reason`` 读取同一 ``motte_meta`` kv，apps/api 的
  MaintenanceModeMiddleware 同理）。
- ``consistent_backup``：屏障 + sqlite online backup + **引用制** artifact 快照
  （逐文件 sha256 校验）+ Manifest v2；缺引用文件 → manifest 标 incomplete 并抛
  BackupIncomplete，绝不静默标成功。
- ``consistent_backup_postgres``：屏障 + ``pg_dump --format=custom``（argv 列表，
  绝不拼 shell 字符串）；PATH 无 pg_dump 时抛 BackupUnsupported（如实 blocked）。
- ``restore_staging``：恢复到全新 staging 目录，全量校验（manifest hash / DB
  schema 指纹 / DB 哈希 / 每文件哈希 / 计数 / 引用完整性），并置
  ``restored_from_backup`` 守卫阻止 Worker 领取，直到
  ``clear_restore_guard(..., confirm=True)`` 显式解除；queued/running/needs_review
  等 Run 原样保留等操作员决定（协议 §9.2，反例 A16）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from contextlib import ExitStack, closing, contextmanager
from threading import RLock, get_ident
from tempfile import TemporaryDirectory
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .platform import platform_for
from .operation_locks import MaintenanceConflict

if TYPE_CHECKING:
    from .trace_retention_models import TraceArchiveReceipt, TraceRetentionPlan, TraceRetentionResult

try:  # workspace 安装时读包元数据；checkout 直接运行时回退到源码常量
    APP_VERSION = _package_version("motte-storage")
except PackageNotFoundError:  # pragma: no cover - 未安装场景
    APP_VERSION = "0.1.0"

#: Manifest v2（协议 §9.1）；restore_staging 只消费 v2 形状（v1 无 status 字段会被跳过）。
MANIFEST_VERSION = 2

#: baseline/gate 的 list API 默认截断 100 条；计数与引用扫描需要"全部"。
_ALL_LIMIT = 1_000_000

MAINTENANCE_FLAG = "maintenance"
MAINTENANCE_STARTED_AT = "maintenance_started_at"
MAINTENANCE_OWNER = "maintenance_owner"
MAINTENANCE_REASON = "maintenance_reason"
MAINTENANCE_LEASE_UNTIL = "maintenance_lease_until"
MAINTENANCE_ALLOW_TOMBSTONES = "maintenance_allow_tombstone_writes"
RESTORE_GUARD_KEY = "restored_from_backup"

_MAINTENANCE_LOCK = RLock()
_LOCAL_MAINTENANCE_LEASES: dict[tuple[int, int], str] = {}


_ACTIVE_MAINTENANCE: dict[str, tuple[str, ExitStack, Any, int, str | None]] = {}
_MAINTENANCE_KEYS = (
    MAINTENANCE_FLAG, MAINTENANCE_STARTED_AT, MAINTENANCE_OWNER,
    MAINTENANCE_REASON, MAINTENANCE_LEASE_UNTIL, MAINTENANCE_ALLOW_TOMBSTONES,
)

#: staging 恢复后需要操作员显式决定的 Run 状态（协议 §9.2：不自动付费执行）。
UNRESOLVED_RUN_STATUSES = (
    "queued", "preparing", "running", "collecting", "scoring", "needs_review",
)


class BackupUnsupported(RuntimeError):
    """后端/工具不可用（如 PATH 上没有 pg_dump），如实登记为 blocked。"""


class BackupIncomplete(RuntimeError):
    """引用的 artifact 缺失或哈希不符；manifest 已落盘但 status=incomplete。"""

    def __init__(
        self, message: str, *, missing: list[str] | None = None,
        mismatched: list[str] | None = None, manifest: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.missing = list(missing or [])
        self.mismatched = list(mismatched or [])
        self.manifest = manifest


class RestoreIncomplete(RuntimeError):
    """staging 校验失败：manifest/DB/文件哈希、schema 指纹、计数或引用完整性。"""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


# --------------------------------------------------------------- 维护屏障


def _lease_key(store: Any) -> tuple[int, int]:
    return id(store), get_ident()


def _store_identity(store: Any) -> str:
    dsn = getattr(store, "dsn", None)
    if dsn:
        return "postgres:" + dsn
    path = getattr(getattr(store, "runs", None), "_path", None)
    return "sqlite:" + str(Path(path).resolve()) if path else "memory:" + str(id(store))


@contextmanager
def _metadata_transaction(store: Any):
    """Serialize acquire/release with one database transaction, including absent keys."""
    meta = platform_for(store).meta
    path = getattr(meta, "_path", None)
    dsn = getattr(meta, "_dsn", None)
    if path:
        with closing(sqlite3.connect(path, isolation_level=None, timeout=10.0)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection, "?"
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
    elif dsn:
        from .postgres import _connect

        with _connect(dsn) as connection:
            connection.execute("SET LOCAL lock_timeout = '10s'")
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", ("motteavl:maintenance-meta",)
            )
            yield connection, "%s"
    else:
        # InMemoryRunStore is process-local; its metadata shares this RLock.
        with meta._lock:
            yield None, meta


def _meta_values(connection, placeholder) -> dict[str, str]:
    if connection is None:
        return dict(placeholder._rows)
    return dict(connection.execute("SELECT meta_key, meta_value FROM motte_meta").fetchall())


def _set_meta_values(connection, placeholder, values: dict[str, str]) -> None:
    if connection is None:
        placeholder._rows.update(values)
        return
    for key, value in values.items():
        connection.execute(
            "INSERT INTO motte_meta(meta_key, meta_value) VALUES ("
            + placeholder + ", " + placeholder + ") ON CONFLICT(meta_key) "
            "DO UPDATE SET meta_value = excluded.meta_value", (key, value),
        )


def _clear_meta_values(connection, placeholder) -> None:
    for key in _MAINTENANCE_KEYS:
        if connection is None:
            placeholder._rows.pop(key, None)
        else:
            connection.execute("DELETE FROM motte_meta WHERE meta_key = " + placeholder, (key,))


def _initialize_lazy_sqlite_repositories(store: Any) -> None:
    """Materialize runtime-created tables before enumerating the write barrier.

    RunStore already initializes its core repositories. ScoringJob and Resource
    repositories are normally constructed lazily and must not create an
    unguarded table after maintenance has become active.
    """
    path = getattr(getattr(store, "runs", None), "_path", None)
    if path is None:
        return
    from .resource_store import SQLiteResourceStore
    from .scoring_jobs import SQLiteScoringJobs

    SQLiteScoringJobs(str(path))
    SQLiteResourceStore(path)


def _install_write_barrier(connection, placeholder) -> None:
    """Guard business/reference/pin tables even when callers bypass API checks.

    Installation runs in the same transaction as activation, draining existing
    database writers before publishing the flag. Metadata remains writable for
    owner release. Tombstones are frozen unless an audit-writing maintenance
    operation explicitly opts in; their data affects reference scanning. Triggers
    stay installed but inert when maintenance is inactive, including after a
    staging restore. Migration 0015 removes its PostgreSQL guards before dropping
    motte_meta on downgrade. Schema changes during maintenance are not supported.
    """
    if connection is None:
        return
    if placeholder == "?":
        names = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )]
        for name in names:
            if name == "motte_meta":
                continue
            audit_guard = (
                " AND NOT EXISTS (SELECT 1 FROM motte_meta "
                "WHERE meta_key = 'maintenance_allow_tombstone_writes' AND meta_value = 'true')"
                if name == "motte_gc_tombstones" else ""
            )
            quoted = '"' + name.replace('"', '""') + '"'
            for action in ("INSERT", "UPDATE", "DELETE"):
                trigger = '"motte_maintenance_' + name.replace('"', '""') + '_' + action + '"'
                connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {trigger} BEFORE {action} ON {quoted} "
                    "WHEN EXISTS (SELECT 1 FROM motte_meta WHERE meta_key = 'maintenance' "
                    "AND meta_value = 'active')" + audit_guard
                    + " BEGIN SELECT RAISE(ABORT, 'maintenance mode active'); END"
                )
        return
    from psycopg import sql

    connection.execute("""
        CREATE OR REPLACE FUNCTION public.motte_maintenance_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
          IF EXISTS (SELECT 1 FROM public.motte_meta
                     WHERE meta_key = 'maintenance' AND meta_value = 'active')
             AND (TG_TABLE_NAME <> 'motte_gc_tombstones' OR NOT EXISTS (
                  SELECT 1 FROM public.motte_meta
                  WHERE meta_key = 'maintenance_allow_tombstone_writes' AND meta_value = 'true')) THEN
            RAISE EXCEPTION 'maintenance mode active';
          END IF;
          RETURN NULL;
        END $$
    """)
    tables = connection.execute(
        "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public' "
        "AND tablename <> 'motte_meta' ORDER BY tablename"
    ).fetchall()
    for (name,) in tables:
        connection.execute(sql.SQL("DROP TRIGGER IF EXISTS motte_maintenance_write ON {}").format(
            sql.Identifier("public", name)
        ))
        connection.execute(sql.SQL(
            "CREATE TRIGGER motte_maintenance_write BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE "
            "ON {} FOR EACH STATEMENT EXECUTE FUNCTION public.motte_maintenance_guard()"
        ).format(sql.Identifier("public", name)))


def _acquire_operation_locks(store: Any, stack: ExitStack, owner: str,
                             reason: str, artifacts_root: str | Path | None) -> Any:
    from .operation_locks import artifact_maintenance_lock, file_lock

    dsn = getattr(store, "dsn", None)
    path = getattr(getattr(store, "runs", None), "_path", None)
    connection = None
    if dsn:
        from .postgres import _connect

        connection = _connect(dsn)
        stack.callback(connection.close)
        connection.autocommit = True
        for key in ("motteavl:maintenance", "motteavl:executor"):
            acquired = connection.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s))", (key,)
            ).fetchone()[0]
            if not acquired:
                raise MaintenanceConflict(key + " is held by another operation")
    elif path:
        path = Path(path).resolve()
        stack.enter_context(file_lock(str(path) + ".maintenance.lock", label="maintenance"))
        stack.enter_context(file_lock(str(path) + ".worker.lock", label="executor"))
    if artifacts_root is not None:
        stack.enter_context(artifact_maintenance_lock(artifacts_root, owner, reason))
    return connection


def _hold_postgres_writes(connection, *, allow_tombstone_writes: bool = False) -> None:
    """Keep dump and all manifest reads on the same quiescent database state.

    SHARE locks allow pg_dump/readers, block all DML/DDL, and drain writers that
    started before activation (including old repeatable-read transactions).
    Metadata remains writable for release. Tombstones are snapshot inputs too:
    only GC/rollback's explicit audit-writing mode leaves that table unlocked.
    """
    if connection is None:
        return
    from psycopg import sql

    connection.execute("BEGIN")
    connection.execute("SET LOCAL lock_timeout = '10s'")
    tables = connection.execute(
        "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public' "
        "AND tablename <> 'motte_meta' ORDER BY tablename"
    ).fetchall()
    for (name,) in tables:
        if name == "motte_gc_tombstones" and allow_tombstone_writes:
            continue
        connection.execute(sql.SQL("LOCK TABLE {} IN SHARE MODE").format(sql.Identifier("public", name)))


def begin_maintenance(
    store: Any, *, reason: str = "backup", owner: str | None = None,
    artifacts_root: str | Path | None = None, allow_tombstone_writes: bool = False,
) -> dict[str, Any]:
    """Acquire one exclusive operation; re-entry requires its explicit live token.

    A matching reason is never proof of ownership. Running executors must stop
    before maintenance; direct database writers and ArtifactStore mutations are
    barred for the whole window. A crashed operation leaves the persisted flag
    active (fail closed) until an operator explicitly releases its owner token.
    """
    identity = _store_identity(store)
    if identity.startswith("memory:"):
        raise BackupUnsupported("maintenance requires a persistent SQLite or PostgreSQL store")
    with _MAINTENANCE_LOCK:
        if owner is not None:
            held = _ACTIVE_MAINTENANCE.get(owner)
            if held is None or held[0] != identity or held[3] != os.getpid():
                raise MaintenanceConflict("re-entry requires the live maintenance owner")
            if artifacts_root is not None and str(Path(artifacts_root).resolve()) != held[4]:
                raise MaintenanceConflict("re-entry cannot change the locked artifact root")
            with _metadata_transaction(store) as (connection, placeholder):
                values = _meta_values(connection, placeholder)
                if values.get(MAINTENANCE_OWNER) != owner or values.get(MAINTENANCE_REASON) != reason:
                    raise MaintenanceConflict("maintenance owner or reason does not match")
                audit_setting = "true" if allow_tombstone_writes else "false"
                if values.get(MAINTENANCE_ALLOW_TOMBSTONES, "false") != audit_setting:
                    raise MaintenanceConflict("re-entry cannot change tombstone write permission")
                return {"active": True, "started_at": values[MAINTENANCE_STARTED_AT],
                        "reason": reason, "owner": owner}
        token = uuid4().hex
        stack = ExitStack()
        activated = False
        try:
            pg_connection = _acquire_operation_locks(store, stack, token, reason, artifacts_root)
            if maintenance_status(store)["active"]:
                raise MaintenanceConflict("maintenance barrier already owned by another operation")
            _initialize_lazy_sqlite_repositories(store)
            with _metadata_transaction(store) as (connection, placeholder):
                values = _meta_values(connection, placeholder)
                if values.get(MAINTENANCE_FLAG) == "active":
                    raise MaintenanceConflict("maintenance barrier already owned by another operation")
                _install_write_barrier(connection, placeholder)
                started_at = _utc_now_iso()
                _set_meta_values(connection, placeholder, {
                    MAINTENANCE_FLAG: "active", MAINTENANCE_STARTED_AT: started_at,
                    MAINTENANCE_OWNER: token, MAINTENANCE_REASON: reason,
                    MAINTENANCE_LEASE_UNTIL: "session",
                    MAINTENANCE_ALLOW_TOMBSTONES: "true" if allow_tombstone_writes else "false",
                })
            activated = True
            _hold_postgres_writes(pg_connection, allow_tombstone_writes=allow_tombstone_writes)
        except BaseException:
            # Close table/advisory/file locks before cleanup so failure cannot
            # strand resources or deadlock the metadata release transaction.
            stack.close()
            if activated:
                with _metadata_transaction(store) as (connection, placeholder):
                    if _meta_values(connection, placeholder).get(MAINTENANCE_OWNER) == token:
                        _clear_meta_values(connection, placeholder)
            raise
        _LOCAL_MAINTENANCE_LEASES[_lease_key(store)] = token
        _ACTIVE_MAINTENANCE[token] = (
            identity, stack, pg_connection, os.getpid(),
            str(Path(artifacts_root).resolve()) if artifacts_root is not None else None,
        )
        return {"active": True, "started_at": started_at, "reason": reason, "owner": token}


def end_maintenance(
    store: Any, *, owner: str | None = None, reason: str | None = None
) -> dict[str, Any]:
    """Release only this owner; explicit tokens also recover abandoned barriers."""
    with _MAINTENANCE_LOCK:
        token = owner or _LOCAL_MAINTENANCE_LEASES.get(_lease_key(store))
        held = _ACTIVE_MAINTENANCE.get(token)
        identity = _store_identity(store)
        if held is not None and (held[0] != identity or held[3] != os.getpid()):
            raise MaintenanceConflict("maintenance owner belongs to another store or process")
        with ExitStack() as recovery:
            # If the token's process is still alive, another process must not
            # release its flag while it is copying evidence or deleting files.
            if held is None and maintenance_status(store)["active"]:
                _acquire_operation_locks(store, recovery, token or "", reason or "recovery", None)
            with _metadata_transaction(store) as (connection, placeholder):
                values = _meta_values(connection, placeholder)
                if values.get(MAINTENANCE_FLAG) != "active":
                    started_at = None
                    if held is not None:
                        _clear_meta_values(connection, placeholder)
                else:
                    if token != values.get(MAINTENANCE_OWNER) or (
                        reason is not None and reason != values.get(MAINTENANCE_REASON)
                    ):
                        raise MaintenanceConflict("maintenance barrier cannot be cleared by a different operation")
                    started_at = values.get(MAINTENANCE_STARTED_AT)
                    _clear_meta_values(connection, placeholder)
            if held is not None:
                held[1].close()
                del _ACTIVE_MAINTENANCE[token]
            for key, current in list(_LOCAL_MAINTENANCE_LEASES.items()):
                if current == token:
                    del _LOCAL_MAINTENANCE_LEASES[key]
        return {"active": False, "started_at": started_at, "ended_at": _utc_now_iso()}


def maintenance_status(store: Any) -> dict[str, Any]:
    """读取屏障状态：{active, started_at}。"""
    meta = platform_for(store).meta
    return {"active": meta.get(MAINTENANCE_FLAG) == "active", "started_at": meta.get(MAINTENANCE_STARTED_AT)}


# ------------------------------------------------------------------ 工具


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_schema_fingerprint() -> str:
    """SQLite schema 指纹：run_store._SCHEMA DDL 的 sha256 前 16 位。

    SQLite 不走 Alembic（alembic_revision 恒为 None），以 DDL 指纹承载协议 §9.1
    的 schema_version 语义：restore_staging 据此确认备份时的 DDL 与当前代码一致。
    """
    from .run_store import _SCHEMA

    return hashlib.sha256(_SCHEMA.encode("utf-8")).hexdigest()[:16]


def _manifest_content_sha256(manifest: dict[str, Any]) -> str:
    """manifest 自身哈希：对去掉 manifest_sha256 字段后的规范 JSON 计算。"""
    content = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    return hashlib.sha256(
        json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _artifact_path(root: Path, artifact_id: str) -> Path:
    relative = Path(artifact_id)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise ValueError("artifact path escapes root: " + artifact_id)
    resolved_root = root.resolve()
    resolved = (resolved_root / relative).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError("artifact path escapes root: " + artifact_id) from error
    return resolved


def _backup_stamp(now: datetime) -> str:
    # 毫秒后缀避免同秒多次备份互相覆盖
    return now.strftime("%Y%m%dT%H%M%S") + f"m{now.microsecond // 1000:03d}Z"


def _snapshot_sqlite(db_path: Path, snapshot: Path) -> None:
    """sqlite online backup（对 WAL 安全）；结束时 checkpoint 让主文件自足可哈希。

    独立成模块级函数，便于测试在备份窗口中段观测维护屏障状态（反例 A14）。
    """
    with closing(sqlite3.connect(db_path)) as source, closing(
        sqlite3.connect(snapshot)
    ) as destination:
        source.backup(destination)
        destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")


# ------------------------------------------------- artifact 引用收集（宽口径）


def _collect_artifact_refs(value: Any, refs: dict[str, str | None]) -> None:
    from .artifact_refs import collect_artifact_refs

    collect_artifact_refs(value, refs)


def _referenced_artifact_hashes(store: Any) -> dict[str, str | None]:
    from .artifact_refs import referenced_artifact_hashes

    return referenced_artifact_hashes(store)


def _referenced_artifacts(store: Any) -> set[str]:
    return set(_referenced_artifact_hashes(store))


def _backup_references(
    store: Any, artifacts_root: str | Path | None,
) -> tuple[dict[str, str | None], list[str]]:
    """Resolve legacy digest-only refs without treating hashes as file paths."""
    from .artifact_refs import collect_artifact_refs, referenced_artifact_hashes

    hashes: set[str] = set()
    refs = referenced_artifact_hashes(store, hashes=hashes)
    unresolved = hashes - {digest.removeprefix("sha256:") for digest in refs.values() if digest}
    if unresolved and artifacts_root is not None:
        root = Path(artifacts_root).resolve()
        for candidate in sorted(root.rglob("*")):
            if not candidate.is_file():
                continue
            identifier = candidate.relative_to(root).as_posix()
            try:
                source = _artifact_path(root, identifier)
            except ValueError:
                continue
            digest = _sha256_file(source)
            if digest in unresolved:
                # Discovery may fill an unspecified hash, never overwrite an
                # independently asserted immutable path/hash pair.
                collect_artifact_refs({"artifact_id": identifier, "sha256": digest}, refs)
        unresolved -= {digest.removeprefix("sha256:") for digest in refs.values() if digest}
    return refs, ["sha256:" + digest for digest in sorted(unresolved)]


def _validate_restored_reference_closure(store: Any, artifacts_root: Path) -> None:
    """Verify frozen evidence assertions independently of the file inventory."""
    referenced, missing = _backup_references(store, artifacts_root)
    for artifact_id, expected in sorted(referenced.items()):
        try:
            restored_file = _artifact_path(artifacts_root, artifact_id)
        except ValueError as error:
            raise RestoreIncomplete(str(error)) from error
        if not restored_file.is_file():
            missing.append(artifact_id)
        elif expected is not None and _sha256_file(restored_file) != expected.removeprefix("sha256:"):
            raise RestoreIncomplete(f"referenced artifact hash mismatch: {artifact_id}")
    if missing:
        raise RestoreIncomplete(
            "referenced artifacts missing from staging restore", details={"missing": missing},
        )


def _store_counts(store: Any) -> dict[str, int]:
    runs = store.runs.list()
    scoring_passes = (
        sum(len(store.scoring_passes.list_for_run(run.get("id"))) for run in runs)
        if store.scoring_passes is not None
        else 0
    )
    baselines = (
        len(store.baseline_store.list(limit=_ALL_LIMIT))
        if store.baseline_store is not None
        else 0
    )
    gate_results = (
        len(store.gate_store.list_results(limit=_ALL_LIMIT))
        if store.gate_store is not None
        else 0
    )
    return {
        "runs": len(runs),
        "scoring_passes": scoring_passes,
        "baselines": baselines,
        "gate_results": gate_results,
        "statistical_reports": len(store.statistical_reports.list(limit=None)),
        "calibration_records": len(list(store.calibrations.iter_records())),
    }


def _backup_artifact_files(
    refs: dict[str, str | None], artifacts_root: Path, artifact_dir: Path,
) -> tuple[list[dict[str, Any]], list[str], list[str], list[str]]:
    """复制被引用的 artifact 并计算 sha256；返回 (files, missing, mismatched, warnings)。

    只复制 DB 引用的不可变集合（协议 §9.1）；root 里的额外文件属 GC 候选，
    不入备份、记 warning。payload 嵌入 sha256 时逐文件核对。
    """
    files: list[dict[str, Any]] = []
    missing: list[str] = []
    mismatched: list[str] = []
    for artifact_id in sorted(refs):
        try:
            source = _artifact_path(artifacts_root, artifact_id)
        except ValueError:
            missing.append(artifact_id)
            continue
        if not source.is_file():
            missing.append(artifact_id)
            continue
        destination = _artifact_path(artifact_dir, artifact_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        digest = _sha256_file(destination)
        expected = refs.get(artifact_id)
        if expected is not None and expected.removeprefix("sha256:") != digest:
            mismatched.append(artifact_id)
            continue
        files.append({"path": artifact_id, "bytes": destination.stat().st_size, "sha256": digest})
    warnings: list[str] = []
    if artifacts_root.exists():
        present = {
            path.relative_to(artifacts_root).as_posix()
            for path in artifacts_root.rglob("*")
            if path.is_file()
        }
        for extra in sorted(present - set(refs)):
            warnings.append("unreferenced file not backed up: " + extra)
    return files, missing, mismatched, warnings


def _write_manifest_v2(
    target: Path,
    stamp: str,
    *,
    backend: str,
    database: dict[str, Any],
    artifacts_section: dict[str, Any] | None,
    counts: dict[str, int],
    maintenance_window: dict[str, Any],
    schema_version: str | None,
    alembic_revision: str | None,
    missing: list[str],
    mismatched: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "created_at": _utc_now_iso(),
        "app_version": APP_VERSION,
        "backend": backend,
        "schema_version": schema_version,
        "alembic_revision": alembic_revision,
        "database": database,
        "artifacts": artifacts_section,
        "counts": counts,
        "maintenance": maintenance_window,
        "status": "incomplete" if missing or mismatched else "complete",
    }
    if missing:
        manifest["missing"] = missing
    if mismatched:
        manifest["hash_mismatches"] = mismatched
    if warnings:
        manifest["warnings"] = warnings
    manifest["manifest_sha256"] = _manifest_content_sha256(manifest)
    (target / f"manifest-{stamp}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


# ------------------------------------------------------------ 一致备份


def consistent_backup(
    store: Any, target_dir: str | Path, *, artifacts_root: str | Path | None = None,
    reason: str = "backup",
) -> dict[str, Any]:
    """一致备份（协议 §9.1）：维护屏障 + sqlite online backup + 引用制 artifact 快照。

    - 屏障激活期间 API 写入口 503、Worker 拒绝领取；无论成败 finally 解除。
    - 引用的 artifact 缺失或嵌入 sha256 不符 → 落盘 status=incomplete 的 manifest
      后 raise BackupIncomplete（不标成功）；root 额外文件记 warning 不入备份。
    - 返回即落盘的 Manifest v2。
    """
    db_path = getattr(getattr(store, "runs", None), "_path", None)
    if db_path is None:
        raise BackupUnsupported("consistent_backup requires a SQLite-backed store")
    if not Path(db_path).exists():
        raise BackupUnsupported("database file not found: " + str(db_path))
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    stamp = _backup_stamp(datetime.now(UTC))
    snapshot = target / f"runs-{stamp}.db"
    begin = begin_maintenance(store, reason=reason, artifacts_root=artifacts_root)
    try:
        _snapshot_sqlite(Path(db_path), snapshot)
        # Read references and counts from the immutable DB snapshot, not the live
        # store.  This prevents an in-flight worker from producing a mixed
        # database/artifact manifest after the online backup point-in-time.
        from .run_store import SQLiteRunStore

        snapshot_store = SQLiteRunStore(snapshot)
        refs, unresolved_hashes = _backup_references(snapshot_store, artifacts_root)
        artifacts_section: dict[str, Any] | None = None
        missing: list[str] = []
        mismatched: list[str] = []
        warnings: list[str] = []
        if artifacts_root is not None:
            artifact_dir = target / f"artifacts-{stamp}"
            files, missing, mismatched, warnings = _backup_artifact_files(
                refs, Path(artifacts_root), artifact_dir
            )
            artifacts_section = {"dir": artifact_dir.name, "files": files}
        else:
            missing.extend(sorted(refs))
        missing.extend(unresolved_hashes)
        counts = _store_counts(snapshot_store)
        ended_at = _utc_now_iso()
        manifest = _write_manifest_v2(
            target,
            stamp,
            backend="sqlite",
            database={
                "snapshot": snapshot.name,
                "bytes": snapshot.stat().st_size,
                "sha256": _sha256_file(snapshot),
            },
            artifacts_section=artifacts_section,
            counts=counts,
            maintenance_window={"started_at": begin["started_at"], "ended_at": ended_at},
            schema_version=_sqlite_schema_fingerprint(),
            alembic_revision=None,  # SQLite 不走 Alembic；schema 用 DDL 指纹对齐
            missing=missing,
            mismatched=mismatched,
            warnings=warnings,
        )
        if missing or mismatched:
            raise BackupIncomplete(
                "backup incomplete: referenced artifacts missing or hash-mismatched",
                missing=missing,
                mismatched=mismatched,
                manifest=manifest,
            )
        return manifest
    finally:
        end_maintenance(store, owner=begin["owner"])


def consistent_backup_postgres(
    dsn: str,
    target_dir: str | Path,
    *,
    artifacts_root: str | Path | None = None,
    store: Any = None,
    reason: str = "backup",
) -> dict[str, Any]:
    """PG 一致备份：屏障 + ``pg_dump --format=custom``（argv 列表，绝不拼 shell）。

    PATH 上没有 pg_dump 时 raise BackupUnsupported（支持矩阵如实登记 blocked）。
    store 缺省经 motte_storage 工厂按 dsn 构建；artifact 引用制快照与 Manifest v2
    逻辑与 consistent_backup 相同。
    """
    if shutil.which("pg_dump") is None:
        raise BackupUnsupported("pg_dump not available")
    from .postgres import normalize_dsn

    dsn = normalize_dsn(dsn)
    if store is not None and getattr(store, "dsn", None) != dsn:
        raise BackupUnsupported("backup store must use the same PostgreSQL DSN as pg_dump")
    if store is None:
        from .factory import create_run_store

        store = create_run_store(dsn=dsn, storage="postgres")
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    stamp = _backup_stamp(datetime.now(UTC))
    dump_path = target / f"runs-{stamp}.dump"
    begin = begin_maintenance(store, reason=reason, artifacts_root=artifacts_root)
    try:
        completed = subprocess.run(
            ["pg_dump", "--format=custom", "--file", str(dump_path), dsn],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise BackupIncomplete(
                "pg_dump failed: " + (completed.stderr or "").strip()[:500]
            )
        refs, unresolved_hashes = _backup_references(store, artifacts_root)
        artifacts_section: dict[str, Any] | None = None
        missing: list[str] = []
        mismatched: list[str] = []
        warnings: list[str] = []
        if artifacts_root is not None:
            artifact_dir = target / f"artifacts-{stamp}"
            files, missing, mismatched, warnings = _backup_artifact_files(
                refs, Path(artifacts_root), artifact_dir
            )
            artifacts_section = {"dir": artifact_dir.name, "files": files}
        else:
            missing.extend(sorted(refs))
        missing.extend(unresolved_hashes)
        counts = _store_counts(store)
        try:
            from .migrations import current as _alembic_current

            alembic_revision = _alembic_current(dsn)
        except Exception:  # noqa: BLE001 - 版本读取失败按未知登记，不阻断备份
            alembic_revision = None
        ended_at = _utc_now_iso()
        manifest = _write_manifest_v2(
            target,
            stamp,
            backend="postgres",
            database={
                "snapshot": dump_path.name,
                "bytes": dump_path.stat().st_size,
                "sha256": _sha256_file(dump_path),
            },
            artifacts_section=artifacts_section,
            counts=counts,
            maintenance_window={"started_at": begin["started_at"], "ended_at": ended_at},
            schema_version=None,
            alembic_revision=alembic_revision,
            missing=missing,
            mismatched=mismatched,
            warnings=warnings,
        )
        if missing or mismatched:
            raise BackupIncomplete(
                "backup incomplete: referenced artifacts missing or hash-mismatched",
                missing=missing,
                mismatched=mismatched,
                manifest=manifest,
            )
        return manifest
    finally:
        end_maintenance(store, owner=begin["owner"])


# ---------------------------------------------------------- staging 恢复


def _latest_complete_manifest(backup_dir: Path) -> tuple[Path, dict[str, Any]]:
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(backup_dir.glob("manifest-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("status") == "complete":
            candidates.append((path, data))
    if not candidates:
        raise RestoreIncomplete(f"no complete backup manifest found in {backup_dir}")
    return candidates[-1]


def restore_staging(
    backup_dir: str | Path,
    staging_dir: str | Path,
    *,
    expected_manifest_sha256: str | None = None,
    confirm_overwrite: bool = False,
) -> dict[str, Any]:
    """Validate a guarded candidate before replacing any requested staging target."""
    staging = Path(staging_dir)
    if staging.exists() and any(staging.iterdir()) and not confirm_overwrite:
        raise FileExistsError(
            f"staging directory exists and is not empty: {staging} "
            "(pass confirm_overwrite=True to replace it)"
        )
    staging.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".motte-restore-", dir=staging.parent) as temporary:
        candidate = Path(temporary) / "candidate"
        restored = _restore_staging_checked(
            backup_dir, candidate, expected_manifest_sha256=expected_manifest_sha256,
        )
        if staging.exists():
            shutil.rmtree(staging)
        candidate.rename(staging)
        restored["database"] = str(staging / "runs.db")
        return restored


def _restore_staging_checked(
    backup_dir: str | Path,
    staging_dir: str | Path,
    *,
    expected_manifest_sha256: str | None = None,
    confirm_overwrite: bool = False,
) -> dict[str, Any]:
    """恢复到全新 staging 目录（协议 §9.2），全程不触碰线上库。

    校验链：manifest 自身哈希（+ 可选的外部期望哈希）→ DB 快照哈希 → SQLite
    schema 指纹 → 逐 artifact 文件哈希 → 打开 staging 库核对计数 → 引用完整性
    （被引用 artifact 必须在 staging artifacts 里）。任何一环失败 raise
    RestoreIncomplete，绝不标成功。成功后置 ``restored_from_backup`` 守卫
    （Worker 拒绝领取），并列出需要操作员决定的未终态 Run（不改状态、不执行）。
    """
    backup_dir = Path(backup_dir)
    staging = Path(staging_dir)
    manifest_path, manifest = _latest_complete_manifest(backup_dir)
    recomputed = _manifest_content_sha256(manifest)
    if manifest.get("manifest_sha256") != recomputed:
        raise RestoreIncomplete(
            f"manifest hash mismatch: {manifest_path.name}",
            details={"expected": manifest.get("manifest_sha256"), "actual": recomputed},
        )
    if expected_manifest_sha256 is not None and expected_manifest_sha256 != manifest["manifest_sha256"]:
        raise RestoreIncomplete(
            "expected manifest sha256 does not match the restored manifest",
            details={"expected": expected_manifest_sha256, "actual": manifest["manifest_sha256"]},
        )
    if staging.exists() and any(staging.iterdir()) and not confirm_overwrite:
        raise FileExistsError(
            f"staging directory exists and is not empty: {staging} "
            "(pass confirm_overwrite=True to replace it)"
        )
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    if manifest.get("backend") != "sqlite":
        raise RestoreIncomplete(
            "restore_staging only restores sqlite manifests; use pg_restore for: "
            + str(manifest.get("backend"))
        )
    snapshot = backup_dir / manifest["database"]["snapshot"]
    if not snapshot.is_file():
        raise RestoreIncomplete(f"database snapshot missing from backup: {snapshot.name}")
    snapshot_sha = _sha256_file(snapshot)
    if snapshot_sha != manifest["database"].get("sha256"):
        raise RestoreIncomplete(
            "database snapshot hash mismatch",
            details={"expected": manifest["database"].get("sha256"), "actual": snapshot_sha},
        )
    if manifest.get("schema_version") != _sqlite_schema_fingerprint():
        raise RestoreIncomplete(
            "sqlite schema fingerprint mismatch between backup and current code",
            details={"expected": manifest.get("schema_version")},
        )
    db_file = staging / "runs.db"
    shutil.copyfile(snapshot, db_file)
    if _sha256_file(db_file) != manifest["database"]["sha256"]:
        raise RestoreIncomplete("restored staging database hash mismatch")

    artifacts_section = manifest.get("artifacts")
    artifacts_root = staging / "artifacts"
    files_restored = 0
    if artifacts_section:
        source_root = backup_dir / artifacts_section["dir"]
        for entry in artifacts_section.get("files", []):
            artifact_id = entry["path"]
            try:
                source = _artifact_path(source_root, artifact_id)
                destination = _artifact_path(artifacts_root, artifact_id)
            except ValueError as error:
                raise RestoreIncomplete(str(error)) from error
            if not source.is_file():
                raise RestoreIncomplete(f"artifact missing from backup: {artifact_id}")
            digest = _sha256_file(source)
            if digest != entry.get("sha256"):
                raise RestoreIncomplete(
                    f"artifact hash mismatch in backup: {artifact_id}",
                    details={"expected": entry.get("sha256"), "actual": digest},
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            if _sha256_file(destination) != entry["sha256"]:
                raise RestoreIncomplete(f"artifact hash mismatch after copy: {artifact_id}")
            files_restored += 1

    from .run_store import SQLiteRunStore

    staging_store = SQLiteRunStore(db_file)
    expected_counts = dict(manifest.get("counts") or {})
    actual_counts = _store_counts(staging_store)
    for key, expected in expected_counts.items():
        if actual_counts.get(key) != expected:
            raise RestoreIncomplete(
                f"restored count mismatch for {key}",
                details={"expected": expected, "actual": actual_counts.get(key)},
            )
    _validate_restored_reference_closure(staging_store, artifacts_root)

    # 恢复守卫：Worker 的 _platform_block_reason 读到该键即拒绝领取，
    # 需 clear_restore_guard(confirm=True) 显式解除。
    #
    # 快照不可避免地冻结了备份窗口的屏障标志（备份时屏障必须激活）；屏障在备份
    # 结束时已解除（manifest.maintenance.ended_at），staging 不能继承"维护中"
    # 状态，否则恢复后永远 503。staging 的写保护由下面的恢复守卫承担。
    meta = platform_for(staging_store).meta
    for key in _MAINTENANCE_KEYS:
        meta.delete(key)
    meta.set(RESTORE_GUARD_KEY, manifest_path.name)
    unresolved = [
        {"id": run.get("id"), "status": run.get("status")}
        for run in staging_store.runs.list()
        if run.get("status") in UNRESOLVED_RUN_STATUSES
    ]
    return {
        "database": str(db_file),
        "backup": manifest_path.name,
        "artifacts": {"files_restored": files_restored},
        "counts": {"expected": expected_counts, "actual": actual_counts},
        "unresolved_runs": unresolved,
        "restore_guard": "active",
    }


def _postgres_database_name(dsn: str) -> str:
    """Effective libpq database name only; never echo a credential-bearing DSN."""
    from psycopg.conninfo import conninfo_to_dict

    name = conninfo_to_dict(dsn).get("dbname")
    if not isinstance(name, str) or not name:
        raise RestoreIncomplete("staging PostgreSQL DSN must name a database")
    return name


def restore_postgres_staging(
    backup_dir: str | Path,
    staging_dsn: str,
    artifacts_target: str | Path,
    *,
    expected_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Verify a PG backup and restore it into an empty, separately supplied DB.

    This path never drops or cleans database objects. A failed restore leaves the
    staging database for diagnosis; it does not attempt to roll back into or
    replace the source. The target must not have user objects or artifact files.
    """
    backup_root = Path(backup_dir)
    target_root = Path(artifacts_target)
    manifest_path, manifest = _latest_complete_manifest(backup_root)
    if manifest.get("manifest_version") != MANIFEST_VERSION or manifest.get("backend") != "postgres":
        raise RestoreIncomplete("staging restore requires a PostgreSQL manifest v2")
    actual_manifest_hash = _manifest_content_sha256(manifest)
    if manifest.get("manifest_sha256") != actual_manifest_hash:
        raise RestoreIncomplete("manifest hash mismatch")
    if (expected_manifest_sha256 is not None
            and expected_manifest_sha256 != actual_manifest_hash):
        raise RestoreIncomplete("expected manifest sha256 does not match")
    try:
        snapshot = _artifact_path(backup_root, str(manifest["database"]["snapshot"]))
    except (KeyError, TypeError, ValueError) as error:
        raise RestoreIncomplete("invalid PostgreSQL snapshot path") from error
    if not snapshot.is_file():
        raise RestoreIncomplete("database snapshot missing from backup")
    if (snapshot.stat().st_size != manifest["database"].get("bytes")
            or _sha256_file(snapshot) != manifest["database"].get("sha256")):
        raise RestoreIncomplete("database snapshot hash mismatch")
    expected_counts = manifest.get("counts")
    count_keys = {"runs", "scoring_passes", "baselines", "gate_results"}
    if (not isinstance(expected_counts, dict)
            or not count_keys.issubset(expected_counts)
            or any(type(expected_counts[key]) is not int or expected_counts[key] < 0
                   for key in count_keys)):
        raise RestoreIncomplete("invalid PostgreSQL backup counts")
    artifacts = manifest.get("artifacts")
    backed_files: list[tuple[str, Path, str]] = []
    if artifacts is not None:
        try:
            source_root = _artifact_path(backup_root, str(artifacts["dir"]))
            entries = artifacts["files"]
        except (KeyError, TypeError, ValueError) as error:
            raise RestoreIncomplete("invalid artifact section in PostgreSQL backup") from error
        if not isinstance(entries, list):
            raise RestoreIncomplete("invalid artifact list in PostgreSQL backup")
        for entry in entries:
            try:
                artifact_id = str(entry["path"])
                source = _artifact_path(source_root, artifact_id)
                expected_hash = str(entry["sha256"])
                expected_bytes = entry["bytes"]
            except (KeyError, TypeError, ValueError) as error:
                raise RestoreIncomplete("invalid artifact entry in PostgreSQL backup") from error
            if (not source.is_file() or source.stat().st_size != expected_bytes
                    or _sha256_file(source) != expected_hash):
                raise RestoreIncomplete(f"artifact hash mismatch in backup: {artifact_id}")
            backed_files.append((artifact_id, source, expected_hash))
    if (target_root.is_symlink() or
            (target_root.exists() and
             (not target_root.is_dir() or any(target_root.iterdir())))):
        raise FileExistsError(f"staging artifact target is not empty: {target_root}")
    if shutil.which("pg_restore") is None:
        raise BackupUnsupported("pg_restore not available")

    from .factory import create_run_store
    from .postgres import _connect, normalize_dsn

    dsn = normalize_dsn(staging_dsn)
    target_database = _postgres_database_name(dsn)
    with _connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        if cursor.fetchone()[0] != target_database:
            raise RestoreIncomplete("staging PostgreSQL database identity mismatch")
        cursor.execute(
            "SELECT COUNT(*) FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE left(n.nspname, 3) <> 'pg_' "
            "AND n.nspname <> 'information_schema' "
            "AND c.relkind IN ('r','p','v','m','S','f')"
        )
        if cursor.fetchone()[0]:
            raise RestoreIncomplete("staging PostgreSQL database is not empty")
        cursor.execute(
            "SELECT COUNT(*) FROM pg_catalog.pg_namespace "
            "WHERE nspname NOT IN ('public', 'information_schema') "
            "AND left(nspname, 3) <> 'pg_'"
        )
        if cursor.fetchone()[0]:
            raise RestoreIncomplete("staging PostgreSQL database has user schemas")
    completed = subprocess.run(
        ["pg_restore", "--single-transaction", "--exit-on-error",
         "--dbname", dsn, str(snapshot)],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0:
        raise RestoreIncomplete(
            f"pg_restore failed with exit code {completed.returncode}; "
            "inspect the isolated staging target before retrying"
        )
    store = create_run_store(storage="postgres", dsn=dsn)
    # The dump captures maintenance=active. Set the restore guard before
    # clearing that inherited flag, and leave the guard active on any later
    # verification failure. No Worker may claim a queued staging Run.
    meta = platform_for(store).meta
    meta.set(RESTORE_GUARD_KEY, manifest_path.name)
    for key in _MAINTENANCE_KEYS:
        meta.delete(key)
    expected_counts = dict(expected_counts)
    actual_counts = _store_counts(store)
    for key, expected in expected_counts.items():
        if actual_counts.get(key) != expected:
            raise RestoreIncomplete(
                f"restored count mismatch for {key}",
                details={"expected": expected, "actual": actual_counts.get(key)},
            )
    expected_revision = manifest.get("alembic_revision")
    if expected_revision is not None:
        from .migrations import current

        if current(dsn) != expected_revision:
            raise RestoreIncomplete("restored Alembic revision mismatch")
    target_root.mkdir(parents=True, exist_ok=True)
    for artifact_id, source, expected_hash in backed_files:
        try:
            destination = _artifact_path(target_root, artifact_id)
        except ValueError as error:
            raise RestoreIncomplete(str(error)) from error
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if _sha256_file(destination) != expected_hash:
            raise RestoreIncomplete(f"artifact hash mismatch after copy: {artifact_id}")
    _validate_restored_reference_closure(store, target_root)
    unresolved = [
        {"id": run.get("id"), "status": run.get("status")}
        for run in store.runs.list() if run.get("status") in UNRESOLVED_RUN_STATUSES
    ]
    return {
        "database": target_database, "backup": manifest_path.name,
        "artifacts": {"files_restored": len(backed_files)},
        "counts": {"expected": expected_counts, "actual": actual_counts},
        "unresolved_runs": unresolved, "restore_guard": "active",
    }


def clear_restore_guard(db_path_or_store: str | Path | Any, *, confirm: bool = False) -> dict[str, Any]:
    """解除恢复守卫；必须显式 confirm=True（对应 ``motte restore-guard clear --yes``）。"""
    if not confirm:
        raise ValueError("clear_restore_guard requires confirm=True (explicit operator decision)")
    if isinstance(db_path_or_store, (str, Path)):
        db_path = Path(db_path_or_store)
        if not db_path.exists():
            raise FileNotFoundError(f"database not found: {db_path}")
        from .run_store import SQLiteRunStore

        store = SQLiteRunStore(db_path)
    else:
        store = db_path_or_store
    platform_for(store).meta.delete(RESTORE_GUARD_KEY)
    return {"restore_guard": "cleared"}


# ------------------------------------------------ 既有入口（保持向后兼容）


def backup_sqlite(
    db_path: str | Path,
    target_dir: str | Path,
    *,
    artifacts_root: str | Path | None = None,
) -> dict[str, Any]:
    """在线备份 SQLite（sqlite3 backup API，对 WAL 安全）+ 可选 artifacts 快照。

    M7 起的一致备份（屏障 + 引用制校验 + Manifest v2）见 consistent_backup。
    """
    db_path = Path(db_path)
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    # 毫秒后缀避免同秒多次备份互相覆盖
    stamp = _backup_stamp(now)
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
        root = Path(artifacts_root).resolve()
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
    confirm_overwrite: bool = False,
) -> dict[str, Any]:
    """从 backup_dir 中最新一次备份恢复数据库与 artifacts（覆盖目标文件）。

    M7（协议 §9.2）起覆盖已有目标必须显式 ``confirm_overwrite=True``（并要求先
    备份）；新代码优先 restore_staging——恢复到全新 staging 目录全量校验，不碰
    线上库。恢复前请停止 API 与 Worker，避免活连接写坏刚恢复的库。
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
    artifacts_section = manifest.get("artifacts")
    artifacts_target = Path(artifacts_root) if artifacts_root is not None and artifacts_section else None
    overwrites = db_path.exists() or (
        artifacts_target is not None and artifacts_target.exists()
    )
    if overwrites and not confirm_overwrite:
        raise FileExistsError(
            "restore_sqlite would overwrite an existing target; pass "
            "confirm_overwrite=True (protocol 9.2: confirm and back up first)"
        )
    with ExitStack() as staging:
        artifact_source = None
        if manifest.get("manifest_version") == MANIFEST_VERSION:
            if manifest.get("status") != "complete":
                raise RestoreIncomplete("cannot restore an incomplete backup")
            checked_root = Path(staging.enter_context(TemporaryDirectory(prefix="motte-restore-")))
            checked = restore_staging(
                backup_dir, checked_root,
                expected_manifest_sha256=manifest.get("manifest_sha256"),
            )
            snapshot = Path(checked["database"])
            artifact_source = checked_root / "artifacts"
            if checked["artifacts"]["files_restored"] and artifacts_target is None:
                raise RestoreIncomplete("referenced artifacts require an artifacts_root restore target")
        elif artifacts_section:
            artifact_source = backup_dir / (
                artifacts_section.get("snapshot") or artifacts_section.get("dir")
            )
        db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(snapshot, db_path)
        if manifest.get("manifest_version") != MANIFEST_VERSION:
            try:
                # Legacy v1 snapshots predate the complete staging contract.
                from .run_store import SQLiteRunStore

                meta = platform_for(SQLiteRunStore(db_path)).meta
                meta.delete(MAINTENANCE_FLAG)
                meta.delete(MAINTENANCE_STARTED_AT)
            except Exception:  # noqa: BLE001 - old backups may lack platform tables
                pass
        restored: dict[str, Any] = {"database": str(db_path), "backup": manifests[-1].name}
        if artifacts_target is not None:
            if artifacts_target.exists():
                shutil.rmtree(artifacts_target)
            if (manifest.get("manifest_version") == MANIFEST_VERSION
                    and artifact_source is not None and not artifact_source.exists()):
                artifacts_target.mkdir(parents=True)
            else:
                shutil.copytree(artifact_source, artifacts_target)
            restored["artifacts"] = str(artifacts_target)
        return restored


def cleanup_artifacts(
    root: str | Path,
    *,
    older_than_days: float,
    dry_run: bool = True,
    now: float | None = None,
) -> dict[str, Any]:
    """Legacy TTL diagnostics only; unbound deletion cannot verify live references.

    Keep the historical dry-run report shape for callers, but never infer a
    database from an artifact path or permit this entry point to bypass GC.
    """
    if not dry_run:
        raise BackupUnsupported(
            "legacy artifact cleanup apply is disabled: use motte gc plan and motte gc apply "
            "with the correct database and artifact root for reference-aware deletion"
        )
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
        else:
            report["kept"] += 1
    report["deleted"] = deleted
    return report


def _require_trace_retention_owner(store, owner):
    from .operation_locks import _trace_archive_owner
    held = _ACTIVE_MAINTENANCE.get(owner)
    if held is None or held[3] != os.getpid() or held[0] != _store_identity(store) or held[4] is None:
        raise MaintenanceConflict('Trace commit requires the live store/root owner')
    _trace_archive_owner(store, held[4], owner)
    if held[2] is not None and held[2].info.transaction_status.name != 'INTRANS':
        raise MaintenanceConflict('Trace commit requires the original live lock transaction')
    return held


def commit_trace_retention(store: Any, *, owner: str, plan: TraceRetentionPlan,
                           receipts: list[TraceArchiveReceipt]) -> TraceRetentionResult:
    """Commit only the verified fixed receipt INSERT + Trace prefix DELETE operation.

    The existing live owner supplies authority, never a reason string, callback,
    connection or SQL argument. PG's quiescent transaction is never released and
    reacquired. Guard DDL, every receipt/trim and owner clearance commit together.
    """
    from . import trace_retention_models as models
    from .artifacts import ArtifactStore
    from .operation_locks import trace_archive_write_capability
    from .trace_archives import (
        _receipts_on_connection, build_trace_archive, verify_trace_archive,
    )
    from .trace_retention import _plan_at_cutoff, _validate_apply_plan
    from motte_contracts.identity import canonical_sha256

    held = _require_trace_retention_owner(store, owner)
    with _MAINTENANCE_LOCK:
        if _ACTIVE_MAINTENANCE.get(owner) is not held:
            raise MaintenanceConflict('Trace maintenance ownership changed')
        plan = _validate_apply_plan(store, plan, plan.config)
        receipts = [models.TraceArchiveReceipt.model_validate(row) for row in receipts]
        if ([row.prefix for row in receipts] != plan.prefixes or
                any(row.plan_id != plan.plan_id or row.cutoff != plan.cutoff or row.committed_at is not None
                    or row.archive_id != 'trace-archive-' + row.sha256.removeprefix('sha256:')
                    for row in receipts)):
            raise models.TraceArchiveInvalid('pending receipts must match the complete exact plan')
        if _plan_at_cutoff(store, config=plan.config, cutoff=plan.cutoff) != plan:
            raise models.TraceRetentionPlanChanged('retention plan inputs changed before commit')
        artifacts = ArtifactStore(held[4])
        # Reverify durability in this entry point too; calling the fixed commit
        # directly with guessed receipts cannot bypass archive fsync/verification.
        from .operation_locks import _TRACE_ARCHIVE_CAPABILITIES
        capability_key = (os.getpid(), get_ident(), held[4], owner)
        with ExitStack() as capabilities:
            if capability_key not in _TRACE_ARCHIVE_CAPABILITIES:
                capabilities.enter_context(trace_archive_write_capability(
                    store, artifacts_root=held[4], maintenance_owner=owner))
            for receipt in receipts:
                data = artifacts.read_bytes(receipt.artifact_id)
                verify_trace_archive(data, receipt)
                artifacts.put_trace_archive(data, maintenance_owner=owner)
                verify_trace_archive(artifacts.read_bytes(receipt.artifact_id), receipt)

        pg = held[2] is not None
        connection = held[2] if pg else sqlite3.connect(
            store.runs._path, isolation_level=None, timeout=10.0)
        placeholder = '%s' if pg else '?'
        try:
            if not pg:
                connection.execute('BEGIN IMMEDIATE')
                values = _meta_values(connection, placeholder)
            else:
                connection.execute("SELECT pg_advisory_xact_lock(hashtext('motteavl:maintenance-meta'))")
                # Metadata is intentionally outside the business-table SHARE
                # barrier. Hold its owner rows from validation through clearance,
                # including against direct metadata-repository writes.
                values = dict(connection.execute(
                    'SELECT meta_key, meta_value FROM motte_meta FOR UPDATE').fetchall())
            if (values.get(MAINTENANCE_OWNER) != owner or values.get(MAINTENANCE_FLAG) != 'active'
                    or values.get(MAINTENANCE_REASON) != 'trace_retention'
                    or values.get(MAINTENANCE_ALLOW_TOMBSTONES) != 'false'):
                raise MaintenanceConflict('Trace commit maintenance metadata disagrees')
            existing = _receipts_on_connection(connection)
            if existing:
                raise models.TraceRetentionPlanChanged('existing Trace receipts require verified replay')
            # Read exact rows on the lock-owning connection before any DDL/DML.
            for receipt in receipts:
                prefix = receipt.prefix
                rows = connection.execute(
                    'SELECT seq, payload, stored_at FROM trace_events WHERE run_id = ' + placeholder +
                    ' ORDER BY seq', (prefix.run_id,)).fetchall()
                events = [models.StoredTraceEvent(
                    run_id=prefix.run_id, seq=seq,
                    payload=json.loads(payload) if isinstance(payload, str) else payload,
                    stored_at=stamp) for seq, payload, stamp in rows]
                selected = [row for row in events if prefix.first_seq <= row.seq <= prefix.last_seq]
                if (not events or events[-1].seq != prefix.keep_seq
                        or [row.seq for row in selected] != list(range(prefix.first_seq, prefix.last_seq + 1))
                        or canonical_sha256([row.model_dump(mode='json') for row in selected]) != prefix.events_sha256
                        or build_trace_archive(prefix, selected) != artifacts.read_bytes(receipt.artifact_id)):
                    raise models.TraceRetentionPlanChanged('Trace rows changed before fixed commit')

            if pg:
                # Statement triggers cover all actions. Exclude all competing
                # writers before replacing just these two touched-table guards.
                connection.execute('LOCK TABLE trace_events, trace_archive_receipts IN ACCESS EXCLUSIVE MODE')
                guards = connection.execute("""
                    SELECT c.relname, pg_get_triggerdef(t.oid) FROM pg_trigger t
                    JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='public' AND c.relname IN ('trace_events','trace_archive_receipts')
                    AND t.tgname='motte_maintenance_write' ORDER BY c.relname
                """).fetchall()
                if [row[0] for row in guards] != ['trace_archive_receipts', 'trace_events']:
                    raise MaintenanceConflict('Trace commit requires both installed statement guards')
                connection.execute('DROP TRIGGER motte_maintenance_write ON public.trace_archive_receipts')
                connection.execute('DROP TRIGGER motte_maintenance_write ON public.trace_events')
            else:
                guards = connection.execute("""
                    SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name IN (
                    'motte_maintenance_trace_events_DELETE', 'motte_maintenance_trace_archive_receipts_INSERT')
                    ORDER BY name
                """).fetchall()
                if len(guards) != 2:
                    raise MaintenanceConflict('Trace commit requires both installed action guards')
                connection.execute('DROP TRIGGER motte_maintenance_trace_events_DELETE')
                connection.execute('DROP TRIGGER motte_maintenance_trace_archive_receipts_INSERT')
            committed = []
            stamp = models.utc_now()
            for receipt in receipts:
                row = models.TraceArchiveReceipt.model_validate({**receipt.model_dump(), 'committed_at': stamp})
                payload = row.model_dump(mode='json')
                if pg:
                    from psycopg.types.json import Jsonb
                    payload = Jsonb(payload)
                else:
                    payload = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                connection.execute(
                    'INSERT INTO trace_archive_receipts(archive_id, plan_id, run_id, first_seq, last_seq, payload) '
                    'VALUES (' + ', '.join([placeholder] * 6) + ')',
                    (row.archive_id, row.plan_id, row.prefix.run_id, row.prefix.first_seq, row.prefix.last_seq, payload))
                deleted = connection.execute(
                    'DELETE FROM trace_events WHERE run_id = ' + placeholder + ' AND seq >= ' + placeholder +
                    ' AND seq <= ' + placeholder, (row.prefix.run_id, row.prefix.first_seq, row.prefix.last_seq))
                if deleted.rowcount != row.prefix.event_count:
                    raise models.TraceRetentionPlanChanged('Trace prefix delete count disagrees')
                committed.append(row)
            for _, definition in guards:
                connection.execute(definition)
            _clear_meta_values(connection, placeholder)
            result = models.TraceRetentionResult(plan_id=plan.plan_id,
                trimmed_events=sum(row.prefix.event_count for row in committed), receipts=committed)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            if not pg:
                connection.close()
        # Metadata already cleared in the exact receipt/trim transaction. Closing
        # this owner's resources releases the PG session advisory/file locks last.
        held[1].close()
        del _ACTIVE_MAINTENANCE[owner]
        for key, current in list(_LOCAL_MAINTENANCE_LEASES.items()):
            if current == owner:
                del _LOCAL_MAINTENANCE_LEASES[key]
        return result
