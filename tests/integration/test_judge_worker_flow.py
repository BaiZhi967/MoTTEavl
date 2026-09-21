"""R8：Judge 冻结 Provider 快照 + 同一 Worker 执行链路的集成测试。

覆盖 R8 工作包要求：

* 提交期解析**已发布** ModelProfile / ProviderConnection / 价格版本并冻结进作业；
  资源随后改名或升级，旧作业仍只使用原快照，绝不按可变名称重新选模型；
* 秘密明文不进入 job / manifest / event / log（快照只保存非秘密凭据引用）；
* 公开 API -> 同一 WorkerLoop -> 新 ScoringPass -> 历史切换 / 取消；
* 领取顺序不会让 Judge 作业在 Run 持续入队时饿死（反之亦然）；
* prepared / dispatching / 响应已落地 / 发布事务中断 / 提交后通知失败各崩溃窗口：
  不重复计费、终态原子可见、pending/failed 不推进 current。
"""
from __future__ import annotations

import io
import json
from copy import deepcopy

from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.worker.motte_worker.reporting import WorkerReporter
from apps.worker.motte_worker.runtime import WorkerLoop
from motte_contracts.evaluation import observation_evidence_hash
from motte_sdk.scoring_jobs import (
    FrozenProviderFactory,
    JudgeProviderSnapshot,
    ScoringJobService,
)
from motte_sdk.service import RunService
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import SQLiteRunStore

ORIGINAL_BASE_URL = "http://judge-original.local:8000/v1"
REPLACED_BASE_URL = "http://judge-replaced.local:9000/v1"
ORIGINAL_WIRE_MODEL = "wire-judge-original"
REPLACED_WIRE_MODEL = "wire-judge-replaced"
ORIGINAL_CREDENTIAL = "judge-cred-profile"
REPLACED_CREDENTIAL = "replaced-cred-profile"
#: 只存在于凭据链（环境变量）里的真实秘密：任何持久化载体都不得包含它。
SECRET_VALUE = "sk-live-r8-judge-secret-value"
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
REPLAY_MANIFEST = {
    "provider": {
        "kind": "replay",
        "fixture": {"case-1": {"output": {"n": 1}, "expected": {"n": 1}}},
    }
}


class ScriptedProvider:
    """实现 Provider 协议的脚本化对象：记录请求、可注入故障。"""

    kind = "scripted"

    def __init__(self, responses: list[str] | None = None, *, fail_with=None) -> None:
        self.responses = list(responses or [json.dumps(JUDGE_ANSWER)])
        self.fail_with = fail_with
        self.calls: list = []

    def complete(self, request):  # noqa: ANN001 - 协议方法
        self.calls.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        content = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        return {
            "provider": "scripted",
            "model": request.model,
            "content": content,
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
            "cost": {"total": 0.0025, "price_table_version": "price-1"},
            "response_id": f"resp-{len(self.calls)}",
            "metering": {"attempts": 1},
        }


class RecordingFactory:
    """记录每次构造收到的冻结快照；API 侧必须一次都不被调用。"""

    def __init__(self, provider) -> None:  # noqa: ANN001
        self.provider = provider
        self.calls: list[dict] = []

    def __call__(self, snapshot):  # noqa: ANN001 - provider factory 协议
        self.calls.append(
            snapshot.model_dump(mode="json")
            if hasattr(snapshot, "model_dump")
            else {"model": str(snapshot)}
        )
        return self.provider


def observation(case_id: str = "case-1", run_id: str = "run-1", **overrides) -> dict:
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
    payload.update(overrides)
    payload["evidence_hash"] = observation_evidence_hash(payload)
    return payload


