"""Checkpointed apply / resume（M7-T07/T08；协议 §5.3/§5.4，验收 A09/A10/A12）。

- apply 前复验：对来源包磁盘内容重算 content_sha256；与加载时或 dry-run
  计划记录的 hash 不一致 → SourceChangedError（旧计划作废，必须重新 dry-run）。
- 导入次序（协议 §5.3）：provider/model 引用 → dataset/case → scenario →
  artifact（先校验暂存再被 run 引用）→ run（+summary/baseline 注记、score
  逐题分）→ 其余引用单元。每条记录一个事务单元：先目标写入，后写
  ``motte_import_mappings`` 行（提交标记）。崩溃恢复按 mapping_key 跳过已
  提交单元；账本行丢失但目标对象已在的窗口由 store 级幂等检查兜底。
- 历史保真（A10）：in-flight run 拒绝；聚合 summary 只存 ``legacy_summary``
  绝不伪造逐题分；缺 reported_model/gold/pass/price 保持显式 "unknown"，
  永不从当前 ModelProfile/PriceTable 补写（本模块根本没有 resource 入口）。
- Artifact：声明 sha256 与暂存内容校验一致、put_bytes 落盘后读回复核一致，
  才提交引用；任何一步不一致 → 单元 rejected ``artifact_hash_mismatch``，
  不提交引用。旧 baseline 只注记在 run payload（``legacy_baseline``），
  不进 M6 baseline_store、不碰默认指针。
"""
from __future__ import annotations

import hashlib
import math
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from motte_contracts.imports import ImportReport, ImportVerification, MappingRecord
from motte_storage.artifacts import ArtifactStore
from motte_storage.platform import MappingConflict, platform_for

from .plan import (
    IN_FLIGHT_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    imported_run_id,
    plan_import,
    unit_decisions,
)
from .sources import LoadedSource, SourceRecord, package_content_sha256, utc_now

