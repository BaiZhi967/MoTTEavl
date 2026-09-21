"""M7-T11 release smoke：空库 → 健康 → 合成 Run → 报告/Gate → 升级/恢复演练。

覆盖（SQLite、离线、零付费调用）：
- 全新库上 API + Worker 完整闭环（replay 确定性执行）+ M6 报告/Gate/导出；
- 旧形状库（缺 M7 平台表）自动升级后可读写；
- schema 不兼容的恢复路径（备份→staging 校验恢复→守卫解除后可继续执行）；
- 退出码映射沿 M6 冻结值。
PG/Compose 部分由 CI/支持矩阵单独登记（本机无 docker/DSN，如实 not_run）。
"""
from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient
from motte_storage.maintenance import (
    clear_restore_guard,
    consistent_backup,
    restore_staging,
)
from motte_storage.run_store import SQLiteRunStore

from apps.api.app.main import create_app


def _replay_body(tag: str) -> dict:
    return {
        "scenario_version": "replay@1",
        "manifest": {"replay_fixture": {
            "case-1": {"output": "ok", "expected": "ok"},
            "case-2": {"output": "no", "expected": "ok"},
        }},
        "case_ids": ["case-1", "case-2"],
        "request_key": f"release-smoke-{tag}",
    }


def test_release_smoke_end_to_end(tmp_path):
    """空库安装 → 健康 → Run 创建/执行 → 报告 → Gate 发布/求值 → 导出。"""
    db_path = tmp_path / "release.db"
    store = SQLiteRunStore(db_path)
    app = create_app(store=store)
    client = TestClient(app)

    # 空库健康 + 能力握手
    assert client.get("/health").json() == {"status": "ok"}
    capabilities = client.get("/api/v1/capabilities").json()
    assert capabilities["api_version"] == "v1"

    # 两个"模型"（同一确定性 fixture 的两次 Run，构成可比较对照）
    first = client.post("/api/v1/runs", json=_replay_body("a"))
    second = client.post("/api/v1/runs", json=_replay_body("b"))
    assert first.status_code == 202 and second.status_code == 202
    # 幂等键防重复
    replay = client.post("/api/v1/runs", json=_replay_body("a"))
    assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True

    # Worker 真实执行（确定性 replay，零付费调用）
    from apps.worker.motte_worker.runtime import WorkerLoop

    loop = WorkerLoop(app.state.run_service, scoring_jobs=None)
    loop.claim_and_execute(first.json()["id"])
    loop.claim_and_execute(second.json()["id"])

    run_id = first.json()["id"]
    detail = client.get(f"/api/v1/runs/{run_id}").json()
    assert detail["status"] == "completed"
    report = client.get(f"/api/v1/runs/{run_id}/report").json()
    assert report["run_id"] == run_id
    assert report["scoring_pass_id"]

    # 比较 + 基线 + 版本化 Gate + 导出
    comparison = client.get(
        "/api/v1/comparisons",
        params={"baseline": second.json()["id"], "candidate": run_id},
    )
    assert comparison.status_code == 200
    baseline = client.post(
        "/api/v1/baselines",
        json={
            "baseline_id": "release-smoke-b1",
            "entries": [{"run_id": second.json()["id"],
                         "scoring_pass_id": client.get(
                             f"/api/v1/runs/{second.json()['id']}/report"
                         ).json()["scoring_pass_id"]}],
            "created_by": "release-smoke",
            "reason": "release smoke baseline",
        },
    )
    assert baseline.status_code == 201
    policy = client.post("/api/v1/gate-policies", json={
        "policy_id": "release-smoke-policy", "version": "1",
        "rules": [{"rule_id": "acc-min", "kind": "metric_threshold",
                   "metric_id": "accuracy", "operator": "gte", "threshold": 0.5}],
        "created_by": "release-smoke", "reason": "release smoke",
    })
    assert policy.status_code == 201, policy.text
    gate = client.post("/api/v1/gates/versioned", json={
        "run_id": run_id, "policy_id": "release-smoke-policy", "policy_version": "1",
    })
    assert gate.status_code == 200
    gate_result = gate.json()
    assert gate_result["exit_code"] == 0, gate_result
    export_json = client.get(
        f"/api/v1/gates/results/{gate_result['gate_result_id']}/export",
        params={"format": "json"},
    )
    assert export_json.status_code == 200
    payload = export_json.json()
    assert payload["exporter_version"] == "gate-exporter@1"
    assert payload["exit_code"] == 0
    export_junit = client.get(
        f"/api/v1/gates/results/{gate_result['gate_result_id']}/export",
        params={"format": "junit"},
    )
    assert export_junit.status_code == 200
    assert "<testsuite" in export_junit.text


