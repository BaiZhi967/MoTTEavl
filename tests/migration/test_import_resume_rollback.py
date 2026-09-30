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

from tests.security.test_gc_retention import (
    REFERENCE_LOCATIONS,
    REFERENCE_SHAPES,
    _artifact_reference,
    _store_reference,
)

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


@pytest.mark.parametrize("shape", REFERENCE_SHAPES)
@pytest.mark.parametrize("location", REFERENCE_LOCATIONS)
def test_rollback_preserves_other_repository_reference(tmp_path, export_builder, shape, location):
    import hashlib

    content = b"shared imported evidence"
    root = export_builder(
        tmp_path / "pkg", import_id="imp-shared", with_in_flight=False,
        artifacts=[("shared", content), ("unique", b"exclusive evidence")],
    )
    store = SQLiteRunStore(tmp_path / "target.db")
    artifacts_root = tmp_path / "artifacts"
    apply_import(store, artifacts_root, load_source_package(root), operator="op")
    artifact_id = "imports/imp-shared/shared"
    store.runs.create({
        "id": "external", "status": "needs_review", "scenario_version": "replay@1",
        "revision": 1, "manifest": {}, "case_ids": [],
    })
    payload = _artifact_reference(shape, artifact_id, hashlib.sha256(content).hexdigest())
    _store_reference(store, location, "external", payload)

    result = rollback_import(store, artifacts_root, "imp-shared", operator="op", confirm=True)

    assert {"object": artifact_id, "reason": "artifact_in_use"} in result["blocked"]
    assert (artifacts_root / artifact_id).read_bytes() == content
    assert result["deleted_artifacts"] == ["imports/imp-shared/unique"]
    assert store.runs.get("external")["status"] == "needs_review"
    assert artifact_id not in {row["artifact_id"] for row in platform_for(store).tombstones.list()}


def test_rollback_reference_check_and_deletion_share_maintenance_barrier(
    tmp_path, export_builder, monkeypatch,
):
    from motte_sdk.migration import rollback as rollback_module

    source = export_builder(tmp_path / "pkg", import_id="imp-locked", with_in_flight=False)
    store = SQLiteRunStore(tmp_path / "target.db")
    artifacts_root = tmp_path / "artifacts"
    apply_import(store, artifacts_root, load_source_package(source), operator="op")
    observed = []
    original_refs = rollback_module.referenced_artifact_hashes
    original_delete = ArtifactStore.delete

    def scan(*args, **kwargs):
        observed.append(("scan", platform_for(store).meta.get("maintenance_owner")))
        return original_refs(*args, **kwargs)

    def delete(self, artifact_id, **kwargs):
        observed.append(("delete", platform_for(store).meta.get("maintenance_owner")))
        return original_delete(self, artifact_id, **kwargs)

    monkeypatch.setattr(rollback_module, "referenced_artifact_hashes", scan)
    monkeypatch.setattr(ArtifactStore, "delete", delete)

    result = rollback_import(store, artifacts_root, "imp-locked", operator="op", confirm=True)

    assert [stage for stage, _ in observed] == ["scan", "delete"]
    assert observed[0][1] is not None
    assert observed[0][1] == observed[1][1]
    assert result["deactivated_runs"] == [RUN_ID]
    assert platform_for(store).meta.get("maintenance") is None


def test_rollback_preserves_replaced_content_with_digest_only_reference(tmp_path, export_builder):
    source = export_builder(tmp_path / "pkg", import_id="imp-replaced", with_in_flight=False)
    store = SQLiteRunStore(tmp_path / "target.db")
    artifacts_root = tmp_path / "artifacts"
    apply_import(store, artifacts_root, load_source_package(source), operator="op")
    artifact = ArtifactStore(artifacts_root).put_bytes(
        "imports/imp-replaced/art-out", b"replacement evidence",
    )
    store.runs.create({
        "id": "external", "status": "completed", "scenario_version": "replay@1",
        "revision": 1, "manifest": {}, "case_ids": [],
    })
    _store_reference(store, "case", "external", {"artifacts": [{"sha256": artifact.sha256}]})

    result = rollback_import(store, artifacts_root, "imp-replaced", operator="op", confirm=True)

    assert (artifacts_root / artifact.id).read_bytes() == b"replacement evidence"
    assert {"object": artifact.id, "reason": "artifact_in_use"} in result["blocked"]
    assert result["deleted_artifacts"] == []


