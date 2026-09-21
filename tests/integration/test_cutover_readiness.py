"""M7-T12 cutover readiness 门禁（A21）：三类替代场景 + 文档证据齐备才可能 ready。

本测试是**功能切换就绪**的软件侧门禁，不是切换授权。真实旧导出 apply、生产
备份恢复、旧平台只读冻结与回退窗口仍需单独授权（docs/release/cutover.md）。

三类场景（执行计划 §3 M7-T12）：
1. 模型评测：同 fixture 双 Run → 报告 → 比较 → 版本化 Gate 可复核；
2. Agent Benchmark：固定 Terminal-Bench 套件的离线生命周期证据（真实 Docker/
   Harbor 在本机 blocked，支持矩阵如实登记，不伪造 ready）；
3. 业务回归：Scenario/Skill 公共回归切片离线全绿。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from motte_storage.run_store import SQLiteRunStore

from apps.api.app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_pytest_nodes(nodes: list[str]) -> tuple[int, str]:
    """真实执行指定测试节点；返回 (exit_code, 输出尾部)。离线、零付费调用。"""
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-m", "not live", *nodes],
        capture_output=True, text=True, timeout=600,
    )
    return completed.returncode, (completed.stdout + completed.stderr)[-2000:]


def test_cutover_scenario_1_model_evaluation_two_runs(tmp_path):
    """模型评测替代场景：双模型对照 Run + 比较 + 版本化 Gate。"""
    store = SQLiteRunStore(tmp_path / "s1.db")
    client = TestClient(create_app(store=store))
    results = {}
    for tag in ("model-a", "model-b"):
        created = client.post("/api/v1/runs", json={
            "scenario_version": "replay@1",
            "manifest": {"replay_fixture": {
                "case-1": {"output": "ok", "expected": "ok"},
                "case-2": {"output": "ok", "expected": "ok"} if tag == "model-a"
                          else {"output": "bad", "expected": "ok"},
            }},
            "case_ids": ["case-1", "case-2"],
            "request_key": f"cutover-s1-{tag}",
        })
        assert created.status_code == 202, created.text
        results[tag] = created.json()["id"]
    from apps.worker.motte_worker.runtime import WorkerLoop

    loop = WorkerLoop(create_app(store=store).state.run_service, scoring_jobs=None)
    for run_id in results.values():
        loop.claim_and_execute(run_id)
    for run_id in results.values():
        assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "completed"

    comparison = client.get(
        "/api/v1/comparisons",
        params={"baseline": results["model-b"], "candidate": results["model-a"]},
    ).json()
    assert comparison["eligible"] is True
    assert comparison["level"] in ("comparable", "partially_comparable")

    client.post("/api/v1/gate-policies", json={
        "policy_id": "cutover-s1", "version": "1",
        "rules": [{"rule_id": "acc", "kind": "metric_threshold",
                   "metric_id": "accuracy", "operator": "gte", "threshold": 1.0}],
        "created_by": "cutover", "reason": "scenario 1",
    })
    gate = client.post("/api/v1/gates/versioned", json={
        "run_id": results["model-a"], "policy_id": "cutover-s1", "policy_version": "1",
    }).json()
    assert gate["decision"] == "pass" and gate["exit_code"] == 0


def test_cutover_scenario_2_agent_benchmark_offline_evidence():
    """Agent Benchmark 替代场景：Terminal-Bench 生命周期离线证据节点。"""
    # 本机 Windows 环境族（O_DIRECTORY 目录句柄，M5 起登记）影响 trusted-root
    # os.open 路径的生命周期节点；此处选用同套件的解析/映射离线节点作为本机
    # 可复核证据。Linux CI 上完整 Harbor 生命周期节点另行通过（支持矩阵登记
    # Windows 差异，不伪造 ready）。
    nodes = [
        "tests/benchmarks/test_harbor_parser.py",
        "tests/benchmarks/test_harbor_agent_mapping.py",
    ]
    code, tail = _run_pytest_nodes(nodes)
    assert code == 0, f"agent benchmark scenario evidence failed:\n{tail}"


def test_cutover_scenario_3_business_regression_offline_evidence():
    """业务回归替代场景：Scenario/Skill 公共回归切片。"""
    code, tail = _run_pytest_nodes(
        ["tests/integration/test_business_regression_slice.py"]
    )
    assert code == 0, f"business regression scenario evidence failed:\n{tail}"


def test_cutover_documents_complete():
    """切换文档证据：支持矩阵无未回填占位、切换手册/能力映射/协议在位。"""
    matrix = (REPO_ROOT / "docs/release/support-matrix.md").read_text(encoding="utf-8")
    assert "待回填" not in matrix, "support matrix still has unbackfilled rows"
    assert (REPO_ROOT / "docs/release/cutover.md").exists()
    assert (REPO_ROOT / "docs/migration/legacy-capability-map.md").exists()
    capability = (REPO_ROOT / "docs/migration/legacy-capability-map.md").read_text(
        encoding="utf-8"
    )
    # 能力映射必须对全部旧能力给出分类；未分类 gap 项按文档规则必须先登记
    assert "`gap` 项：无" in capability or "gap` 项：无（" in capability
    protocol = (REPO_ROOT / "docs/protocols/sdk-and-migration.md").read_text(
        encoding="utf-8"
    )
    assert "frozen@1" in protocol