def test_release_upgrade_old_shape_database_gets_platform_tables(tmp_path):
    """上一版本（无 M7 平台表）库打开即自动补齐，可继续创建/幂等。"""
    db_path = tmp_path / "legacy-shape.db"
    store = SQLiteRunStore(db_path)
    # 模拟旧库：删除 M7 新增的平台表（字面量 DDL，无动态构造）
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TABLE IF EXISTS motte_request_keys")
        connection.execute("DROP TABLE IF EXISTS motte_meta")
        connection.execute("DROP TABLE IF EXISTS motte_imports")
        connection.execute("DROP TABLE IF EXISTS motte_import_mappings")
        connection.execute("DROP TABLE IF EXISTS motte_gc_tombstones")
        connection.commit()
    upgraded = SQLiteRunStore(db_path)
    client = TestClient(create_app(store=upgraded))
    created = client.post("/api/v1/runs", json=_replay_body("upgrade"))
    assert created.status_code == 202
    replay = client.post("/api/v1/runs", json=_replay_body("upgrade"))
    assert replay.status_code == 200
    assert replay.json()["id"] == created.json()["id"]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM motte_request_keys"
        ).fetchone()[0] == 1


def test_release_recovery_path_from_backup(tmp_path):
    """schema/数据异常时的恢复路径：一致备份 → staging 校验恢复 → 守卫内不执行
    → 解除守卫后新任务正常执行。"""
    db_path = tmp_path / "live.db"
    store = SQLiteRunStore(db_path)
    app = create_app(store=store)
    client = TestClient(app)
    created = client.post("/api/v1/runs", json=_replay_body("recover"))
    assert created.status_code == 202

    artifacts_root = tmp_path / "artifacts"
    backup_dir = tmp_path / "backups"
    manifest = consistent_backup(store, backup_dir, artifacts_root=artifacts_root)
    assert manifest["status"] == "complete"

    staging = tmp_path / "staging"
    report = restore_staging(backup_dir, staging)
    assert report["restore_guard"] == "active"
    # 恢复出的 staging 中未决 run 不被 Worker 领取
    from apps.worker.motte_worker.runtime import WorkerLoop
    from motte_sdk.service import RunService

    staging_store = SQLiteRunStore(staging / "runs.db")
    staging_loop = WorkerLoop(
        RunService(staging_store), scoring_jobs=None, execution_lock_held=True
    )
    assert staging_loop.run_once() is None

    clear_restore_guard(staging_store, confirm=True)
    # 解除守卫后 staging 可正常执行 queued run（确定性 replay）
    result = staging_loop.claim_and_execute(created.json()["id"])
    assert result["status"] == "completed"


def test_release_exit_code_mapping_frozen(tmp_path):
    """M6 冻结的退出码映射在 M7 导出中不变（0/1/3/5/6）。"""
    from motte_contracts.gates import DECISION_EXIT_CODES

    assert DECISION_EXIT_CODES == {
        "pass": 0, "quality_fail": 1, "execution_error": 3,
        "insufficient_evidence": 5, "not_comparable": 5, "safety_block": 6,
    }
    json.dumps(DECISION_EXIT_CODES)  # 可序列化（导出路径可用）