def test_rollback_does_not_exclude_an_unmapped_run_claiming_same_import(tmp_path, export_builder):
    source = export_builder(tmp_path / "pkg", import_id="imp-owned", with_in_flight=False)
    store = SQLiteRunStore(tmp_path / "target.db")
    artifacts_root = tmp_path / "artifacts"
    apply_import(store, artifacts_root, load_source_package(source), operator="op")
    artifact_id = "imports/imp-owned/art-out"
    store.runs.create({
        "id": "imp-unmapped", "status": "needs_review", "scenario_version": "replay@1",
        "revision": 1, "manifest": {"import_source": {"import_id": "imp-owned"}},
        "case_ids": [], "artifact_refs": [{"artifact_id": artifact_id}],
    })

    result = rollback_import(store, artifacts_root, "imp-owned", operator="op", confirm=True)

    assert {"object": artifact_id, "reason": "artifact_in_use"} in result["blocked"]
    assert (artifacts_root / artifact_id).exists()
    assert "rolled_back_import" not in store.runs.get("imp-unmapped")


def test_rollback_fails_closed_if_reference_repository_cannot_be_read(
    tmp_path, export_builder, monkeypatch,
):
    source = export_builder(tmp_path / "pkg", import_id="imp-scan-fail", with_in_flight=False)
    store = SQLiteRunStore(tmp_path / "target.db")
    artifacts_root = tmp_path / "artifacts"
    apply_import(store, artifacts_root, load_source_package(source), operator="op")

    def unavailable(*args, **kwargs):
        raise RuntimeError("reference repository unavailable")

    monkeypatch.setattr(store.gate_store, "list_results", unavailable)
    with pytest.raises(RuntimeError, match="reference repository unavailable"):
        rollback_import(store, artifacts_root, "imp-scan-fail", operator="op", confirm=True)

    assert (artifacts_root / "imports/imp-scan-fail/art-out").exists()
    assert "rolled_back_import" not in store.runs.get(RUN_ID)
    assert platform_for(store).tombstones.list() == []
    assert platform_for(store).meta.get("maintenance") is None


def test_rollback_crash_after_unlink_preserves_intent_without_replay_confirmation(
    tmp_path, export_builder, monkeypatch,
):
    class CrashAfterDelete(BaseException):
        pass

    source = export_builder(tmp_path / "pkg", import_id="imp-crash", with_in_flight=False)
    store = SQLiteRunStore(tmp_path / "target.db")
    artifacts_root = tmp_path / "artifacts"
    apply_import(store, artifacts_root, load_source_package(source), operator="op")
    artifact_id = "imports/imp-crash/art-out"
    original_delete = ArtifactStore.delete

    def crash_after_delete(self, *args, **kwargs):
        original_delete(self, *args, **kwargs)
        raise CrashAfterDelete()

    monkeypatch.setattr(ArtifactStore, "delete", crash_after_delete)
    with pytest.raises(CrashAfterDelete):
        rollback_import(store, artifacts_root, "imp-crash", operator="op", confirm=True)
    assert not (artifacts_root / artifact_id).exists()
    audit = platform_for(store).tombstones.list()[0]
    assert audit["deletion_status"] == "deleting"
    assert "deleted_at" not in audit

    monkeypatch.undo()
    result = rollback_import(store, artifacts_root, "imp-crash", operator="op", confirm=True)
    assert result["deleted_artifacts"] == []
    assert {"object": artifact_id, "reason": "absence_unconfirmed"} in result["skipped_artifacts"]
    audit = platform_for(store).tombstones.list()[0]
    assert audit["deletion_status"] == "absence_unconfirmed"
    assert "deleted_at" not in audit


