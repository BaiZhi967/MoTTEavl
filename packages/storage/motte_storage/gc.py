"""M7 retention/GC：引用与 pin 保护、dry-run 计划、tombstone 审计（协议 §9.3）。

原则（frozen@1）：
- 默认 dry-run；apply 必须显式确认，且必须持有维护屏障（与采集/评分/备份互斥）；
- 删除只针对 Artifact 文件，按保留类与 TTL 决定；被任何 run/case/invocation/
  external job/baseline/导入账本引用的文件永不删除（A18）；
- needs_review、imported（manifest.import_source）、baseline 引用的 run 的全部
  证据视为 pinned；
- 每次删除写 tombstone（gc_run_id、artifact_id、sha256、bytes、原因、时间）到
  motte_gc_tombstones；
- trace 事件没有可靠时间戳，M7 GC v1 不做 DB 行裁剪；plan 里如实输出
  trace_retention 报告（受保护 run 清单），不假装可裁剪。
"""
from __future__ import annotations

import hashlib
import os
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .artifact_refs import (
    collect_artifact_refs, iter_artifact_records, referenced_artifact_hashes, resolve_artifact_refs,
)
from .artifacts import ArtifactMutationUnsupported, ArtifactStore
from .deletion_audit import audit_missing_artifact, delete_with_audit
from .maintenance import begin_maintenance, end_maintenance
from .platform import platform_for
from .trace_archives import is_trace_archive_id

PINNED_RUN_STATUSES = ("needs_review",)
IMPORTED_PREFIX = "imports/"


@dataclass
class GCPlan:
    gc_run_id: str
    dry_run: bool = True
    deletable: list[dict[str, Any]] = field(default_factory=list)
    protected: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    trace_retention: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "gc_run_id": self.gc_run_id,
            "dry_run": self.dry_run,
            "deletable_count": len(self.deletable),
            "deletable_bytes": sum(item["bytes"] for item in self.deletable),
            "protected_count": len(self.protected),
            "warnings": self.warnings,
            "trace_retention": self.trace_retention,
        }


def _collect_artifact_ids(value: Any, found: set[str]) -> None:
    """Compatibility wrapper over the shared evidence decoder."""
    refs: dict[str, str | None] = {}
    collect_artifact_refs(value, refs)
    found.update(refs)


def _walk_store(store: Any):
    yield from iter_artifact_records(store)


def _pinned_run_ids(store: Any) -> set[str]:
    """needs_review 与 imported run 的 id（其证据全部视为 pinned）。"""
    pinned: set[str] = set()
    runs = getattr(store, "runs", None)
    if runs is None or not hasattr(runs, "list"):
        return pinned
    for run in runs.list():
        if run.get("status") in PINNED_RUN_STATUSES:
            pinned.add(run["id"])
            continue
        manifest = run.get("manifest") or {}
        if isinstance(manifest, dict) and manifest.get("import_source"):
            pinned.add(run["id"])
    # baseline 条目引用的 run 同样 pin
    baseline_store = getattr(store, "baseline_store", None)
    if baseline_store is not None and hasattr(baseline_store, "list"):
        for baseline in baseline_store.list(limit=10000):
            for entry in baseline.get("entries", []):
                ref = entry.get("ref") or {}
                if isinstance(ref, dict) and ref.get("run_id"):
                    pinned.add(ref["run_id"])
    return pinned


def plan_gc(
    store: Any,
    artifacts_root: str | Path,
    *,
    artifact_ttl_days: float = 90,
    import_audit_protected: bool = True,
    now: datetime | None = None,
) -> GCPlan:
    """只读计算删除计划；不修改任何状态（默认 dry-run 的事实来源）。"""
    plan = GCPlan(gc_run_id="gc-" + uuid4().hex[:16])
    root = Path(artifacts_root)
    referenced_hashes: set[str] = set()
    referenced = resolve_artifact_refs(
        referenced_artifact_hashes(store, hashes=referenced_hashes), root,
    )
    pinned_runs = _pinned_run_ids(store)
    plan.trace_retention = {
        "db_row_pruning": "not_implemented_no_event_timestamps",
        "pinned_run_count": len(pinned_runs),
        "pinned_run_ids": sorted(pinned_runs),
    }
    if not root.exists():
        plan.warnings.append(f"artifact root missing: {root}")
        return plan
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=artifact_ttl_days)
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        stat = path.stat()
        entry = {
            "artifact_id": rel, "bytes": stat.st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mtime": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
        }
        if is_trace_archive_id(rel):
            entry["reason"] = "trace_archive"
            plan.protected.append(entry)
            continue
        if rel in referenced or entry["sha256"] in referenced_hashes:
            entry["reason"] = "referenced"
            plan.protected.append(entry)
            continue
        if import_audit_protected and rel.startswith(IMPORTED_PREFIX):
            entry["reason"] = "import_audit"
            plan.protected.append(entry)
            continue
        if datetime.fromtimestamp(stat.st_mtime, UTC) >= cutoff:
            entry["reason"] = "younger_than_ttl"
            plan.protected.append(entry)
            continue
        entry["reason"] = "artifact_ttl_expired"
        plan.deletable.append(entry)
    return plan


