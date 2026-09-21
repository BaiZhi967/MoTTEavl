"""受限回退（M7-T08；协议 §5.4，验收 A13）。

只撤销本 import_id 创建且未被**任何**其他资源（其他 run / 其他导入批次的
映射）引用的对象；共享对象 → 阻断并输出诊断，批次继续标记其余对象。

- 需要显式 ``confirm=True``，否则抛 RollbackNotConfirmed（回退是破坏性意图）。
- 删除范围的前置断言（绝不碰既有证据）：
  - artifact 目标 id 必须形如 ``imports/<import_id>/…``；
  - run 目标 id 必须以 ``imp-`` 前缀开头，且其 manifest.import_source.import_id
    必须等于本批次；任一不满足 → 该对象 blocked，绝不触碰。
- 存储层事实约束（platform.py 冻结、RunStore 无行删除 API，且本管线不允许
  绕过 store API 写裸 DELETE）：run 行**不做物理删除**，按协议 §5.4 的替代
  语义"整批标记 inactive（保留审计）"执行——run 以 CAS transition 打上
  ``rolled_back_import`` 注记，终态保持不变（绝不回到 queued）。artifact
  文件通过 ArtifactStore.delete 物理删除并写 GC tombstone 审计行；账本
  mapping 行一律保留不删（tombstone 即回退的审计记录）。
"""
from __future__ import annotations

from typing import Any

from motte_storage.artifacts import ArtifactStore
from motte_storage.platform import platform_for

from .sources import utc_now

#: 回退操作在 GC tombstone 表里的 gc_run_id 前缀（区别于常规 GC）。
ROLLBACK_TOMBSTONE_PREFIX = "import-rollback:"


class RollbackNotConfirmed(ValueError):
    """回退必须显式确认（协议 §5.4 operator intent）。"""

    code = "ROLLBACK_NOT_CONFIRMED"


def _run_artifact_keys(run: dict[str, Any]) -> set[str]:
    """收集一个 run payload 里引用的 artifact id 与内容 sha256。"""
    keys: set[str] = set()
    for ref in run.get("artifact_refs") or []:
        if isinstance(ref, dict):
            if isinstance(ref.get("artifact_id"), str):
                keys.add(ref["artifact_id"])
            if isinstance(ref.get("sha256"), str):
                keys.add(ref["sha256"])
    legacy = (run.get("manifest") or {}).get("legacy") or {}
    for entry in legacy.get("artifacts") or []:
        if isinstance(entry, dict) and isinstance(entry.get("sha256"), str):
            keys.add(entry["sha256"])
    return keys


def rollback_import(
    store: Any, artifacts_root: str, import_id: str, *, operator: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """回退一个导入批次；返回只读结果（blocked 项保留诊断、不删除）。"""
    if not confirm:
        raise RollbackNotConfirmed(
            "rollback is destructive: re-run with confirm=True after reviewing the batch"
        )
    platform = platform_for(store)
    ledger = platform.imports
    batch = ledger.get_import(import_id)
    if batch is None:
        raise KeyError(f"import not found: {import_id}")

    mappings = ledger.mappings_for(import_id)
    artifact_store = ArtifactStore(artifacts_root)
    artifact_prefix = f"imports/{import_id}/"

    # 其他批次的映射：同目标 id 或同内容 sha 都算"跨批引用"。
    other_mappings: list[dict[str, Any]] = []
    for other in ledger.list_imports():
        if other.get("import_id") != import_id:
            other_mappings.extend(ledger.mappings_for(other["import_id"]))

    blocked: list[dict[str, str]] = []
    deleted_artifacts: list[str] = []
    deactivated_runs: list[str] = []
    inactive_references = 0
    tombstones: list[dict[str, Any]] = []

    for mapping in mappings:
        target_type = mapping.get("target_type", "")
        target_id = mapping.get("target_id", "")
        if target_type == "artifact":
            if not target_id.startswith(artifact_prefix):
                blocked.append({"object": target_id, "reason": "foreign_object"})
                continue
            sha = mapping.get("artifact_sha256", "")
            referenced = False
            for run in store.runs.list():
                own = (run.get("manifest") or {}).get("import_source", {})
                if own.get("import_id") == import_id:
                    # 本批次自己的 run 正在同一批次里停用，不构成外部引用。
                    continue
                keys = _run_artifact_keys(run)
                if target_id in keys or sha in keys:
                    referenced = True
                    break
            if not referenced:
                referenced = any(
                    other.get("target_id") == target_id
                    or other.get("artifact_sha256") == sha
                    for other in other_mappings
                )
            if referenced:
                blocked.append({"object": target_id, "reason": "artifact_in_use"})
                continue
            artifact_store.delete(target_id)
            deleted_artifacts.append(target_id)
            tombstones.append({
                "gc_run_id": ROLLBACK_TOMBSTONE_PREFIX + import_id,
                "artifact_id": target_id,
                "sha256": sha,
                "reason": "import_rollback",
                "import_id": import_id,
                "operator": operator,
                "deleted_at": utc_now(),
            })
        elif target_type == "run":
            if not target_id.startswith("imp-"):
                blocked.append({"object": target_id, "reason": "foreign_object"})
                continue
            run = store.runs.get(target_id)
            if run is None:
                blocked.append({"object": target_id, "reason": "run_missing"})
                continue
            source = (run.get("manifest") or {}).get("import_source") or {}
            if source.get("import_id") != import_id:
                blocked.append({"object": target_id, "reason": "foreign_object"})
                continue
            if run.get("rolled_back_import"):
                deactivated_runs.append(target_id)  # 幂等重放
                continue
            store.runs.transition(
                target_id,
                expected_revision=run["revision"],
                expected_status=run["status"],
                status=run["status"],  # 终态保持：回退绝不把历史变回 queued
                changes={"rolled_back_import": {
                    "import_id": import_id, "operator": operator,
                    "rolled_back_at": utc_now(),
                }},
            )
            deactivated_runs.append(target_id)
        else:
            # 引用类映射（provider/dataset/case/...）：只保留账本审计，无实体可删。
            inactive_references += 1

    platform.tombstones.append(tombstones)
    ledger.set_status(import_id, "rolled_back")
    return {
        "import_id": import_id,
        "status": "rolled_back",
        "operator": operator,
        "manifest_sha256": batch.get("manifest_sha256", ""),
        "deactivated_runs": deactivated_runs,
        "deleted_artifacts": deleted_artifacts,
        "blocked": blocked,
        "inactive_reference_mappings": inactive_references,
        "tombstoned": len(tombstones),
        "kept_mapping_rows": len(mappings),
    }