@pytest.mark.parametrize("reference_kind", ["baseline", "gate", "experiment_cell", "superseding_cell"])
def test_rollback_preserves_artifacts_transitively_pinned_by_other_resources(
    tmp_path, export_builder, reference_kind,
):
    source = export_builder(tmp_path / "pkg", import_id="imp-pinned", with_in_flight=False)
    store = SQLiteRunStore(tmp_path / "target.db")
    artifacts_root = tmp_path / "artifacts"
    apply_import(store, artifacts_root, load_source_package(source), operator="op")
    ref = {"run_id": RUN_ID, "scoring_pass_id": "frozen-pass", "report_schema": "run-report@2",
           "evidence_hash": "sha256:baseline"}
    if reference_kind == "baseline":
        baseline = store.baseline_store.put({
            "baseline_id": "independent", "entries": [{"cell_key": None, "ref": ref}],
            "comparison_policy_hash": "sha256:policy", "eligibility": "formal",
            "reason": "retain historical evidence", "created_by": "operator",
            "created_at": "2026-09-30T00:00:00Z", "metrics": {},
        })
    elif reference_kind == "gate":
        store.gate_store.put_result({
            "gate_result_id": "independent", "policy_id": "policy", "refs": {"baseline": ref},
        })
    elif reference_kind == "experiment_cell":
        _store_reference(store, "experiment_cell", None, {"baseline": ref})
    else:
        store.experiments.publish_spec_and_cells(
            {"experiment_id": "ref-experiment", "version": "1"},
            [{"cell_id": "pending-cell", "experiment_id": "ref-experiment",
              "experiment_version": "1", "allocation_status": "allocated", "repeat_index": 0,
              "factor_assignment": {}, "run_id": "unrelated", "superseding_run_ids": [RUN_ID]}],
        )

    result = rollback_import(store, artifacts_root, "imp-pinned", operator="op", confirm=True)

    artifact_id = "imports/imp-pinned/art-out"
    assert (artifacts_root / artifact_id).exists()
    assert {"object": artifact_id, "reason": "artifact_in_use"} in result["blocked"]
    assert {"object": RUN_ID, "reason": "run_in_use"} in result["blocked"]
    assert "rolled_back_import" not in store.runs.get(RUN_ID)
    if reference_kind == "baseline":
        assert store.baseline_store.get("independent") == baseline


@pytest.mark.parametrize("reference_state", ["none", "foreign", "foreign_child", "foreign_score", "unconfirmed"])
def test_rollback_backup_archives_only_confirmed_inactive_own_references(
    tmp_path, export_builder, reference_state,
):
    from motte_storage.maintenance import BackupIncomplete, consistent_backup

    source = export_builder(tmp_path / "pkg", import_id="imp-archive", with_in_flight=False)
    store = SQLiteRunStore(tmp_path / "target.db")
    artifacts_root = tmp_path / "artifacts"
    apply_import(store, artifacts_root, load_source_package(source), operator="op")
    rollback_import(store, artifacts_root, "imp-archive", operator="op", confirm=True)
    artifact_id = "imports/imp-archive/art-out"
    mappings_before = platform_for(store).imports.mappings_for("imp-archive")
    inactive_before = store.runs.get(RUN_ID)
    audit = platform_for(store).tombstones.list()[0]
    if reference_state == "foreign":
        store.runs.create({
            "id": "foreign", "status": "needs_review", "scenario_version": "replay@1",
            "revision": 1, "manifest": {"raw_ref": artifact_id}, "case_ids": [],
        })
    elif reference_state == "foreign_child":
        _store_reference(store, "invocation", RUN_ID, {"raw_ref": artifact_id})
    elif reference_state == "foreign_score":
        store.scores.replace_for_run(RUN_ID, [
            {"case_id": "new", "value": 1, "details": {"raw_ref": artifact_id}},
        ])
    elif reference_state == "unconfirmed":
        audit.pop("deleted_at", None)
        platform_for(store).tombstones.append([{**audit, "deletion_status": "absence_unconfirmed"}])

    if reference_state == "none":
        manifest = consistent_backup(store, tmp_path / "backups", artifacts_root=artifacts_root)
        assert manifest["status"] == "complete"
    else:
        with pytest.raises(BackupIncomplete):
            consistent_backup(store, tmp_path / "backups", artifacts_root=artifacts_root)
    assert platform_for(store).imports.mappings_for("imp-archive") == mappings_before
    assert store.runs.get(RUN_ID) == inactive_before


def test_rollback_preserves_foreign_child_on_its_own_imported_run(tmp_path, export_builder):
    source = export_builder(tmp_path / "pkg", import_id="imp-new-child", with_in_flight=False)
    store = SQLiteRunStore(tmp_path / "target.db")
    artifacts_root = tmp_path / "artifacts"
    apply_import(store, artifacts_root, load_source_package(source), operator="op")
    artifact_id = "imports/imp-new-child/art-out"
    _store_reference(store, "invocation", RUN_ID, {"raw_ref": artifact_id})

    result = rollback_import(store, artifacts_root, "imp-new-child", operator="op", confirm=True)

    assert (artifacts_root / artifact_id).exists()
    assert {"object": RUN_ID, "reason": "run_in_use"} in result["blocked"]
    assert "rolled_back_import" not in store.runs.get(RUN_ID)


