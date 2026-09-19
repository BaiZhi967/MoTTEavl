"""Direct LLM 全链路（CLI/API/Worker 一致性、评分口径、恢复语义）。零网络零费用。

真实 provider 用假 opener 替换（与 tests/runtime/test_gsm8k_smoke.py 同一手法）：只验证发出去的
请求形状与落库评分，不产生任何真实调用。
"""
import json

import pytest

from motte_cli.main import main
from motte_contracts.direct_llm import import_direct_llm_jsonl, scenario_for
from motte_sdk.direct_llm import resolve_direct_llm_manifest
from motte_sdk.service import RunService
from motte_storage.resource_store import InMemoryResourceStore, SQLiteResourceStore
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore

GOLD = "PRIVATE SYNTHETIC GOLD"


def source_jsonl(count=6):
    """合成题目：题面里带序号，期望答案带 GOLD 标记（用于断言它不上线）。"""
    return "\n".join(json.dumps({"input": f"SYNTHETIC question {i}: reply with ok",
                                 "expected": f"{GOLD} ok-{i}"}) for i in range(count)) + "\n"


def synthetic_resources(count=6):
    resources = InMemoryResourceStore()
    record = import_direct_llm_jsonl(source_jsonl(count).encode(), name="direct-llm-synthetic",
                                     version="1", license_id="synthetic-only", source="synthetic",
                                     synthetic=True)
    resources.datasets.put(record)
    scenario = scenario_for(record, version="1")
    resources.scenarios.put(scenario)
    return resources, scenario, record


def _install_fake_provider(monkeypatch, reply):
    """把 build_case_provider 换成假 HTTP：记录请求体，按题面里的序号回放内容。"""
    import motte_provider.config as config
    from motte_provider.openai_compatible import CaseDrivenProvider, OpenAICompatibleProvider
    from motte_provider.transport import HTTPTransport

    captured = []
    sent = []

    class Response:
        def __init__(self, content):
            self.content = content

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": self.content},
                                            "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 2, "completion_tokens": 3}}).encode()

    def opener(request, *, timeout):
        body = json.loads(request.data)
        captured.append(body)
        prompt = body["messages"][-1]["content"]
        sent.append(prompt)
        index = int(prompt.split("question ")[1].split(":")[0])
        return Response(reply(index))

    def factory(provider, cases, **kwargs):
        return CaseDrivenProvider(OpenAICompatibleProvider(
            HTTPTransport(provider["base_url"], None, opener=opener,
                          max_retries=provider["max_retries"]),
            provider["model"], parameters=provider["parameters"]), cases)

    monkeypatch.setattr(config, "build_case_provider", factory)
    return captured, sent


def _seed_model(resources):
    resources.providers.put({"name": "local", "kind": "openai_compatible",
                             "base_url": "https://offline.invalid", "max_retries": 9})
    resources.models.put({"id": "m", "provider": "local", "model": "synthetic-model",
                          "max_output_tokens": 2048})


def test_worker_executes_a_direct_llm_run_with_verbatim_prompts(monkeypatch):
    """题目按题面原样上线（无 GSM8K 那种答案后缀），期望答案与快照不上线。"""
    from apps.worker.motte_worker.runtime import WorkerLoop
    from motte_sdk.resolve import prepare_run

    resources, scenario, _ = synthetic_resources()
    _seed_model(resources)
    # 走真实创建期链路（API/CLI 同一条 prepare_run），拿到展开后的 provider 配置
    manifest, case_ids = prepare_run("direct-llm-synthetic@1", {"model": "m"}, [], resources)
    assert manifest["provider"]["max_retries"] == 0
    service = RunService(InMemoryRunStore())
    run = service.create_run("direct-llm-synthetic@1", manifest, case_ids)
    assert len(run["case_ids"]) == 6

    captured, sent = _install_fake_provider(monkeypatch, lambda index: f"{GOLD} ok-{index}")
    result = WorkerLoop(service).claim_and_execute()

    assert result["status"] == "completed" and len(sent) == 6
    assert sent[0] == "SYNTHETIC question 0: reply with ok"
    assert all(body["max_tokens"] == 1024 for body in captured)
    assert all(score["passed"] for score in result["scores"])
    wire = json.dumps(captured)
    assert GOLD not in wire and "expected" not in wire and "provenance" not in wire
    assert service.rescore(result["id"])["scores"] == result["scores"]
    assert len(captured) == 6


