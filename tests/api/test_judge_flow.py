"""R8：Judge 公开 API 契约（预检 / 持久 submit / 读取与历史 / 幂等取消）。

覆盖 R8 工作包要求：

* 预检与提交都由服务端解析资源、证据与计数：客户端给的 sample_count /
  observation 内容一律不接受（schema 层拒绝未知字段）；
* provider factory 不可用时**明确拒绝**提交，且不留下任何作业；
* GET / 历史 / 取消零模型调用，取消幂等；
* Invocation 的 purpose / owner / job / pass 身份在存储、API 与报告中一致；
* 多指标 Judge pass 的 Gate 跨包口径（R9 修复）：覆盖与质量按 Case 计并与
  motte_eval 的 ``denominator`` 口径一致——同一 Case 的多条指标行不冒充多个
  attempted case；覆盖不足的反例仍然必须拒绝。
"""
from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.worker.motte_worker.reporting import WorkerReporter
from apps.worker.motte_worker.runtime import WorkerLoop
from motte_contracts.evaluation import observation_evidence_hash
from motte_sdk.scoring_jobs import ScoringJobService
from motte_sdk.service import RunService
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import SQLiteRunStore

BASE_URL = "http://judge-original.local:8000/v1"
WIRE_MODEL = "wire-judge-original"
SECRET_VALUE = "sk-live-r8-api-secret-value"
JUDGE_ANSWER = {
    "criteria": [
        {"criterion_id": "task_completion", "passed": True, "reason": "done",
         "evidence": ["event:12"]},
        {"criterion_id": "constraint_adherence", "passed": True, "reason": "ok",
         "evidence": ["event:12"]},
        {"criterion_id": "evidence_grounding", "passed": True, "reason": "grounded",
         "evidence": ["event:12"]},
    ],
}


class ScriptedProvider:
    kind = "scripted"

    def __init__(self) -> None:
        self.calls: list = []

    def complete(self, request):  # noqa: ANN001 - 协议方法
        self.calls.append(request)
        return {
            "provider": "scripted",
            "model": request.model,
            "content": json.dumps(JUDGE_ANSWER),
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
            "cost": {"total": 0.0025, "price_table_version": "price-1"},
            "response_id": f"resp-{len(self.calls)}",
            "metering": {"attempts": 1},
        }


class OneCriterionFailingProvider(ScriptedProvider):
    """三条准则都判了，但有一条明确失败：Case 级合取必须据此判该 Case 未通过。"""

    def complete(self, request):  # noqa: ANN001 - 协议方法
        response = super().complete(request)
        answer = json.loads(response["content"])
        answer["criteria"][1] = {
            **answer["criteria"][1], "passed": False, "reason": "constraint broken",
        }
        return {**response, "content": json.dumps(answer)}


class RecordingFactory:
    def __init__(self, provider) -> None:  # noqa: ANN001
        self.provider = provider
        self.calls: list = []

    def __call__(self, snapshot):  # noqa: ANN001
        self.calls.append(snapshot)
        return self.provider


def observation(case_id: str = "case-1", run_id: str = "run-1") -> dict:
    payload = {
        "schema_version": 1,
        "observation_id": f"obs-{case_id}",
        "run_id": run_id,
        "case_id": case_id,
        "final_output": "alpha",
        "termination": {"reason": "final_answer"},
        "event_refs": [{"kind": "event", "run_id": run_id, "locator": "12"}],
        "artifact_refs": [],
        "coverage": {"complete": True},
        "tool_calls": [{
            "call_id": "call-1", "tool_name": "write_file",
            "arguments": {}, "status": "succeeded", "step": 1,
        }],
        "workspace": {"before": [], "after": []},
    }
    payload["evidence_hash"] = observation_evidence_hash(payload)
    return payload


