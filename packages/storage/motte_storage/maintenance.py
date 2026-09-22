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
import shutil
import sqlite3
import subprocess
import time
from contextlib import closing
from threading import RLock, get_ident
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from pathlib import Path
from typing import Any
from uuid import uuid4

from .platform import platform_for

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
RESTORE_GUARD_KEY = "restored_from_backup"

_MAINTENANCE_LOCK = RLock()
_LOCAL_MAINTENANCE_LEASES: dict[tuple[int, int], str] = {}


class MaintenanceConflict(RuntimeError):
    """A maintenance operation attempted to take or release another lease."""

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


def begin_maintenance(store: Any, *, reason: str = "backup") -> dict[str, Any]:
    """Acquire the shared maintenance barrier for one named operation.

    Re-entry by the same operation remains idempotent, but a different reason
    cannot steal or replace the active owner.  The lease token is returned to
    internal callers and persisted in the shared metadata store.
    """
    meta = platform_for(store).meta
    with _MAINTENANCE_LOCK:
        if meta.get(MAINTENANCE_FLAG) == "active":
            current_reason = meta.get(MAINTENANCE_REASON) or "backup"
            if current_reason != reason:
                raise MaintenanceConflict(
                    f"maintenance barrier owned by {current_reason!r}, cannot acquire for {reason!r}"
                )
            owner = meta.get(MAINTENANCE_OWNER)
            started_at = meta.get(MAINTENANCE_STARTED_AT)
            if not owner:
                owner = uuid4().hex
                meta.set(MAINTENANCE_OWNER, owner)
            _LOCAL_MAINTENANCE_LEASES[_lease_key(store)] = owner
            return {"active": True, "started_at": started_at, "reason": reason, "owner": owner}

        owner = uuid4().hex
        started_at = _utc_now_iso()
        meta.set(MAINTENANCE_FLAG, "active")
        meta.set(MAINTENANCE_STARTED_AT, started_at)
        meta.set(MAINTENANCE_OWNER, owner)
        meta.set(MAINTENANCE_REASON, reason)
        # The token is a lease identity; no caller may clear it without matching it.
        meta.set(MAINTENANCE_LEASE_UNTIL, "session")
        _LOCAL_MAINTENANCE_LEASES[_lease_key(store)] = owner
        return {"active": True, "started_at": started_at, "reason": reason, "owner": owner}


def end_maintenance(
    store: Any, *, owner: str | None = None, reason: str | None = None
) -> dict[str, Any]:
    """Release only the lease owned by this operation.

    The optional owner preserves the old API for the thread that acquired the
    lease; explicit owners are required across worker/operation boundaries.
    """
    meta = platform_for(store).meta
    with _MAINTENANCE_LOCK:
        if meta.get(MAINTENANCE_FLAG) != "active":
            return {"active": False, "started_at": None, "ended_at": _utc_now_iso()}
        current_owner = meta.get(MAINTENANCE_OWNER)
        expected_owner = owner or _LOCAL_MAINTENANCE_LEASES.get(_lease_key(store))
        current_reason = meta.get(MAINTENANCE_REASON) or "backup"
        if expected_owner != current_owner or (reason is not None and reason != current_reason):
            raise MaintenanceConflict("maintenance barrier cannot be cleared by a different operation")
        started_at = meta.get(MAINTENANCE_STARTED_AT)
        for key in (
            MAINTENANCE_FLAG, MAINTENANCE_STARTED_AT, MAINTENANCE_OWNER,
            MAINTENANCE_REASON, MAINTENANCE_LEASE_UNTIL,
        ):
            meta.delete(key)
        _LOCAL_MAINTENANCE_LEASES.pop(_lease_key(store), None)
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


#: payload 中直接承载 artifact id 的字符串键。
_ARTIFACT_STRING_KEYS = frozenset({"artifact_id", "artifact", "raw_ref", "raw_bundle_artifact"})


