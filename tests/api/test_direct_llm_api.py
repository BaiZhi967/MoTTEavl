"""Direct LLM 评测控制台端点的离线测试（内置样例直接读仓库自带 JSONL，零网络零费用）。

隔离要求：`create_app()` 默认 store 指向开发者本地 SQLite，这里一律显式传内存 store，
避免测试夹具污染本地数据集/场景（见 tests/conftest.py）。
"""
import json

from fastapi.testclient import TestClient

from apps.api.app.main import _direct_llm_accuracy, create_app
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore

CUSTOM = "\n".join([
    json.dumps({"input": "CUSTOM one", "expected": "1"}),
    json.dumps({"input": "CUSTOM two", "expected": "2"}),
    json.dumps({"input": "CUSTOM three"}),
]) + "\n"


def _client(store=None, resources=None):
    application = create_app(store or InMemoryRunStore(),
                             resources if resources is not None else InMemoryResourceStore())
    return TestClient(application), application


def _seeded(client, builtin="direct-llm-exact-answer"):
    response = client.post("/api/v1/benchmarks/direct-llm/import", json={"builtin": builtin})
    assert response.status_code == 201, response.text
    return response.json()


# ------------------------------------------------------------------ 内置样例

def test_builtins_endpoint_lists_the_shipped_samples():
    client, _ = _client()
    payload = client.get("/api/v1/benchmarks/direct-llm/builtins").json()
    assert [item["id"] for item in payload["items"]] == [
        "direct-llm-exact-answer", "direct-llm-classify", "direct-llm-json-extract"]
    assert [item["cases"] for item in payload["items"]] == [8, 8, 7]
    assert all(item["importable"] for item in payload["items"])
    assert payload["total"] == 3


# ------------------------------------------------------------------ 导入

def test_import_builtin_then_overview_and_idempotency():
    client, _ = _client()
    receipt = _seeded(client, "direct-llm-classify")
    assert receipt["imported"] == "direct-llm-classify@1"
    assert receipt["scenario"] == "direct-llm-classify@1"
    assert receipt["suite"] == "direct-llm" and receipt["scorer"] == "contains"
    assert receipt["cases"] == 8 and receipt["source"] == "builtin:direct-llm-classify"
    # 同内容重复导入：复用版本，不越攒版本号
    assert _seeded(client, "direct-llm-classify") == receipt

    overview = client.get("/api/v1/benchmarks/direct-llm").json()
    assert overview["total"] == 1
    item = overview["items"][0]
    assert item["scenario"] == "direct-llm-classify@1"
    assert item["dataset"] == "direct-llm-classify@1"
    assert item["suite"] == "direct-llm" and item["cases"] == 8
    assert item["eval"]["scorer"] == "contains"
    assert item["provenance"]["source"] == "builtin:direct-llm-classify"
    assert item["runs"] == []


def test_import_pasted_jsonl_uses_defaults_and_custom_scorer():
    client, _ = _client()
    response = client.post("/api/v1/benchmarks/direct-llm/import",
                           json={"content": CUSTOM, "name": "custom-set", "scorer": "contains"})
    assert response.status_code == 201, response.text
    receipt = response.json()
    assert receipt["imported"] == "custom-set@1" and receipt["cases"] == 3
    assert receipt["scorer"] == "contains" and receipt["source"] == "local-jsonl"
    cases = client.get("/api/v1/benchmarks/direct-llm/cases",
                       params={"dataset": "custom-set@1"}).json()
    assert [item["case_id"] for item in cases["items"]] == [
        "custom-set-0000", "custom-set-0001", "custom-set-0002"]
    assert [item["scorer"] for item in cases["items"]] == ["contains"] * 3
    assert cases["items"][2]["expected"] is None


def test_import_needs_name_when_content_is_anonymous():
    client, _ = _client()
    response = client.post("/api/v1/benchmarks/direct-llm/import", json={"content": CUSTOM})
    assert response.status_code == 201, response.text
    assert response.json()["imported"] == "direct-llm-custom@1"


