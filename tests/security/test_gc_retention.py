"""M7 GC/retention 测试：pin 保护、dry-run 默认、tombstone、屏障互斥（A18）。"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from motte_storage.gc import apply_gc, plan_gc
from motte_storage.platform import platform_for
from motte_storage.run_store import SQLiteRunStore

from apps.api.app.main import create_app


def _make_store_with_evidence(tmp_path: Path):
    store = SQLiteRunStore(tmp_path / "gc.db")
    store.runs.create(
        {"id": "run-live", "status": "completed", "revision": 1,
         "scenario_version": "replay@1",
         "manifest": {"artifacts": ["live/report.json"]}, "case_ids": []},
        event={"run_id": "run-live", "type": "completed", "status": "completed"},
    )
    store.runs.create(
        {"id": "run-review", "status": "needs_review", "revision": 1,
         "scenario_version": "replay@1",
         "manifest": {"artifacts": ["review/evidence.bin"]}, "case_ids": []},
        event={"run_id": "run-review", "type": "needs_review", "status": "needs_review"},
    )
    store.runs.create(
        {"id": "run-imported", "status": "completed", "revision": 1,
         "scenario_version": "replay@1",
         "manifest": {"import_source": {"system": "legacy", "record_id": "r1",
                                        "content_hash": "sha256:x", "import_id": "i1"},
                      "artifacts": ["imports/i1/source.json"]},
         "case_ids": []},
        event={"run_id": "run-imported", "type": "completed", "status": "completed"},
    )
    # baseline pin：引用 run-stale 的报告（run 本身已完成，但被 baseline 引用）
    store.runs.create(
        {"id": "run-stale", "status": "completed", "revision": 1,
         "scenario_version": "replay@1",
         "manifest": {"artifacts": ["stale/old.json"]}, "case_ids": []},
        event={"run_id": "run-stale", "type": "completed", "status": "completed"},
    )
    store.baseline_store.put({
        "baseline_id": "b1",
        "entries": [{"cell_key": None, "ref": {
            "run_id": "run-stale", "scoring_pass_id": "p1",
            "report_schema": "run-report@2", "evidence_hash": "sha256:y",
        }}],
        "comparison_policy_hash": "sha256:z", "eligibility": "formal",
        "reason": "test baseline",
        "created_by": "t", "created_at": "2026-09-22T00:00:00Z", "metrics": {},
    })
    return store


def _make_artifacts(root: Path):
    old = datetime.now(UTC) - timedelta(days=200)
    fresh = datetime.now(UTC)
    files = {
        "live/report.json": old,       # 过期但被 run-live 引用 → 保护
        "review/evidence.bin": old,    # 过期但 needs_review → pin
        "imports/i1/source.json": old, # 过期但 import audit → 保护
        "stale/old.json": old,         # 过期且被 baseline 引用的 run 持有 → pin
        "orphan/dead.json": old,       # 过期且无引用 → 可删
        "orphan/fresh.json": fresh,    # 未过期 → 保留
        "imports/i2/orphan.json": old, # 过期、无直接引用但属导入审计 → 保护
    }
    for rel, stamp in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        stamp_epoch = stamp.timestamp()
        os.utime(path, (stamp_epoch, stamp_epoch))


def test_gc_plan_keeps_child_only_case_artifact_reference(tmp_path):
    store = _make_store_with_evidence(tmp_path)
    store.case_runs.upsert({
        "run_id": "run-live", "case_id": "child-only",
        "artifact_refs": [{"id": "child/only.bin"}],
    })
    root = tmp_path / "artifacts"
    _make_artifacts(root)
    child = root / "child/only.bin"
    child.parent.mkdir(parents=True, exist_ok=True)
    child.write_bytes(b"x")
    old = (datetime.now(UTC) - timedelta(days=200)).timestamp()
    os.utime(child, (old, old))

    plan = plan_gc(store, root, artifact_ttl_days=90)
    assert "child/only.bin" not in {item["artifact_id"] for item in plan.deletable}
    assert any(item["artifact_id"] == "child/only.bin" and item["reason"] == "referenced"
               for item in plan.protected)


def test_gc_plan_does_not_swallow_repository_type_error(tmp_path):
    store = _make_store_with_evidence(tmp_path)
    original = store.case_runs.list_for_run

    def broken(_run_id):
        raise TypeError("repository API shape changed")

    store.case_runs.list_for_run = broken
    try:
        with pytest.raises(TypeError, match="repository API shape changed"):
            plan_gc(store, tmp_path / "artifacts")
    finally:
        store.case_runs.list_for_run = original


def test_gc_plan_dry_run_protects_pins_and_references(tmp_path):
    store = _make_store_with_evidence(tmp_path)
    root = tmp_path / "artifacts"
    _make_artifacts(root)
    plan = plan_gc(store, root, artifact_ttl_days=90)
    protected_ids = {entry["artifact_id"] for entry in plan.protected}
    deletable_ids = {entry["artifact_id"] for entry in plan.deletable}
    assert deletable_ids == {"orphan/dead.json"}
    assert {"live/report.json", "review/evidence.bin", "imports/i1/source.json",
            "stale/old.json", "orphan/fresh.json", "imports/i2/orphan.json"} <= protected_ids
    # pin 原因可解释（A18：保留并说明原因）
    reasons = {entry["artifact_id"]: entry["reason"] for entry in plan.protected}
    assert reasons["live/report.json"] == "referenced"
    assert reasons["orphan/fresh.json"] == "younger_than_ttl"
    assert reasons["imports/i2/orphan.json"] == "import_audit"
    assert reasons["imports/i1/source.json"] == "referenced"  # 双重保护按引用报告
    # needs_review / imported / baseline 引用的 run 进入 trace 保留报告
    assert set(plan.trace_retention["pinned_run_ids"]) >= {
        "run-review", "run-imported", "run-stale"}
    # dry-run 零删除
    assert (root / "orphan/dead.json").exists()


def test_gc_apply_requires_confirm_then_deletes_with_tombstone(tmp_path):
    store = _make_store_with_evidence(tmp_path)
    root = tmp_path / "artifacts"
    _make_artifacts(root)
    plan = plan_gc(store, root)
    with pytest.raises(ValueError):
        apply_gc(store, root, plan)  # 默认拒绝执行
    result = apply_gc(store, root, plan, confirm=True)
    assert result["deleted"] == 1
    assert not (root / "orphan/dead.json").exists()
    for rel in ("live/report.json", "review/evidence.bin", "imports/i1/source.json",
                "stale/old.json", "orphan/fresh.json"):
        assert (root / rel).exists()
    tombstones = platform_for(store).tombstones.list()
    assert len(tombstones) == 1
    assert tombstones[0]["artifact_id"] == "orphan/dead.json"
    assert tombstones[0]["reason"] == "artifact_ttl_expired"
    assert "sha256" in tombstones[0] and "deleted_at" in tombstones[0]
    # 屏障在 apply 后解除
    assert platform_for(store).meta.get("maintenance") is None


def test_gc_apply_skips_newly_referenced_files(tmp_path):
    store = _make_store_with_evidence(tmp_path)
    root = tmp_path / "artifacts"
    _make_artifacts(root)
    plan = plan_gc(store, root)
    # 计划之后、执行之前：run-live 开始引用孤儿文件（重算引用必须跳过它）
    current = store.runs.get("run-live")
    store.runs.save({
        **current,
        "manifest": {**current["manifest"],
                     "artifacts": ["live/report.json", "orphan/dead.json"]},
    })
    result = apply_gc(store, root, plan, confirm=True)
    assert result["deleted"] == 0
    assert (root / "orphan/dead.json").exists()
    assert result["skipped"][0]["reason"] == "protected_after_recheck"


def test_gc_apply_holds_maintenance_barrier(tmp_path, monkeypatch):
    """GC apply 执行期间 API 写入被 503 拒绝（与采集/评分互斥，A14 同源屏障）。"""
    store = _make_store_with_evidence(tmp_path)
    root = tmp_path / "artifacts"
    _make_artifacts(root)
    plan = plan_gc(store, root)
    app = create_app(store=store)
    client = TestClient(app)

    observed = {}

    original_unlink = os.unlink

    def spy_unlink(path, *args, **kwargs):
        assert kwargs.get("dir_fd") is not None
        observed["maintenance_during_delete"] = platform_for(store).meta.get("maintenance")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", spy_unlink)
        patch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, spy_unlink})
        result = apply_gc(store, root, plan, confirm=True)
    assert result["deleted"] == 1
    assert observed["maintenance_during_delete"] == "active"
    assert client.post(
        "/api/v1/runs",
        json={"scenario_version": "json_extract@1"},
    ).status_code == 202  # 屏障解除后写入恢复


REFERENCE_SHAPES = (
    "locator", "raw_ref", "raw_bundle_artifact", "id", "artifact_id", "sha256",
)
REFERENCE_LOCATIONS = (
    "run", "case", "invocation", "score_set", "baseline", "gate", "event", "experiment_cell",
)


def _artifact_reference(shape, artifact_id, sha256):
    if shape == "locator":
        ref = {"event_refs": [{"kind": "artifact", "run_id": "external", "locator": artifact_id}]}
    elif shape in {"raw_ref", "raw_bundle_artifact"}:
        ref = {shape: artifact_id}
    elif shape == "sha256":
        ref = {"artifacts": [{"sha256": sha256}]}
    else:
        ref = {"artifact_refs": [{shape: artifact_id}]}
    return {"nested": [{"observation": ref}]}


def _store_reference(store, location, run_id, payload):
    """Persist a reference in exactly one repository, without touching Run evidence."""
    if location == "run":
        current = store.runs.get(run_id)
        store.runs.save({**current, "evidence": payload})
    elif location == "case":
        store.case_runs.upsert({"run_id": run_id, "case_id": "ref-case", "evidence": payload})
    elif location == "invocation":
        store.invocations.create({
            "id": "ref-invocation", "run_id": run_id, "case_id": "ref-case",
            "kind": "model", "step": 1, "status": "prepared", "request_summary": payload,
        })
    elif location == "score_set":
        store.scoring_passes.append(
            {"id": "ref-pass", "run_id": run_id},
            [{"case_id": "ref-case", "value": 1, "details": payload}],
        )
        # Historical backends may keep scores only in score_sets. Remove the
        # redundant embedded copy so the test cannot pass by scanning the pass.
        import json
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(store.runs._path)) as connection, connection:
            connection.execute(
                "UPDATE scoring_passes SET payload = ? WHERE id = ?",
                (json.dumps({"id": "ref-pass", "run_id": run_id}), "ref-pass"),
            )
    elif location == "baseline":
        store.baselines.put({
            "id": "ref-baseline", "run_id": run_id, "scoring_pass_id": "old-pass",
            "metrics": {}, "evidence": payload,
        })
    elif location == "gate":
        store.gate_store.put_result({
            "gate_result_id": "ref-gate", "policy_id": "ref-policy", "evidence": payload,
        })
    elif location == "experiment_cell":
        store.experiments.publish_spec_and_cells(
            {"experiment_id": "ref-experiment", "version": "1"},
            [{"cell_id": "pending-cell", "experiment_id": "ref-experiment",
              "experiment_version": "1", "allocation_status": "pending", "repeat_index": 0,
              "factor_assignment": {}, "prepared_run": {"manifest": payload, "case_ids": []}}],
        )
    elif location == "event":
        store.events.append({"run_id": run_id, "type": "evidence", "data": payload})
    else:
        raise AssertionError(location)


def _old_artifact(root, artifact_id, content):
    from motte_storage.artifacts import ArtifactStore

    artifact = ArtifactStore(root).put_bytes(artifact_id, content)
    old = (datetime.now(UTC) - timedelta(days=200)).timestamp()
    os.utime(root / artifact_id, (old, old))
    return artifact


@pytest.mark.parametrize("shape", REFERENCE_SHAPES)
@pytest.mark.parametrize("location", REFERENCE_LOCATIONS)
@pytest.mark.parametrize("reference_after_plan", [False, True], ids=["plan", "apply-recheck"])
def test_gc_preserves_every_nested_reference(tmp_path, shape, location, reference_after_plan):
    store = _make_store_with_evidence(tmp_path)
    root = tmp_path / "artifacts"
    artifact = _old_artifact(root, "evidence/only.bin", b"referenced evidence")
    _old_artifact(root, "orphan.bin", b"unreferenced evidence")
    payload = _artifact_reference(shape, artifact.id, artifact.sha256)
    if reference_after_plan:
        plan = plan_gc(store, root)
        assert artifact.id in {entry["artifact_id"] for entry in plan.deletable}
    # needs_review cannot compensate for a missing decoder; its child evidence
    # must be found by the same complete traversal as completed Run evidence.
    _store_reference(store, location, "run-review", payload)
    if not reference_after_plan:
        plan = plan_gc(store, root)
        assert artifact.id in {entry["artifact_id"] for entry in plan.protected}
    assert "run-review" in plan.trace_retention["pinned_run_ids"]

    result = apply_gc(store, root, plan, confirm=True)

    assert (root / artifact.id).read_bytes() == b"referenced evidence"
    assert not (root / "orphan.bin").exists()
    assert result["deleted"] == 1
    assert artifact.id not in {row["artifact_id"] for row in platform_for(store).tombstones.list()}
    if reference_after_plan:
        assert any(row["artifact_id"] == artifact.id and row["reason"] == "protected_after_recheck"
                   for row in result["skipped"])


@pytest.mark.parametrize("key", ["artifacts", "artifact_ids"])
def test_gc_keeps_legacy_string_reference(tmp_path, key):
    store = _make_store_with_evidence(tmp_path)
    root = tmp_path / "artifacts"
    artifact = _old_artifact(root, "legacy/only.bin", b"legacy reference")
    _store_reference(store, "case", "run-review", {key: artifact.id})

    plan = plan_gc(store, root)

    assert [row["artifact_id"] for row in plan.protected] == [artifact.id]


def test_gc_keeps_import_mapping_only_reference_when_import_prefix_protection_disabled(tmp_path):
    store = _make_store_with_evidence(tmp_path)
    root = tmp_path / "artifacts"
    artifact = _old_artifact(root, "imports/batch/only.bin", b"ledger-only evidence")
    ledger = platform_for(store).imports
    ledger.begin_import({"import_id": "batch", "manifest_sha256": "manifest", "status": "applied"})
    ledger.put_mapping({
        "mapping_key": "only", "import_id": "batch", "target_type": "artifact",
        "target_id": artifact.id, "artifact_sha256": artifact.sha256,
    })

    plan = plan_gc(store, root, import_audit_protected=False)
    assert [row["artifact_id"] for row in plan.protected] == [artifact.id]
    assert plan.protected[0]["reason"] == "referenced"


def test_gc_rechecks_current_content_instead_of_using_stale_plan_hash(tmp_path):
    store = _make_store_with_evidence(tmp_path)
    root = tmp_path / "artifacts"
    artifact = _old_artifact(root, "changed.bin", b"old unreferenced content")
    plan = plan_gc(store, root)
    # A writer replaces the file after planning; a new digest-only reference
    # points to the replacement. Neither stale consent nor hash is applicable.
    from motte_storage.artifacts import ArtifactStore

    replacement = ArtifactStore(root).put_bytes(artifact.id, b"new referenced content")
    _store_reference(store, "case", "run-review", {"artifacts": [{"sha256": replacement.sha256}]})

    result = apply_gc(store, root, plan, confirm=True)

    assert (root / artifact.id).read_bytes() == b"new referenced content"
    assert result["deleted"] == 0
    assert result["skipped"][0]["reason"] in {"changed_since_plan", "protected_after_recheck"}
    assert platform_for(store).tombstones.list() == []


def test_gc_does_not_treat_non_artifact_locator_or_hash_as_reference(tmp_path):
    store = _make_store_with_evidence(tmp_path)
    root = tmp_path / "artifacts"
    artifact = _old_artifact(root, "123", b"ordinary orphan")
    _store_reference(store, "case", "run-review", {
        "kind": "event", "locator": artifact.id, "sha256": artifact.sha256,
        "id": artifact.id, "configuration": {"sha256": artifact.sha256},
    })

    plan = plan_gc(store, root)

    assert [row["artifact_id"] for row in plan.deletable] == [artifact.id]


@pytest.mark.parametrize("location", ["scoring_job", "calibration_invocation"])
def test_gc_preserves_durable_job_and_parentless_calibration_evidence(tmp_path, location):
    from motte_storage.scoring_jobs import scoring_jobs_for
    from tests.storage.test_scoring_jobs import invocation_record, job_record

    store = _make_store_with_evidence(tmp_path)
    root = tmp_path / "artifacts"
    artifact = _old_artifact(root, "judge/only.bin", b"durable judge evidence")
    payload = _artifact_reference("locator", artifact.id, artifact.sha256)
    owner = {"kind": "calibration", "calibration_job_id": "cal-1"}
    job = job_record(owner=owner, status="completed")
    if location == "scoring_job":
        job["evidence"] = payload
    scoring_jobs_for(store).submit(job)
    if location == "calibration_invocation":
        invocation = invocation_record(owner={**owner, "sample_id": "sample-1"})
        invocation["request_summary"] = payload
        store.invocations.create(invocation)
    assert store.runs.get("calibration:cal-1") is None
    assert getattr(store, "scoring_jobs", None) is None

    plan = plan_gc(store, root)

    assert [row["artifact_id"] for row in plan.protected] == [artifact.id]
    assert apply_gc(store, root, plan, confirm=True)["deleted"] == 0
    assert (root / artifact.id).exists()


def test_gc_keeps_pending_cell_only_reference_before_any_run_exists(tmp_path):
    store = SQLiteRunStore(tmp_path / "gc.db")
    root = tmp_path / "artifacts"
    artifact = _old_artifact(root, "prepared/model.bin", b"frozen preparation")
    _store_reference(store, "experiment_cell", None, {"raw_bundle_artifact": artifact.id})
    assert store.runs.list() == []
    assert store.experiments.get_cell("pending-cell")["allocation_status"] == "pending"

    plan = plan_gc(store, root)

    assert [row["artifact_id"] for row in plan.protected] == [artifact.id]
    assert apply_gc(store, root, plan, confirm=True)["deleted"] == 0
    assert (root / artifact.id).exists()


def test_gc_apply_fails_closed_if_reference_repository_cannot_be_read(tmp_path, monkeypatch):
    store = _make_store_with_evidence(tmp_path)
    root = tmp_path / "artifacts"
    artifact = _old_artifact(root, "candidate.bin", b"do not delete after incomplete scan")
    plan = plan_gc(store, root)

    def unavailable(_run_id):
        raise RuntimeError("reference repository unavailable")

    monkeypatch.setattr(store.case_runs, "list_for_run", unavailable)
    with pytest.raises(RuntimeError, match="reference repository unavailable"):
        apply_gc(store, root, plan, confirm=True)

    assert (root / artifact.id).exists()
    assert platform_for(store).tombstones.list() == []
    assert platform_for(store).meta.get("maintenance") is None


class _CrashAfterUnlink(BaseException):
    """Simulates a process exit before deletion completion can be persisted."""


def test_gc_crash_after_unlink_retains_durable_intent_and_replay_is_unconfirmed(tmp_path, monkeypatch):
    store = _make_store_with_evidence(tmp_path)
    root = tmp_path / "artifacts"
    artifact = _old_artifact(root, "crash.bin", b"planned deletion")
    plan = plan_gc(store, root)
    original_unlink = os.unlink

    def crash_after_unlink(path, *args, **kwargs):
        assert kwargs.get("dir_fd") is not None
        result = original_unlink(path, *args, **kwargs)
        if Path(path).name == artifact.id:
            raise _CrashAfterUnlink()
        return result

    monkeypatch.setattr(os, "unlink", crash_after_unlink)
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, crash_after_unlink})
    with pytest.raises(_CrashAfterUnlink):
        apply_gc(store, root, plan, confirm=True)
    assert not (root / artifact.id).exists()
    intent = platform_for(store).tombstones.list()[0]
    assert intent["artifact_id"] == artifact.id
    assert intent["deletion_status"] == "deleting"
    assert "deleted_at" not in intent

    monkeypatch.undo()
    replay = apply_gc(store, root, plan, confirm=True)
    assert replay["deleted"] == 0
    assert replay["skipped"][0]["reason"] == "absence_unconfirmed"
    audit = platform_for(store).tombstones.list()[0]
    assert audit["deletion_status"] == "absence_unconfirmed"
    assert audit["deletion_started_at"] == intent["deletion_started_at"]
    assert "deleted_at" not in audit


def test_gc_failed_unlink_records_failed_attempt_without_claiming_deletion(tmp_path, monkeypatch):
    store = _make_store_with_evidence(tmp_path)
    root = tmp_path / "artifacts"
    artifact = _old_artifact(root, "failed.bin", b"retained deletion candidate")
    plan = plan_gc(store, root)
    original_unlink = os.unlink

    def failed_unlink(path, *args, **kwargs):
        assert kwargs.get("dir_fd") is not None
        if Path(path).name == artifact.id:
            raise PermissionError("simulated unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", failed_unlink)
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, failed_unlink})
    with pytest.raises(PermissionError, match="simulated unlink failure"):
        apply_gc(store, root, plan, confirm=True)

    assert (root / artifact.id).exists()
    audit = platform_for(store).tombstones.list()[0]
    assert audit["deletion_status"] == "failed"
    assert audit["error_type"] == "PermissionError"
    assert "deleted_at" not in audit


def test_gc_replay_of_confirmed_deletion_does_not_replace_completion(tmp_path):
    store = _make_store_with_evidence(tmp_path)
    root = tmp_path / "artifacts"
    _old_artifact(root, "deleted.bin", b"delete once")
    plan = plan_gc(store, root)
    assert apply_gc(store, root, plan, confirm=True)["deleted"] == 1
    first = platform_for(store).tombstones.list()[0]

    result = apply_gc(store, root, plan, confirm=True)

    assert result["deleted"] == 0
    assert result["skipped"][0]["reason"] == "already_deleted"
    assert platform_for(store).tombstones.list()[0] == first


@pytest.mark.parametrize("alias", ["./real/evidence.bin", "real//evidence.bin", "link/evidence.bin"])
def test_gc_protects_lexical_and_in_root_symlink_reference_aliases(tmp_path, alias):
    store = _make_store_with_evidence(tmp_path)
    root = tmp_path / "artifacts"
    artifact = _old_artifact(root, "real/evidence.bin", b"aliased evidence")
    (root / "link").symlink_to(root / "real", target_is_directory=True)
    _store_reference(store, "case", "run-review", {"raw_ref": alias})

    plan = plan_gc(store, root)

    assert artifact.id in {row["artifact_id"] for row in plan.protected}
    assert apply_gc(store, root, plan, confirm=True)["deleted"] == 0
    assert (root / artifact.id).exists()


@pytest.mark.parametrize("failure", ["integrity", "list", "get", "conflict"])
def test_report_reader_failure_aborts_gc_before_any_deletion(tmp_path, monkeypatch, failure):
    from tests.storage.test_statistical_report_references import put_report, damage_report_reader
    from motte_storage.maintenance import maintenance_status

    store = SQLiteRunStore(tmp_path / "report-failure.db")
    root = tmp_path / "artifacts"
    _old_artifact(root, "a-first-orphan.bin", b"must survive")
    evidence = _old_artifact(root, "report-evidence.bin", b"also survives")
    plan = plan_gc(store, root, artifact_ttl_days=1)
    put_report(store, evidence={"artifact_id": evidence.id, "sha256": evidence.sha256})
    error = damage_report_reader(store, monkeypatch, failure)
    with pytest.raises(error):
        apply_gc(store, root, plan, confirm=True)
    with pytest.raises(error):
        plan_gc(store, root)
    assert (root / "a-first-orphan.bin").read_bytes() == b"must survive"
    assert (root / evidence.id).read_bytes() == b"also survives"
    assert platform_for(store).tombstones.list() == []
    assert maintenance_status(store)["active"] is False


def test_gc_protects_report_sentinel_beyond_default_list_limit(tmp_path):
    from tests.storage.test_statistical_report_references import put_report

    store = SQLiteRunStore(tmp_path / "many-reports.db")
    reports = [put_report(store, evidence={"artifact_id": f"report-{index}.bin"})
               for index in range(106)]
    sentinel = sorted(reports, key=lambda report: report["report_id"])[-1]
    artifact_id = sentinel["body"]["result"]["diagnostic"]["artifact_id"]
    assert sentinel not in store.statistical_reports.list(limit=100)
    root = tmp_path / "artifacts"
    _old_artifact(root, artifact_id, b"sentinel evidence")
    _old_artifact(root, "orphan.bin", b"unprotected control")
    plan = plan_gc(store, root, artifact_ttl_days=1)
    assert {item["artifact_id"] for item in plan.protected} == {artifact_id}
    result = apply_gc(store, root, plan, confirm=True)
    assert result["deleted"] == 1
    assert (root / artifact_id).read_bytes() == b"sentinel evidence"
    assert not (root / "orphan.bin").exists()


@pytest.mark.parametrize("artifact_id", [
    "trace-archives/sha256/" + "a" * 64 + ".json",
    "trace-archives/crash-orphan.part", "trace-archives/other/nested.bin",
])
def test_orphan_archive_has_no_ttl(tmp_path, artifact_id):
    store = SQLiteRunStore(tmp_path / "archive-gc.db")
    root = tmp_path / "artifacts"
    path = root / artifact_id
    path.parent.mkdir(parents=True)
    path.write_bytes(b"crash orphan with no receipt")
    old = (datetime.now(UTC) - timedelta(days=1000)).timestamp()
    os.utime(path, (old, old))

    plan = plan_gc(store, root, artifact_ttl_days=1, import_audit_protected=False)

    assert artifact_id not in {row["artifact_id"] for row in plan.deletable}
    assert next(row for row in plan.protected if row["artifact_id"] == artifact_id)[
        "reason"
    ] == "trace_archive"
    assert apply_gc(store, root, plan, confirm=True)["deleted"] == 0
    assert path.read_bytes() == b"crash orphan with no receipt"
    assert platform_for(store).tombstones.list() == []


@pytest.mark.parametrize("alias", ["trace-archives/orphan.bin", "./trace-archives/orphan.bin",
                                    "alias/orphan.bin"])
def test_gc_apply_rejects_injected_reserved_archive_candidates(tmp_path, alias):
    import hashlib
    from motte_storage.gc import GCPlan

    store = SQLiteRunStore(tmp_path / "archive-gc.db")
    root = tmp_path / "artifacts"
    path = root / "trace-archives/orphan.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"orphan")
    (root / "alias").symlink_to(path.parent, target_is_directory=True)
    plan = GCPlan(gc_run_id="tampered", deletable=[{
        "artifact_id": alias, "bytes": 6, "sha256": hashlib.sha256(b"orphan").hexdigest(),
    }])

    result = apply_gc(store, root, plan, confirm=True)

    assert result["deleted"] == 0
    assert result["skipped"][0]["reason"] == "trace_archive"
    assert path.read_bytes() == b"orphan"
    assert platform_for(store).tombstones.list() == []


def test_gc_mutation_boundary_cannot_follow_replaced_parent(tmp_path, monkeypatch):
    import hashlib
    from motte_storage.gc import GCPlan

    store = SQLiteRunStore(tmp_path / "race.db")
    root = tmp_path / "artifacts"
    archive = root / "trace-archives/sha256/orphan.json"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"immutable evidence")
    ordinary = root / "ordinary"
    ordinary.mkdir()
    target = ordinary / archive.name
    data = b"ordinary evidence"
    target.write_bytes(data)
    plan = GCPlan(gc_run_id="race", deletable=[{
        "artifact_id": "ordinary/orphan.json", "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }])
    original_path_unlink, original_unlink = Path.unlink, os.unlink
    attacked = False

    def swap():
        nonlocal attacked
        if not attacked:
            attacked = True
            ordinary.rename(root / "detached-ordinary")
            ordinary.symlink_to(archive.parent, target_is_directory=True)

    def path_unlink(path, *args, **kwargs):
        if path == target:
            swap()
        return original_path_unlink(path, *args, **kwargs)

    def unlink(path, *args, **kwargs):
        if Path(path).name == target.name:
            swap()
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", path_unlink)
    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, unlink})
    try:
        apply_gc(store, root, plan, confirm=True)
    except (ValueError, OSError):
        pass
    assert attacked
    assert archive.read_bytes() == b"immutable evidence"
    assert all(row.get("deletion_status") != "deleted"
               for row in platform_for(store).tombstones.list())


@pytest.mark.parametrize("missing", ["nofollow", "dir_fd", "native_unavailable"])
def test_gc_fails_closed_without_safe_mutation_primitives(tmp_path, monkeypatch, missing):
    store = _make_store_with_evidence(tmp_path)
    root = tmp_path / "artifacts"
    artifact = _old_artifact(root, "candidate.bin", b"preserved deletion candidate")
    plan = plan_gc(store, root)
    if missing == "nofollow":
        monkeypatch.delattr(os, "O_NOFOLLOW")
    elif missing == "dir_fd":
        monkeypatch.setattr(os, "supports_dir_fd", set())
    else:
        from types import SimpleNamespace
        import motte_storage.artifacts as artifact_module
        simulated_os = SimpleNamespace(**vars(os))
        simulated_os.name = "nt"
        monkeypatch.setattr(artifact_module, "os", simulated_os)
        import motte_storage._windows_artifact_io as native
        def unavailable():
            raise artifact_module.ArtifactMutationUnsupported(
                "safe artifact mutation primitives are unsupported")
        monkeypatch.setattr(native, "_api", unavailable)
    with pytest.raises(ValueError, match="safe artifact mutation primitives are unsupported"):
        apply_gc(store, root, plan, confirm=True)
    assert (root / artifact.id).read_bytes() == b"preserved deletion candidate"
    assert platform_for(store).tombstones.list() == []
    assert platform_for(store).meta.get("maintenance") is None


def test_gc_does_not_follow_new_leaf_alias_to_path_only_referenced_file(tmp_path):
    store = _make_store_with_evidence(tmp_path)
    root = tmp_path / "artifacts"
    data = b"identical bytes are not permission to delete another path"
    protected = _old_artifact(root, "protected.bin", data)
    candidate = _old_artifact(root, "candidate.bin", data)
    _store_reference(store, "case", "run-review", {"raw_ref": protected.id})
    plan = plan_gc(store, root)
    assert candidate.id in {row["artifact_id"] for row in plan.deletable}
    assert protected.id in {row["artifact_id"] for row in plan.protected}
    (root / candidate.id).unlink()
    (root / candidate.id).symlink_to(root / protected.id)

    result = apply_gc(store, root, plan, confirm=True)

    assert result["deleted"] == 0
    assert (root / protected.id).read_bytes() == data
    assert platform_for(store).tombstones.list() == []