def _collect_artifact_refs(value: Any, refs: dict[str, str | None]) -> None:
    """递归收集 payload 里的 artifact 引用（artifact id → 嵌入的 sha256 或 None）。

    payload 形状随采集后端/里程碑演进变化，这里按**约定启发式宽口径**收集：

    - ``artifact_id`` / ``artifact`` / ``raw_ref`` / ``raw_bundle_artifact`` 的字符串值；
    - 名字含 "artifact" 的列表：字符串项、以及 dict 项的 ``id``（覆盖
      cli_runtime 的 ``artifact_refs: [{id, kind, uri, sha256}]`），嵌入 sha256
      一并记录用于备份期校验；
    - ``kind == "artifact"`` 的证据引用 dict 的 ``locator``（cli_runtime 的
      EvidenceRef 冻结 stdout/stderr 的写法）。

    宽口径的取舍：多扫的代价是备份多带一个文件（或对悬空引用 fail-closed 报
    incomplete），漏扫的代价是备份悄悄缺证据——按协议 §9.1 选 fail-closed。
    """
    if isinstance(value, dict):
        kind = value.get("kind")
        for key, item in value.items():
            if isinstance(item, str) and item and (
                key in _ARTIFACT_STRING_KEYS or (key == "locator" and kind == "artifact")
            ):
                refs.setdefault(item, None)
            elif "artifact" in key.lower() and isinstance(item, list):
                for entry in item:
                    if isinstance(entry, str) and entry:
                        refs.setdefault(entry, None)
                    elif isinstance(entry, dict):
                        identifier = entry.get("id")
                        if isinstance(identifier, str) and identifier:
                            embedded = entry.get("sha256")
                            refs.setdefault(
                                identifier, embedded if isinstance(embedded, str) else None
                            )
                        _collect_artifact_refs(entry, refs)
            else:
                _collect_artifact_refs(item, refs)
    elif isinstance(value, list):
        for entry in value:
            _collect_artifact_refs(entry, refs)


def _referenced_artifact_hashes(store: Any) -> dict[str, str | None]:
    """从 store 各仓库的 payload 收集被引用的 artifact id 及嵌入哈希。

    覆盖协议 §9.1 的清单：runs payload、case_runs、agent_invocations、
    external_jobs（job + records）、baselines、import 账本（best-effort：平台
    账本不可用时跳过，不让它单独阻断备份）。external_job_conflicts 是审计侧
    记录、其 incoming payload 未必对应已落盘文件，不纳入引用面。
    """
    refs: dict[str, str | None] = {}
    for run in store.runs.list():
        run_id = run.get("id")
        _collect_artifact_refs(run, refs)
        for repo_name in (
            "case_runs", "attempts", "invocations", "scoring_passes",
            "commands", "trials", "runtime_sessions", "scoring_jobs",
        ):
            repo = getattr(store, repo_name, None)
            if repo is None or not hasattr(repo, "list_for_run"):
                continue
            for record in repo.list_for_run(run_id):
                _collect_artifact_refs(record, refs)
                if repo_name == "scoring_passes":
                    score_sets = getattr(store, "score_sets", None)
                    if score_sets is not None and hasattr(score_sets, "list_for_pass"):
                        for score in score_sets.list_for_pass(record.get("id", "")):
                            _collect_artifact_refs(score, refs)
        external_jobs = getattr(store, "external_jobs", None)
        if external_jobs is not None:
            for job in external_jobs.jobs_for_run(run_id):
                _collect_artifact_refs(job, refs)
                for record in external_jobs.list_records(job.get("job_id", "")):
                    _collect_artifact_refs(record, refs)
        baselines = getattr(store, "baselines", None)
        if baselines is not None and hasattr(baselines, "get_for_run"):
            for baseline in baselines.get_for_run(run_id):
                _collect_artifact_refs(baseline, refs)
    baseline_store = getattr(store, "baseline_store", None)
    if baseline_store is not None:
        for baseline in baseline_store.list(limit=_ALL_LIMIT):
            _collect_artifact_refs(baseline, refs)
    gate_store = getattr(store, "gate_store", None)
    if gate_store is not None and hasattr(gate_store, "list_results"):
        for result in gate_store.list_results(limit=_ALL_LIMIT):
            _collect_artifact_refs(result, refs)
    try:
        ledger = platform_for(store).imports
        for record in ledger.list_imports():
            _collect_artifact_refs(record, refs)
            for mapping in ledger.mappings_for(record.get("import_id", "")):
                _collect_artifact_refs(mapping, refs)
    except Exception:  # noqa: BLE001 - 导入账本缺失不阻断备份
        pass
    return refs


