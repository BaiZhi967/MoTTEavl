"""M2-T04：C-Eval 外部数据准备与来源治理。

反例与期望：
- 损坏 hash、重复 ID、半下载（截断/空文件/缺文件）均不 ready。
- 受限官方来源：自报 approval 而无受信核验器不解锁；受信核验通过才取得
  verified-official 溯源；合成/本地自供数据始终可用（user-supplied）。
- 无 gold 分区：ready 但 unscored，样本清单如实标记，不伪造正确答案。
- Catalog 区分 registered/prepared/runnable/verified 并给出阻塞原因。
"""
import json

import pytest

from motte_benchmark.opencompass.profiles import (
    ceval_external_profile,
)
from motte_sdk.benchmark_catalog import (
    BenchmarkCatalog,
    prepare_ceval_external_dataset,
)


def _row(row_id, subject, answer="A", question="1+1=?"):
    row = {
        "id": row_id,
        "subject": subject,
        "question": question,
        "A": "1",
        "B": "2",
        "C": "3",
        "D": "4",
    }
    if answer is not None:
        row["answer"] = answer
    return row


def _jsonl(rows):
    return "\n".join(
        json.dumps(row, ensure_ascii=False) for row in rows
    ).encode("utf-8")


GOOD_FILES = {
    "logic_val.jsonl": _jsonl([_row("logic-1", "logic"), _row("logic-2", "logic", "B")]),
    "law_val.jsonl": _jsonl([_row("law-1", "law", "C")]),
}


def test_dataset_prepare_respects_source_governance():
    # 本地自供（合成）数据：可准备，provenance 是 user-supplied。
    prepared = prepare_ceval_external_dataset(
        files=GOOD_FILES, dataset_revision="rev-synthetic-1",
    )
    assert prepared.state == "ready"
    assert prepared.provenance == "user-supplied"
    assert len(prepared.manifest) == 3
    assert all(entry.has_gold for entry in prepared.manifest)
    assert prepared.unscored is False

    # 受限官方来源：自报 approval、无受信核验器 → 阻断，不得升格 official。
    blocked = prepare_ceval_external_dataset(
        files=GOOD_FILES,
        dataset_revision="rev-official-1",
        provenance_target="verified-official",
        approval_evidence={
            "actor": "operator",
            "purpose": "restricted ceval import",
            "ticket": "T-1",
            "source_id": "ceval",
            "actions": ["import"],
            "approved_by": "operator",
            "issued_at": "2026-09-19T00:00:00Z",
            "expires_at": "2027-09-19T00:00:00Z",
        },
        approval_verifier=None,
    )
    assert blocked.state == "failed"
    assert "OFFICIAL_APPROVAL_UNVERIFIED" in blocked.reasons
    assert blocked.provenance != "verified-official"

    # 受信核验器拒绝 → 同样阻断；通过 → verified-official。
    class RejectingVerifier:
        def __call__(self, source, action, evidence):
            return False

    rejected = prepare_ceval_external_dataset(
        files=GOOD_FILES,
        dataset_revision="rev-official-1",
        provenance_target="verified-official",
        approval_evidence={
            "actor": "runner-operator",
            "purpose": "restricted ceval import for external benchmark",
            "ticket": "T-2",
            "source_id": "ceval",
            "actions": ["import"],
            "approved_by": "compliance",
            "issued_at": "2026-09-19T00:00:00Z",
            "expires_at": "2027-09-19T00:00:00Z",
        },
        approval_verifier=RejectingVerifier(),
    )
    assert rejected.state == "failed"
    assert "OFFICIAL_APPROVAL_UNVERIFIED" in rejected.reasons

    class AcceptingVerifier:
        def __call__(self, source, action, evidence):
            return action == "import"

    official = prepare_ceval_external_dataset(
        files=GOOD_FILES,
        dataset_revision="rev-official-1",
        provenance_target="verified-official",
        license_evidence={
            "declared_ids": ["CC-BY-NC-SA-4.0"],
            "evidence_version": "ceval-license-v1",
            "evidence_urls": ["https://github.com/hkust-nlp/ceval/blob/main/LICENSE"],
        },
        approval_evidence={
            "actor": "runner-operator",
            "purpose": "restricted ceval import for external benchmark",
            "ticket": "T-2",
            "source_id": "ceval",
            "actions": ["import"],
            "approved_by": "compliance",
            "issued_at": "2026-09-19T00:00:00Z",
            "expires_at": "2027-09-19T00:00:00Z",
        },
        approval_verifier=AcceptingVerifier(),
    )
    assert official.state == "ready"
    assert official.provenance == "verified-official"

    # 官方路径还要求非缺失的许可证据；缺失不自动同意。
    no_license = prepare_ceval_external_dataset(
        files=GOOD_FILES,
        dataset_revision="rev-official-2",
        provenance_target="verified-official",
        approval_evidence={
            "actor": "runner-operator",
            "purpose": "restricted ceval import for external benchmark",
            "ticket": "T-2",
            "source_id": "ceval",
            "actions": ["import"],
            "approved_by": "compliance",
            "issued_at": "2026-09-19T00:00:00Z",
            "expires_at": "2027-09-19T00:00:00Z",
        },
        license_evidence=None,
        approval_verifier=AcceptingVerifier(),
    )
    assert no_license.state == "failed"
    assert "OFFICIAL_LICENSE_MISSING" in no_license.reasons