def test_import_defaults_license_and_rejects_bad_requests():
    client, _ = _client()
    assert client.post("/api/v1/benchmarks/direct-llm/import", json={}).status_code == 422
    both = client.post("/api/v1/benchmarks/direct-llm/import",
                       json={"content": CUSTOM, "builtin": "direct-llm-classify"})
    assert both.status_code == 422 and "exactly one" in both.json()["error"]["message"]
    assert client.post("/api/v1/benchmarks/direct-llm/import",
                       json={"content": 3}).status_code == 422
    assert client.post("/api/v1/benchmarks/direct-llm/import",
                       json={"content": CUSTOM, "scorer": "fuzzy"}).status_code == 422
    assert client.post("/api/v1/benchmarks/direct-llm/import",
                       json={"content": "not json\n"}).status_code == 422
    assert client.post("/api/v1/benchmarks/direct-llm/import",
                       json={"content": CUSTOM, "license": ""}).status_code == 422
    assert client.post("/api/v1/benchmarks/direct-llm/import",
                       json={"content": CUSTOM, "api_key": "sk-x"}).status_code == 422
    unknown = client.post("/api/v1/benchmarks/direct-llm/import",
                          json={"builtin": "nope"})
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "BUILTIN_UNAVAILABLE"


def test_import_reports_version_conflict_as_409():
    client, _ = _client()
    body = {"content": CUSTOM, "name": "custom-set", "version": "1"}
    assert client.post("/api/v1/benchmarks/direct-llm/import", json=body).status_code == 201
    changed = json.dumps({"input": "changed", "expected": "9"}) + "\n"
    conflict = client.post("/api/v1/benchmarks/direct-llm/import", json={**body, "content": changed})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "RESOURCE_CONFLICT"


# ------------------------------------------------------------------ 题目浏览

def test_cases_pagination_search_and_missing_dataset():
    client, _ = _client()
    _seeded(client)
    first = client.get("/api/v1/benchmarks/direct-llm/cases",
                       params={"dataset": "direct-llm-exact-answer@1", "limit": 3}).json()
    assert first["total"] == 8 and first["dataset_total"] == 8 and first["limit"] == 3
    assert [item["source_line"] for item in first["items"]] == [1, 2, 3]
    assert first["items"][0]["input"].startswith("中国的首都")
    second = client.get("/api/v1/benchmarks/direct-llm/cases",
                        params={"dataset": "direct-llm-exact-answer@1", "offset": 6,
                                "limit": 5}).json()
    assert [item["case_id"] for item in second["items"]] == [
        "direct-llm-exact-answer-0006", "direct-llm-exact-answer-0007"]
    searched = client.get("/api/v1/benchmarks/direct-llm/cases",
                          params={"dataset": "direct-llm-exact-answer@1", "query": "0003"}).json()
    assert searched["total"] == 1 and searched["items"][0]["case_id"] == "direct-llm-exact-answer-0003"
    missing = client.get("/api/v1/benchmarks/direct-llm/cases", params={"dataset": "nope@1"})
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "DATASET_NOT_FOUND"


# ------------------------------------------------------------------ 发起跑测

def _model(client, model_id="probe"):
    client.post("/api/v1/providers", json={"name": "local", "kind": "openai_compatible",
                                           "base_url": "https://local.test/v1"})
    client.post("/api/v1/models", json={"id": model_id, "provider": "local",
                                        "model": "probe-1", "capabilities": {},
                                        "max_output_tokens": 8192})
    return model_id