def test_systemic_failure_stops_run_and_report_uses_judged_denominator():
    from apps.api.app.main import _build_report

    resources, scenario, _ = synthetic_resources()
    manifest = resolve_direct_llm_manifest(scenario, {}, resources)
    service = RunService(InMemoryRunStore())
    run = service.create_run("direct-llm-synthetic@1", manifest, list(manifest["cases"]))
    calls = []

    def invoke(case_id):
        calls.append(case_id)
        if len(calls) == 3:
            return {"error": {"class": "auth", "message": "synthetic denied"}}
        return {"content": [f"{GOLD} ok-0", "nope", "whatever"][len(calls) - 1],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
                "cost": {"total": 0, "price_table_version": "synthetic-free"}}

    result = service.execute(run["id"], provider=invoke)
    assert result["status"] == "failed" and len(calls) == 3
    assert [score["outcome"] for score in result["scores"][:3]] == [
        "correct", "wrong_answer", "call_failed"]
    report = _build_report(result)
    assert report["summary"]["not_attempted"] == 3
    assert report["summary"]["judged"] == 3 and report["summary"]["selected"] == 6
    assert report["summary"]["pass_rate"] == pytest.approx(1 / 3)
    assert report["summary"]["denominator"] == "judged_cases"
    assert report["summary"]["scorer_version"] == "direct-llm-answer-v1"
    assert report["cost"]["total"] == 0
    # 只有成功应答的两题带 usage（第三题是错误信封，其余未尝试）
    assert report["usage"] == {"prompt_tokens": 4, "completion_tokens": 6}
    assert service.rescore(run["id"])["scores"] == result["scores"]
    assert len(calls) == 3


def test_transient_failure_gets_one_second_chance():
    """瞬时失败题主循环后补跑一次：补跑仍失败则 run 判 failed，其余题不受影响。"""
    resources, scenario, _ = synthetic_resources()
    manifest = resolve_direct_llm_manifest(scenario, {}, resources)
    service = RunService(InMemoryRunStore())
    run = service.create_run("direct-llm-synthetic@1", manifest, list(manifest["cases"]))
    calls = []

    class RateError(Exception):
        error_class = "rate_limit"

    def invoke(case_id):
        calls.append(case_id)
        if case_id.endswith("0002"):
            raise RateError("synthetic busy")
        return {"content": f"{GOLD} ok-{case_id[-1]}"}

    result = service.execute(run["id"], provider=invoke)
    # 主循环 6 次 + 瞬时失败题补跑 1 次（只补一次，不做无限重试）
    assert result["status"] == "failed" and len(calls) == 7
    outcomes = [score["outcome"] for score in result["scores"]]
    assert outcomes[2] == "call_failed" and outcomes.count("call_failed") == 1
    assert outcomes.count("correct") == 5


def test_cancel_before_worker_marks_every_case_not_attempted():
    from apps.worker.motte_worker.runtime import WorkerLoop

    resources, scenario, _ = synthetic_resources()
    manifest = resolve_direct_llm_manifest(scenario, {}, resources)
    service = RunService(InMemoryRunStore())
    run = service.create_run("direct-llm-synthetic@1", manifest, list(manifest["cases"]))
    cancelled = service.cancel(run["id"], reason="operator cancelled before worker")
    assert cancelled["status"] == "cancelled"
    assert len(cancelled["scores"]) == 6
    assert all(score["outcome"] == "not_attempted" and not score["attempted"]
               and not score["judged"] for score in cancelled["scores"])
    assert [event["type"] for event in service.events(run["id"])] == [
        "queued", "cancelled", "scoring_pass_created"
    ]
    assert WorkerLoop(service).claim_and_execute() is None