def _referenced_artifacts(store: Any) -> set[str]:
    return set(_referenced_artifact_hashes(store))


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
        if expected is not None and expected != digest:
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
    begin = begin_maintenance(store, reason=reason)
    try:
        _snapshot_sqlite(Path(db_path), snapshot)
        # Read references and counts from the immutable DB snapshot, not the live
        # store.  This prevents an in-flight worker from producing a mixed
        # database/artifact manifest after the online backup point-in-time.
        from .run_store import SQLiteRunStore

        snapshot_store = SQLiteRunStore(snapshot)
        refs = _referenced_artifact_hashes(snapshot_store)
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
        end_maintenance(store, owner=begin["owner"])
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
    if store is None:
        from .factory import create_run_store

        store = create_run_store(dsn=dsn, storage="postgres")
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    stamp = _backup_stamp(datetime.now(UTC))
    dump_path = target / f"runs-{stamp}.dump"
    begin = begin_maintenance(store, reason=reason)
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
        refs = _referenced_artifact_hashes(store)
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
        end_maintenance(store, owner=begin["owner"])
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
    referenced = _referenced_artifacts(staging_store)
    missing_refs: list[str] = []
    for artifact_id in sorted(referenced):
        try:
            restored_file = _artifact_path(artifacts_root, artifact_id)
        except ValueError as error:
            raise RestoreIncomplete(str(error)) from error
        if not restored_file.is_file():
            missing_refs.append(artifact_id)
    if missing_refs:
        raise RestoreIncomplete(
            "referenced artifacts missing from staging restore",
            details={"missing": missing_refs},
        )

    # 恢复守卫：Worker 的 _platform_block_reason 读到该键即拒绝领取，
    # 需 clear_restore_guard(confirm=True) 显式解除。
    #
    # 快照不可避免地冻结了备份窗口的屏障标志（备份时屏障必须激活）；屏障在备份
    # 结束时已解除（manifest.maintenance.ended_at），staging 不能继承"维护中"
    # 状态，否则恢复后永远 503。staging 的写保护由下面的恢复守卫承担。
    meta = platform_for(staging_store).meta
    meta.delete(MAINTENANCE_FLAG)
    meta.delete(MAINTENANCE_STARTED_AT)
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
    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(snapshot, db_path)
    try:
        # v2 一致快照冻结了备份窗口的维护标志；恢复后的库不应停留在维护模式
        # （v1 快照没有平台表，跳过即可）。
        from .run_store import SQLiteRunStore

        meta = platform_for(SQLiteRunStore(db_path)).meta
        meta.delete(MAINTENANCE_FLAG)
        meta.delete(MAINTENANCE_STARTED_AT)
    except Exception:  # noqa: BLE001 - 旧备份无平台表
        pass
    restored: dict[str, Any] = {"database": str(db_path), "backup": manifests[-1].name}
    if artifacts_target is not None:
        # v1 manifest 记 "snapshot"，v2 记 "dir"；两者都是完整 artifact 树
        source = backup_dir / (
            artifacts_section.get("snapshot") or artifacts_section.get("dir")
        )
        if artifacts_target.exists():
            shutil.rmtree(artifacts_target)
        shutil.copytree(source, artifacts_target)
        restored["artifacts"] = str(artifacts_target)
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
