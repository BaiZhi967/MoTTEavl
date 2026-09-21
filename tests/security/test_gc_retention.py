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


def test_gc_apply_holds_maintenance_barrier(tmp_path):
    """GC apply 执行期间 API 写入被 503 拒绝（与采集/评分互斥，A14 同源屏障）。"""
    store = _make_store_with_evidence(tmp_path)
    root = tmp_path / "artifacts"
    _make_artifacts(root)
    plan = plan_gc(store, root)
    app = create_app(store=store)
    client = TestClient(app)

    observed = {}

    original_unlink = Path.unlink

    def spy_unlink(self, *args, **kwargs):
        observed["maintenance_during_delete"] = platform_for(store).meta.get("maintenance")
        return original_unlink(self, *args, **kwargs)

    Path.unlink = spy_unlink
    try:
        result = apply_gc(store, root, plan, confirm=True)
    finally:
        Path.unlink = original_unlink
    assert result["deleted"] == 1
    assert observed["maintenance_during_delete"] == "active"
    assert client.post(
        "/api/v1/runs",
        json={"scenario_version": "json_extract@1"},
    ).status_code == 202  # 屏障解除后写入恢复
