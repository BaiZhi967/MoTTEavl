"""M2 review 贯穿性端到端样例（review 修复安排一节的必须样例）。

场景：公开 API 入口 → 数据准备（4 题、gold=B）→ 创建 Run → 实际 fake
Runner 进程（opencompass_partial：按冻结 runner-config 的前 3 题产出，
预测 B,B,A，exit 3）→ 采集 → 冻结映射导入 → 不可变 ScoreSet → 报告 → Gate。

断言：selected=4、attempted=3、correct=2、wrong=1、not_attempted=1、
Runner 非零退出；selected-case diagnostic accuracy=0.5、coverage=0.75、
执行失败；全覆盖 Gate 不通过；所有结果 ID、原始 Artifact hash 和
ScoringPass 可追溯；原生分数独立保存。
"""
import json

from fastapi.testclient import TestClient
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import SQLiteRunStore

from apps.api.app.main import create_app

ROWS = [
    {"id": "logic-1", "subject": "logic", "question": "q1", "A": "1", "B": "2",
     "C": "3", "D": "4", "answer": "B", "split": "val"},
    {"id": "logic-2", "subject": "logic", "question": "q2", "A": "1", "B": "2",
     "C": "3", "D": "4", "answer": "B", "split": "val"},
    {"id": "logic-3", "subject": "logic", "question": "q3", "A": "1", "B": "2",
     "C": "3", "D": "4", "answer": "B", "split": "val"},
    {"id": "logic-4", "subject": "logic", "question": "q4", "A": "1", "B": "2",
     "C": "3", "D": "4", "answer": "B", "split": "val"},
]


def test_e2e_sample_partial_runner_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("MOTTE_JOB_WORK_ROOT", str(tmp_path / "jobs"))

    resources = InMemoryResourceStore()
    resources.models.put({
        "id": "m-e2e", "provider": "openai", "model": "gpt-e2e",
        "lifecycle": "published", "context_window": 8192,
    })
    client = TestClient(create_app(
        store=SQLiteRunStore(tmp_path / "runs.db"), resource_store=resources,
    ))

    prepared = client.post("/api/v1/benchmarks/external/ceval/prepare", json={
        "files": {"data.jsonl": "\n".join(json.dumps(r, ensure_ascii=False) for r in ROWS)},
        "dataset_revision": "rev-e2e-1",
    })
    assert prepared.status_code == 200 and prepared.json()["rows"] == 4

    from motte_benchmark import registry
    from motte_benchmark.fake_runner import self_argv
    from motte_benchmark.opencompass.adapter import CevalJobAdapter

    registry.register_adapter("ceval-opencompass", lambda: CevalJobAdapter(
        argv=self_argv(),
        extra_env={
            "MOTTE_FAKE_MODE": "opencompass_partial",
            "MOTTE_FAKE_PREDICTIONS": "B,B,A",  # 2 对 1 错
            "MOTTE_FAKE_CASE_LIMIT": "3",
            "MOTTE_FAKE_EXIT_CODE": "3",
        },
    ))
    try:
        created = client.post("/api/v1/benchmarks/external/ceval/runs", json={
            "model": "m-e2e", "scope": "full",
        })
        assert created.status_code == 202, created.json()
        run_id = created.json()["id"]

        from motte_sdk.dispatcher import RunDispatcher

        service = client.app.state.run_service
        finished = RunDispatcher(service).dispatch(run_id)

        # ---- 执行失败（非零退出），部分结果保留。
        assert finished["status"] == "failed"
        assert finished["error"]["code"] == "JOB_NONZERO_EXIT"
        assert finished["error"]["details"]["exit_code"] == 3
        by_case = {row["case_id"]: row for row in finished["cases"]}
        assert set(by_case) == {"logic-1", "logic-2", "logic-3", "logic-4"}
        assert by_case["logic-4"]["outcome"] == "not_attempted"
        assert [by_case[f"logic-{i}"]["outcome"] for i in (1, 2, 3)] == [
            "responded",
        ] * 3

        # ---- 不可变 ScoreSet：有 gold 的结果实际评分。
        scores = {score["case_id"]: score for score in finished["scores"]}
        assert scores["logic-1"]["passed"] is True   # B vs B
        assert scores["logic-2"]["passed"] is True   # B vs B
        assert scores["logic-3"]["passed"] is False  # A vs B
        assert scores["logic-4"]["passed"] is None   # 未作答不消失

        # ---- 诊断聚合：selected-case accuracy=0.5、coverage=0.75。
        aggregate = finished["scoring_pass"]["summary"]["aggregate"]
        assert aggregate["selected"] == 4
        assert aggregate["attempted"] == 3
        assert aggregate["correct"] == 2
        assert aggregate["wrong"] == 1
        assert aggregate["not_attempted"] == 1
        assert aggregate["accuracy"] == 0.5
        assert aggregate["coverage"] == 0.75
        assert aggregate["runner_exit_code"] == 3

        # ---- 结果 ID、原始 Artifact hash 与 ScoringPass 可追溯。
        jobs = client.get(f"/api/v1/runs/{run_id}/external-jobs").json()["jobs"]
        assert len(jobs) == 1
        job = jobs[0]
        assert job["status"] == "failed"
        records = service.store.external_jobs.list_records(job["job_id"])
        assert sorted(record["source_record_key"] for record in records) == [
            "logic-1", "logic-2", "logic-3",
        ]
        evidence = job["checkpoint"]["evidence"]
        assert evidence["outcome_artifact"]
        assert evidence["raw_bundle_artifact"]
        from motte_storage.artifacts import ArtifactStore

        artifacts = ArtifactStore(tmp_path / "artifacts")
        bundle = json.loads(artifacts.read_bytes(evidence["raw_bundle_artifact"]))
        content = next(
            entry["content"] for rel, entry in bundle["files"].items()
            if "ceval-logic.json" in rel
        )
        raw_doc = json.loads(content)
        assert len(raw_doc["details"]) == 3
        assert finished["scoring_pass"]["id"]
        assert finished["scoring_pass"]["source_snapshot_hash"].startswith("sha256:")

        # ---- 原生分数独立保存（native.* 与 diagnostic.* 互不覆盖）。
        native = job["metrics"]["ceval_native"]
        assert native["ceval_logic/accuracy"] == 0.0  # Runner 自报（无 gold 视角）
        summary_metrics = finished["scoring_pass"]["summary"]["external_job_metrics"]
        assert summary_metrics["native"]["ceval_logic/accuracy"] == 0.0
        # parser 侧 diagnostic 用 Runner 报告的 gold（本场景未报告 → 0）；
        # 平台侧聚合用冻结 gold（上断言 0.5），两套口径互不覆盖。
        assert summary_metrics["diagnostic"]["per_subject"] == {"logic": 0.0}

        # ---- 全覆盖 Gate 不通过（coverage 0.75 < 1.0）；阈值规则文本含指标身份。
        gate = client.post("/api/v1/gates", json={
            "run_id": run_id,
            "policy": {
                "metric": "accuracy", "op": "gte", "threshold": 0.5,
                "required_coverage": 1.0,
            },
            "scoring_pass_id": finished["current_scoring_pass_id"],
        }).json()
        assert gate["passed"] is False
        rules = {rule["id"]: rule for rule in gate["rules"]}
        assert rules["coverage"]["passed"] is False
        assert "accuracy" in rules["metric_threshold"]["reason"]
        assert gate["report_refs"]["candidate"]["scoring_pass_id"] == (
            finished["current_scoring_pass_id"]
        )
    finally:
        registry.unregister_adapter("ceval-opencompass")
