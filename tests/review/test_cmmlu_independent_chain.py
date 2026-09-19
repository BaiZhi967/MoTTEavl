"""M2 review R16：CMMLU 独立可执行链路（不借 C-Eval 测试结果）。

独立 suite/adapter/parser 命名空间 + 通用 API 面 + 独立确定性 Runner
（opencompass_ok + MOTTE_FAKE_DATASET=cmmlu，学科 logical）走完
prepare → run → dispatch → 评分 → 报告。
"""
import json

from fastapi.testclient import TestClient
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import SQLiteRunStore

from apps.api.app.main import create_app

ROWS = [
    {"id": "cm-1", "subject": "logical", "question": "q1", "A": "1", "B": "2",
     "C": "3", "D": "4", "answer": "B", "split": "test"},
    {"id": "cm-2", "subject": "logical", "question": "q2", "A": "1", "B": "2",
     "C": "3", "D": "4", "answer": "B", "split": "test"},
]


def test_cmmlu_independent_end_to_end_chain(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("MOTTE_JOB_WORK_ROOT", str(tmp_path / "jobs"))

    resources = InMemoryResourceStore()
    resources.models.put({
        "id": "m-cmmlu", "provider": "openai", "model": "gpt-cmmlu",
        "lifecycle": "published", "context_window": 8192,
    })
    client = TestClient(create_app(
        store=SQLiteRunStore(tmp_path / "runs.db"), resource_store=resources,
    ))

    # 目录同时注册 ceval 与 cmmlu：条目互不继承。
    catalog = client.get("/api/v1/benchmarks/external/catalog").json()["benchmarks"]
    assert {entry["benchmark_id"] for entry in catalog} == {"ceval", "cmmlu"}

    prepared = client.post("/api/v1/benchmarks/external/cmmlu/prepare", json={
        "files": {"cmmlu_test.jsonl": "\n".join(
            json.dumps(row, ensure_ascii=False) for row in ROWS
        )},
        "dataset_revision": "cmmlu-rev-1",
    })
    assert prepared.status_code == 200, prepared.json()
    assert prepared.json()["rows"] == 2

    # ceval 拒绝 CMMLU 学科（白名单独立）；cmmlu 自身可用。
    cross = client.post("/api/v1/benchmarks/external/ceval/prepare", json={
        "files": {"x.jsonl": json.dumps(ROWS[0], ensure_ascii=False)},
        "dataset_revision": "ceval-cross-1",
    })
    assert cross.status_code == 422
    assert any(
        reason.startswith("UNKNOWN_SUBJECT")
        for reason in cross.json()["error"]["details"]["reasons"]
    )

    from motte_benchmark import registry
    from motte_benchmark.fake_runner import self_argv
    from motte_benchmark.opencompass.adapter import CmmluJobAdapter

    registry.register_adapter("cmmlu-opencompass", lambda: CmmluJobAdapter(
        argv=self_argv(),
        extra_env={"MOTTE_FAKE_MODE": "opencompass_ok", "MOTTE_FAKE_DATASET": "cmmlu"},
    ))
    try:
        status = client.get("/api/v1/benchmarks/external/catalog").json()["benchmarks"]
        cmmlu_entry = next(e for e in status if e["benchmark_id"] == "cmmlu")
        assert cmmlu_entry["status"] == "runnable"

        created = client.post("/api/v1/benchmarks/external/cmmlu/runs", json={
            "model": "m-cmmlu", "scope": "full",
        })
        assert created.status_code == 202, created.json()
        run_id = created.json()["id"]
        assert created.json()["scenario_version"] == "cmmlu-external@1"

        from motte_sdk.dispatcher import RunDispatcher

        service = client.app.state.run_service
        finished = RunDispatcher(service).dispatch(run_id)
        assert finished["status"] == "completed"
        assert finished["manifest"]["benchmark_provenance"]["suite"] == "cmmlu-external"
        # 独立确定性 Runner：cmmlu-logical.json 的 2 行经冻结映射评分。
        scores = {score["case_id"]: score["passed"] for score in finished["scores"]}
        assert scores == {"cm-1": True, "cm-2": False}
        aggregate = finished["scoring_pass"]["summary"]["aggregate"]
        assert aggregate == {
            "selected": 2, "attempted": 2, "correct": 1, "wrong": 1,
            "not_attempted": 0, "accuracy": 0.5, "coverage": 1.0,
            "unscored": False, "runner_exit_code": 0,
            "denominator": "selected_cases",
        }
        jobs = client.get(f"/api/v1/runs/{run_id}/external-jobs").json()["jobs"]
        native = jobs[0]["metrics"]["ceval_native"]
        assert native["cmmlu_logical/accuracy"] == 0.5
        # 聚合不产生 C-Eval 四大类。
        diagnostic = jobs[0]["metrics"]["ceval_diagnostic"]
        assert not [key for key in diagnostic["aggregate"] if key.startswith("ceval_")]
        assert diagnostic["per_subject"] == {"logical": 0.5}
    finally:
        registry.unregister_adapter("cmmlu-opencompass")


def test_cmmlu_cli_group_exists():
    from motte_cli.main import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["cmmlu", "preflight"])
    assert args.command == "cmmlu"
    assert args.cmmlu_command == "preflight"
    args = parser.parse_args(["cmmlu", "run", "--model", "m", "--files", "a=b",
                              "--revision", "r"])
    assert args.scope == "custom-subset"