def test_cases_without_expectation_stay_out_of_the_denominator():
    from apps.api.app.main import _build_report

    raw = ("\n".join([
        json.dumps({"input": "SYNTHETIC judged", "expected": "yes"}),
        json.dumps({"input": "SYNTHETIC unjudged"}),
    ]) + "\n").encode()
    resources = InMemoryResourceStore()
    record = import_direct_llm_jsonl(raw, name="direct-llm-mixed", version="1",
                                     license_id="synthetic-only")
    resources.datasets.put(record)
    manifest = resolve_direct_llm_manifest(scenario_for(record, version="1"), {}, resources)
    service = RunService(InMemoryRunStore())
    run = service.create_run("direct-llm-mixed@1", manifest, list(manifest["cases"]))
    result = service.execute(run["id"], provider=lambda _: {"content": "yes"})
    assert [score["outcome"] for score in result["scores"]] == ["correct", "no_expectation"]
    summary = _build_report(result)["summary"]
    assert summary["selected"] == 2 and summary["judged"] == 1
    assert summary["accuracy"] == 1.0 and summary["pass_rate"] == 1.0
    assert summary["no_expectation"] == 1


def test_restart_does_not_repeat_an_indeterminate_paid_call(tmp_path):
    from apps.worker.motte_worker.runtime import WorkerLoop

    resources, scenario, _ = synthetic_resources()
    manifest = resolve_direct_llm_manifest(scenario, {}, resources)
    path = tmp_path / "direct-llm.db"
    service = RunService(SQLiteRunStore(path))
    run = service.create_run("direct-llm-synthetic@1", manifest, list(manifest["cases"]))
    calls = []

    def interrupted(case_id):
        calls.append(case_id)
        if len(calls) == 3:
            raise KeyboardInterrupt("synthetic crash")
        return {"content": f"{GOLD} ok-{case_id[-1]}"}

    with pytest.raises(KeyboardInterrupt):
        service.execute(run["id"], provider=interrupted)
    reopened = RunService(SQLiteRunStore(path))
    assert WorkerLoop(reopened).recover_interrupted() == []
    uncertain = reopened.get_run(run["id"])
    assert uncertain["status"] == "needs_review"
    assert len(uncertain["cases"]) == 2
    assert reopened.store.attempts.list_for_run(run["id"])[-1]["status"] == "indeterminate"

    # Repeating work requires an explicit operator retry and creates a child Run.
    child = reopened.retry(run["id"])
    retried = []
    result = reopened.execute(
        child["id"], provider=lambda case_id: retried.append(case_id) or {"content": GOLD}
    )
    assert retried == run["case_ids"]
    assert result["status"] == "completed" and len(result["scores"]) == 6


def test_cli_api_worker_manifest_parity(tmp_path, capsys):
    """CLI 导入 + CLI/API 创建：两次展开出的 manifest 与题集完全一致。"""
    from fastapi.testclient import TestClient

    from apps.api.app.main import create_app

    db = str(tmp_path / "runs.db")
    assert main(["direct-llm", "import", "--builtin", "direct-llm-json-extract", "--db", db]) == 0
    receipt = json.loads(capsys.readouterr().out)
    resources = SQLiteResourceStore(db)
    _seed_model(resources)

    assert main(["direct-llm", "run", "--scenario", receipt["scenario"], "--model", "m",
                 "--db", db]) == 0
    cli_run = json.loads(capsys.readouterr().out)
    assert len(cli_run["case_ids"]) == 7

    service = RunService(SQLiteRunStore(db))
    client = TestClient(create_app(service.store, resources))
    response = client.post("/api/v1/benchmarks/direct-llm/runs",
                           json={"model": "m", "scenario": receipt["scenario"]})
    assert response.status_code == 202, response.text
    api_run = response.json()
    assert api_run["manifest"] == cli_run["manifest"]
    assert api_run["case_ids"] == cli_run["case_ids"]
    assert api_run["manifest"]["provider"]["parameters"]["max_output_tokens"] == 1024
    provenance = api_run["manifest"]["benchmark_provenance"]
    assert provenance["suite"] == "direct-llm" and provenance["scorer"] == "regex"
    # 题面原样上线：与不可变快照逐字一致，没有任何套件后缀
    snapshot = api_run["manifest"]["benchmark_snapshot"]["dataset"]["cases"]
    for case in snapshot:
        assert api_run["manifest"]["cases"][case["case_id"]]["prompt"] == case["input"]