def make_run(store, *, run_id: str = "run-1", case_ids=("case-1",), saved=("case-1",)) -> dict:
    store.runs.create(
        {
            "id": run_id, "schema_version": 2, "revision": 1,
            "scenario_version": "replay@1", "status": "completed",
            "manifest": {"evaluation": {"scorer_id": "deterministic", "scorer_version": "1"}},
            "requested_manifest": {}, "case_ids": list(case_ids),
            "created_at": "2026-09-21T00:00:00+00:00",
            "updated_at": "2026-09-21T00:00:00+00:00",
        },
        event={"run_id": run_id, "type": "queued", "status": "completed"},
    )
    for case_id in saved:
        store.case_runs.upsert({
            "run_id": run_id, "case_id": case_id, "outcome": "responded",
            "result": {"observation": observation(case_id, run_id)}, "expected": {},
        })
    return store.runs.get(run_id)


def seed_model(
    resources, *, model_id: str = "judge-model", wire_model: str = WIRE_MODEL,
    provider_name: str = "judge-conn", kind: str = "openai_compatible",
    with_price: bool = True, price_version: str = "price-1",
) -> None:
    if resources.providers.get(provider_name) is None:
        connection = {"name": provider_name, "kind": kind, "generation": 1, "enabled": True}
        if kind != "replay":
            connection["base_url"] = BASE_URL
            connection["credentials"] = "judge-cred-profile"
            connection["api_key_env"] = "R8_JUDGE_KEY"
        resources.providers.put(connection)
    resources.models.put({
        "id": model_id, "provider": provider_name, "model": wire_model,
        "capabilities": {"text": True}, "parameters": {"temperature": 0.0},
        "max_output_tokens": 4096, "lifecycle": "published",
        "published_at": "2026-09-21T00:00:00+00:00", "generation": 1,
    })
    if with_price:
        resources.price_tables.put({
            "model_id": model_id, "version": price_version,
            "input_per_million": 1.0, "output_per_million": 2.0,
        })


def submit_body(**overrides) -> dict:
    body = {
        "request_key": "judge-req-1",
        "run_id": "run-1",
        "mode": "single",
        "spec": {
            "judge_profile_id": "judge-answer-quality",
            "model": "judge-model",
            "rubric_id": "answer-quality",
            "rubric_version": "1",
            "parameters": {"temperature": 0.0},
            "budget": {"max_calls": 4, "max_prompt_tokens": 40_000,
                       "max_completion_tokens": 2_048},
        },
        "case_ids": ["case-1"],
        "authorisation": {"authorised": True, "actor": "operator", "max_calls": 4,
                          "max_total_tokens": 400_000},
        "publish_policy": "all_scored",
        "repeats": 1,
    }
    body.update(overrides)
    return body


def preflight_body(**overrides) -> dict:
    body = submit_body(**overrides)
    body.pop("request_key")
    return body


def environment(tmp_path, *, factory=True, provider=None):
    store = SQLiteRunStore(tmp_path / "runs.db")
    resources = InMemoryResourceStore()
    seed_model(resources)
    make_run(store)
    provider = provider or ScriptedProvider()
    api_factory = RecordingFactory(provider) if factory else None
    client = TestClient(
        create_app(store, resource_store=resources, judge_provider_factory=api_factory)
    )
    return store, resources, provider, api_factory, client


def test_judge_spec_catalog_drives_existing_preflight_contract(tmp_path):
    _store, resources, _provider, _factory, client = environment(tmp_path)
    seed_model(
        resources, model_id="replay-judge", provider_name="replay-conn",
        kind="replay", with_price=False,
    )
    resources.providers.put({
        "name": "disabled-conn", "kind": "openai_compatible", "generation": 1,
        "enabled": False, "base_url": BASE_URL, "credentials": "disabled-cred",
    })
    resources.models.put({
        "id": "disabled-judge", "provider": "disabled-conn", "model": WIRE_MODEL,
        "capabilities": {"text": True}, "parameters": {}, "max_output_tokens": 4096,
        "lifecycle": "published", "published_at": "2026-09-21T00:00:00+00:00",
        "generation": 1,
    })

    listing = client.get("/api/v1/judge-specs")
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert len(items) == 3
    assert {item["model_resource_id"] for item in items} == {"judge-model"}
    assert all(item["modes"] == ["single"] for item in items)
    selected = next(item for item in items if item["rubric"]["rubric_id"] == "answer-quality")
    assert selected["model_resource_id"] == "judge-model"
    assert selected["spec"]["model"] == "judge-model"

    detail = client.get(f"/api/v1/judge-specs/{selected['judge_id']}")
    assert detail.status_code == 200
    assert detail.json() == selected
    calibration = client.get(
        f"/api/v1/judge-specs/{selected['judge_id']}/calibration"
    )
    assert calibration.status_code == 200
    assert calibration.json()["status"] == "not_run"

    preflight = client.post(
        "/api/v1/judges/preflight",
        json=preflight_body(spec=selected["spec"]),
    )
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["model_resource_id"] == "judge-model"
    assert preflight.json()["executed"] is False