#: 协议 §5.3 的导入次序；未列出的类型作为纯引用单元垫后。
IMPORT_ORDER: tuple[str, ...] = (
    "provider_connection", "model_profile", "dataset", "case", "scenario",
    "artifact", "run", "run_summary", "score", "baseline",
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class SourceChangedError(RuntimeError):
    """apply 前复验发现来源包内容与计划记录的 hash 不一致。"""

    code = "SOURCE_CHANGED"


class _UnitRejected(Exception):
    """单元内受控拒绝（携带诊断码，不中断批次）。"""

    def __init__(self, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.diagnostic = diagnostic


class _UnitConflict(Exception):
    """单元内目标/账本冲突（同 key 异内容），停止该单元、批次继续。"""

    def __init__(self, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.diagnostic = diagnostic


class _ApplySession:
    def __init__(
        self, store: Any, artifacts_root: str | Path, source: LoadedSource, operator: str,
    ) -> None:
        self.store = store
        self.source = source
        self.operator = operator
        self.ledger = platform_for(store).imports
        self.artifact_store = ArtifactStore(artifacts_root)
        self.import_id = source.manifest.import_id
        self.transform_version = source.manifest.transform_version
        self.counts = {"created": 0, "reused": 0, "rejected": 0, "conflicted": 0, "pending": 0}
        self.planned = 0
        self.diagnostics: list[str] = []
        self.warnings: list[str] = []
        self.missing_artifacts: list[str] = []
        #: sha256 → 本次会话已提交的 artifact 目标 id。
        self.committed_artifacts: dict[str, str] = {}
        self.created_run_ids: list[str] = []
        #: 来源 run id → 目标 run id（annotation/score 单元寻址用）。
        self.run_ids_by_source: dict[str, str] = {}
        # run_linked 预索引：summary/baseline 在 run 创建时即烘焙进 payload，
        # 随后在各自单元阶段提交映射行（保持协议 §5.3 的账本次序）。
        self.summaries: dict[str, SourceRecord] = {}
        for record in source.records_of("run_summary"):
            run_ref = record.data.get("run_id")
            if isinstance(run_ref, str) and run_ref:
                self.summaries.setdefault(run_ref, record)
        self.baselines: dict[str, list[SourceRecord]] = {}
        for record in source.records_of("baseline"):
            run_ref = record.data.get("run_id")
            if isinstance(run_ref, str) and run_ref:
                self.baselines.setdefault(run_ref, []).append(record)
        self.scores_by_run: dict[str, list[SourceRecord]] = {}
        for record in source.records_of("score"):
            run_ref = record.data.get("run_id")
            if isinstance(run_ref, str) and run_ref:
                self.scores_by_run.setdefault(run_ref, []).append(record)
        self.score_rows_by_run: dict[str, list[dict[str, Any]]] = {}
        self.score_rejects: dict[str, str] = {}

    # ------------------------------------------------------------- commit
    def _commit(
        self, record: SourceRecord, target_type: str, target_id: str,
        **audit: Any,
    ) -> None:
        mapping = MappingRecord(
            mapping_key=record.mapping_key,
            import_id=self.import_id,
            source={
                "source_system": self.source.manifest.source_system,
                "source_record_type": record.record_type,
                "source_id": record.source_id,
                "source_version": record.source_version,
                "content_hash": record.content_hash,
            },
            target_type=target_type,
            target_id=target_id,
            status="created",
        ).model_dump(mode="json")
        mapping.update({"operator": self.operator, "committed_at": utc_now(), **audit})
        self.ledger.put_mapping(mapping)

    def _resolve_run(self, run_ref: Any) -> str:
        return self.run_ids_by_source.get(str(run_ref)) or imported_run_id(str(run_ref))

    # ------------------------------------------------------------- units
    def apply_unit(self, record: SourceRecord) -> str:
        try:
            if record.record_type == "artifact":
                outcome = self._apply_artifact(record)
            elif record.record_type == "run":
                outcome = self._apply_run(record)
            elif record.record_type in ("run_summary", "baseline"):
                outcome = self._apply_run_annotation(record)
            elif record.record_type == "score":
                outcome = self._apply_score(record)
            else:
                outcome = self._apply_reference(record)
        except _UnitRejected as rejection:
            self.counts["rejected"] += 1
            self.diagnostics.append(f"{rejection.diagnostic}:{record.unit_ref()}")
            return "rejected"
        except (_UnitConflict, MappingConflict) as conflict:
            # 同 mapping_key / 同目标 id 绑定了不同来源内容：该单元停止，批次
            # 继续（协议 §5.4）。
            diagnostic = getattr(conflict, "diagnostic", "mapping_content_conflict")
            self.counts["conflicted"] += 1
            self.diagnostics.append(f"{diagnostic}:{record.unit_ref()}")
            return "conflicted"
        self.counts[outcome] += 1
        return outcome

    def _apply_reference(self, record: SourceRecord) -> str:
        """配置/数据/场景类：只建立账本引用，不发布为可执行资源（T07）。"""
        target_id = f"{record.record_type}:{record.source_id}"
        self._commit(record, record.record_type, target_id, legacy_payload=record.data)
        return "created"

    def _apply_artifact(self, record: SourceRecord) -> str:
        sha = record.data.get("sha256")
        if not isinstance(sha, str) or not _HEX64.match(sha):
            raise _UnitRejected("artifact_sha_invalid")
        source_file = self.source.artifact_files.get(sha)
        if source_file is None:
            entry = f"{record.unit_ref()}:{sha}"
            if entry not in self.missing_artifacts:
                self.missing_artifacts.append(entry)
            raise _UnitRejected("artifact_missing")
        content = source_file.read_bytes()
        if hashlib.sha256(content).hexdigest() != sha:
            # hash 不匹配 → 该 record 拒绝，不提交任何引用（协议 §5.3）。
            raise _UnitRejected("artifact_hash_mismatch")
        artifact_id = f"imports/{self.import_id}/{record.source_id}"
        self.artifact_store.put_bytes(
            artifact_id, content,
            kind=str(record.data.get("kind") or "file"),
            media_type=record.data.get("media_type") if isinstance(
                record.data.get("media_type"), str) else None,
        )
        # put_bytes 即暂存+提交（内容可校验）；提交引用前读回复核。
        readback = self.artifact_store.read_bytes(artifact_id)
        if hashlib.sha256(readback).hexdigest() != sha:
            self.artifact_store.delete(artifact_id)
            raise _UnitRejected("artifact_readback_mismatch")
        self.committed_artifacts[sha] = artifact_id
        self._commit(record, "artifact", artifact_id, artifact_sha256=sha)
        return "created"

    def _apply_run(self, record: SourceRecord) -> str:
        status = str(record.data.get("status") or "")
        if status in IN_FLIGHT_RUN_STATUSES:
            raise _UnitRejected("in_flight_job_not_importable")
        if status not in TERMINAL_RUN_STATUSES:
            raise _UnitRejected(f"unsupported_run_status:{status}")

        run_id = imported_run_id(record.source_id)
        import_source = {
            "system": self.source.manifest.source_system,
            "record_id": record.source_id,
            "content_hash": record.content_hash,
            "import_id": self.import_id,
        }
        existing = self.store.runs.get(run_id)
        if existing is not None:
            stored = (existing.get("manifest") or {}).get("import_source") or {}
            if stored.get("content_hash") == record.content_hash:
                # 崩溃恢复：store 写入已落、账本提交标记未写。
                self.run_ids_by_source[record.source_id] = run_id
                self._commit(record, "run", run_id)
                return "reused"
            raise _UnitConflict("run_id_content_conflict")

        case_ids = [
            case_id for case_id in (record.data.get("case_ids") or [])
            if isinstance(case_id, str) and case_id
        ]
        case_records = {cr.source_id: cr for cr in self.source.records_of("case")}
        summary = self.summaries.get(record.source_id)
        baselines = self.baselines.get(record.source_id, [])

        # 缺 reported model / gold / pass / price → 显式 unknown（协议 §5.3）。
        reported_model = record.data.get("reported_model")
        model_value = (
            reported_model if isinstance(reported_model, str) and reported_model else "unknown"
        )
        manifest = {
            "schema_version": 2,
            "execution": {"backend_id": "legacy-import", "backend_version":
                          self.transform_version},
            "import_source": import_source,
            "origin": "imported",
            "distributable": False,
            "model": model_value,
            "evaluation": {"scorer_id": "unknown", "scorer_version": "unknown",
                           "scoring_pass": "unknown"},
            "pricing": "unknown",
            # 原始字段整体保留（unknown 字段也绝不丢弃，协议 §5.2）。
            "legacy": deepcopy(record.data),
        }
        payload: dict[str, Any] = {
            "id": run_id,
            "status": status,
            "scenario_version": (
                f"legacy-{self.source.manifest.source_system}"
                f"@{self.source.manifest.source_schema_version}"
            ),
            "manifest": manifest,
            "case_ids": case_ids,
            "reported_model": model_value,
            "gold": "unknown",
            "price": "unknown",
            "created_at": record.data.get("created_at") or utc_now(),
            "updated_at": record.data.get("completed_at") or utc_now(),
        }

        artifact_refs: list[dict[str, Any]] = []
        declared = record.data.get("artifacts")
        if isinstance(declared, list):
            for entry in declared:
                sha = entry.get("sha256") if isinstance(entry, dict) else None
                if not isinstance(sha, str):
                    continue
                artifact_id = self.committed_artifacts.get(sha)
                if artifact_id is None:
                    self.warnings.append(
                        f"run {run_id}: artifact {sha} not imported; reference not committed"
                    )
                    continue
                artifact_refs.append({
                    "artifact_id": artifact_id, "sha256": sha, "available": True,
                })
        payload["artifact_refs"] = artifact_refs

        if summary is not None:
            # 聚合分只存 legacy summary，绝不伪造逐题分（协议 §5.3 / A10）。
            payload["legacy_summary"] = {
                "score": summary.data.get("score"),
                "metric": summary.data.get("metric"),
                "aggregation": summary.data.get("aggregation"),
                "per_case_scores_available": bool(
                    summary.data.get("per_case_scores_available")
                ),
                "source": {
                    "record_id": summary.source_id,
                    "content_hash": summary.content_hash,
                },
            }
        if baselines:
            first = baselines[0]
            payload["legacy_baseline"] = {
                **deepcopy(first.data),
                "source": {"record_id": first.source_id, "content_hash": first.content_hash},
            }
            if len(baselines) > 1:
                payload["legacy_baseline"]["merged_records"] = [b.source_id for b in baselines]

        stored = self.store.runs.create(
            payload,
            event={"run_id": run_id, "type": "created", "status": status,
                   "origin": "imported", "import_source": import_source},
        )
        self.store.events.append({
            "run_id": run_id, "type": "run_imported", "status": status,
            "import_source": import_source,
        })
        for case_id in case_ids:
            case_record = case_records.get(case_id)
            self.store.case_runs.upsert({
                "run_id": run_id, "case_id": case_id, "outcome": "imported",
                "result": {"legacy": deepcopy(case_record.data)} if case_record else None,
            })
        self.run_ids_by_source[record.source_id] = run_id
        self.created_run_ids.append(run_id)
        assert stored["id"] == run_id
        self._commit(record, "run", run_id)
        return "created"

    def _apply_run_annotation(self, record: SourceRecord) -> str:
        """run_summary / baseline：注记已在 run 创建时烘焙；这里提交账本行。"""
        run_ref = record.data.get("run_id")
        run_id = self._resolve_run(run_ref)
        run = self.store.runs.get(run_id)
        if run is None:
            raise _UnitRejected("orphan_reference")
        key = "legacy_summary" if record.record_type == "run_summary" else "legacy_baseline"
        if key not in run:
            raise _UnitRejected("annotation_target_missing")
        self._commit(record, record.record_type, run_id)
        return "created"

    def _score_row(self, record: SourceRecord) -> dict[str, Any]:
        case_id = record.data.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise _UnitRejected("score_case_id_missing")
        value = record.data.get("value")
        numeric = (
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value)
        )
        passed = record.data.get("passed")
        if passed is not None and not isinstance(passed, bool):
            passed = None
        return {
            "case_id": case_id,
            "metric_id": "legacy.score",
            "evaluator_id": "legacy-import",
            "evaluator_version": self.transform_version,
            "scorer_version": self.transform_version,
            "metric_status": "scored" if numeric else "insufficient_evidence",
            "value": float(value) if numeric else None,
            "passed": passed,
            "denominator": bool(numeric or passed is not None),
            "reason": None if numeric else "legacy_score_not_numeric",
            "details": {"source": "legacy-import", "imported": True,
                        "legacy_metric": record.data.get("metric")},
        }

    def _prepare_score_rows(self, run_ref: str) -> list[dict[str, Any]]:
        """首个 score 单元触达该 run 时预校验整组，构造 pass 的 score 行。"""
        if run_ref in self.score_rows_by_run:
            return self.score_rows_by_run[run_ref]
        run_id = self._resolve_run(run_ref)
        run = self.store.runs.get(run_id)
        assert run is not None  # 调用方已确认 run 存在
        selected = set(run.get("case_ids") or [])
        rows: list[dict[str, Any]] = []
        for record in self.scores_by_run.get(run_ref, []):
            if record.source_id in self.score_rejects:
                continue
            try:
                row = self._score_row(record)
            except _UnitRejected as rejection:
                self.score_rejects[record.source_id] = rejection.diagnostic
                continue
            if row["case_id"] not in selected:
                self.score_rejects[record.source_id] = "case_not_in_run"
                continue
            if any(existing["case_id"] == row["case_id"] for existing in rows):
                self.score_rejects[record.source_id] = "duplicate_legacy_score"
                continue
            rows.append(row)
        self.score_rows_by_run[run_ref] = rows
        return rows

    def _apply_score(self, record: SourceRecord) -> str:
        run_ref = str(record.data.get("run_id") or "")
        run_id = self._resolve_run(run_ref)
        run = self.store.runs.get(run_id)
        if run is None:
            raise _UnitRejected("orphan_reference")
        rows = self._prepare_score_rows(run_ref)
        if record.source_id in self.score_rejects:
            raise _UnitRejected(self.score_rejects[record.source_id])
        pass_id = f"pass-{run_id}-legacy"
        if rows and self.store.scoring_passes.get(pass_id) is None:
            self.store.scoring_passes.append(
                {
                    "id": pass_id, "run_id": run_id, "source": "legacy-import",
                    "created_at": utc_now(),
                    "summary": {"imported": True,
                                "source_system": self.source.manifest.source_system,
                                "record_count": len(rows)},
                },
                rows,
                expected_run_revision=run["revision"],
                expected_run_status=run["status"],
                event={"run_id": run_id, "type": "legacy_scores_imported"},
            )
        # pass 已在（崩溃恢复 / 同 run 首条已建）：该单元幂等提交映射行。
        self._commit(record, "score", f"{pass_id}:{record.source_id}")
        return "created"

    # -------------------------------------------------------- verification
    def verify(self) -> ImportVerification:
        hashes_match = True
        for sha, artifact_id in self.committed_artifacts.items():
            try:
                readback = self.artifact_store.read_bytes(artifact_id)
            except OSError:
                hashes_match = False
                self.warnings.append(f"artifact vanished after commit: {artifact_id}")
                continue
            if hashlib.sha256(readback).hexdigest() != sha:
                hashes_match = False
                self.warnings.append(f"artifact hash mismatch after commit: {artifact_id}")
        references_ok = True
        for run_id in self.created_run_ids:
            run = self.store.runs.get(run_id)
            if run is None:
                references_ok = False
                continue
            for ref in run.get("artifact_refs") or []:
                if not ref.get("available"):
                    references_ok = False
            case_rows = {row.get("case_id") for row in self.store.case_runs.list_for_run(run_id)}
            for case_id in run.get("case_ids") or []:
                if case_id not in case_rows:
                    references_ok = False
        return ImportVerification(
            counts_match=self.counts["pending"] == 0
            and self.counts["created"] + self.counts["reused"] + self.counts["rejected"]
            + self.counts["conflicted"] == self.planned,
            hashes_match=hashes_match,
            references_ok=references_ok,
        )


def _ordered_units(units: list[Any]) -> list[Any]:
    def rank(unit: Any) -> tuple[int, str]:
        record = unit.record
        if record is None:
            type_name = unit.load_reject.record_type if unit.load_reject else ""
        else:
            type_name = record.record_type
        try:
            return (IMPORT_ORDER.index(type_name), type_name)
        except ValueError:
            return (len(IMPORT_ORDER), type_name)

    return sorted(units, key=lambda unit: (rank(unit), unit.unit_ref))


def apply_import(
    store: Any, artifacts_root: str | Path, source: LoadedSource, *, operator: str,
    plan_report: ImportReport | None = None,
) -> ImportReport:
    """执行（或续跑）一次导入；每单元一个提交点，可从中断处恢复。"""
    if plan_report is not None and plan_report.import_id != source.manifest.import_id:
        raise ValueError("plan report belongs to a different import batch")
    current_hash = package_content_sha256(source.root)
    if current_hash != source.content_sha256:
        raise SourceChangedError(
            "source package changed on disk since it was loaded; reload and re-plan"
        )
    if plan_report is not None and plan_report.content_sha256 != current_hash:
        raise SourceChangedError(
            "source package changed since the dry-run plan; the plan is void, re-plan first"
        )
    if plan_report is None:
        plan_report = plan_import(store, source, operator=operator)

    session = _ApplySession(store, artifacts_root, source, operator)
    units = _ordered_units(unit_decisions(store, source))
    session.planned = len(units)
    # 预决策（reused/conflicted/rejected）直接定案；只有 create 单元进入执行，
    # pending 度量的是"本应执行却没执行完"的单元数（崩溃场景下 > 0）。
    pre_decided = {"reused": 0, "rejected": 0, "conflicted": 0}
    create_units = 0
    for unit in units:
        if unit.decision != "create":
            if (
                unit.decision == "reused" and unit.record is not None
                and unit.record.record_type == "run"
                and session.ledger.get_mapping(unit.record.mapping_key) is None
            ):
                # 崩溃恢复窗口：目标 Run 已在而账本提交标记缺失 → 补写映射行。
                session._commit(
                    unit.record, "run", imported_run_id(unit.record.source_id)
                )
            pre_decided[unit.decision] += 1
            if unit.diagnostic:
                session.diagnostics.append(unit.diagnostic)
            continue
        create_units += 1
        assert unit.record is not None
        session.apply_unit(unit.record)
    # pending 只看 create 单元的执行结果（此时 counts 仅含执行计数）。
    session.counts["pending"] = create_units - (
        session.counts["created"] + session.counts["reused"]
        + session.counts["rejected"] + session.counts["conflicted"]
    )
    for key, value in pre_decided.items():
        session.counts[key] += value

    verification = session.verify()
    session.ledger.set_status(session.import_id, "applied")
    warnings = list(plan_report.warnings) + session.warnings
    missing = list(plan_report.missing_artifacts) + session.missing_artifacts
    diagnostics = list(plan_report.diagnostics) + session.diagnostics
    return ImportReport(
        import_id=session.import_id,
        source_system=source.manifest.source_system,
        content_sha256=current_hash,
        planned=session.planned,
        created=session.counts["created"],
        reused=session.counts["reused"],
        rejected=session.counts["rejected"],
        conflicted=session.counts["conflicted"],
        pending=session.counts["pending"],
        missing_artifacts=tuple(missing),
        unknown_fields=plan_report.unknown_fields,
        warnings=tuple(warnings),
        credentials_to_rebind=plan_report.credentials_to_rebind,
        diagnostics=tuple(diagnostics),
        checkpoint=session.counts["created"] + session.counts["reused"],
        verification=verification,
        notes=(
            "each record is one transaction unit: target writes first, ledger mapping row last",
            "runs are created with terminal statuses only; imported runs are never claimable",
        ),
    )