def make_completed_run(
    store, *, run_id: str = "run-1", case_ids: tuple[str, ...] = ("case-1",),
) -> dict:
    """已冻结证据的 subject Run：Judge 只读它保存的 Observation。"""
    store.runs.create(
        {
            "id": run_id,
            "schema_version": 2,
            "revision": 1,
            "scenario_version": "replay@1",
            "status": "completed",
            "manifest": {"evaluation": {"scorer_id": "deterministic", "scorer_version": "1"}},
            "requested_manifest": {},
            "case_ids": list(case_ids),
            "created_at": "2026-09-21T00:00:00+00:00",
            "updated_at": "2026-09-21T00:00:00+00:00",
        },
        event={"run_id": run_id, "type": "queued", "status": "completed"},
    )
    for case_id in case_ids:
        store.case_runs.upsert({
            "run_id": run_id,
            "case_id": case_id,
            "outcome": "responded",
            "result": {"observation": observation(case_id=case_id, run_id=run_id)},
            "expected": {},
        })
    return store.runs.get(run_id)


def seed_resources(
    resources, *, base_url: str = ORIGINAL_BASE_URL,
    wire_model: str = ORIGINAL_WIRE_MODEL, credentials: str = ORIGINAL_CREDENTIAL,
    provider_name: str = "judge-conn", model_id: str = "judge-model",
    price_version: str = "price-1", price_in: float = 1.0, price_out: float = 2.0,
) -> None:
    resources.providers.put({
        "name": provider_name,
        "kind": "openai_compatible",
        "base_url": base_url,
        "credentials": credentials,
        "api_key_env": "R8_JUDGE_KEY",
        "generation": 1,
        "enabled": True,
    })
    resources.models.put({
        "id": model_id,
        "provider": provider_name,
        "model": wire_model,
        "capabilities": {"text": True},
        "parameters": {"temperature": 0.0},
        "max_output_tokens": 4096,
        "lifecycle": "published",
        "published_at": "2026-09-21T00:00:00+00:00",
        "generation": 1,
    })
    resources.price_tables.put({
        "model_id": model_id,
        "version": price_version,
        "input_per_million": price_in,
        "output_per_million": price_out,
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
            "budget": {
                "max_calls": 4,
                "max_prompt_tokens": 40_000,
                "max_completion_tokens": 2_048,
            },
        },
        "case_ids": ["case-1"],
        "authorisation": {
            "authorised": True,
            "actor": "operator",
            "max_calls": 4,
            "max_total_tokens": 400_000,
        },
        "publish_policy": "all_scored",
        "repeats": 1,
    }
    body.update(overrides)
    return body



# ==================================================================== CLI