def test_rollback_never_deletes_foreign_target_through_import_namespace_symlink(
    tmp_path, export_builder,
):
    source = export_builder(tmp_path / "pkg", import_id="imp-symlink", with_in_flight=False)
    store = SQLiteRunStore(tmp_path / "target.db")
    artifacts_root = tmp_path / "artifacts"
    apply_import(store, artifacts_root, load_source_package(source), operator="op")
    artifact_id = "imports/imp-symlink/art-out"
    imported_path = artifacts_root / artifact_id
    content = imported_path.read_bytes()
    foreign = ArtifactStore(artifacts_root).put_bytes("foreign/existing.bin", content)
    imported_path.unlink()
    imported_path.symlink_to(artifacts_root / foreign.id)

    result = rollback_import(store, artifacts_root, "imp-symlink", operator="op", confirm=True)

    assert (artifacts_root / foreign.id).read_bytes() == content
    assert imported_path.is_symlink()
    assert {"object": artifact_id, "reason": "foreign_object"} in result["blocked"]
    assert result["deleted_artifacts"] == []
    assert platform_for(store).tombstones.list() == []


def test_report_pins_survive_owned_import_exclusion(tmp_path, export_builder):
    from motte_storage import artifact_refs
    from tests.storage.test_statistical_report_references import put_report

    store = SQLiteRunStore(tmp_path / "reports.db")
    artifacts = tmp_path / "artifacts"
    runs, passes = [], []
    for name in ("baseline", "candidate", "control"):
        source = export_builder(
            tmp_path / name, import_id=f"imp-{name}", run_id=f"run-{name}",
            source_system=f"legacy-{name}",
            summary_id=f"sum-{name}", baseline_id=f"bl-{name}", with_in_flight=False,
            with_scores=True, artifacts=[(f"art-{name}", name.encode())],
        )
        apply_import(store, artifacts, load_source_package(source), operator="test")
        runs.append(f"imp-run-{name}")
        passes.append(store.scoring_passes.list_for_run(runs[-1])[0]["id"])
    report = put_report(store, runs=runs[:2], passes=passes[:2])
    options = {"exclude_run_ids": runs, "exclude_import_ids": {
        "imp-baseline", "imp-candidate", "imp-control"}}
    assert set(runs[:2]) <= artifact_refs.referenced_run_ids(store, **options)
    assert set(passes[:2]) <= artifact_refs.referenced_pass_ids(store, **options)
    for name, run_id in zip(("baseline", "candidate"), runs[:2], strict=True):
        before = store.runs.get(run_id)
        result = rollback_import(store, artifacts, f"imp-{name}", operator="test", confirm=True)
        artifact_id = f"imports/imp-{name}/art-{name}"
        assert {"object": run_id, "reason": "run_in_use"} in result["blocked"]
        assert {"object": artifact_id, "reason": "artifact_in_use"} in result["blocked"]
        assert result["deleted_artifacts"] == result["deactivated_runs"] == []
        assert store.runs.get(run_id) == before
        assert (artifacts / artifact_id).read_bytes() == name.encode()
    control = rollback_import(store, artifacts, "imp-control", operator="test", confirm=True)
    assert control["deactivated_runs"] == [runs[2]]
    assert control["deleted_artifacts"] == ["imports/imp-control/art-control"]
    assert not (artifacts / "imports/imp-control/art-control").exists()
    assert store.statistical_reports.get(report["report_id"]) == report


@pytest.mark.parametrize("failure", ["integrity", "list", "get", "conflict"])
def test_report_reader_failure_aborts_rollback_without_mutation(tmp_path, export_builder, monkeypatch, failure):
    from tests.storage.test_statistical_report_references import put_report, damage_report_reader
    from motte_storage.maintenance import maintenance_status

    store = SQLiteRunStore(tmp_path / "report-failure.db")
    source = export_builder(tmp_path / "source", import_id="imp-failure", with_in_flight=False)
    artifacts = tmp_path / "artifacts"
    apply_import(store, artifacts, load_source_package(source), operator="test")
    put_report(store, runs=(RUN_ID, "other-run"))
    ledger = platform_for(store).imports
    before = store.runs.get(RUN_ID), ledger.get_import("imp-failure"), ledger.mappings_for("imp-failure")
    error = damage_report_reader(store, monkeypatch, failure)
    with pytest.raises(error):
        rollback_import(store, artifacts, "imp-failure", operator="test", confirm=True)
    assert (store.runs.get(RUN_ID), ledger.get_import("imp-failure"),
            ledger.mappings_for("imp-failure")) == before
    assert (artifacts / "imports/imp-failure/art-out").exists()
    assert platform_for(store).tombstones.list() == []
    assert maintenance_status(store)["active"] is False