def test_prepare_rejects_corrupt_duplicate_and_partial():
    # 声明 checksum 与内容不符 → 不 ready。
    import hashlib

    law_hash = hashlib.sha256(GOOD_FILES["law_val.jsonl"]).hexdigest()
    mismatched = prepare_ceval_external_dataset(
        files=GOOD_FILES,
        dataset_revision="rev-1",
        declared_sha256={"logic_val.jsonl": "0" * 64, "law_val.jsonl": law_hash},
    )
    assert mismatched.state == "failed"
    assert any(reason.startswith("CHECKSUM_MISMATCH") for reason in mismatched.reasons)
    mismatched_files = [reason for reason in mismatched.reasons
                        if reason.startswith("CHECKSUM_MISMATCH")]
    assert mismatched_files == ["CHECKSUM_MISMATCH:logic_val.jsonl"]

    # 重复 case ID（跨学科）→ 不 ready。
    duplicated = dict(GOOD_FILES)
    duplicated["law_val.jsonl"] = _jsonl([_row("logic-1", "law")])
    duplicate = prepare_ceval_external_dataset(
        files=duplicated, dataset_revision="rev-1",
    )
    assert duplicate.state == "failed"
    assert "DUPLICATE_CASE_ID:logic-1" in duplicate.reasons

    # 半下载：截断 JSONL → 不 ready。
    truncated = dict(GOOD_FILES)
    truncated["law_val.jsonl"] = GOOD_FILES["law_val.jsonl"][:20]
    partial = prepare_ceval_external_dataset(
        files=truncated, dataset_revision="rev-1",
    )
    assert partial.state == "failed"
    assert any(reason.startswith("SOURCE_PARSE") for reason in partial.reasons)

    # 空文件 / 声明了却缺失的文件 → 不 ready。
    empty = dict(GOOD_FILES)
    empty["law_val.jsonl"] = b""
    no_rows = prepare_ceval_external_dataset(files=empty, dataset_revision="rev-1")
    assert no_rows.state == "failed"

    logic_hash = hashlib.sha256(GOOD_FILES["logic_val.jsonl"]).hexdigest()
    missing = prepare_ceval_external_dataset(
        files={"logic_val.jsonl": GOOD_FILES["logic_val.jsonl"]},
        dataset_revision="rev-1",
        declared_sha256={"logic_val.jsonl": logic_hash, "law_val.jsonl": "1" * 64},
    )
    assert missing.state == "failed"
    assert "MISSING_ARTIFACT:law_val.jsonl" in missing.reasons

    # 未知学科（不在官方 52 学科清单）→ 拒绝，不静默聚合（review R16 收紧）。
    alien = prepare_ceval_external_dataset(
        files={"alien_val.jsonl": _jsonl([_row("x-1", "not-a-ceval-subject")])},
        dataset_revision="rev-1",
    )
    assert alien.state == "failed"
    assert any(reason.startswith("UNKNOWN_SUBJECT") for reason in alien.reasons)


