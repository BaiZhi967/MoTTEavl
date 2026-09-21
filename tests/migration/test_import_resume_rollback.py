"""M7-T08 checkpoint / resume / 受限回退（验收 A09/A12/A13）。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from motte_sdk.migration import (
    RollbackNotConfirmed,
    apply_import,
    load_source_package,
    rollback_import,
)
from motte_storage.artifacts import ArtifactStore
from motte_storage.platform import platform_for
from motte_storage.run_store import SQLiteRunStore

from .conftest import build_legacy_export

RUN_ID = "imp-run-9001"


def _bombing_platform(monkeypatch, real_platform_for, *, fail_after: int) -> dict:
    """包装 platform_for：put_mapping 成功 fail_after 次后抛错（模拟崩溃）。

    计数器跨 platform_for 调用共享：plan 的 begin_import 照常透传，只有
    apply 的映射提交会被引爆——即"目标写入已落、账本提交标记未写"的窗口。
    """
    counter = {"commits": 0}

    class LedgerProxy:
        def __init__(self, inner):
            self._inner = inner

        def put_mapping(self, mapping):
            if counter["commits"] >= fail_after:
                raise RuntimeError("simulated crash after committed units")
            counter["commits"] += 1
            return self._inner.put_mapping(mapping)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def fake_platform_for(store):
        real = real_platform_for(store)
        return SimpleNamespace(
            requests=real.requests, meta=real.meta,
            imports=LedgerProxy(real.imports), tombstones=real.tombstones,
        )

    from motte_sdk.migration import apply as apply_module

    monkeypatch.setattr(apply_module, "platform_for", fake_platform_for)
    return counter


def test_crash_mid_apply_resumes_without_duplicates(tmp_path, export_builder, monkeypatch):
    root = export_builder(
        tmp_path / "pkg", import_id="imp-resume", with_in_flight=False, with_scores=True
    )
    source = load_source_package(root)
    store = SQLiteRunStore(tmp_path / "target.db")
    artifacts_root = tmp_path / "artifacts"
    from motte_storage.platform import platform_for as real_platform_for

    _bombing_platform(monkeypatch, real_platform_for, fail_after=7)
    with pytest.raises(RuntimeError, match="simulated crash"):
        apply_import(store, artifacts_root, source, operator="op")

    ledger = real_platform_for(store).imports
    assert len(ledger.mappings_for("imp-resume")) == 7
    # 崩溃点在第 8 单元（run）：store 写入先于账本提交 → Run 已在、映射行缺失。
    assert store.runs.get(RUN_ID) is not None
    assert len(store.runs.list()) == 1

    monkeypatch.undo()
    report = apply_import(store, artifacts_root, source, operator="op")

    # created + reused == planned，且没有重复行。
    assert report.planned == 12
    assert report.created + report.reused == 12
    assert report.pending == 0
    assert report.reused == 8  # 7 个已提交单元 + 崩溃窗口恢复的 run 单元
    assert report.verification.counts_match is True
    assert len(store.runs.list()) == 1
    assert len(store.case_runs.list_for_run(RUN_ID)) == 2
    passes = store.scoring_passes.list_for_run(RUN_ID)
    assert len(passes) == 1
    assert len(store.score_sets.list_for_pass(passes[0]["id"])) == 2
    assert ledger.get_import("imp-resume")["status"] == "applied"
    assert len(ledger.mappings_for("imp-resume")) == 12


def test_rollback_requires_explicit_confirmation(tmp_path, export_builder):
    root = export_builder(tmp_path / "pkg", import_id="imp-confirm")
    store = SQLiteRunStore(tmp_path / "target.db")
    apply_import(store, tmp_path / "artifacts", load_source_package(root), operator="op")

    with pytest.raises(RollbackNotConfirmed):
        rollback_import(store, tmp_path / "artifacts", "imp-confirm", operator="op")


def test_rollback_deletes_only_own_unreferenced_objects(tmp_path, export_builder):
    shared_payload = b"shared artifact content X\n"
    unique_payload = b"exclusive artifact content Y\n"
    root_b1 = export_builder(
        tmp_path / "b1", import_id="imp-b1", with_in_flight=False,
        artifacts=[("art-shared", shared_payload)],
    )
    # B2 用自己的 record id（同内容 sha 的另一个 artifact 记录、自己的
    # summary/baseline/run），避免与 B1 的 mapping_key 撞车 → 冲突而非创建。
    root_b2 = export_builder(
        tmp_path / "b2", import_id="imp-b2", run_id="run-9003",
        summary_id="sum-9003", baseline_id="bl-9003", with_in_flight=False,
        artifacts=[("art-shared2", shared_payload), ("art-uniq", unique_payload)],
    )
    store = SQLiteRunStore(tmp_path / "target.db")
    artifacts_root = tmp_path / "artifacts"
    apply_import(store, artifacts_root, load_source_package(root_b1), operator="op")
    report_b2 = apply_import(store, artifacts_root, load_source_package(root_b2), operator="op")
    assert report_b2.created == 5
    assert store.runs.get("imp-run-9003") is not None

    result = rollback_import(store, artifacts_root, "imp-b2", operator="op", confirm=True)

    shared_b2 = "imports/imp-b2/art-shared2"
    unique_b2 = "imports/imp-b2/art-uniq"
    # 共享内容（B1 的 run 仍引用同一 sha）→ 阻断保留；独占内容 → 删除 + tombstone。
    assert ArtifactStore(artifacts_root).read_bytes(shared_b2) == shared_payload
    assert not (artifacts_root / "imports" / "imp-b2" / "art-uniq").exists()
    assert {"object": shared_b2, "reason": "artifact_in_use"} in result["blocked"]
    assert result["deleted_artifacts"] == [unique_b2]
    assert result["inactive_reference_mappings"] == 2

    # B1 完全不受影响。
    run_b1 = store.runs.get(RUN_ID)
    assert run_b1["status"] == "completed"
    assert "rolled_back_import" not in run_b1
    assert ArtifactStore(artifacts_root).read_bytes(
        "imports/imp-b1/art-shared") == shared_payload

    # B2 的 run 被停用注记，但终态保持（绝不回 queued）。
    run_b2 = store.runs.get("imp-run-9003")
    assert run_b2["status"] == "completed"
    assert run_b2["rolled_back_import"]["import_id"] == "imp-b2"

    # 审计轨迹：账本行保留、批次 rolled_back、tombstone 可查。
    ledger = platform_for(store).imports
    assert ledger.get_import("imp-b2")["status"] == "rolled_back"
    assert len(ledger.mappings_for("imp-b2")) == report_b2.created
    tombstones = platform_for(store).tombstones.list()
    assert any(entry["artifact_id"] == unique_b2 for entry in tombstones)
    assert any(entry["gc_run_id"] == "import-rollback:imp-b2" for entry in tombstones)
    assert result["kept_mapping_rows"] == report_b2.created
    assert result["deactivated_runs"] == ["imp-run-9003"]


def test_rollback_of_missing_batch_raises(tmp_path):
    store = SQLiteRunStore(tmp_path / "target.db")
    with pytest.raises(KeyError):
        rollback_import(store, tmp_path / "artifacts", "imp-nope",
                        operator="op", confirm=True)


def test_rollback_is_idempotent_and_keeps_terminal_status(tmp_path, export_builder):
    root = export_builder(tmp_path / "pkg", import_id="imp-twice", with_in_flight=False)
    store = SQLiteRunStore(tmp_path / "target.db")
    artifacts_root = tmp_path / "artifacts"
    apply_import(store, artifacts_root, load_source_package(root), operator="op")

    first = rollback_import(store, artifacts_root, "imp-twice", operator="op", confirm=True)
    second = rollback_import(store, artifacts_root, "imp-twice", operator="op", confirm=True)

    assert first["status"] == second["status"] == "rolled_back"
    assert second["deactivated_runs"] == first["deactivated_runs"] == [RUN_ID]
    run = store.runs.get(RUN_ID)
    assert run["status"] == "completed"
    assert run["rolled_back_import"]["import_id"] == "imp-twice"
    # 账本行保留（审计不删除），批次终态一致。
    ledger = platform_for(store).imports
    assert len(ledger.mappings_for("imp-twice")) == first["kept_mapping_rows"]