def apply_gc(
    store: Any,
    artifacts_root: str | Path,
    plan: GCPlan,
    *,
    confirm: bool = False,
) -> dict[str, Any]:
    """执行计划：必须 confirm，且自动持有维护屏障；每次删除写 tombstone。

    apply 前重算引用集：计划之后新增的引用会使对应条目被跳过并记录为
    protected_after_recheck，绝不删除仍被引用的文件。
    """
    if not confirm:
        raise ValueError("gc apply requires confirm=True (dry-run is the default)")
    if plan.dry_run is False and not plan.deletable:
        pass  # 空计划也允许执行（幂等）
    root = Path(artifacts_root).resolve()
    platform_stores = platform_for(store)
    # 与采集/评分/备份互斥：以带 owner 的维护租约保护整个 apply。
    lease = begin_maintenance(
        store, reason="gc", artifacts_root=root, allow_tombstone_writes=True,
    )
    deleted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    try:
        # Same decoder and repository traversal as planning and backup.
        referenced_hashes: set[str] = set()
        referenced = resolve_artifact_refs(
            referenced_artifact_hashes(store, hashes=referenced_hashes), root,
        )
        artifacts = ArtifactStore(root) if root.is_dir() else None
        for entry in plan.deletable:
            rel = entry["artifact_id"]
            if is_trace_archive_id(rel):
                skipped.append({**entry, "reason": "trace_archive"})
                continue
            if rel in referenced or entry["sha256"] in referenced_hashes:
                skipped.append({**entry, "reason": "protected_after_recheck"})
                continue
            target = root / rel
            try:
                resolved = target.resolve()
                resolved.relative_to(root.resolve())
            except (ValueError, OSError):
                skipped.append({**entry, "reason": "path_escape_blocked"})
                continue
            if is_trace_archive_id(resolved.relative_to(root).as_posix()):
                skipped.append({**entry, "reason": "trace_archive"})
                continue
            audit_entry = {**entry, "gc_run_id": plan.gc_run_id}
            with ExitStack() as target_handles:
                try:
                    if artifacts is None:
                        raise FileNotFoundError("artifact root is absent")
                    target = target_handles.enter_context(
                        artifacts._pinned_mutation_target(rel, resolve_aliases=False, read=True)
                    )
                except FileNotFoundError:
                    reason = audit_missing_artifact(platform_stores.tombstones, audit_entry)
                    skipped.append({**entry, "reason": reason})
                    continue
                except ArtifactMutationUnsupported:
                    raise  # Unsupported is an operation blocker, not a path-escape skip.
                except (OSError, ValueError):
                    skipped.append({**entry, "reason": "path_escape_blocked"})
                    continue
                current_bytes = target.read_bytes()
                current_sha256 = hashlib.sha256(current_bytes).hexdigest()
                if current_sha256 in referenced_hashes:
                    skipped.append({**entry, "reason": "protected_after_recheck"})
                    continue
                if current_sha256 != entry["sha256"] or len(current_bytes) != entry["bytes"]:
                    skipped.append({**entry, "reason": "changed_since_plan"})
                    continue
                deleted.append(delete_with_audit(
                    platform_stores.tombstones, audit_entry, delete=target.unlink, exists=target.exists,
                ))
    finally:
        end_maintenance(store, owner=lease["owner"])
    return {
        "gc_run_id": plan.gc_run_id,
        "deleted": len(deleted),
        "deleted_bytes": sum(item["bytes"] for item in deleted),
        "skipped": skipped,
        "tombstones": len(platform_stores.tombstones.list(limit=10000)),
    }