def jobs_of(store):
    """durable ScoringJob 仓库（存储层工厂）：断言"没有落作业"时使用。"""
    from motte_storage.scoring_jobs import scoring_jobs_for

    return scoring_jobs_for(store)


def run_worker(store, provider):
    service = ScoringJobService(store, provider_factory=RecordingFactory(provider))
    return WorkerLoop(
        RunService(store), reporter=WorkerReporter(io.StringIO()), scoring_jobs=service
    )


# ==================================================================== 预检

def test_preflight_is_zero_side_effect_and_counts_are_server_resolved(tmp_path):
    store, resources, provider, api_factory, client = environment(tmp_path)
    response = client.post("/api/v1/judges/preflight", json=preflight_body())
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["executed"] is False
    assert report["sample_count"] == 1  # 服务端解析的已保存 Observation 数
    assert report["max_calls"] == 1
    assert report["model"] == WIRE_MODEL
    assert report["provider_snapshot"]["model_resource_id"] == "judge-model"
    assert report["price_coverage"]["known"] is True
    assert report["price_coverage"]["price_table_version"] == "price-1"
    assert report["budget_executable"] is True
    # 预检不落作业、不构造 Provider、不调用模型。
    assert jobs_of(store).list_for_run("run-1") == []
    assert provider.calls == [] and api_factory.calls == []
    assert SECRET_VALUE not in response.text


def test_client_supplied_counts_and_observation_content_are_rejected(tmp_path):
    store, resources, provider, api_factory, client = environment(tmp_path)
    # 未知字段（sample_count / observations / estimated_*）在 schema 层被拒绝。
    for extra in (
        {"sample_count": 99},
        {"observations": {"case-1": observation()}},
        {"estimated_prompt_tokens": 1},
        {"estimated_completion_tokens": 1},
    ):
        response = client.post("/api/v1/judges", json=submit_body(**extra))
        assert response.status_code == 422, extra
    # 不属于该 Run 的 case / 没有已保存证据的 case 都在提交期拒绝。
    other = client.post(
        "/api/v1/judges", json=submit_body(request_key="r-other", case_ids=["case-9"])
    )
    assert other.status_code == 422
    assert other.json()["error"]["code"] == "JUDGE_EVIDENCE_MISSING"
    unsaved = client.post(
        "/api/v1/judges",
        json=submit_body(request_key="r-unsaved", case_ids=["case-2"]),
    )
    assert unsaved.status_code == 422
    assert unsaved.json()["error"]["code"] == "JUDGE_EVIDENCE_MISSING"
    assert client.post(
        "/api/v1/judges", json=submit_body(request_key="r-norun", run_id="run-missing")
    ).status_code == 422
    assert jobs_of(store).list_for_run("run-1") == []
    assert provider.calls == [] and api_factory.calls == []