def test_run_creation_projects_cases_and_records_the_budget():
    client, _ = _client()
    _seeded(client)
    model = _model(client)
    response = client.post("/api/v1/benchmarks/direct-llm/runs", json={
        "model": model, "scenario": "direct-llm-exact-answer@1",
        "parameters": {"temperature": 0.3, "max_output_tokens": 256},
        "case_selection": {"mode": "random", "count": 3, "seed": "deadbeef"}})
    assert response.status_code == 202, response.text
    run = response.json()
    assert len(run["case_ids"]) == 3 and run["status"] == "queued"
    manifest = run["manifest"]
    assert manifest["provider"]["parameters"]["temperature"] == 0.3
    assert manifest["provider"]["parameters"]["max_output_tokens"] == 256
    assert manifest["provider"]["max_retries"] == 0
    assert set(manifest["cases"]) == set(run["case_ids"])
    provenance = manifest["benchmark_provenance"]
    assert provenance["suite"] == "direct-llm" and provenance["scorer"] == "exact"
    assert provenance["max_output_tokens"] == 256
    assert provenance["run_selection"] == {"mode": "random", "count": 3, "seed": "deadbeef"}


def test_run_creation_validates_scenario_model_and_selection():
    client, _ = _client()
    _seeded(client)
    model = _model(client)
    assert client.post("/api/v1/benchmarks/direct-llm/runs", json={}).status_code == 422
    missing_scenario = client.post("/api/v1/benchmarks/direct-llm/runs", json={
        "model": model, "scenario": "direct-llm-nope@1"})
    assert missing_scenario.status_code == 422
    assert missing_scenario.json()["error"]["code"] == "SCENARIO_NOT_FOUND"
    assert client.post("/api/v1/benchmarks/direct-llm/runs",
                       json={"scenario": "direct-llm-exact-answer@1"}).status_code == 422
    bad_id = client.post("/api/v1/benchmarks/direct-llm/runs", json={
        "model": model, "scenario": "direct-llm-exact-answer@1",
        "case_selection": {"mode": "ids", "case_ids": ["nope"]}})
    assert bad_id.status_code == 422 and "not in dataset" in bad_id.json()["error"]["message"]
    bad_budget = client.post("/api/v1/benchmarks/direct-llm/runs", json={
        "model": model, "scenario": "direct-llm-exact-answer@1",
        "parameters": {"max_output_tokens": 0}})
    assert bad_budget.status_code == 422
    bad_level = client.post("/api/v1/benchmarks/direct-llm/runs", json={
        "model": model, "scenario": "direct-llm-exact-answer@1", "reasoning_level": ""})
    assert bad_level.status_code == 422
    bad_parameters = client.post("/api/v1/benchmarks/direct-llm/runs", json={
        "model": model, "scenario": "direct-llm-exact-answer@1", "parameters": 7})
    assert bad_parameters.status_code == 422
    assert client.post("/api/v1/benchmarks/direct-llm/runs", json={
        "model": model, "scenario": "direct-llm-exact-answer@1",
        "api_key": "sk-x"}).status_code == 422


def test_run_creation_derives_scenario_from_dataset_name_and_version():
    client, _ = _client()
    _seeded(client)
    model = _model(client)
    run = client.post("/api/v1/benchmarks/direct-llm/runs", json={
        "model": model, "dataset_name": "direct-llm-exact-answer", "dataset_version": "1"}).json()
    assert run["scenario_version"] == "direct-llm-exact-answer@1"
    assert len(run["case_ids"]) == 8


# ------------------------------------------------------------------ 报告与准确率

def _execute(client, application, run_id, answers):
    service = application.state.run_service
    return service.execute(run_id, provider=lambda case_id: {
        "content": answers.get(case_id, "?"), "usage": {"total_tokens": 7},
        "cost": {"total": 0.001, "price_table_version": "v1"}})


