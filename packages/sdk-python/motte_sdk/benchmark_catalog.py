"""外部 Benchmark 数据目录与准备状态（M2-T04）。

复用既有来源治理（SourceSpec/revision/checksum/license、严格 JSONL 解析、
ApprovalVerifier 受信核验协议），不复制第二套治理服务。本模块交付外部
job-based Benchmark 的数据准备：

- 准备状态 unprepared/preparing/ready/failed；ready 必须同时满足完整文件、
  声明 checksum 校验通过、样本清单生成。部分下载不得 ready。
- 合法本地自供（user-supplied）与已核验官方来源（verified-official）分开；
  官方路径必须经过受信核验器与固定版本许可证据，自报 approval 不解锁。
- 无 gold 分区准备为 ready 但 unscored；样本清单逐条标记 has_gold，
  不伪造正确答案。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal

from motte_contracts.dataset_sources import SourceOverrideEvidence

from .dataset_sources import SourcePipelineError, parse_jsonl

CEVAL_BENCHMARK_ID = "ceval"
CEVAL_BENCHMARK_VERSION = "1"

PROVENANCE_USER = "user-supplied"
PROVENANCE_OFFICIAL = "verified-official"

_ROW_REQUIRED_FIELDS = frozenset({"id", "subject", "question", "A", "B", "C", "D"})
_ANSWER_LABELS = frozenset({"A", "B", "C", "D"})


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class DatasetFile:
    logical_name: str
    sha256: str
    size_bytes: int
    declared_sha256: str | None = None
    checksum_matches: bool | None = None


@dataclass(frozen=True)
class SampleManifestEntry:
    case_id: str
    subject: str
    row_sha256: str
    has_gold: bool


@dataclass(frozen=True)
class PreparedBenchmarkDataset:
    benchmark_id: str
    benchmark_version: str
    dataset_revision: str
    state: Literal["unprepared", "preparing", "ready", "failed"]
    reasons: tuple[str, ...] = ()
    provenance: str = PROVENANCE_USER
    files: tuple[DatasetFile, ...] = ()
    manifest: tuple[SampleManifestEntry, ...] = ()
    unscored: bool = False
    license_evidence: dict[str, Any] | None = None
    row_count: int = 0
    gold_count: int = 0


def _safe_logical_name(name: str) -> bool:
    return (
        isinstance(name, str)
        and bool(name)
        and "/" not in name
        and "\\" not in name
        and ".." not in name
        and not name.startswith(".")
    )


def _validate_rows(
    rows: list[dict[str, object]], logical_name: str,
) -> tuple[list[tuple[str, str, str, bool]], list[str]]:
    """逐行校验并产出 (case_id, subject, row_sha256, has_gold) 与失败原因。"""
    problems: list[str] = []
    entries: list[tuple[str, str, str, bool]] = []
    for line_no, row in enumerate(rows, 1):
        missing = _ROW_REQUIRED_FIELDS - set(row)
        if missing:
            problems.append(
                f"ROW_SCHEMA:{logical_name}:{line_no}:missing:{','.join(sorted(missing))}"
            )
            continue
        row_id = row.get("id")
        subject = row.get("subject")
        if not isinstance(row_id, str) or not row_id.strip():
            problems.append(f"ROW_SCHEMA:{logical_name}:{line_no}:id")
            continue
        if not isinstance(subject, str) or not subject.strip():
            problems.append(f"ROW_SCHEMA:{logical_name}:{line_no}:subject")
            continue
        answer = row.get("answer")
        if answer is None or (isinstance(answer, str) and not answer.strip()):
            has_gold = False
        elif isinstance(answer, str) and answer in _ANSWER_LABELS:
            has_gold = True
        else:
            problems.append(f"ROW_SCHEMA:{logical_name}:{line_no}:answer")
            continue
        canonical = hashlib.sha256(
            "\0".join(str(row.get(key, "")) for key in sorted(row)).encode("utf-8"),
        ).hexdigest()
        entries.append((row_id.strip(), subject.strip(), canonical, has_gold))
    return (entries, problems)


def prepare_ceval_external_dataset(
    *,
    files: dict[str, bytes],
    dataset_revision: str,
    declared_sha256: dict[str, str] | None = None,
    provenance_target: Literal["user-supplied", "verified-official"] = "user-supplied",
    approval_evidence: dict[str, Any] | None = None,
    approval_verifier: Any | None = None,
    license_evidence: dict[str, Any] | None = None,
    benchmark_version: str = CEVAL_BENCHMARK_VERSION,
) -> PreparedBenchmarkDataset:
    """校验并冻结 C-Eval 外部数据：文件、checksum、样本清单与来源治理。"""
    reasons: list[str] = []
    official_ok = provenance_target == PROVENANCE_OFFICIAL
    evidence: SourceOverrideEvidence | None = None
    if official_ok:
        try:
            evidence = SourceOverrideEvidence.model_validate(approval_evidence or {})
        except Exception as error:  # noqa: BLE001 - 证据形状问题进入 reasons
            reasons.append(f"OFFICIAL_APPROVAL_INVALID:{type(error).__name__}")
        if evidence is not None and approval_verifier is None:
            reasons.append("OFFICIAL_APPROVAL_UNVERIFIED")
        elif evidence is not None:
            if not approval_verifier(None, "import", evidence):
                reasons.append("OFFICIAL_APPROVAL_UNVERIFIED")
        if not license_evidence:
            reasons.append("OFFICIAL_LICENSE_MISSING")
        elif not isinstance(license_evidence.get("declared_ids"), list) or not license_evidence.get(
            "declared_ids"
        ):
            reasons.append("OFFICIAL_LICENSE_MISSING")

    if not files:
        reasons.append("EMPTY_DATASET")
    file_entries: list[DatasetFile] = []
    rows_by_file: dict[str, list[tuple[str, str, str, bool]]] = {}
    for logical_name in sorted(files):
        if not _safe_logical_name(logical_name):
            reasons.append(f"UNSAFE_LOGICAL_NAME:{logical_name}")
            continue
        content = files[logical_name]
        digest = _digest(content)
        declared = (declared_sha256 or {}).get(logical_name)
        matches: bool | None = None
        if declared is not None:
            matches = declared == digest
            if not matches:
                reasons.append(f"CHECKSUM_MISMATCH:{logical_name}")
        file_entries.append(DatasetFile(
            logical_name=logical_name,
            sha256=digest,
            size_bytes=len(content),
            declared_sha256=declared,
            checksum_matches=matches,
        ))
        if not content.strip():
            reasons.append(f"SOURCE_PARSE_FAILED:{logical_name}:empty")
            continue
        try:
            parsed = parse_jsonl(content)
        except SourcePipelineError as error:
            reasons.append(f"SOURCE_PARSE_FAILED:{logical_name}:{error.code}")
            continue
        entries, problems = _validate_rows(parsed, logical_name)
        reasons.extend(problems)
        if not entries:
            reasons.append(f"SOURCE_PARSE_FAILED:{logical_name}:no-rows")
        rows_by_file[logical_name] = entries

    for declared_name in sorted(declared_sha256 or {}):
        if declared_name not in files:
            reasons.append(f"MISSING_ARTIFACT:{declared_name}")

    seen: dict[str, str] = {}
    manifest: list[SampleManifestEntry] = []
    for logical_name in sorted(rows_by_file):
        for case_id, subject, row_sha256, has_gold in rows_by_file[logical_name]:
            if case_id in seen:
                reasons.append(f"DUPLICATE_CASE_ID:{case_id}")
                continue
            seen[case_id] = logical_name
            manifest.append(SampleManifestEntry(
                case_id=case_id,
                subject=subject,
                row_sha256=row_sha256,
                has_gold=has_gold,
            ))

    gold_count = sum(1 for entry in manifest if entry.has_gold)
    state = "ready" if not reasons and manifest else "failed"
    provenance = (
        PROVENANCE_OFFICIAL
        if official_ok
        and not any(reason.startswith("OFFICIAL_") for reason in reasons)
        else PROVENANCE_USER
    )
    return PreparedBenchmarkDataset(
        benchmark_id=CEVAL_BENCHMARK_ID,
        benchmark_version=benchmark_version,
        dataset_revision=dataset_revision,
        state=state,
        reasons=tuple(dict.fromkeys(reasons)),
        provenance=provenance,
        files=tuple(file_entries),
        manifest=tuple(manifest),
        unscored=gold_count == 0,
        license_evidence=dict(license_evidence) if license_evidence else None,
        row_count=len(manifest),
        gold_count=gold_count,
    )


@dataclass
class _CatalogEntry:
    benchmark_id: str
    benchmark_version: str
    dataset: PreparedBenchmarkDataset | None = None
    runner_connected: bool = False
    profile_validated: bool = False
    live_verified: bool = False
    notes: dict[str, Any] = field(default_factory=dict)


class BenchmarkCatalog:
    """registered / prepared / runnable / verified 四级状态与阻塞原因（M2-G05）。"""

    def __init__(self) -> None:
        self._entries: dict[str, _CatalogEntry] = {}

    def register(
        self, benchmark_id: str, *, benchmark_version: str = "1", replace: bool = False,
    ) -> None:
        if benchmark_id in self._entries and not replace:
            raise ValueError("benchmark already registered: " + benchmark_id)
        self._entries[benchmark_id] = _CatalogEntry(
            benchmark_id=benchmark_id, benchmark_version=benchmark_version,
        )

    def update_dataset(
        self, benchmark_id: str, dataset: PreparedBenchmarkDataset,
    ) -> None:
        self._entry(benchmark_id).dataset = dataset

    def mark_runner_connected(self, benchmark_id: str, connected: bool = True) -> None:
        self._entry(benchmark_id).runner_connected = connected

    def mark_profile_validated(self, benchmark_id: str, validated: bool = True) -> None:
        self._entry(benchmark_id).profile_validated = validated

    def mark_live_verified(self, benchmark_id: str, verified: bool = True) -> None:
        self._entry(benchmark_id).live_verified = verified

    def _entry(self, benchmark_id: str) -> _CatalogEntry:
        try:
            return self._entries[benchmark_id]
        except KeyError:
            raise KeyError("unknown benchmark: " + benchmark_id) from None

    def status(self, benchmark_id: str) -> dict[str, Any]:
        entry = self._entry(benchmark_id)
        blockers: list[str] = []
        dataset_ready = entry.dataset is not None and entry.dataset.state == "ready"
        if not dataset_ready:
            blockers.append("DATASET_UNPREPARED")
            if entry.dataset is not None and entry.dataset.reasons:
                blockers.extend(entry.dataset.reasons)
        if not entry.runner_connected:
            blockers.append("RUNNER_NOT_CONNECTED")
        if not entry.profile_validated:
            blockers.append("PROFILE_NOT_VALIDATED")
        if not dataset_ready:
            status = "registered"
        elif entry.runner_connected and entry.profile_validated:
            status = "verified" if entry.live_verified else "runnable"
        else:
            status = "prepared"
        return {
            "benchmark_id": entry.benchmark_id,
            "benchmark_version": entry.benchmark_version,
            "status": status,
            "blockers": blockers,
            "dataset": (
                {
                    "state": entry.dataset.state,
                    "provenance": entry.dataset.provenance,
                    "revision": entry.dataset.dataset_revision,
                    "rows": entry.dataset.row_count,
                    "gold_rows": entry.dataset.gold_count,
                    "unscored": entry.dataset.unscored,
                }
                if entry.dataset is not None else None
            ),
        }
