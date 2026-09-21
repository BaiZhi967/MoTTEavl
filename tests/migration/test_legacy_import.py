"""M7-T07 apply：历史保真、幂等与冲突语义（验收 A10）。"""
from __future__ import annotations

import hashlib
import json

import pytest

from motte_sdk.migration import (
    SourceChangedError,
    apply_import,
    load_source_package,
    plan_import,
)
from motte_storage.artifacts import ArtifactStore
from motte_storage.run_store import SQLiteRunStore

from .conftest import ARTIFACT_PAYLOAD, build_legacy_export, write_json

RUN_ID = "imp-run-9001"


def test_full_apply_of_completed_legacy_run(legacy_source, tmp_path):
    store = SQLiteRunStore(tmp_path / "target.db")
    artifacts_root = tmp_path / "artifacts"

    report = apply_import(store, artifacts_root, legacy_source, operator="accept")

    assert report.planned == 11
    assert report.created == 10
    assert report.rejected == 1
    assert report.pending == 0
    assert report.verification.counts_match is True
    assert report.verification.hashes_match is True
    assert report.verification.references_ok is True

    run = store.runs.get(RUN_ID)
    assert run is not None and run["status"] == "completed"
    source_block = run["manifest"]["import_source"]
    assert source_block["system"] == "legacy-eval"
    assert source_block["record_id"] == "run-9001"
    assert source_block["import_id"] == legacy_source.manifest.import_id
    assert source_block["content_hash"].startswith("sha256:")
    assert run["manifest"]["origin"] == "imported"
    assert run["manifest"]["distributable"] is False
    # 原始字段（含 unknown 字段）整体保留在 manifest.legacy。
    assert run["manifest"]["legacy"]["legacy_note"] == "hand-tuned legacy run"

    # 缺 reported model / gold / pass / price → 显式 unknown，绝不从当前配置补写。
    assert run["reported_model"] == "unknown"
    assert run["manifest"]["model"] == "unknown"
    assert run["gold"] == "unknown"
    assert run["price"] == "unknown"
    assert run["manifest"]["evaluation"]["scoring_pass"] == "unknown"

    # A10：2 题 + 聚合 summary，零 score_sets，绝不伪造逐题分。
    assert run["case_ids"] == ["case-101", "case-102"]
    case_rows = store.case_runs.list_for_run(RUN_ID)
    assert [row["case_id"] for row in case_rows] == ["case-101", "case-102"]
    assert all(row["outcome"] == "imported" for row in case_rows)
    assert store.scoring_passes.list_for_run(RUN_ID) == []
    assert run["legacy_summary"]["score"] == 0.5
    assert run["legacy_summary"]["per_case_scores_available"] is False

    events = store.events.list_for_run(RUN_ID)
    assert [event["type"] for event in events][:2] == ["created", "run_imported"]

    # artifact：hash 校验后提交，可经 ArtifactStore 读回。
    sha = hashlib.sha256(ARTIFACT_PAYLOAD).hexdigest()
    artifact_id = f"imports/{legacy_source.manifest.import_id}/art-out"
    assert run["artifact_refs"] == [{"artifact_id": artifact_id, "sha256": sha,
                                     "available": True}]
    assert hashlib.sha256(ArtifactStore(artifacts_root).read_bytes(artifact_id)).hexdigest() \
        == sha

    # 旧 baseline 只注记在 run payload，不进 M6 baseline_store、不碰默认指针。
    assert store.baseline_store.list() == []
    assert store.baseline_store.get_default("global") is None
    assert run["legacy_baseline"]["name"] == "legacy-baseline-q3"
    assert run["legacy_baseline"]["metrics"] == {"accuracy": 0.5}

    # 进行中旧 Job 不迁移。
    assert store.runs.get("imp-run-9002") is None
    assert any("in_flight_job_not_importable" in item for item in report.diagnostics)

    # 凭据只列 rebind 引用名。
    assert report.credentials_to_rebind == ("LEGACY_OPENAI_API_KEY",)

    # imported Run 不可被 Dispatcher 领取（终态 + import_source 双保险）。
    from motte_sdk.dispatcher import RunDispatcher
    from motte_sdk.service import RunService

    assert RunDispatcher(RunService(store)).claim() is None
    assert store.runs.get(RUN_ID)["status"] == "completed"