def test_unknown_deprecated_and_non_http_judge_models_are_rejected(tmp_path):
    store, resources, provider, _api_factory, client = environment(tmp_path)
    unknown = client.post(
        "/api/v1/judges/preflight",
        json=preflight_body(spec={**submit_body()["spec"], "model": "nope"}),
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "MODEL_NOT_FOUND"

    resources.models.put(
        {
            **resources.models.get("judge-model"),
            "lifecycle": "deprecated",
            "deprecated_at": "2026-09-22T00:00:00+00:00",
            "generation": 2,
        },
        expected_generation=1,
    )
    deprecated = client.post("/api/v1/judges/preflight", json=preflight_body())
    assert deprecated.status_code == 422
    assert deprecated.json()["error"]["code"] == "MODEL_DEPRECATED"

    # replay 之类的非 HTTP adapter 不能作为 Judge Provider：必须在提交期拒绝。
    seed_model(resources, model_id="replay-judge", provider_name="replay-conn", kind="replay")
    replay = client.post(
        "/api/v1/judges/preflight",
        json=preflight_body(spec={**submit_body()["spec"], "model": "replay-judge"}),
    )
    assert replay.status_code == 422
    assert replay.json()["error"]["code"] == "JUDGE_ADAPTER_UNSUPPORTED"
    assert jobs_of(store).list_for_run("run-1") == []


def test_hard_cost_cap_without_a_known_price_is_refused(tmp_path):
    store, resources, provider, _api_factory, client = environment(tmp_path)
    seed_model(resources, model_id="unpriced-judge", with_price=False)
    body = preflight_body(
        spec={**submit_body()["spec"], "model": "unpriced-judge"},
    )
    report = client.post("/api/v1/judges/preflight", json=body)
    assert report.status_code == 200, report.text
    assert report.json()["price_coverage"]["known"] is False
    # 估算绝不冒充硬上限：未知价格下硬上限请求在提交期被拒绝。
    capped = preflight_body(
        spec={
            **submit_body()["spec"],
            "model": "unpriced-judge",
            "budget": {"max_calls": 4, "hard_cost_cap_usd": 0.01},
        },
    )
    refused = client.post("/api/v1/judges/preflight", json=capped)
    assert refused.status_code == 422
    assert jobs_of(store).list_for_run("run-1") == []
    assert provider.calls == []


def test_submit_refuses_without_a_provider_factory_and_leaves_no_job(tmp_path):
    store, resources, provider, _api_factory, client = environment(tmp_path, factory=False)
    response = client.post("/api/v1/judges", json=submit_body())
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "JUDGE_PROVIDER_UNAVAILABLE"
    assert jobs_of(store).list_for_run("run-1") == []
    assert store.scoring_passes.list_for_run("run-1") == []


# ==================================================================== 提交与历史

def test_submit_is_idempotent_and_conflicts_on_changed_content(tmp_path):
    store, resources, provider, _api_factory, client = environment(tmp_path)
    first = client.post("/api/v1/judges", json=submit_body())
    assert first.status_code == 202, first.text
    again = client.post("/api/v1/judges", json=submit_body())
    assert again.json()["job_id"] == first.json()["job_id"]
    assert again.json()["reused"] is True
    changed = client.post(
        "/api/v1/judges",
        json=submit_body(
            spec={**submit_body()["spec"], "parameters": {"temperature": 0.7}}
        ),
    )
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "SCORING_JOB_CONFLICT"
    assert len(jobs_of(store).list_for_run("run-1")) == 1


def test_judge_invocation_purpose_owner_job_and_pass_stay_consistent(tmp_path):
    store, resources, provider, _api_factory, client = environment(tmp_path)
    job = client.post("/api/v1/judges", json=submit_body()).json()
    worker = run_worker(store, provider)
    outcome = worker.claim_and_execute()
    assert outcome["status"] == "completed"
    pass_id = outcome["receipt"]["scoring_pass_id"]

    invocations = client.get("/api/v1/runs/run-1/invocations").json()["items"]
    assert len(invocations) == 1
    invocation = invocations[0]
    assert invocation["purpose"] == "judge"
    assert invocation["job_id"] == job["job_id"]
    assert invocation["kind"] == "model"
    assert invocation["run_id"] == "run-1"
    assert invocation["case_id"] == "case-1"
    assert invocation["model"] == WIRE_MODEL
    assert invocation["owner"]["kind"] == "subject"
    assert invocation["owner"]["run_id"] == "run-1"

    passes = client.get("/api/v1/runs/run-1/scoring-passes").json()["items"]
    assert len(passes) == 1
    assert passes[0]["id"] == pass_id
    assert passes[0]["purpose"] == "judge"
    assert passes[0]["job_id"] == job["job_id"]
    assert passes[0]["judge"]["owner"]["kind"] == "subject"
    assert passes[0]["judge"]["save_policy"] == "append_pass_append_trials"

    # 报告读取同一条 pass：历史切换时 scores 来自该 pass 的 ScoreSet 行。
    report = client.get(
        "/api/v1/runs/run-1/report", params={"scoring_pass_id": pass_id},
    )
    assert report.status_code == 200
    assert report.json()["scoring_pass_id"] == pass_id
    assert {score["metric_id"] for score in report.json()["scores"]} == {
        "task_completion", "constraint_adherence", "evidence_grounding",
    }

    receipt = client.get(f"/api/v1/judges/{job['job_id']}").json()["receipt"]
    assert receipt["scoring_pass_id"] == pass_id
    assert receipt["job_id"] == job["job_id"]
    assert receipt["owner"]["kind"] == "subject"
    assert receipt["cost"]["known"] is True
    assert receipt["cost"]["total_usd"] == pytest.approx(0.0025)


def test_calibration_identity_is_not_dropped_by_api_serialization(tmp_path):
    store, resources, provider, _api_factory, client = environment(tmp_path)
    body = submit_body(
        spec={
            **submit_body()["spec"],
            "calibration_version": "cal-1",
        },
    )
    job = client.post("/api/v1/judges", json=body).json()
    worker = run_worker(store, provider)
    outcome = worker.claim_and_execute()
    assert outcome["status"] == "completed"
    stored = store.scoring_passes.list_for_run("run-1")[0]
    assert stored["judge"]["calibration_version"] == "cal-1"
    # 契约模型（ScoringPass）没有 judge/purpose/job_id 字段：API 序列化不能丢掉
    # R3 的统一 pass 身份（rubric / spec / calibration / owner）。
    served = client.get("/api/v1/runs/run-1/scoring-passes").json()["items"][0]
    assert served["judge"]["calibration_version"] == "cal-1"
    assert served["judge"]["rubric_id"] == "answer-quality"
    assert served["judge"]["rubric_version"] == "1"
    assert served["judge"]["spec_sha256"] == job["judge_spec_sha256"]
    assert served["judge"]["input_selector"] == stored["judge"]["input_selector"]


def test_get_and_history_of_an_unknown_job_are_404(tmp_path):
    store, resources, provider, _api_factory, client = environment(tmp_path)
    assert client.get("/api/v1/judges/nope").status_code == 404
    assert client.get("/api/v1/runs/run-missing/judge-jobs").status_code == 404
    assert client.post("/api/v1/judges/nope/cancel", json={}).status_code == 404


# ==================================================================== 跨包阻断

POLICY_FOR_ONE_CASE = {
    "metric": "accuracy", "op": "gte", "threshold": 0.5, "required_coverage": 1.0,
}


def test_gate_on_a_multi_metric_judge_pass_is_not_blocked_by_case_coverage(tmp_path):
    """多指标 Judge pass 的 gate 不再被"3 个 ScoreSet 行 vs 1 个 case"误判。

    R9 修复：候选覆盖与质量按 **Case** 计，并与 motte_eval 的 denominator 口径
    一致——同一 Case 的 3 条指标行只算一个 attempted case，因此 1 个 case 的 Run
    不会抛出 ValueError("attempted 3 exceeds selected 1")。
    """
    store, resources, provider, _api_factory, client = environment(tmp_path)
    client.post("/api/v1/judges", json=submit_body())
    worker = run_worker(store, provider)
    assert worker.claim_and_execute()["status"] == "completed"
    scores = store.score_sets.list_for_pass(
        store.runs.get("run-1")["current_scoring_pass_id"]
    )
    assert len(scores) == 3  # 一个 case 的三条指标行
    assert all(score["denominator"] is True for score in scores)

    response = client.post(
        "/api/v1/gates",
        json={"run_id": "run-1", "policy": {"min_pass_rate": 0.5}},
    )
    assert response.status_code == 200, response.text

    coverage = client.post(
        "/api/v1/gates",
        json={"run_id": "run-1", "policy": dict(POLICY_FOR_ONE_CASE)},
    )
    assert coverage.status_code == 200, coverage.text
    body = coverage.json()
    summary = body["coverage_summary"]
    assert summary["selected"] == 1
    assert summary["attempted"] == 1  # 3 条指标行 ≠ 3 个 case
    assert summary["not_attempted"] == 0
    assert summary["coverage"] == 1.0
    # 三条准则全部通过 = 该 Case 通过；accuracy 是 Case 口径的 ratio，不会超过 1。
    assert summary["metric_values"]["accuracy"] == 1.0
    assert body["passed"] is True, body["rules"]


def test_gate_quality_is_a_per_case_conjunction_not_a_metric_row_count(tmp_path):
    """一条准则失败 = 该 Case 未通过：accuracy 不会超过 1，也不会被多行抬分。"""
    provider = OneCriterionFailingProvider()
    store = SQLiteRunStore(tmp_path / "runs.db")
    resources = InMemoryResourceStore()
    seed_model(resources)
    make_run(store)
    client = TestClient(create_app(
        store, resource_store=resources,
        judge_provider_factory=RecordingFactory(provider),
    ))
    client.post("/api/v1/judges", json=submit_body())
    worker = run_worker(store, provider)
    assert worker.claim_and_execute()["status"] == "completed"
    assert len(store.score_sets.list_for_pass(
        store.runs.get("run-1")["current_scoring_pass_id"]
    )) == 3

    response = client.post(
        "/api/v1/gates",
        json={"run_id": "run-1", "policy": dict(POLICY_FOR_ONE_CASE)},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    summary = body["coverage_summary"]
    assert summary["attempted"] == 1
    assert summary["metric_values"]["accuracy"] == 0.0
    assert body["passed"] is False
    threshold = next(rule for rule in body["rules"] if rule["id"] == "metric_threshold")
    assert threshold["passed"] is False
    assert "accuracy=0.0 (ratio) not gte 0.5" in threshold["reason"]


def test_gate_still_refuses_when_multi_metric_case_coverage_is_incomplete(tmp_path):
    """覆盖不足仍然拒绝：两条 case 只判了一条，多指标行不能把缺的 case 补上。"""
    store = SQLiteRunStore(tmp_path / "runs.db")
    resources = InMemoryResourceStore()
    seed_model(resources)
    make_run(store, case_ids=("case-1", "case-2"), saved=("case-1",))
    provider = ScriptedProvider()
    client = TestClient(create_app(
        store, resource_store=resources,
        judge_provider_factory=RecordingFactory(provider),
    ))
    client.post("/api/v1/judges", json=submit_body())
    worker = run_worker(store, provider)
    assert worker.claim_and_execute()["status"] == "completed"
    scores = store.score_sets.list_for_pass(
        store.runs.get("run-1")["current_scoring_pass_id"]
    )
    assert len(scores) == 3  # 已判的 case-1 的三条指标行

    response = client.post(
        "/api/v1/gates",
        json={"run_id": "run-1", "policy": dict(POLICY_FOR_ONE_CASE)},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    summary = body["coverage_summary"]
    assert summary["selected"] == 2
    assert summary["attempted"] == 1
    assert summary["not_attempted"] == 1
    assert summary["coverage"] == 0.5
    assert body["passed"] is False
    rules = {rule["id"]: rule for rule in body["rules"]}
    assert rules["coverage"]["passed"] is False
    assert "coverage 0.5 < required 1.0" in rules["coverage"]["reason"]
