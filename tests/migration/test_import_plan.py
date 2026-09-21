"""M7-T06 dry-run：零目标修改、复用检测与来源安全边界（验收 A08/A11）。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from motte_sdk.migration import (
    SourcePackageError,
    apply_import,
    load_source_package,
    plan_import,
)
from motte_storage.platform import platform_for
from motte_storage.run_store import SQLiteRunStore

from .conftest import FIXTURE_ROOT, build_legacy_export, write_json

#: 业务表（迁移禁止 dry-run 触碰）；表名来自固定清单，非外部输入。
BUSINESS_TABLES = (
    "runs", "case_runs", "trace_events", "scores", "scoring_passes", "score_sets",
    "case_attempts", "m6_baselines", "default_baselines", "run_commands",
    "agent_invocations",
)


def business_state(db_path: Path) -> dict[str, list[tuple]]:
    with sqlite3.connect(str(db_path)) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in BUSINESS_TABLES
        }


class _NoReadResourceStore:
    """任何属性读取都爆炸的哨兵：证明 dry-run 绝不读资源库补写历史。"""

    def __getattr__(self, name: str):
        raise AssertionError(f"resource store must not be read during planning: {name}")


def test_plan_is_a_pure_dry_run(legacy_source, tmp_path):
    store = SQLiteRunStore(tmp_path / "target.db")
    before = business_state(tmp_path / "target.db")

    report = plan_import(
        store, legacy_source, operator="tester", resource_store=_NoReadResourceStore()
    )

    assert business_state(tmp_path / "target.db") == before
    # 唯一写入是导入账本批次行（平台审计表，不是业务数据）。
    ledger = platform_for(store).imports
    batches = ledger.list_imports()
    assert [batch["import_id"] for batch in batches] == [report.import_id]
    assert batches[0]["status"] == "planned"
    assert batches[0]["operator"] == "tester"

    # 计数：fixture 有 11 条记录单元，其中 run-9002 为进行中 → rejected。
    assert report.planned == 11
    assert report.created == 0
    assert report.pending == 10
    assert report.rejected == 1
    assert report.reused == 0
    assert report.conflicted == 0
    assert report.missing_artifacts == ()
    assert "run/run-9001#legacy_note" in report.unknown_fields
    assert report.credentials_to_rebind == ("LEGACY_OPENAI_API_KEY",)
    assert any("in_flight_job_not_importable" in item for item in report.diagnostics)
    assert any("license restricted" in item for item in report.warnings)
    assert report.verification.counts_match is True
    assert any("audit-only" in note for note in report.notes)


def test_replan_after_apply_reports_all_reused(legacy_source, tmp_path):
    store = SQLiteRunStore(tmp_path / "target.db")
    apply_import(store, tmp_path / "artifacts", legacy_source, operator="op")

    fresh = load_source_package(FIXTURE_ROOT)
    report = plan_import(store, fresh, operator="op")

    assert report.planned == 11
    assert report.reused == 10
    assert report.pending == 0
    assert report.created == 0
    assert report.rejected == 1
    assert report.conflicted == 0


def test_missing_artifact_secret_field_and_values_never_leak(tmp_path):
    missing_sha = "ab" * 32
    root = build_legacy_export(
        tmp_path / "pkg", import_id="imp-a11", missing_artifact_shas=[missing_sha]
    )
    record_path = root / "records" / "dataset" / "ds-leg.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["api_key"] = "sk-live-do-not-leak-123"
    write_json(record_path, record)

    source = load_source_package(root)
    store = SQLiteRunStore(tmp_path / "target.db")
    report = plan_import(store, source, operator="tester")

    assert report.missing_artifacts == (f"run/run-9001:{missing_sha}",)
    # secret 字段拒绝的是整条 record；诊断只带字段名，绝不含字段值。
    assert any("secret_field_rejected" in item for item in report.diagnostics)
    assert "sk-live-do-not-leak-123" not in report.model_dump_json()
    assert report.rejected == 2  # secret record + in-flight run
    # dataset 被拒后仍在 unknown/reused 语义之外：planned 计入全部单元。
    assert report.planned == 11


def _build_tmp_package(tmp_path: Path, import_id: str = "imp-bad") -> Path:
    return build_legacy_export(tmp_path / "pkg", import_id=import_id)


def test_unsafe_path_references_rejected(tmp_path):
    for bad in ("../../etc/passwd", "/etc/passwd", "C:/Windows/system32/evil.dat",
                "\\\\server\\share\\evil.dat"):
        root = _build_tmp_package(tmp_path / _sanitize(bad))
        record_path = root / "records" / "scenario" / "scen-leg.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["config"] = {"source_path": bad}
        write_json(record_path, record)
        with pytest.raises(SourcePackageError) as error:
            load_source_package(root)
        assert error.value.code == "unsafe_path_reference"


def _sanitize(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value)[:40]


def test_symlink_rejected(tmp_path):
    root = _build_tmp_package(tmp_path)
    outside = tmp_path / "outside-secret.json"
    outside.write_text("{}", encoding="utf-8")
    link = root / "records" / "case" / "case-evil.json"
    try:
        link.symlink_to(outside)
    except OSError:  # pragma: no cover - 平台无 symlink 权限时跳过
        pytest.skip("symlinks are not available on this platform")
    with pytest.raises(SourcePackageError) as error:
        load_source_package(root)
    assert error.value.code == "symlink_in_package"


def test_oversize_record_rejected(tmp_path):
    root = _build_tmp_package(tmp_path)
    payload = {"id": "case-huge", "dataset_id": "ds-leg", "input": {"blob": "x" * (5 << 20)}}
    write_json(root / "records" / "case" / "case-huge.json", payload)
    with pytest.raises(SourcePackageError) as error:
        load_source_package(root)
    assert error.value.code == "record_too_large"


def test_package_quotas_enforced(tmp_path, monkeypatch):
    from motte_sdk.migration import sources as sources_module

    root = _build_tmp_package(tmp_path)
    monkeypatch.setattr(sources_module, "MAX_PACKAGE_FILES", 3)
    with pytest.raises(SourcePackageError) as error:
        load_source_package(root)
    assert error.value.code == "too_many_files"

    monkeypatch.setattr(sources_module, "MAX_PACKAGE_FILES", 20_000)
    monkeypatch.setattr(sources_module, "MAX_PACKAGE_BYTES", 16)
    with pytest.raises(SourcePackageError) as error:
        load_source_package(root)
    assert error.value.code == "package_too_large"

    monkeypatch.setattr(sources_module, "MAX_PACKAGE_BYTES", 512 * 1024 * 1024)
    monkeypatch.setattr(sources_module, "MAX_RECORDS", 2)
    with pytest.raises(SourcePackageError) as error:
        load_source_package(root)
    assert error.value.code == "too_many_records"


def test_manifest_failures_are_structured(tmp_path):
    root = _build_tmp_package(tmp_path)
    (root / "manifest.json").unlink()
    with pytest.raises(SourcePackageError) as error:
        load_source_package(root)
    assert error.value.code == "manifest_missing"

    root = _build_tmp_package(tmp_path / "again")
    (root / "manifest.json").write_text(json.dumps({"import_id": 1}), encoding="utf-8")
    with pytest.raises(SourcePackageError) as error:
        load_source_package(root)
    assert error.value.code == "invalid_manifest"

    with pytest.raises(SourcePackageError) as error:
        load_source_package(tmp_path / "not-a-package")
    assert error.value.code == "package_not_found"


def test_report_carries_counts_and_hash(legacy_source, tmp_path):
    store = SQLiteRunStore(tmp_path / "target.db")
    report = plan_import(store, legacy_source, operator="tester")
    assert report.content_sha256 == legacy_source.content_sha256
    assert report.content_sha256.startswith("sha256:")
    assert report.import_id == legacy_source.manifest.import_id
    assert report.source_system == "legacy-eval"