def test_cli_submit_status_history_and_cancel_share_the_api_compiler(tmp_path, capsys):
    """CLI 与 API 共用同一编译路径：冻结同一份快照，执行仍由 Worker 完成。"""
    from motte_cli.main import main as cli_main
    from motte_storage.resource_store import SQLiteResourceStore

    path = tmp_path / "cli-runs.db"
    store = SQLiteRunStore(path)
    resources = SQLiteResourceStore(path)
    seed_resources(resources)
    make_completed_run(store)
    provider = ScriptedProvider()

    spec = submit_body(request_key="cli-req-1")
    assert cli_main(["judge", "submit", "--spec", json.dumps(spec), "--db", str(path)]) == 0
    submitted = json.loads(capsys.readouterr().out)
    assert submitted["status"] == "queued"
    assert submitted["provider_snapshot"]["model"] == ORIGINAL_WIRE_MODEL
    assert submitted["provider_snapshot"]["endpoint"] == ORIGINAL_BASE_URL
    job_id = submitted["job_id"]

    # 读取与历史：零模型调用、零新作业。
    assert cli_main(["judge", "status", job_id, "--db", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "queued"
    assert cli_main(["judge", "history", "--run", "run-1", "--db", str(path)]) == 0
    history = json.loads(capsys.readouterr().out)
    assert [item["job_id"] for item in history["items"]] == [job_id]
    assert provider.calls == []

    # 执行仍由 WorkerLoop 完成：CLI 不构造 Provider、不调用模型。
    worker = worker_for(store, provider)
    outcome = worker.claim_and_execute()
    assert outcome["status"] == "completed"
    assert len(provider.calls) == 1
    assert cli_main(["judge", "status", job_id, "--db", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"

    # 第二个作业：CLI 取消幂等，且不发布 pass。
    second = submit_body(request_key="cli-req-2")
    assert cli_main(["judge", "submit", "--spec", json.dumps(second), "--db", str(path)]) == 0
    second_id = json.loads(capsys.readouterr().out)["job_id"]
    assert cli_main(["judge", "cancel", second_id, "--db", str(path)]) == 0
    cancelled = json.loads(capsys.readouterr().out)
    assert cancelled["outcome"] == "cancelled"
    assert cli_main(["judge", "cancel", second_id, "--db", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "already_cancelled"
    assert len(store.scoring_passes.list_for_run("run-1")) == 1
    assert len(provider.calls) == 1

    # 未知作业：稳定的非 0 退出码 + 结构化错误。
    assert cli_main(["judge", "status", "sjob-missing", "--db", str(path)]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "JUDGE_NOT_FOUND"

def preflight_body(**overrides) -> dict:
    body = submit_body(**overrides)
    body.pop("request_key")
    return body


def build_environment(tmp_path, *, provider=None):
    """共享同一持久存储的 API + Worker 环境（R8 的公开链路）。"""
    store = SQLiteRunStore(tmp_path / "runs.db")
    resources = InMemoryResourceStore()
    seed_resources(resources)
    make_completed_run(store)
    provider = provider or ScriptedProvider()
    api_factory = RecordingFactory(provider)
    client = TestClient(
        create_app(store, resource_store=resources, judge_provider_factory=api_factory)
    )
    return store, resources, provider, api_factory, client


def worker_for(store, provider, *, stream: io.StringIO | None = None) -> WorkerLoop:
    factory = RecordingFactory(provider)
    service = ScoringJobService(store, provider_factory=factory)
    loop = WorkerLoop(
        RunService(store),
        reporter=WorkerReporter(stream or io.StringIO()),
        scoring_jobs=service,
    )
    loop.judge_factory = factory  # 测试专用：保留快照记录
    return loop


def assert_no_secret(payloads: dict[str, object]) -> None:
    for name, payload in payloads.items():
        text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
        assert SECRET_VALUE not in text, f"{name} leaked the credential plaintext"

def jobs_of(store):
    """durable ScoringJob 仓库（存储层工厂）：断言"没有落作业"时使用。"""
    from motte_storage.scoring_jobs import scoring_jobs_for

    return scoring_jobs_for(store)


# ==================================================================== 冻结快照

def test_submitted_job_keeps_the_frozen_snapshot_after_rename_and_upgrade(
    tmp_path, monkeypatch,
):
    """提交后改名/升级，旧作业仍使用原快照；新提交看到新资源。"""
    monkeypatch.setenv("R8_JUDGE_KEY", SECRET_VALUE)
    store, resources, provider, api_factory, client = build_environment(tmp_path)

    submitted = client.post("/api/v1/judges", json=submit_body())
    assert submitted.status_code == 202, submitted.text
    job = submitted.json()
    frozen = job["provider_snapshot"]
    assert frozen["model_resource_id"] == "judge-model"
    assert frozen["model"] == ORIGINAL_WIRE_MODEL
    assert frozen["endpoint"] == ORIGINAL_BASE_URL
    assert frozen["adapter_id"] == "openai_compatible"
    assert frozen["credential_ref"] == ORIGINAL_CREDENTIAL
    assert frozen["api_key_env"] == "R8_JUDGE_KEY"
    assert frozen["price_table_version"] == "price-1"
    assert frozen["snapshot_sha256"].startswith("sha256:")
    # 提交只解析资源：API 进程不构造 Provider、更不调用模型。
    assert api_factory.calls == [] and provider.calls == []

    # 提交后改名 / 升级：连接就地换端点与凭据引用；价格表出现新版本；
    # 已发布模型档案不可改写，只能弃用（这正是"升级"在资源层的表现）。
    resources.providers.put({
        **resources.providers.get("judge-conn"),
        "base_url": REPLACED_BASE_URL,
        "credentials": REPLACED_CREDENTIAL,
        "generation": 2,
    })
    resources.price_tables.put({
        "model_id": "judge-model", "version": "price-2",
        "input_per_million": 100.0, "output_per_million": 200.0,
    })
    resources.models.put(
        {
            **resources.models.get("judge-model"),
            "lifecycle": "deprecated",
            "deprecated_at": "2026-09-22T00:00:00+00:00",
            "generation": 2,
        },
        expected_generation=1,
    )
    # 新提交按当前资源解析：旧模型已弃用被拒绝，新模型看到替换后的端点。
    deprecated = client.post("/api/v1/judges/preflight", json=preflight_body())
    assert deprecated.status_code == 422
    assert deprecated.json()["error"]["code"] == "MODEL_DEPRECATED"
    seed_resources(
        resources, base_url=REPLACED_BASE_URL, wire_model=REPLACED_WIRE_MODEL,
        credentials=REPLACED_CREDENTIAL, provider_name="judge-conn",
        model_id="judge-model-2", price_version="price-2",
    )
    refreshed = client.post(
        "/api/v1/judges/preflight",
        json=preflight_body(spec={**submit_body()["spec"], "model": "judge-model-2"}),
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["provider_snapshot"]["endpoint"] == REPLACED_BASE_URL

    # 旧作业仍然只消费原快照：Worker 构造 Provider 时收到的是提交期冻结的内容。
    worker = worker_for(store, provider)
    outcome = worker.claim_and_execute()
    assert outcome is not None
    assert len(worker.judge_factory.calls) == 1
    seen = worker.judge_factory.calls[0]
    assert seen["snapshot_sha256"] == frozen["snapshot_sha256"]
    assert seen["model"] == ORIGINAL_WIRE_MODEL
    assert seen["endpoint"] == ORIGINAL_BASE_URL
    assert seen["credential_ref"] == ORIGINAL_CREDENTIAL
    assert seen["price_table_version"] == "price-1"
    assert [request.model for request in provider.calls] == [ORIGINAL_WIRE_MODEL]

    passes = store.scoring_passes.list_for_run("run-1")
    assert len(passes) == 1
    assert passes[0]["judge"]["model"] == ORIGINAL_WIRE_MODEL
    assert passes[0]["judge"]["provider_snapshot_sha256"] == frozen["snapshot_sha256"]
    assert store.runs.get("run-1")["current_scoring_pass_id"] == passes[0]["id"]

    # 秘密明文不进入 job / pass / invocation / event / report / log。
    assert_no_secret({
        "job": jobs_of(store).get(job["job_id"]),
        "pass": passes[0],
        "invocation": store.invocations.list_for_run("run-1"),
        "events": store.events.list_for_run("run-1"),
        "report": client.get("/api/v1/runs/run-1/report").json(),
        "job_view": client.get(f"/api/v1/judges/{job['job_id']}").json(),
        "worker_log": worker.reporter.stream.getvalue(),
        "snapshot": frozen,
    })


def test_frozen_factory_resolves_the_secret_reference_only_when_building(
    tmp_path, monkeypatch,
):
    """Worker 从快照构造 Provider；秘密引用只在构造期解析，快照里没有明文。"""
    monkeypatch.setenv("R8_JUDGE_KEY", SECRET_VALUE)
    store, resources, provider, _api_factory, client = build_environment(tmp_path)
    job = client.post("/api/v1/judges", json=submit_body()).json()
    frozen = JudgeProviderSnapshot.model_validate(job["provider_snapshot"])

    resolved: list[dict] = []

    def fake_resolve_api_key(profile=None, *, env_name=None):  # noqa: ANN001
        resolved.append({"profile": profile, "env_name": env_name})
        return SECRET_VALUE

    import motte_provider.config as provider_config

    monkeypatch.setattr(provider_config, "resolve_api_key", fake_resolve_api_key)
    built = FrozenProviderFactory()(frozen)

    assert resolved == [{"profile": ORIGINAL_CREDENTIAL, "env_name": "R8_JUDGE_KEY"}]
    assert built.model == ORIGINAL_WIRE_MODEL
    assert built.transport.base_url == ORIGINAL_BASE_URL + "/"
    assert SECRET_VALUE not in json.dumps(frozen.model_dump(mode="json"))
    assert provider.calls == []


# ==================================================================== 公开链路

def test_api_to_worker_publishes_a_new_pass_and_history_stays_read_only(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("R8_JUDGE_KEY", SECRET_VALUE)
    store, resources, provider, api_factory, client = build_environment(tmp_path)

    submitted = client.post("/api/v1/judges", json=submit_body())
    assert submitted.status_code == 202, submitted.text
    job = submitted.json()
    assert job["status"] == "queued"
    assert job["preflight"]["sample_count"] == 1
    assert job["preflight"]["max_calls"] == 1

    # 幂等 submit：同一 request_key 同内容复用同一作业。
    again = client.post("/api/v1/judges", json=submit_body())
    assert again.status_code == 202
    assert again.json()["job_id"] == job["job_id"]
    assert again.json()["reused"] is True
    assert len(jobs_of(store).list_for_run("run-1")) == 1

    worker = worker_for(store, provider)
    outcome = worker.claim_and_execute()
    assert outcome is not None
    assert outcome["status"] == "completed"
    assert outcome["publish_outcome"] == "published"
    assert outcome["published"] is True

    pass_id = outcome["receipt"]["scoring_pass_id"]
    run = store.runs.get("run-1")
    assert run["current_scoring_pass_id"] == pass_id

    # 历史：GET /judges/{id}、GET /runs/{id}/judge-jobs 与 scoring-passes 一致。
    detail = client.get(f"/api/v1/judges/{job['job_id']}").json()
    assert detail["status"] == "completed"
    assert detail["receipt"]["scoring_pass_id"] == pass_id
    history = client.get("/api/v1/runs/run-1/judge-jobs").json()
    assert [item["job_id"] for item in history["items"]] == [job["job_id"]]
    passes = client.get("/api/v1/runs/run-1/scoring-passes").json()
    assert [item["id"] for item in passes["items"]] == [pass_id]
    assert passes["items"][0]["purpose"] == "judge"
    assert passes["items"][0]["job_id"] == job["job_id"]
    assert passes["items"][0]["judge"]["rubric_id"] == "answer-quality"
    assert passes["items"][0]["judge"]["spec_sha256"] == job["judge_spec_sha256"]

    # 历史切换与 GET 都是只读：零模型调用、零新 pass。
    calls_before = len(provider.calls)
    switched = client.get(
        "/api/v1/runs/run-1/report", params={"scoring_pass_id": pass_id}
    )
    assert switched.status_code == 200
    assert switched.json()["scoring_pass_id"] == pass_id
    for _ in range(2):
        client.get("/api/v1/runs/run-1/judge-jobs")
        client.get(f"/api/v1/judges/{job['job_id']}")
        client.get("/api/v1/runs/run-1/report")
    assert len(provider.calls) == calls_before
    assert len(store.scoring_passes.list_for_run("run-1")) == 1
    assert api_factory.calls == []


def test_api_cancel_is_idempotent_and_never_publishes_a_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("R8_JUDGE_KEY", SECRET_VALUE)
    store, resources, provider, _api_factory, client = build_environment(tmp_path)
    job = client.post("/api/v1/judges", json=submit_body()).json()

    first = client.post(f"/api/v1/judges/{job['job_id']}/cancel", json={"reason": "stop"})
    assert first.status_code == 200, first.text
    assert first.json()["outcome"] == "cancelled"
    second = client.post(f"/api/v1/judges/{job['job_id']}/cancel", json={"reason": "stop"})
    assert second.status_code == 200
    assert second.json()["outcome"] == "already_cancelled"

    assert provider.calls == []
    assert store.scoring_passes.list_for_run("run-1") == []
    assert store.runs.get("run-1").get("current_scoring_pass_id") is None
    assert client.get(f"/api/v1/judges/{job['job_id']}").json()["status"] == "cancelled"
    assert client.post("/api/v1/judges/does-not-exist/cancel", json={}).status_code == 404


# ==================================================================== 崩溃窗口

def test_recovery_requeues_a_prepared_job_without_any_call(tmp_path, monkeypatch):
    monkeypatch.setenv("R8_JUDGE_KEY", SECRET_VALUE)
    store, resources, provider, _api_factory, client = build_environment(tmp_path)
    job = client.post("/api/v1/judges", json=submit_body()).json()

    # 崩溃窗口「prepared」：领取后、dispatch 前进程消失。
    assert jobs_of(store).claim(job["job_id"]) is not None
    worker = worker_for(store, provider)
    assert worker.recover_interrupted() == []
    recovered = jobs_of(store).get(job["job_id"])
    assert recovered["status"] == "queued"
    assert recovered["recovery"]["reason"] == "prepared_without_dispatch_evidence"
    assert provider.calls == []

    outcome = worker.claim_and_execute()
    assert outcome["status"] == "completed"
    assert len(provider.calls) == 1
    assert len(store.scoring_passes.list_for_run("run-1")) == 1


def test_recovery_of_a_dispatched_call_is_indeterminate_and_never_resent(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("R8_JUDGE_KEY", SECRET_VALUE)
    crashing = ScriptedProvider(fail_with=KeyboardInterrupt())
    store, resources, provider, _api_factory, client = build_environment(
        tmp_path, provider=crashing
    )
    job = client.post("/api/v1/judges", json=submit_body()).json()

    worker = worker_for(store, crashing)
    try:
        worker.claim_and_execute()
    except KeyboardInterrupt:
        pass  # 真实崩溃：dispatched 边界已落库，结果未知
    dispatched = jobs_of(store).get(job["job_id"])
    assert dispatched["status"] == "dispatching"
    assert [item["status"] for item in dispatched["calls"]] == ["dispatching"]
    assert len(crashing.calls) == 1

    # 恢复：不确定，绝不自动重发；current 与 pass 表不变。
    assert worker.recover_interrupted() == []
    recovered = jobs_of(store).get(job["job_id"])
    assert recovered["status"] == "indeterminate"
    assert recovered["failure"]["code"] == "CALL_OUTCOME_INDETERMINATE"
    assert recovered["cost_total_usd"] is None
    assert store.scoring_passes.list_for_run("run-1") == []
    assert store.runs.get("run-1").get("current_scoring_pass_id") is None
    assert store.invocations.list_for_run("run-1")[0]["outcome"] == "indeterminate"

    # 再次领取不会产生第二次调用。
    assert worker.claim_and_execute() is None
    assert len(crashing.calls) == 1


def test_settled_response_is_published_exactly_once_after_a_publish_crash(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("R8_JUDGE_KEY", SECRET_VALUE)
    store, resources, provider, _api_factory, client = build_environment(tmp_path)
    job = client.post("/api/v1/judges", json=submit_body()).json()
    worker = worker_for(store, provider)
    service = worker.scoring_jobs

    real_publish = service.jobs.publish
    attempts: list[int] = []

    def flaky_publish(*args, **kwargs):  # noqa: ANN002, ANN003
        attempts.append(1)
        raise RuntimeError("worker died inside the publish transaction")

    service.jobs.publish = flaky_publish
    try:
        worker.claim_and_execute()
    except RuntimeError:
        pass
    # 崩溃窗口「响应已持久化、发布事务未提交」：调用已 settled，但没有半个 pass。
    settled = jobs_of(store).get(job["job_id"])
    assert settled["status"] == "settled"
    assert settled["billed_calls"] == 1
    assert store.scoring_passes.list_for_run("run-1") == []
    assert store.runs.get("run-1").get("current_scoring_pass_id") is None
    assert len(provider.calls) == 1

    service.jobs.publish = real_publish
    outcome = worker.claim_and_execute()
    assert outcome["status"] == "completed"
    assert outcome["publish_outcome"] == "published"
    assert len(attempts) == 1
    # 零新模型调用：settled 的响应只做确定性重放。
    assert len(provider.calls) == 1
    passes = store.scoring_passes.list_for_run("run-1")
    assert len(passes) == 1
    assert store.runs.get("run-1")["current_scoring_pass_id"] == passes[0]["id"]

    # 通知丢失后的重复领取：只返回原 receipt，不再发布、不再计费。
    from motte_storage.integrity import RunConflictError  # noqa: F401 - 断言用不到

    repeated = service.run_claimed(jobs_of(store).get(job["job_id"]))
    assert repeated["published"] is False
    assert repeated["publish_outcome"] == "already_completed"
    assert repeated["receipt"]["scoring_pass_id"] == passes[0]["id"]
    assert len(provider.calls) == 1
    assert len(store.scoring_passes.list_for_run("run-1")) == 1


def test_publish_conflict_keeps_the_job_settled_and_retries_atomically(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("R8_JUDGE_KEY", SECRET_VALUE)
    store, resources, provider, _api_factory, client = build_environment(tmp_path)
    job = client.post("/api/v1/judges", json=submit_body()).json()
    worker = worker_for(store, provider)
    service = worker.scoring_jobs

    from motte_storage.integrity import RunConflictError

    real_publish = service.jobs.publish
    calls: list[int] = []

    def conflicted(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append(1)
        raise RunConflictError("run revision or status changed")

    service.jobs.publish = conflicted
    outcome = worker.claim_and_execute()
    assert outcome["publish_outcome"] == "publish_conflict"
    assert outcome["published"] is False
    conflict = jobs_of(store).get(job["job_id"])
    assert conflict["status"] == "settled"
    assert conflict["publish_conflict"]["attempts"] == 1
    # 全有或全无：没有半个 pass，current 未推进。
    assert store.scoring_passes.list_for_run("run-1") == []
    assert store.runs.get("run-1").get("current_scoring_pass_id") is None
    assert len(provider.calls) == 1

    service.jobs.publish = real_publish
    retried = worker.claim_and_execute()
    assert retried["status"] == "completed"
    assert len(calls) == 1
    assert len(provider.calls) == 1
    passes = store.scoring_passes.list_for_run("run-1")
    assert len(passes) == 1
    assert store.runs.get("run-1")["current_scoring_pass_id"] == passes[0]["id"]


# ==================================================================== 领取顺序

def test_judge_jobs_are_not_starved_by_continuously_queued_runs(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("R8_JUDGE_KEY", SECRET_VALUE)
    store, resources, provider, _api_factory, client = build_environment(tmp_path)
    job = client.post("/api/v1/judges", json=submit_body()).json()
    service = RunService(store)
    worker = worker_for(store, provider)

    completed_runs = 0
    for _ in range(6):
        # Run 持续入队：任何一轮都不应让 Judge 作业永远得不到调度。
        service.create_run("replay@1", REPLAY_MANIFEST, case_ids=["case-1"])
        worker.claim_and_execute()
        completed_runs += sum(
            1 for run in store.runs.list() if run["status"] == "completed"
        )
        if jobs_of(store).get(job["job_id"])["status"] == "completed":
            break
    assert jobs_of(store).get(job["job_id"])["status"] == "completed"
    assert completed_runs >= 1
    assert len(provider.calls) == 1


def test_runs_are_not_starved_by_continuously_submitted_judge_jobs(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("R8_JUDGE_KEY", SECRET_VALUE)
    store, resources, provider, _api_factory, client = build_environment(tmp_path)
    service = RunService(store)
    worker = worker_for(store, provider)

    created = service.create_run("replay@1", REPLAY_MANIFEST, case_ids=["case-1"])
    for index in range(6):
        # Judge 队列持续补充：Run 仍必须在本轮内被领取并完成。
        client.post(
            "/api/v1/judges", json=submit_body(request_key=f"judge-req-{index}")
        )
        worker.claim_and_execute()
    assert store.runs.get(created["id"])["status"] == "completed"
    assert len(store.scoring_passes.list_for_run("run-1")) >= 1