def test_report_uses_judged_denominator_and_reports_usage():
    client, application = _client()
    _seeded(client)
    model = _model(client)
    run = client.post("/api/v1/benchmarks/direct-llm/runs", json={
        "model": model, "scenario": "direct-llm-exact-answer@1"}).json()
    answers = {"direct-llm-exact-answer-0000": "北京", "direct-llm-exact-answer-0001": "wrong"}
    result = _execute(client, application, run["id"], answers)
    assert result["status"] == "completed"
    scores = result["scores"]
    assert [score["outcome"] for score in scores[:3]] == ["correct", "wrong_answer", "wrong_answer"]
    assert all(score["judged"] for score in scores)

    report = client.get(f"/api/v1/runs/{run['id']}/report").json()
    summary = report["summary"]
    assert summary["cases"] == 8 and summary["scored"] == 8 and summary["passed"] == 1
    assert summary["failed"] == 7 and summary["judged"] == 8
    assert summary["denominator"] == "judged_cases"
    assert summary["pass_rate"] == 0.125 and summary["scorer_version"] == "direct-llm-answer-v1"
    assert report["cost"] == {"total": 0.008, "price_table_versions": ["v1"],
                              "known_cases": 8, "unknown_cases": 0}
    assert report["usage"] == {"total_tokens": 56}
    assert report["benchmark"]["suite"] == "direct-llm"
    assert report["scores"][0]["case_id"] == "direct-llm-exact-answer-0000"


def test_overview_reports_accuracy_of_completed_runs():
    client, application = _client()
    _seeded(client)
    model = _model(client)
    run = client.post("/api/v1/benchmarks/direct-llm/runs", json={
        "model": model, "scenario": "direct-llm-exact-answer@1",
        "case_selection": {"mode": "ids",
                           "case_ids": ["direct-llm-exact-answer-0000",
                                        "direct-llm-exact-answer-0001"]}}).json()
    _execute(client, application, run["id"], {"direct-llm-exact-answer-0000": "北京"})
    overview = client.get("/api/v1/benchmarks/direct-llm").json()
    entries = overview["items"][0]["runs"]
    assert [entry["id"] for entry in entries] == [run["id"]]
    assert entries[0]["accuracy"] == 0.5 and entries[0]["status"] == "completed"


def test_direct_llm_accuracy_helper_denominators():
    scores = [{"case_id": "a", "outcome": "correct", "judged": True},
              {"case_id": "b", "outcome": "wrong_answer", "judged": True},
              {"case_id": "c", "outcome": "no_expectation", "judged": False}]
    run = {"manifest": {"benchmark_provenance": {"selected_count": 3}}}
    assert _direct_llm_accuracy(run, scores) == 0.5
    # 分数条数对不上选中题数（运行未跑完）时不给结论
    assert _direct_llm_accuracy(run, scores[:2]) is None
    assert _direct_llm_accuracy({"manifest": {}}, scores) is None
    assert _direct_llm_accuracy(
        {"manifest": {"benchmark_provenance": {"selected_count": 1}}},
        [{"case_id": "c", "outcome": "no_expectation", "judged": False}]) is None
    assert _direct_llm_accuracy(
        {"manifest": {"benchmark_provenance": {"selected_count": 2}}},
        [{"case_id": "a", "outcome": "correct", "judged": True},
         {"case_id": "b", "outcome": "wrong_answer", "judged": True}]) == 0.5
    # 运行级子集优先于数据集级题数
    assert _direct_llm_accuracy(
        {"manifest": {"benchmark_provenance": {"selected_count": 8,
                                              "run_selection": {"count": 2}}}},
        [{"case_id": "a", "outcome": "correct", "judged": True},
         {"case_id": "b", "outcome": "correct", "judged": True}]) == 1.0


def test_rescore_reproduces_scores_without_new_calls():
    client, application = _client()
    _seeded(client)
    model = _model(client)
    run = client.post("/api/v1/benchmarks/direct-llm/runs", json={
        "model": model, "scenario": "direct-llm-exact-answer@1",
        "case_selection": {"mode": "ids",
                           "case_ids": ["direct-llm-exact-answer-0000"]}}).json()
    original = _execute(client, application, run["id"], {"direct-llm-exact-answer-0000": "北京"})
    rescored = client.post(f"/api/v1/runs/{run['id']}/rescore").json()
    assert rescored["scores"] == original["scores"]
