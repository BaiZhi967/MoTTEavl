"""M2-T07：C-Eval 公共入口（API 预检与入队）。

反例与期望：
- Runner 不存在（adapter 未注册）→ 创建 422，Job/模型调用 0 次。
- 未准备数据 → 422 DATASET_UNPREPARED。
- 合成数据 prepare + 注册 adapter 后 → 202 真实排队；分派后 Job 记录
  可查询；全程无 Provider 调用（假 Runner 离线）。
- 静态预检只读：返回原因清单，不触发模型探针。
"""
import pytest
from fastapi.testclient import TestClient
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore

from apps.api.app.main import create_app


def _client():
    return TestClient(create_app(store=InMemoryRunStore(), resource_store=InMemoryResourceStore()))


def _prepare_body():
    import json

    rows = [
        {"id": "logic-1", "subject": "logic", "question": "1+1=?", "A": "1", "B": "2", "C": "3", "D": "4", "answer": "B"},
        {"id": "logic-2", "subject": "logic", "question": "2+2=?", "A": "3", "B": "4", "C": "5", "D": "6", "answer": "B"},
    ]
    return {"files": {"logic_val.jsonl": "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)},
            "dataset_revision": "rev-api-1"}


def test_ceval_preflight_and_enqueue():
    client = _client()

    # 未准备数据：创建前失败，0 次调用。
    response = client.post("/api/v1/benchmarks/external/ceval/runs", json={"model": "m1"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DATASET_UNPREPARED"

    prepared = client.post("/api/v1/benchmarks/external/ceval/prepare", json=_prepare_body())
    assert prepared.status_code == 200
    assert prepared.json()["state"] == "ready"
    assert prepared.json()["provenance"] == "user-supplied"

    # 数据已备、Runner 不存在：仍 422（RUNNER_NOT_CONNECTED），Job 0 次。
    response = client.post("/api/v1/benchmarks/external/ceval/runs", json={"model": "m1"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "RUNNER_NOT_CONNECTED"
    jobs = client.get("/api/v1/benchmarks/external/catalog").json()["benchmarks"]
    assert jobs[0]["benchmark_id"] == "ceval"
    assert jobs[0]["status"] == "prepared"
    assert "RUNNER_NOT_CONNECTED" in jobs[0]["blockers"]

    # 注册 job 适配器（离线假 Runner，写 OpenCompass 形态输出）→ runnable。
    from motte_benchmark.fake_runner import self_argv
    from motte_benchmark.opencompass.adapter import CevalJobAdapter
    from motte_benchmark import registry as adapter_registry

    adapter_registry.register_adapter("ceval-opencompass", lambda: CevalJobAdapter(
        argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": "opencompass_ok"},
    ))
    try:
        catalog = client.get("/api/v1/benchmarks/external/catalog").json()["benchmarks"]
        assert catalog[0]["status"] == "runnable"

        # 静态预检：只读原因清单，不触发模型探针（未知模型给原因）。
        preflight = client.get("/api/v1/benchmarks/external/ceval/preflight", params={"model": "m1"})
        assert preflight.status_code == 200
        assert preflight.json()["ok"] is False
        assert any(reason.startswith("MODEL_") for reason in preflight.json()["reasons"])

        response = client.post("/api/v1/benchmarks/external/ceval/runs", json={"model": "m1"})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "MODEL_IDENTITY_MISSING"

        # 发布一个模型档案后：202 真实排队。
        resources = InMemoryResourceStore()
        resources.models.put({
            "id": "m1",
            "provider": "openai",
            "model": "gpt-test",
            "capabilities": {"input_modalities": ["text"]},
            "lifecycle": "published",
        })
        client2 = TestClient(create_app(store=InMemoryRunStore(), resource_store=resources))
        prepared2 = client2.post("/api/v1/benchmarks/external/ceval/prepare", json=_prepare_body())
        assert prepared2.status_code == 200
        adapter_registry.register_adapter("ceval-opencompass", lambda: CevalJobAdapter(
            argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": "opencompass_ok"},
        ), replace=True)
        response = client2.post("/api/v1/benchmarks/external/ceval/runs", json={"model": "m1"})
        assert response.status_code == 202
        run_id = response.json()["id"]
        assert response.json()["status"] == "queued"

        # 分派：一个 Job 终态；无 Provider 调用（假 Runner 离线）。
        from motte_sdk.dispatcher import RunDispatcher
        from apps.api.app.main import create_app as _create  # noqa: F401

        service = client2.app.state.run_service
        finished = RunDispatcher(service).dispatch(run_id)
        assert finished["status"] in {"completed", "failed"}
        jobs_response = client2.get(f"/api/v1/runs/{run_id}/external-jobs").json()["jobs"]
        assert len(jobs_response) == 1
        assert jobs_response[0]["status"] in {"settled", "failed"}
        assert jobs_response[0]["launch_token"].startswith("launch-")
        # 模型调用 0：run 的执行后端是外部 Job，不经 Provider。
        assert finished["manifest"]["execution"]["backend_id"] == "external-benchmark"
    finally:
        adapter_registry.unregister_adapter("ceval-opencompass")


def test_prepare_governance_block_maps_to_422():
    client = _client()
    body = _prepare_body()
    body["provenance_target"] = "verified-official"
    body["approval_evidence"] = {
        "actor": "operator", "purpose": "p", "ticket": "T", "source_id": "ceval",
        "actions": ["import"], "approved_by": "operator",
        "issued_at": "2026-09-19T00:00:00Z", "expires_at": "2027-09-19T00:00:00Z",
    }
    response = client.post("/api/v1/benchmarks/external/ceval/prepare", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DATASET_PREPARE_FAILED"
    assert "OFFICIAL_APPROVAL_UNVERIFIED" in response.json()["error"]["details"]["reasons"]


def test_openapi_declares_external_endpoints():
    client = _client()
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    assert "/api/v1/benchmarks/external/ceval/prepare" in paths
    assert "/api/v1/benchmarks/external/ceval/runs" in paths
    assert "/api/v1/benchmarks/external/ceval/preflight" in paths
    assert "/api/v1/benchmarks/external/catalog" in paths