def test_no_gold_partition_marks_unscored():
    files = {
        "logic_test.jsonl": _jsonl([
            _row("logic-1", "logic", answer=None),
            _row("logic-2", "logic", answer=None),
        ]),
    }
    prepared = prepare_ceval_external_dataset(
        files=files, dataset_revision="rev-test-1",
    )
    assert prepared.state == "ready"
    assert prepared.unscored is True
    assert all(not entry.has_gold for entry in prepared.manifest)
    # 混合分区：有 gold 才计 gold，缺失如实标记。
    mixed = {
        "mix.jsonl": _jsonl([
            _row("m-1", "logic", answer="A"),
            _row("m-2", "logic", answer=None),
        ]),
    }
    mixed_prepared = prepare_ceval_external_dataset(
        files=mixed, dataset_revision="rev-mixed-1",
    )
    assert mixed_prepared.state == "ready"
    assert mixed_prepared.unscored is False
    by_id = {entry.case_id: entry.has_gold for entry in mixed_prepared.manifest}
    assert by_id == {"m-1": True, "m-2": False}


def test_catalog_states_and_blockers():
    catalog = BenchmarkCatalog()
    catalog.register("ceval", benchmark_version="1")
    entry = catalog.status("ceval")
    assert entry["status"] == "registered"
    assert "DATASET_UNPREPARED" in entry["blockers"]

    prepared = prepare_ceval_external_dataset(
        files=GOOD_FILES, dataset_revision="rev-synthetic-1",
    )
    catalog.update_dataset("ceval", prepared)
    entry = catalog.status("ceval")
    assert entry["status"] == "prepared"
    # Runner/Profile 尚未接通（T05/T07 负责）：runnable 仍被阻塞。
    assert "RUNNER_NOT_CONNECTED" in entry["blockers"]
    assert "PROFILE_NOT_VALIDATED" in entry["blockers"]

    catalog.mark_profile_validated("ceval", validated=True)
    assert "PROFILE_NOT_VALIDATED" not in catalog.status("ceval")["blockers"]

    with pytest.raises(KeyError):
        catalog.status("unknown-bench")


def test_profiles_pin_versions_and_legal_few_shot():
    profile = ceval_external_profile(
        dataset_revision="rev-synthetic-1",
        subjects=("logic", "math"),
        split="val",
        few_shot=5,
        few_shot_split="dev",
        seed=7,
        runner_version="opencompass-pin-1",
        environment_digest="sha256:" + "3" * 64,
    )
    assert profile["benchmark_id"] == "ceval"
    assert profile["benchmark_version"] == "1"
    assert profile["few_shot"]["count"] == 5
    assert profile["few_shot"]["source_split"] == "dev"
    assert profile["split"] == "val"
    assert profile["selected_subjects"] == ("logic", "math")

    # few-shot 示例只能来自与评测分区不同的合法分区。
    with pytest.raises(ValueError, match="few-shot"):
        ceval_external_profile(
            dataset_revision="rev-1", subjects=("logic",), split="val",
            few_shot=5, few_shot_split="val", seed=1,
            runner_version="opencompass-pin-1",
            environment_digest="sha256:" + "3" * 64,
        )
    # 占位版本不允许进入 profile。
    with pytest.raises(ValueError, match="placeholder"):
        ceval_external_profile(
            dataset_revision="rev-1", subjects=("logic",), split="val",
            few_shot=0, seed=1, runner_version="latest",
            environment_digest="sha256:" + "3" * 64,
        )