def test_second_apply_of_same_source_is_all_reused(legacy_source, tmp_path):
    store = SQLiteRunStore(tmp_path / "target.db")
    apply_import(store, tmp_path / "artifacts", legacy_source, operator="op")

    fresh = load_source_package(legacy_source.root)
    report = apply_import(store, tmp_path / "artifacts", fresh, operator="op")

    assert report.created == 0
    assert report.reused == 10
    assert report.planned == 11
    assert len(store.runs.list()) == 1
    assert len(store.case_runs.list_for_run(RUN_ID)) == 2
    assert store.scoring_passes.list_for_run(RUN_ID) == []


def test_score_records_attach_to_a_single_legacy_pass(tmp_path):
    root = build_legacy_export(
        tmp_path / "pkg", import_id="imp-scores", with_in_flight=False, with_scores=True
    )
    source = load_source_package(root)
    store = SQLiteRunStore(tmp_path / "target.db")
    report = apply_import(store, tmp_path / "artifacts", source, operator="op")

    assert report.planned == 12  # 9 条基础单元 + 1 artifact + 2 条逐题 score
    assert report.created == 12
    passes = store.scoring_passes.list_for_run(RUN_ID)
    assert len(passes) == 1
    pass_id = passes[0]["id"]
    rows = store.score_sets.list_for_pass(pass_id)
    assert sorted(row["case_id"] for row in rows) == ["case-101", "case-102"]
    assert sorted(row["value"] for row in rows) == [0.0, 1.0]
    run = store.runs.get(RUN_ID)
    assert run["current_scoring_pass_id"] == pass_id
    assert run["status"] == "completed"  # 评分导入不改终态
    assert any(event["type"] == "legacy_scores_imported"
               for event in store.events.list_for_run(RUN_ID))


def test_same_id_different_content_conflicts(export_builder, tmp_path):
    root = export_builder(tmp_path / "pkg", import_id="imp-conflict")
    store = SQLiteRunStore(tmp_path / "target.db")
    apply_import(store, tmp_path / "artifacts", load_source_package(root), operator="op")

    record_path = root / "records" / "run" / "run-9001.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["legacy_note"] = "mutated after first import"
    write_json(record_path, record)

    report = apply_import(
        store, tmp_path / "artifacts", load_source_package(root), operator="op"
    )

    assert report.conflicted == 1
    assert report.created == 0
    assert report.reused == 9
    assert any("mapping_content_conflict" in item for item in report.diagnostics)
    # 目标不被覆盖：仍是第一次导入的内容。
    run = store.runs.get(RUN_ID)
    assert run["manifest"]["legacy"]["legacy_note"] == "hand-tuned legacy run"
    assert len(store.runs.list()) == 1


def test_source_changed_between_plan_and_apply_invalidates_plan(export_builder, tmp_path):
    root = export_builder(tmp_path / "pkg", import_id="imp-changed")
    source = load_source_package(root)
    store = SQLiteRunStore(tmp_path / "target.db")
    plan = plan_import(store, source, operator="op")

    record_path = root / "records" / "case" / "case-101.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["input"] = {"prompt": "tampered"}
    write_json(record_path, record)

    with pytest.raises(SourceChangedError):
        apply_import(store, tmp_path / "artifacts", source, operator="op",
                     plan_report=plan)
    # 计划作废后目标零业务写入。
    assert store.runs.list() == []


def test_artifact_hash_mismatch_rejects_unit_without_committing_reference(tmp_path):
    root = build_legacy_export(tmp_path / "pkg", import_id="imp-badhash")
    # 声明的 sha 与工件真实内容不一致：篡改 artifact 记录的 sha256。
    record_path = root / "records" / "artifact" / "art-out.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["sha256"] = "cd" * 32
    write_json(record_path, record)
    # 同步换文件位置，保证"文件存在但内容不匹配"。
    artifacts_dir = root / "artifacts"
    real_sha = hashlib.sha256(ARTIFACT_PAYLOAD).hexdigest()
    (artifacts_dir / "cd"[:2]).mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "cd" / ("cd" * 32)).write_bytes(ARTIFACT_PAYLOAD + b"tampered")

    source = load_source_package(root)
    store = SQLiteRunStore(tmp_path / "target.db")
    report = apply_import(store, tmp_path / "artifacts", source, operator="op")

    assert any("artifact_hash_mismatch" in item for item in report.diagnostics)
    assert not (tmp_path / "artifacts" / "imports" / "imp-badhash" / "art-out").exists()
    run = store.runs.get(RUN_ID)
    # run 的 artifact 引用未提交（该 run 声明的真实 sha 没有对应记录）。
    declared = {entry["sha256"] for entry in run["manifest"]["legacy"]["artifacts"]}
    assert real_sha in declared
    assert run["artifact_refs"] == []
    assert any("reference not committed" in warning for warning in report.warnings)
