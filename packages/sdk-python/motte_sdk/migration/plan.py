"""Dry-run 导入计划（M7-T06；协议 §5.2，验收 A08）。

只读来源 + 目标元数据（``store.runs`` 与导入账本；``resource_store`` 参数
被显式接受但**不读取**——协议 §5.3 禁止用当前 ModelProfile/PriceTable/
ScoringPass 补写历史，参数仅为调用方保持 API 对等），输出 planned/reused/
conflicted/rejected、unknown fields、missing artifacts、凭据 rebind 清单
（只列引用名）与计数。

对目标的唯一写入是导入账本的批次行（``begin_import``，status=planned）：
这是平台审计表，不是业务数据。业务表（runs/case_runs/scores/scoring_
passes/…）与 Artifact、credential、执行状态零修改；测试以业务表快照断言。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from motte_contracts.imports import ImportReport, ImportVerification
from motte_storage.platform import platform_for

from .sources import LoadedSource, RejectedSourceRecord, SourceRecord, utc_now

#: 来源 run 的进行中状态：永不迁移（协议 §5.3 ``in_flight_job_not_importable``）。
IN_FLIGHT_RUN_STATUSES = frozenset({
    "queued", "pending", "running", "preparing", "collecting", "scoring",
})

#: 可导入的旧系统终态（协议 §5.3：completed/failed/cancelled，永不 queued）。
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})

#: 本迁移 transform 认识的字段；其余进 unknown_fields（协议 §5.2 不静默丢弃，
#: 原始字段整体保留在 run manifest.legacy 下，见 apply.py）。
KNOWN_RECORD_FIELDS: dict[str, frozenset[str]] = {
    "provider_connection": frozenset({
        "id", "name", "provider", "api_key_env", "credentials_profile",
        "base_url", "created_at", "updated_at",
    }),
    "model_profile": frozenset({
        "id", "name", "provider", "model", "license", "params",
        "created_at", "updated_at",
    }),
    "dataset": frozenset({
        "id", "name", "version", "format", "license", "case_count", "source",
        "created_at", "updated_at",
    }),
    "case": frozenset({
        "id", "dataset_id", "input", "expected", "meta", "weight",
        "created_at", "updated_at",
    }),
    "scenario": frozenset({
        "id", "name", "dataset_id", "license", "config", "created_at", "updated_at",
    }),
    "run": frozenset({
        "id", "status", "created_at", "updated_at", "started_at", "completed_at",
        "reported_model", "model_profile_id", "provider_connection_id",
        "scenario_id", "dataset_id", "case_ids", "artifacts", "parameters",
        "tags", "summary",
    }),
    "run_summary": frozenset({
        "id", "run_id", "score", "metric", "aggregation", "per_case_scores_available",
        "created_at",
    }),
    "score": frozenset({
        "id", "run_id", "case_id", "value", "passed", "metric", "scorer", "created_at",
    }),
    "artifact": frozenset({
        "id", "sha256", "kind", "media_type", "filename", "bytes", "run_id", "created_at",
    }),
    "baseline": frozenset({
        "id", "run_id", "name", "metrics", "note", "created_at",
    }),
}

#: 凭据引用字段：只迁引用名，永不迁值（协议 §5.2）。
CREDENTIAL_REF_KEYS = frozenset({"api_key_env", "credentials_profile"})

_PLAN_NOTES = (
    "dry-run: no target business data, artifact, credential or state was modified",
    "ledger begin_import(status=planned) is an audit-only platform-table write",
    "resource_store was not read; history is never filled from current profiles (protocol 5.3)",
)


@dataclass
class PlannedUnit:
    """一条来源单元的计划决策（apply 复用同一决策逻辑执行）。"""

    record: SourceRecord | None = None
    load_reject: RejectedSourceRecord | None = None
    decision: str = "create"  # create | reused | conflicted | rejected
    diagnostic: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def unit_ref(self) -> str:
        if self.record is not None:
            return self.record.unit_ref()
        assert self.load_reject is not None
        return self.load_reject.unit_ref()


def imported_run_id(source_id: str) -> str:
    """导入 Run 的目标 id：来源 id 加 ``imp-`` 前缀并清洗。"""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", source_id).strip("-.")
    return "imp-" + (cleaned or "unknown")


def unit_decisions(store: Any, source: LoadedSource) -> list[PlannedUnit]:
    """逐单元决策：账本映射与目标 Run 的幂等/冲突检测（全部只读）。"""
    ledger = platform_for(store).imports
    existing_runs: dict[str, dict[str, Any]] = {}
    for run in store.runs.list():
        existing_runs[run.get("id", "")] = run

    units: list[PlannedUnit] = []
    for rejected in source.rejected:
        units.append(PlannedUnit(
            load_reject=rejected, decision="rejected", diagnostic=rejected.diagnostic,
        ))
    for record in source.records:
        unit = PlannedUnit(record=record)
        if record.record_type == "run":
            status = str(record.data.get("status") or "")
            if status in IN_FLIGHT_RUN_STATUSES:
                unit.decision = "rejected"
                unit.diagnostic = f"in_flight_job_not_importable:{record.unit_ref()}"
            elif status not in TERMINAL_RUN_STATUSES:
                unit.decision = "rejected"
                unit.diagnostic = f"unsupported_run_status:{record.unit_ref()}:{status}"
        if unit.decision == "create":
            mapping = ledger.get_mapping(record.mapping_key)
            if mapping is not None:
                committed_hash = mapping.get("source", {}).get("content_hash", "")
                if committed_hash == record.content_hash:
                    unit.decision = "reused"
                else:
                    unit.decision = "conflicted"
                    unit.diagnostic = f"mapping_content_conflict:{record.unit_ref()}"
            elif record.record_type == "run":
                # 账本行丢失但目标 Run 已在（崩溃恢复窗口）：按 Run 内容判定。
                run = existing_runs.get(imported_run_id(record.source_id))
                if run is not None:
                    stored = (run.get("manifest") or {}).get("import_source") or {}
                    if stored.get("content_hash") == record.content_hash:
                        unit.decision = "reused"
                    else:
                        unit.decision = "conflicted"
                        unit.diagnostic = f"run_id_content_conflict:{record.unit_ref()}"
        units.append(unit)
    return units


def _missing_artifacts(source: LoadedSource) -> list[str]:
    declared = source.declared_artifact_shas()
    return [
        f"{unit_ref}:{sha}"
        for sha, unit_refs in sorted(declared.items())
        if sha not in source.artifact_files
        for unit_ref in unit_refs
    ]


def _unknown_fields(source: LoadedSource) -> list[str]:
    unknown: list[str] = []
    for record in source.records:
        known = KNOWN_RECORD_FIELDS.get(record.record_type)
        if known is None:
            continue  # 未知 record 类型整体按引用导入，字段不判 unknown
        for key in sorted(record.data):
            if key not in known:
                unknown.append(f"{record.unit_ref()}#{key}")
    return unknown


def _credentials_to_rebind(source: LoadedSource) -> list[str]:
    names: set[str] = set()
    for record in source.records_of("provider_connection"):
        for key, value in record.data.items():
            if key in CREDENTIAL_REF_KEYS and isinstance(value, str) and value:
                names.add(value)
    return sorted(names)


def plan_import(
    store: Any, source: LoadedSource, *, operator: str, resource_store: Any = None,
) -> ImportReport:
    """dry-run：零业务写入，返回只读 ImportReport（协议 §5.2）。"""
    # resource_store 刻意不读：见模块 docstring（协议 §5.3 禁止补写历史）。
    ledger = platform_for(store).imports
    ledger.begin_import({
        "import_id": source.manifest.import_id,
        "manifest_sha256": source.manifest.manifest_hash(),
        "status": "planned",
        "operator": operator,
        "planned_at": utc_now(),
        "source_system": source.manifest.source_system,
        "content_sha256": source.content_sha256,
    })
    units = unit_decisions(store, source)
    reused = sum(1 for unit in units if unit.decision == "reused")
    rejected = sum(1 for unit in units if unit.decision == "rejected")
    conflicted = sum(1 for unit in units if unit.decision == "conflicted")
    planned = len(units)
    pending = planned - reused - rejected - conflicted
    warnings = list(source.warnings)
    warnings.extend(f"license restricted: {ref}" for ref in sorted(source.restricted))
    return ImportReport(
        import_id=source.manifest.import_id,
        source_system=source.manifest.source_system,
        content_sha256=source.content_sha256,
        planned=planned,
        created=0,
        reused=reused,
        rejected=rejected,
        conflicted=conflicted,
        pending=pending,
        missing_artifacts=tuple(_missing_artifacts(source)),
        unknown_fields=tuple(_unknown_fields(source)),
        warnings=tuple(warnings),
        credentials_to_rebind=tuple(_credentials_to_rebind(source)),
        diagnostics=tuple(unit.diagnostic for unit in units if unit.diagnostic),
        checkpoint=reused,
        verification=ImportVerification(
            counts_match=planned == pending + reused + rejected + conflicted,
        ),
        notes=_PLAN_NOTES,
    )
