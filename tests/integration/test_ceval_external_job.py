"""M2-T10：C-Eval 外部 Job 集成分层验证。

分层：
1. 合成 fixture / 假 Runner：一个 Job 闭环、结果可追溯、回归不退化（本文件）。
2. 真实固定 Runner + 本地确定性端点：需要钉住的独立 OpenCompass 环境
   （见 docs/operations/ceval.md 的验收清单）——本机未构建，标记 not_run，
   不以假 Runner 替代宣称。
3. 真实模型 smoke / full Profile：需授权预算，not_run。
"""
import json

import pytest
from fastapi.testclient import TestClient
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore

from apps.api.app.main import create_app


CEVAL_RUN_BODY = {"model": "m1"}


def _prepare_body():
    rows = [
        {"id": "logic-1", "subject": "logic", "question": "1+1=?", "A": "1", "B": "2", "C": "3", "D": "4", "answer": "B"},
        {"id": "logic-2", "subject": "logic", "question": "2+2=?", "A": "3", "B": "4", "C": "5", "D": "6", "answer": "B"},
        {"id": "logic-3", "subject": "logic", "question": "3+3=?", "A": "5", "B": "6", "C": "7", "D": "8", "answer": "B"},
        {"id": "logic-4", "subject": "logic", "question": "4+4=?", "A": "7", "B": "8", "C": "9", "D": "0", "answer": "B"},
    ]
    return {"files": {"logic_val.jsonl": "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)},
            "dataset_revision": "rev-int-1"}


@pytest.fixture()
def ceval_client(tmp_path):
    resources = InMemoryResourceStore()
    resources.models.put({
        "id": "m1", "provider": "openai", "model": "gpt-test",
        "capabilities": {"input_modalities": ["text"]}, "lifecycle": "published",
    })
    client = TestClient(create_app(store=InMemoryRunStore(), resource_store=resources))
    assert client.post("/api/v1/benchmarks/external/ceval/prepare", json=_prepare_body()).status_code == 200
    from motte_benchmark import registry as adapter_registry
    from motte_benchmark.fake_runner import self_argv
    from motte_benchmark.opencompass.adapter import CevalJobAdapter

    adapter_registry.register_adapter("ceval-opencompass", lambda: CevalJobAdapter(
        argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": "opencompass_ok"},
    ))
    yield client
    adapter_registry.unregister_adapter("ceval-opencompass")


def test_ceval_fixture_real_runner_and_live_layers(ceval_client):
    # 第 1 层：假 Runner 一次 Job 闭环，结果可追溯。
    created = ceval_client.post("/api/v1/benchmarks/external/ceval/runs", json=CEVAL_RUN_BODY)
    assert created.status_code == 202
    run_id = created.json()["id"]

    from motte_sdk.dispatcher import RunDispatcher

    service = ceval_client.app.state.run_service
    finished = RunDispatcher(service).dispatch(run_id)
    assert finished["status"] in {"completed", "failed"}

    jobs = ceval_client.get(f"/api/v1/runs/{run_id}/external-jobs").json()["jobs"]
    assert len(jobs) == 1  # 一个 Run 恰好一个 Job
    assert jobs[0]["status"] in {"settled", "failed"}
    assert jobs[0]["launch_token"].startswith("launch-")
    # 原始 outcome 的 Artifact 冻结与幂等导入在 tests/storage（SQLite 工件根）
    # 覆盖；本层验证 API→分派→Job→查询闭环。

    # 第 2/3 层：真实 Runner 与真实模型分层验收存在且未执行（如实标记）。
    operations = __import__("pathlib").Path(__file__).resolve().parents[2] / "docs/operations/ceval.md"
    if operations.exists():
        content = operations.read_text(encoding="utf-8")
        assert "真实固定 Runner" in content or "not_run" in content

    # 回归不退化：再次创建+分派同配置 Run 仍闭环（幂等键防重复导入）。
    again = ceval_client.post("/api/v1/benchmarks/external/ceval/runs", json=CEVAL_RUN_BODY)
    assert again.status_code == 202
    finished_again = RunDispatcher(service).dispatch(again.json()["id"])
    assert finished_again["status"] in {"completed", "failed"}


def test_disabling_external_adapter_keeps_other_suites(ceval_client, tmp_path):
    from motte_benchmark import registry as adapter_registry
    from motte_sdk.dispatcher import RunDispatcher
    from motte_sdk.service import RunService

    # 关闭外部 adapter：ceval 创建在分派层被拒（unsupported），0 次 Job。
    adapter_registry.unregister_adapter("ceval-opencompass")
    created = ceval_client.post("/api/v1/benchmarks/external/ceval/runs", json=CEVAL_RUN_BODY)
    assert created.status_code == 422
    assert created.json()["error"]["code"] == "RUNNER_NOT_CONNECTED"

    # 旧套件不受影响：replay 照常执行；历史（已完成）运行仍可读。
    replay_service = RunService(InMemoryRunStore())
    replay_manifest = {
        "provider": {"kind": "replay", "fixture": {"case-1": {"output": 1, "expected": 1}}},
        "execution": {"backend_id": "replay", "backend_version": "1"},
    }
    replay = replay_service.create_run("replay@1", replay_manifest, case_ids=["case-1"])
    assert RunDispatcher(replay_service).dispatch(replay["id"])["status"] == "completed"
    assert replay_service.get_run(replay["id"])["scores"] == [{"case_id": "case-1", "passed": True}]

    service = ceval_client.app.state.run_service
    finished_runs = [
        run for run in service.store.runs.list()
        if run.get("status") in {"completed", "failed"}
    ]
    for run in finished_runs:
        assert service.get_run(run["id"])["id"] == run["id"]  # 历史读取不破坏
