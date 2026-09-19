"""外部 Benchmark 数据目录、准备状态与 Run 输入构造（M2-T04 + review R01/R13/R14）。

复用既有来源治理（SourceSpec/revision/checksum/license、严格 JSONL 解析、
ApprovalVerifier 受信核验协议），不复制第二套治理服务。本模块交付外部
job-based Benchmark 的数据准备与创建入口共享服务：

- 准备状态 unprepared/preparing/ready/failed；ready 必须同时满足完整文件、
  声明 checksum 校验通过、样本清单生成。部分下载不得 ready。
- 合法本地自供（user-supplied）与已核验官方来源（verified-official）分开；
  官方路径必须经过受信核验器与固定版本许可证据，自报 approval 不解锁。
- 无 gold 分区准备为 ready 但 unscored；样本清单逐条标记 has_gold，
  不伪造正确答案。
- 准备结果持久化（review R13）：``BenchmarkCatalog`` 可挂接
  ``benchmark_datasets`` 存储，API/CLI/Worker 共享，重启不丢。
- 学科白名单（review R16）：ceval 只接受官方 52 学科，cmmlu 只接受 67
  学科，未知学科拒绝而非静默聚合失败。
- 创建入口统一预检（review R14）：模型生命周期、split、few-shot、scope
  范围在入队前由 ``validate_external_run_request`` 校验，API/CLI 共用。
- Runner 可消费配置（review R01）：``prepare_external_run_inputs`` 用冻结
  数据 + 模型快照 + 凭据引用构造 ``build_opencompass_config`` 的完整配置
  并钉进 manifest，同步冻结 ``case_expectations``（gold 平台侧评分来源）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping

from motte_contracts.dataset_sources import SourceOverrideEvidence

from .dataset_sources import SourcePipelineError, parse_jsonl

CEVAL_BENCHMARK_ID = "ceval"
CEVAL_BENCHMARK_VERSION = "1"
CMMLU_BENCHMARK_ID = "cmmlu"
CMMLU_BENCHMARK_VERSION = "1"

# 外部 MCQ 评分（ceval/cmmlu 共用样本级对错判定；聚合口径各自冻结在 Profile）。
EXTERNAL_MCQ_SCORER = "external-mcq"
EXTERNAL_MCQ_SCORER_VERSION = "external-mcq@1"

PROVENANCE_USER = "user-supplied"
PROVENANCE_OFFICIAL = "verified-official"

_ROW_REQUIRED_FIELDS = frozenset({"id", "subject", "question", "A", "B", "C", "D"})
_ANSWER_LABELS = frozenset({"A", "B", "C", "D"})

_SCOPE_VALUES = ("smoke", "custom-subset", "full")
_SMOKE_LIMIT = 20


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class BenchmarkDescriptor:
    """一个外部 job-based Benchmark 的身份与创建参数（ceval/cmmlu 共用骨架）。"""

    benchmark_id: str
    benchmark_version: str
    suite: str
    scenario_version: str
    adapter_id: str
    adapter_version: str
    parser_version: str
    splits: tuple[str, ...]
    default_split: str
    default_few_shot_split: str
    profile_builder: Callable[..., dict[str, Any]]
    allowed_subjects: frozenset[str]


def _ceval_profile_builder(**kwargs: Any) -> dict[str, Any]:
    from motte_benchmark.opencompass.profiles import ceval_external_profile

    return ceval_external_profile(**kwargs)


def _cmmlu_profile_builder(**kwargs: Any) -> dict[str, Any]:
    from motte_benchmark.opencompass.cmmlu import cmmlu_external_profile

    return cmmlu_external_profile(**kwargs)


def _ceval_subjects() -> frozenset[str]:
    from motte_benchmark.opencompass.parser import CEVAL_ALL_SUBJECTS

    return frozenset(CEVAL_ALL_SUBJECTS)


def _cmmlu_subjects() -> frozenset[str]:
    from motte_benchmark.opencompass.cmmlu import CMMLU_SUBJECT_META

    return frozenset(CMMLU_SUBJECT_META)


def benchmark_descriptor(benchmark_id: str) -> BenchmarkDescriptor:
    try:
        return BENCHMARK_DESCRIPTORS[benchmark_id]
    except KeyError:
        known = ", ".join(sorted(BENCHMARK_DESCRIPTORS))
        raise KeyError(
            f"unknown external benchmark: {benchmark_id} (known: {known})"
        ) from None


BENCHMARK_DESCRIPTORS: dict[str, BenchmarkDescriptor] = {
    CEVAL_BENCHMARK_ID: BenchmarkDescriptor(
        benchmark_id=CEVAL_BENCHMARK_ID,
        benchmark_version=CEVAL_BENCHMARK_VERSION,
        suite="ceval-external",
        scenario_version="ceval-external@1",
        adapter_id="ceval-opencompass",
        adapter_version="1",
        parser_version="ceval-opencompass-parser@1",
        splits=("val", "test", "dev"),
        default_split="val",
        default_few_shot_split="dev",
        profile_builder=_ceval_profile_builder,
        allowed_subjects=_ceval_subjects(),
    ),
    CMMLU_BENCHMARK_ID: BenchmarkDescriptor(
        benchmark_id=CMMLU_BENCHMARK_ID,
        benchmark_version=CMMLU_BENCHMARK_VERSION,
        suite="cmmlu-external",
        scenario_version="cmmlu-external@1",
        adapter_id="cmmlu-opencompass",
        adapter_version="1",
        parser_version="cmmlu-opencompass-parser@1",
        splits=("test", "dev"),
        default_split="test",
        default_few_shot_split="dev",
        profile_builder=_cmmlu_profile_builder,
        allowed_subjects=_cmmlu_subjects(),
    ),
}


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
    split: str = ""


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
    # 冻结的逐行内容（含题干/选项/gold 与解析出的 split）：Runner 配置与
    # 平台侧评分的唯一数据来源（review R01/R03）。
    rows: tuple[dict[str, Any], ...] = ()
    dataset_splits: tuple[str, ...] = ()


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
    rows: list[dict[str, object]],
    logical_name: str,
    *,
    allowed_subjects: frozenset[str] | None,
    default_split: str,
) -> tuple[list[tuple[str, str, str, bool, str, dict[str, Any]]], list[str]]:
    """逐行校验并产出清单条目与冻结行；失败原因逐条可定位。"""
    problems: list[str] = []
    entries: list[tuple[str, str, str, bool, str, dict[str, Any]]] = []
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
        if allowed_subjects is not None and subject.strip() not in allowed_subjects:
            problems.append(f"UNKNOWN_SUBJECT:{logical_name}:{line_no}:{subject.strip()}")
            continue
        answer = row.get("answer")
        if answer is None or (isinstance(answer, str) and not answer.strip()):
            has_gold = False
        elif isinstance(answer, str) and answer in _ANSWER_LABELS:
            has_gold = True
        else:
            problems.append(f"ROW_SCHEMA:{logical_name}:{line_no}:answer")
            continue
        split = row.get("split")
        if split is None or (isinstance(split, str) and not split.strip()):
            split = default_split
        if not isinstance(split, str) or not split.strip():
            problems.append(f"ROW_SCHEMA:{logical_name}:{line_no}:split")
            continue
        canonical = hashlib.sha256(
            "\0".join(str(row.get(key, "")) for key in sorted(row)).encode("utf-8"),
        ).hexdigest()
        frozen_row = {
            "id": row_id.strip(),
            "subject": subject.strip(),
            "question": str(row.get("question", "")),
            "A": str(row.get("A", "")),
            "B": str(row.get("B", "")),
            "C": str(row.get("C", "")),
            "D": str(row.get("D", "")),
            "answer": answer if has_gold else None,
            "split": split.strip(),
        }
        entries.append((row_id.strip(), subject.strip(), canonical, has_gold, split.strip(), frozen_row))
    return (entries, problems)


def prepare_external_dataset(
    *,
    files: dict[str, bytes],
    dataset_revision: str,
    declared_sha256: dict[str, str] | None = None,
    provenance_target: Literal["user-supplied", "verified-official"] = "user-supplied",
    approval_evidence: dict[str, Any] | None = None,
    approval_verifier: Any | None = None,
    license_evidence: dict[str, Any] | None = None,
    benchmark_version: str = "1",
    benchmark_id: str = CEVAL_BENCHMARK_ID,
    default_split: str | None = None,
) -> PreparedBenchmarkDataset:
    """校验并冻结外部基准数据：文件、checksum、样本清单与来源治理。"""
    descriptor = benchmark_descriptor(benchmark_id)
    if default_split is None:
        default_split = descriptor.default_split
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
    rows_by_file: dict[str, list[tuple[str, str, str, bool, str, dict[str, Any]]]] = {}
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
        entries, problems = _validate_rows(
            parsed, logical_name,
            allowed_subjects=descriptor.allowed_subjects,
            default_split=default_split,
        )
        reasons.extend(problems)
        if not entries:
            reasons.append(f"SOURCE_PARSE_FAILED:{logical_name}:no-rows")
        rows_by_file[logical_name] = entries

    for declared_name in sorted(declared_sha256 or {}):
        if declared_name not in files:
            reasons.append(f"MISSING_ARTIFACT:{declared_name}")

    seen: dict[str, str] = {}
    manifest: list[SampleManifestEntry] = []
    frozen_rows: list[dict[str, Any]] = []
    splits_seen: set[str] = set()
    for logical_name in sorted(rows_by_file):
        for case_id, subject, row_sha256, has_gold, split, frozen_row in rows_by_file[logical_name]:
            if case_id in seen:
                reasons.append(f"DUPLICATE_CASE_ID:{case_id}")
                continue
            seen[case_id] = logical_name
            manifest.append(SampleManifestEntry(
                case_id=case_id,
                subject=subject,
                row_sha256=row_sha256,
                has_gold=has_gold,
                split=split,
            ))
            frozen_rows.append(frozen_row)
            splits_seen.add(split)

    gold_count = sum(1 for entry in manifest if entry.has_gold)
    state = "ready" if not reasons and manifest else "failed"
    provenance = (
        PROVENANCE_OFFICIAL
        if official_ok
        and not any(reason.startswith("OFFICIAL_") for reason in reasons)
        else PROVENANCE_USER
    )
    return PreparedBenchmarkDataset(
        benchmark_id=benchmark_id,
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
        rows=tuple(frozen_rows),
        dataset_splits=tuple(sorted(splits_seen)),
    )


def prepare_ceval_external_dataset(
    *,
    files: dict[str, bytes],
    dataset_revision: str,
    declared_sha256: dict[str, str] | None = None,
    provenance_target: Literal["user-supplied", "verified-official"] = "user-supplied",
    approval_evidence: dict[str, Any] | None = None,
    approval_verifier: Any = None,
    license_evidence: dict[str, Any] | None = None,
    benchmark_version: str = CEVAL_BENCHMARK_VERSION,
    default_split: str | None = None,
) -> PreparedBenchmarkDataset:
    """C-Eval 包装（M2-T04 兼容入口；CMMLU 用 prepare_external_dataset）。"""
    return prepare_external_dataset(
        files=files, dataset_revision=dataset_revision,
        declared_sha256=declared_sha256, provenance_target=provenance_target,
        approval_evidence=approval_evidence, approval_verifier=approval_verifier,
        license_evidence=license_evidence, benchmark_version=benchmark_version,
        benchmark_id=CEVAL_BENCHMARK_ID, default_split=default_split,
    )


def prepare_cmmlu_external_dataset(
    *,
    files: dict[str, bytes],
    dataset_revision: str,
    declared_sha256: dict[str, str] | None = None,
    provenance_target: Literal["user-supplied", "verified-official"] = "user-supplied",
    approval_evidence: dict[str, Any] | None = None,
    approval_verifier: Any = None,
    license_evidence: dict[str, Any] | None = None,
    benchmark_version: str = CMMLU_BENCHMARK_VERSION,
    default_split: str | None = None,
) -> PreparedBenchmarkDataset:
    """CMMLU 包装：67 学科白名单独立生效（review R16）。"""
    return prepare_external_dataset(
        files=files, dataset_revision=dataset_revision,
        declared_sha256=declared_sha256, provenance_target=provenance_target,
        approval_evidence=approval_evidence, approval_verifier=approval_verifier,
        license_evidence=license_evidence, benchmark_version=benchmark_version,
        benchmark_id=CMMLU_BENCHMARK_ID, default_split=default_split or "test",
    )


# ------------------------------------------------------------------ 序列化（R13）


def dataset_to_payload(dataset: PreparedBenchmarkDataset) -> dict[str, Any]:
    """把准备结果 dump 成可持久化 JSON（含逐行内容与治理证据）。"""
    return {
        "benchmark_id": dataset.benchmark_id,
        "benchmark_version": dataset.benchmark_version,
        "dataset_revision": dataset.dataset_revision,
        "state": dataset.state,
        "reasons": list(dataset.reasons),
        "provenance": dataset.provenance,
        "unscored": dataset.unscored,
        "license_evidence": dataset.license_evidence,
        "row_count": dataset.row_count,
        "gold_count": dataset.gold_count,
        "dataset_splits": list(dataset.dataset_splits),
        "files": [
            {
                "logical_name": item.logical_name,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
                "declared_sha256": item.declared_sha256,
                "checksum_matches": item.checksum_matches,
            }
            for item in dataset.files
        ],
        "manifest": [
            {
                "case_id": item.case_id,
                "subject": item.subject,
                "row_sha256": item.row_sha256,
                "has_gold": item.has_gold,
                "split": item.split,
            }
            for item in dataset.manifest
        ],
        "rows": [dict(row) for row in dataset.rows],
    }


def dataset_from_payload(payload: Mapping[str, Any]) -> PreparedBenchmarkDataset:
    """从持久化 JSON 重建准备结果（review R13：重启/多实例共享）。"""
    data = dict(payload)
    files = tuple(DatasetFile(**item) for item in data.get("files") or [])
    manifest = tuple(SampleManifestEntry(**item) for item in data.get("manifest") or [])
    rows = tuple(dict(row) for row in data.get("rows") or [])
    known = {
        "benchmark_id", "benchmark_version", "dataset_revision", "state", "reasons",
        "provenance", "unscored", "license_evidence", "row_count", "gold_count",
        "dataset_splits",
    }
    fields = {key: value for key, value in data.items() if key in known}
    return PreparedBenchmarkDataset(
        **fields,
        files=files,
        manifest=manifest,
        rows=rows,
        dataset_splits=tuple(data.get("dataset_splits") or ()),
    )


# ------------------------------------------------------------------ 目录（R13）


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
    """registered / prepared / runnable / verified 四级状态与阻塞原因（M2-G05）。

    挂接 ``datasets_store``（RunStore.benchmark_datasets）后，准备结果持久
    化：新实例/重启从存储恢复，不再把 ready 数据降回 unprepared（R13）。
    """

    def __init__(self, datasets_store: Any = None) -> None:
        self._entries: dict[str, _CatalogEntry] = {}
        self._store = datasets_store

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
        if self._store is not None:
            from datetime import UTC, datetime

            payload = dataset_to_payload(dataset)
            payload["created_at"] = datetime.now(UTC).isoformat()
            self._store.put(payload)
        self._entry(benchmark_id).dataset = dataset

    def dataset(self, benchmark_id: str) -> PreparedBenchmarkDataset | None:
        entry = self._entry(benchmark_id)
        if entry.dataset is not None:
            return entry.dataset
        if self._store is not None:
            payload = self._store.latest(benchmark_id)
            if payload is not None:
                restored = dataset_from_payload(payload)
                entry.dataset = restored
                return restored
        return None

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
        dataset = self.dataset(benchmark_id)
        blockers: list[str] = []
        dataset_ready = dataset is not None and dataset.state == "ready"
        if not dataset_ready:
            blockers.append("DATASET_UNPREPARED")
            if dataset is not None and dataset.reasons:
                blockers.extend(dataset.reasons)
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
                    "state": dataset.state,
                    "provenance": dataset.provenance,
                    "revision": dataset.dataset_revision,
                    "rows": dataset.row_count,
                    "gold_rows": dataset.gold_count,
                    "unscored": dataset.unscored,
                }
                if dataset is not None else None
            ),
        }


# ------------------------------------------------------------------ 创建入口（R01/R14）


def external_runner_version() -> str:
    from motte_benchmark.opencompass.parser import RUNNER_VERSION_PIN

    return f"opencompass-{RUNNER_VERSION_PIN}"


def external_environment_digest(benchmark_id: str) -> str:
    """环境指纹：Runner 版本 + adapter/parser/benchmark 身份的规范 hash。

    不再只是 Runner 版本字符串的 hash（review R01）：同一 runner_version 下
    换 adapter/解析器/基准版本会得到不同 digest。
    """
    descriptor = benchmark_descriptor(benchmark_id)
    identity = {
        "runner_version": external_runner_version(),
        "adapter_id": descriptor.adapter_id,
        "adapter_version": descriptor.adapter_version,
        "parser_version": descriptor.parser_version,
        "benchmark_id": descriptor.benchmark_id,
        "benchmark_version": descriptor.benchmark_version,
    }
    return "sha256:" + hashlib.sha256(json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _model_view(model_record: Mapping[str, Any] | None) -> dict[str, Any]:
    if model_record is None:
        return {}
    if hasattr(model_record, "model_dump"):
        record = model_record.model_dump(mode="json")
    else:
        record = dict(model_record)
    return {
        "provider": record.get("provider"),
        "model": record.get("model") or record.get("id"),
        "parameters": dict(record.get("parameters") or {}),
        "max_output_tokens": record.get("max_output_tokens"),
        "context_window": record.get("context_window"),
        "lifecycle": record.get("lifecycle"),
    }


def resolve_selection(
    dataset: PreparedBenchmarkDataset,
    *,
    scope: str,
    split: str,
    case_ids: list[str] | None,
) -> list[dict[str, Any]]:
    """scope → 冻结的评估行（full=分区全部；smoke=前 20；custom-subset=给定）。"""
    split_rows = [row for row in dataset.rows if str(row.get("split")) == split]
    by_id = {str(row.get("id")): row for row in split_rows}
    if scope == "full":
        return list(split_rows)
    if scope == "smoke":
        return list(split_rows[:_SMOKE_LIMIT])
    if case_ids is not None:
        return [by_id[case_id] for case_id in case_ids if case_id in by_id]
    return list(split_rows)


def validate_external_run_request(
    dataset: PreparedBenchmarkDataset,
    *,
    benchmark_id: str,
    model_record: Mapping[str, Any] | None,
    scope: str = "custom-subset",
    split: str | None = None,
    few_shot: int = 0,
    few_shot_split: str | None = None,
    case_ids: list[str] | None = None,
    runner_connected: bool = True,
) -> list[str]:
    """API/CLI 共用的入队前校验（review R14）；返回原因清单（空=通过）。

    模型生命周期、分区真实性、few-shot 可用性、scope 完整范围与上下文
    预算都在创建 Run 之前校验；失败时 0 个 Job 启动。
    """
    descriptor = benchmark_descriptor(benchmark_id)
    if split is None:
        split = descriptor.default_split
    if few_shot_split is None:
        few_shot_split = descriptor.default_few_shot_split
    reasons: list[str] = []
    if dataset.state != "ready":
        reasons.append("DATASET_UNPREPARED")
    if not runner_connected:
        reasons.append("RUNNER_NOT_CONNECTED")
    model_view = _model_view(model_record)
    if not str(model_view.get("model") or "").strip():
        reasons.append("MODEL_IDENTITY_MISSING")
    elif str(model_view.get("lifecycle") or "") != "published":
        reasons.append(f"MODEL_NOT_PUBLISHED:{model_view.get('lifecycle')!r}")

    if split not in dataset.dataset_splits:
        reasons.append(
            f"SPLIT_NOT_IN_DATASET:{split} (prepared: {list(dataset.dataset_splits)})",
        )
    if few_shot:
        if few_shot_split not in dataset.dataset_splits:
            reasons.append(
                f"FEWSHOT_SPLIT_NOT_IN_DATASET:{few_shot_split} "
                f"(prepared: {list(dataset.dataset_splits)})",
            )
        else:
            dev_rows = [
                row for row in dataset.rows
                if str(row.get("split")) == few_shot_split
            ]
            if len(dev_rows) < few_shot:
                reasons.append(
                    f"FEWSHOT_EXAMPLES_INSUFFICIENT:{len(dev_rows)} < {few_shot}",
                )
        if few_shot_split == split:
            reasons.append(
                f"FEWSHOT_SPLIT_CONFLICT:few-shot partition {few_shot_split!r} "
                "must differ from the evaluated split",
            )
    if scope not in _SCOPE_VALUES:
        reasons.append(f"SCOPE_INVALID:{scope} (must be one of {_SCOPE_VALUES})")

    if dataset.state == "ready":
        split_ids = [
            str(row.get("id")) for row in dataset.rows
            if str(row.get("split")) == split
        ]
        if scope == "full" and case_ids is not None and sorted(case_ids) != sorted(split_ids):
            reasons.append(
                f"SCOPE_FULL_MISMATCH:selection {len(case_ids)} != split rows {len(split_ids)}",
            )
        if case_ids is not None:
            known = set(split_ids)
            unknown = [case_id for case_id in case_ids if case_id not in known]
            if unknown:
                reasons.append("UNKNOWN_CASE_ID:" + ",".join(unknown[:5]))
            if not case_ids:
                reasons.append("SELECTION_EMPTY")
        if not split_ids:
            reasons.append(f"SELECTION_EMPTY:split {split!r} has no rows")

        # 上下文预算（保守：目标题干 + few-shot 示例的字节上界）。
        context_window = model_view.get("context_window")
        if isinstance(context_window, int) and context_window > 0 and dataset.rows:
            base = max(
                len(str(row.get("question", ""))) + sum(
                    len(str(row.get(label, ""))) for label in ("A", "B", "C", "D")
                )
                for row in dataset.rows
            )
            dev_rows = [
                row for row in dataset.rows
                if str(row.get("split")) == few_shot_split
            ]
            example_bytes = 0
            for row in dev_rows[:max(few_shot, 0)]:
                example_bytes += len(str(row.get("question", ""))) + sum(
                    len(str(row.get(label, ""))) for label in ("A", "B", "C", "D")
                )
            estimate = base + example_bytes
            if estimate > context_window:
                reasons.append(
                    f"CONTEXT_WINDOW_INSUFFICIENT:prompt~{estimate} > window {context_window}",
                )
    return reasons


def prepare_external_run_inputs(
    dataset: PreparedBenchmarkDataset,
    *,
    benchmark_id: str,
    model_id: str,
    model_record: Mapping[str, Any] | None,
    few_shot: int = 0,
    seed: int = 0,
    scope: str = "custom-subset",
    split: str | None = None,
    few_shot_split: str | None = None,
    credentials: Mapping[str, Any] | None = None,
    retry_policy: Mapping[str, int] | None = None,
    case_ids: list[str] | None = None,
    runner_connected: bool = True,
    resources: Any = None,
) -> dict[str, Any]:
    """构造外部 Run 的完整冻结输入（API 与 CLI 共用，review R01/R02）。

    产出 scenario_version、含 ``benchmark_provenance``/``evaluation`` 评分
    身份的 manifest（经插件 prepare 装配）、Runner 可消费的
    ``external_benchmark.runner_config``（模型快照 + 题目 prompt + 凭据引用
    + 配置 hash）与平台侧 gold 来源 ``case_expectations``。
    """
    from motte_benchmark.opencompass.config import (
        OpenCompassConfigError,
        build_opencompass_config,
    )

    descriptor = benchmark_descriptor(benchmark_id)
    if split is None:
        split = descriptor.default_split
    if few_shot_split is None:
        few_shot_split = descriptor.default_few_shot_split
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("MODEL_REQUIRED: model profile id is required")
    reasons = validate_external_run_request(
        dataset,
        benchmark_id=benchmark_id,
        model_record=model_record,
        scope=scope,
        split=split,
        few_shot=few_shot,
        few_shot_split=few_shot_split,
        case_ids=case_ids,
        runner_connected=runner_connected,
    )
    if reasons:
        raise ValueError("RUN_REQUEST_INVALID: " + "; ".join(reasons))

    profile = descriptor.profile_builder(
        dataset_revision=dataset.dataset_revision,
        subjects=sorted({str(row.get("subject")) for row in dataset.rows}),
        split=split,
        few_shot=few_shot,
        few_shot_split=few_shot_split,
        seed=seed,
        runner_version=external_runner_version(),
        environment_digest=external_environment_digest(benchmark_id),
    )
    eval_rows = resolve_selection(dataset, scope=scope, split=split, case_ids=case_ids)
    if not eval_rows:
        raise ValueError("SELECTION_EMPTY: no eval rows resolved for the request")
    dev_rows = [
        row for row in dataset.rows if str(row.get("split")) == few_shot_split
    ][:max(few_shot, 0)]
    model_view = _model_view(model_record)
    try:
        runner_config = build_opencompass_config(
            profile=profile,
            model=model_view,
            eval_rows=eval_rows,
            few_shot_rows=dev_rows,
            scope=scope,
            credentials=credentials,
            retry_policy=retry_policy,
        )
    except OpenCompassConfigError as error:
        raise ValueError(f"RUN_REQUEST_INVALID:{error}") from error

    case_expectations = {
        str(row.get("id")): (row.get("answer") if row.get("answer") else None)
        for row in eval_rows
    }
    manifest: dict[str, Any] = {
        "model": model_id,
        "scope": scope,
        "case_expectations": case_expectations,
        "execution": {"backend_id": "external-benchmark", "backend_version": "1"},
        "external_benchmark": {
            "adapter_id": descriptor.adapter_id,
            "adapter_version": descriptor.adapter_version,
            "runner_version": profile["runner_version"],
            "dataset_revision": dataset.dataset_revision,
            "environment_digest": profile["environment_digest"],
            "profile": profile,
            "limits": {"poll_interval_seconds": 0.1},
            "parser_version": descriptor.parser_version,
            "runner_config": runner_config,
        },
    }
    from .benchmark_plugins import prepare_with_plugin

    scenario = {
        "suite": descriptor.suite,
        "plugin_version": "1",
        "requested": {
            "model": model_id, "scope": scope, "split": split,
            "few_shot": few_shot, "few_shot_split": few_shot_split, "seed": seed,
        },
    }
    resolved = prepare_with_plugin(descriptor.suite, scenario, manifest, resources)
    return {
        "scenario_version": descriptor.scenario_version,
        "manifest": resolved,
        "case_ids": [str(row.get("id")) for row in eval_rows],
    }


def prepare_ceval_run_inputs(
    dataset: PreparedBenchmarkDataset,
    *,
    model_id: str,
    model_record: Mapping[str, Any] | None = None,
    few_shot: int = 0,
    seed: int = 0,
    scope: str = "custom-subset",
    split: str | None = None,
    few_shot_split: str | None = None,
    credentials: Mapping[str, Any] | None = None,
    case_ids: list[str] | None = None,
    runner_connected: bool = True,
    resources: Any = None,
) -> dict[str, Any]:
    """C-Eval 兼容包装（M2-T07）；真实入口是 prepare_external_run_inputs。"""
    return prepare_external_run_inputs(
        dataset,
        benchmark_id=CEVAL_BENCHMARK_ID,
        model_id=model_id,
        model_record=model_record,
        few_shot=few_shot,
        seed=seed,
        scope=scope,
        split=split,
        few_shot_split=few_shot_split,
        credentials=credentials,
        case_ids=case_ids,
        runner_connected=runner_connected,
        resources=resources,
    )
