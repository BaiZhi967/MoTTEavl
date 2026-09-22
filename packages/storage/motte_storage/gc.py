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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .maintenance import begin_maintenance, end_maintenance
from .platform import platform_for

ARTIFACT_KEYS = ("artifact_id", "artifact_ids", "artifacts")
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
    """递归收集载荷里引用的 artifact id（键名约定 + EvidencePin 形状）。"""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ARTIFACT_KEYS:
                if isinstance(child, str) and child:
                    found.add(child)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, str) and item:
                            found.add(item)
                        elif isinstance(item, dict) and isinstance(item.get("artifact_id"), str):
                            found.add(item["artifact_id"])
            elif key == "artifact_id" and isinstance(child, str):
                found.add(child)
            elif "artifact" in key.lower() and isinstance(child, list):
                for item in child:
                    if isinstance(item, str) and item:
                        found.add(item)
                    elif isinstance(item, dict):
                        identifier = item.get("id") or item.get("artifact_id")
                        if isinstance(identifier, str) and identifier:
                            found.add(identifier)
            _collect_artifact_ids(child, found)
    elif isinstance(value, list):
        for item in value:
            _collect_artifact_ids(item, found)


def _walk_store(store: Any):
    """遍历所有可能承载 artifact 引用的仓储。

    Child repositories intentionally use their run-scoped list APIs.  Calling a
    repository with the wrong shape is an implementation error and must propagate
    instead of silently dropping retention references.
    """
    runs_repo = getattr(store, "runs", None)
    if runs_repo is None or not hasattr(runs_repo, "list"):
        raise AttributeError("store.runs.list is required for artifact GC")
    runs = runs_repo.list()
    for run in runs:
        yield run
        run_id = run.get("id") if isinstance(run, dict) else None
        if not run_id:
            continue
        for repo_name in (
            "case_runs", "attempts", "invocations", "scoring_passes",
            "commands", "trials", "runtime_sessions", "scoring_jobs",
        ):
            repo = getattr(store, repo_name, None)
            if repo is None:
                continue
            list_for_run = getattr(repo, "list_for_run", None)
            if list_for_run is None:
                continue
            child_records = list_for_run(run_id)
            for record in child_records:
                yield record
                if repo_name == "scoring_passes" and isinstance(record, dict):
                    score_sets = getattr(store, "score_sets", None)
                    if score_sets is not None and hasattr(score_sets, "list_for_pass"):
                        for score in score_sets.list_for_pass(record.get("id", "")):
                            yield score
        external_jobs = getattr(store, "external_jobs", None)
        if external_jobs is not None and hasattr(external_jobs, "jobs_for_run"):
            for job in external_jobs.jobs_for_run(run_id):
                yield job
                for record in external_jobs.list_records(job.get("job_id", "")):
                    yield record
        baselines = getattr(store, "baselines", None)
        if baselines is not None and hasattr(baselines, "get_for_run"):
            for baseline in baselines.get_for_run(run_id):
                yield baseline

    baseline_store = getattr(store, "baseline_store", None)
    if baseline_store is not None and hasattr(baseline_store, "list"):
        for record in baseline_store.list(limit=1_000_000):
            yield record
    gate_store = getattr(store, "gate_store", None)
    if gate_store is not None and hasattr(gate_store, "list_results"):
        for record in gate_store.list_results(limit=1_000_000):
            yield record


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
    referenced: set[str] = set()
    for record in _walk_store(store):
        _collect_artifact_ids(record, referenced)
    pinned_runs = _pinned_run_ids(store)
    # pinned run 的载荷整体再扫一遍（含 case/invocation 的引用已在 referenced）。
    runs = getattr(store, "runs", None)
    if runs is not None and hasattr(runs, "list"):
        for run in runs.list():
            if run["id"] in pinned_runs:
                _collect_artifact_ids(run, referenced)
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
        if rel in referenced:
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
    lease = begin_maintenance(store, reason="gc")
    deleted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    try:
        # 重算引用保护
        referenced: set[str] = set()
        for record in _walk_store(store):
            _collect_artifact_ids(record, referenced)
        pinned_runs = _pinned_run_ids(store)
        runs = getattr(store, "runs", None)
        if runs is not None and hasattr(runs, "list"):
            for run in runs.list():
                if run["id"] in pinned_runs:
                    _collect_artifact_ids(run, referenced)
        now_iso = datetime.now(UTC).isoformat()
        for entry in plan.deletable:
            rel = entry["artifact_id"]
            if rel in referenced:
                skipped.append({**entry, "reason": "protected_after_recheck"})
                continue
            target = root / rel
            try:
                resolved = target.resolve()
                resolved.relative_to(root.resolve())
            except (ValueError, OSError):
                skipped.append({**entry, "reason": "path_escape_blocked"})
                continue
            if not target.is_file():
                skipped.append({**entry, "reason": "missing"})
                continue
            target.unlink()
            deleted.append({
                "gc_run_id": plan.gc_run_id, "artifact_id": rel,
                "sha256": entry["sha256"], "bytes": entry["bytes"],
                "reason": entry["reason"], "deleted_at": now_iso,
            })
        if deleted:
            platform_stores.tombstones.append(deleted)
    finally:
        end_maintenance(store, owner=lease["owner"])
    return {
        "gc_run_id": plan.gc_run_id,
        "deleted": len(deleted),
        "deleted_bytes": sum(item["bytes"] for item in deleted),
        "skipped": skipped,
        "tombstones": len(platform_stores.tombstones.list(limit=10000)),
    }
