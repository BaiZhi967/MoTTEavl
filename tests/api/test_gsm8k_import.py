"""Offline tests for the GSM8K benchmark console endpoints (download is monkeypatched).

隔离要求：`create_app()` 的默认 store 指向开发者本地 SQLite（var/runs.db），
这里一律显式传内存 store，避免测试夹具污染本地数据集/场景（见 tests/conftest.py）。
"""
import json
import urllib.request

from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore
from motte_sdk.service import RunService

FAKE_ROWS = "\n".join(
    json.dumps({"question": f"SYNTHETIC {i}: compute one.", "answer": f"reason\n#### {i}"})
    for i in range(25)
).encode()
REVISION = "a" * 40
LATEST = "c" * 40
COMMITS_URL = "https://api.github.com/repos/openai/grade-school-math/commits"


def _fake_urlopen(request, timeout):
    """同时服务最新 commit 解析（GitHub API）与源文件下载（raw），不产生真实网络。"""
    url = request.full_url
    if url.startswith(COMMITS_URL):
        payload = json.dumps([{"sha": LATEST}]).encode()
    else:
        assert url.startswith("https://raw.githubusercontent.com/openai/grade-school-math/")
        payload = FAKE_ROWS

    class Response:
        status = 200

        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return Response()


def _client(store=None, resources=None):
    return TestClient(create_app(store or InMemoryRunStore(),
                                 resources if resources is not None else InMemoryResourceStore()))


def test_import_idempotent_and_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_DATASET_DIR", str(tmp_path / "datasets"))
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    client = _client()
    body = {"revision": REVISION, "license": "MIT", "scope": "smoke"}
    first = client.post("/api/v1/benchmarks/gsm8k/import", json=body)
    assert first.status_code == 201, first.text
    payload = first.json()
    assert payload["imported"] == "gsm8k-test@1"
    assert payload["scenario"] == "gsm8k-test-smoke@1"
    assert payload["scope"] == "smoke" and payload["benchmark"] == "gsm8k-20"
    assert payload["cases"] == 20 and payload["revision"] == REVISION
    # 同内容重复导入幂等
    assert client.post("/api/v1/benchmarks/gsm8k/import", json=body).json() == payload
    # 源文件按 revision 持久化到受控目录
    assert list((tmp_path / "datasets").glob("test-*.jsonl"))
    # 概览可见场景与题数
    overview = client.get("/api/v1/benchmarks/gsm8k").json()
    assert [item["scenario"] for item in overview["items"]] == ["gsm8k-test-smoke@1"]
    assert overview["items"][0]["scope"] == "smoke"
    assert overview["items"][0]["cases"] == 20
    assert overview["items"][0]["runs"] == []
    # 校验错误
    assert client.post("/api/v1/benchmarks/gsm8k/import", json={"revision": "short"}).status_code == 422
    assert client.post("/api/v1/benchmarks/gsm8k/import", json={}).status_code == 422


def test_default_import_downloads_latest_full_and_reuses_version(tmp_path, monkeypatch):
    """默认（只给 license）= 解析官方最新 commit + 全量 + 自动版本号，重复导入不新增版本。"""
    monkeypatch.setenv("MOTTE_DATASET_DIR", str(tmp_path / "datasets"))
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    resources = InMemoryResourceStore()
    client = _client(resources=resources)
    first = client.post("/api/v1/benchmarks/gsm8k/import", json={"license": "MIT"})
    assert first.status_code == 201, first.text
    payload = first.json()
    assert payload["revision"] == LATEST  # 解析出的 sha 被固定进 provenance
    assert payload["scope"] == "full" and payload["benchmark"] == "gsm8k-full"
    assert payload["cases"] == 25  # 全量 = 源文件全部行
    assert payload["scenario"] == "gsm8k-test-full@1"
    assert (tmp_path / "datasets" / f"test-{LATEST}.jsonl").exists()
    assert resources.datasets.get("gsm8k-test", "1")["provenance"]["revision"] == LATEST
    # 再点一次：内容相同 → 复用版本 1，不会攒出新版本
    assert client.post("/api/v1/benchmarks/gsm8k/import", json={"license": "MIT"}).json() == payload
    assert [dataset["version"] for dataset in resources.datasets.list()] == ["1"]
    # 换成冒烟：内容不同 → 自动取下一个空号，两个 scope 并存
    smoke = client.post("/api/v1/benchmarks/gsm8k/import", json={"license": "MIT", "scope": "smoke"}).json()
    assert smoke["imported"] == "gsm8k-test@2" and smoke["scenario"] == "gsm8k-test-smoke@2"
    assert smoke["cases"] == 20
    overview = client.get("/api/v1/benchmarks/gsm8k").json()
    assert [(item["scenario"], item["scope"], item["cases"]) for item in overview["items"]] == [
        ("gsm8k-test-full@1", "full", 25), ("gsm8k-test-smoke@2", "smoke", 20)]


def test_full_scope_imports_entire_split(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_DATASET_DIR", str(tmp_path / "datasets"))
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    client = _client()
    response = client.post("/api/v1/benchmarks/gsm8k/import",
                           json={"revision": REVISION, "license": "MIT", "scope": "full"})
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["imported"] == "gsm8k-test@1"
    assert payload["scenario"] == "gsm8k-test-full@1"
    assert payload["scope"] == "full" and payload["benchmark"] == "gsm8k-full"
    # 全量 = 源文件全部 25 行；题数写死进数据集，运行时分母不再假设 20
    assert payload["cases"] == 25
    overview = client.get("/api/v1/benchmarks/gsm8k").json()
    assert [item["scenario"] for item in overview["items"]] == ["gsm8k-test-full@1"]
    assert overview["items"][0]["cases"] == 25
    assert overview["items"][0]["benchmark"]["selected_count"] == 25
    assert overview["items"][0]["benchmark"]["selection"] == "all-rows-in-file-order"


def test_unknown_scope_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_DATASET_DIR", str(tmp_path / "datasets"))
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    response = _client().post("/api/v1/benchmarks/gsm8k/import",
                              json={"revision": REVISION, "license": "MIT", "scope": "everything"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONTRACT_INVALID"


def test_latest_resolution_failure_is_reported(tmp_path, monkeypatch):
    """GitHub API 不可达/被墙时给出 502 与「改用显式 commit」提示，不落任何数据。"""
    monkeypatch.setenv("MOTTE_DATASET_DIR", str(tmp_path / "datasets"))

    def unavailable(request, timeout):
        raise OSError("proxy refused")

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    resources = InMemoryResourceStore()
    response = _client(resources=resources).post("/api/v1/benchmarks/gsm8k/import", json={"license": "MIT"})
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "SOURCE_UNAVAILABLE" and "explicit 40-character commit" in error["message"]
    assert resources.datasets.list() == []


def test_smoke_and_full_coexist_on_distinct_versions(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_DATASET_DIR", str(tmp_path / "datasets"))
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    client = _client()
    assert client.post("/api/v1/benchmarks/gsm8k/import",
                       json={"revision": REVISION, "license": "MIT", "scope": "smoke"}).status_code == 201
    full = client.post("/api/v1/benchmarks/gsm8k/import",
                       json={"revision": REVISION, "license": "MIT", "scope": "full", "version": "2"})
    assert full.status_code == 201, full.text
    overview = client.get("/api/v1/benchmarks/gsm8k").json()
    # 资源清单按 (name, version) 排序，两个 scope 各自独立成场景
    assert [(item["scenario"], item["scope"], item["cases"]) for item in overview["items"]] == [
        ("gsm8k-test-full@2", "full", 25), ("gsm8k-test-smoke@1", "smoke", 20)]
    # 同一 name@version 换 scope 属于内容冲突，需显式换版本号
    conflict = client.post("/api/v1/benchmarks/gsm8k/import",
                           json={"revision": REVISION, "license": "MIT", "scope": "full", "version": "1"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "RESOURCE_CONFLICT"


def test_import_conflict_on_same_version(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_DATASET_DIR", str(tmp_path / "datasets"))
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    resources = InMemoryResourceStore()
    client = TestClient(create_app(InMemoryRunStore(), resources))
    body = {"revision": REVISION, "license": "MIT", "scope": "smoke"}
    assert client.post("/api/v1/benchmarks/gsm8k/import", json=body).status_code == 201
    # 同版本不同内容（换 license 不改 cases，但记录不同）仍是冲突：基准版本不可覆盖
    conflict = client.post("/api/v1/benchmarks/gsm8k/import", json={**body, "license": "Apache-2.0"})
    assert conflict.status_code == 409


def test_gsm8k_accuracy_helper():
    from apps.api.app.main import _gsm8k_accuracy

    scores = [{"outcome": "correct"}] * 18 + [{"outcome": "wrong_answer"}] * 2

    def run(selected: int, rows=scores, run_selection=None):
        provenance = {"selected_count": selected}
        if run_selection is not None:
            provenance["run_selection"] = run_selection
        return {"manifest": {"benchmark_provenance": provenance}, "scores": rows}

    assert _gsm8k_accuracy(run(20)) == 0.9
    # 分母来自运行自己选中的题数（子集运行按子集算），分数行数不齐或缺失元数据时不猜
    assert _gsm8k_accuracy({**run(20), "scores": scores[:10]}) is None
    assert _gsm8k_accuracy({"id": "run-1"}) is None
    assert _gsm8k_accuracy({"manifest": {"benchmark_provenance": {"selected_count": 20}}, "scores": []}) is None
    subset = [{"outcome": "correct"}] * 9 + [{"outcome": "wrong_answer"}]
    assert _gsm8k_accuracy(run(1319, subset, {"mode": "random", "count": 10, "seed": "aabbccdd"})) == 0.9
    assert _gsm8k_accuracy(run(10, [{"outcome": "wrong_answer"}] * 10)) == 0.0
    assert _gsm8k_accuracy(run(20, subset)) is None


def test_run_with_case_selection_and_reasoning_level(tmp_path, monkeypatch):
    """跑测接口：指定题目 / 随机 N 题 + 思考强度都写进 manifest 与快照，分母是子集题数。"""
    monkeypatch.setenv("MOTTE_DATASET_DIR", str(tmp_path / "datasets"))
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    resources = InMemoryResourceStore()
    client = TestClient(create_app(InMemoryRunStore(), resources))
    assert client.post("/api/v1/benchmarks/gsm8k/import",
                       json={"revision": REVISION, "license": "MIT", "scope": "full"}).status_code == 201
    resources.providers.put({"name": "local", "kind": "openai_compatible",
                             "base_url": "https://offline.invalid", "max_retries": 0})
    resources.models.put({"id": "reasoner", "provider": "local", "model": "synthetic-reasoner",
                          "max_output_tokens": 4096,
                          "reasoning": {"supported": True, "levels": ["low", "high"],
                                        "control": "{\"reasoning_effort\": reasoningLevel}",
                                        "default_level": "low"}})

    explicit = client.post("/api/v1/benchmarks/gsm8k/runs", json={
        "model": "reasoner", "scenario": "gsm8k-test-full@1", "reasoning_level": "high",
        "case_selection": {"mode": "ids", "case_ids": ["gsm8k-test-0004", "gsm8k-test-0001"]}})
    assert explicit.status_code == 202, explicit.text
    run = explicit.json()
    assert run["case_ids"] == ["gsm8k-test-0001", "gsm8k-test-0004"]
    assert list(run["manifest"]["cases"]) == run["case_ids"]  # 只投影选中题的 prompt
    provenance = run["manifest"]["benchmark_provenance"]
    assert provenance["run_selection"] == {"mode": "ids", "count": 2, "seed": None}
    assert provenance["selected_count"] == 25  # 数据集级题数保持不变
    assert run["manifest"]["provider"]["reasoning_level"] == "high"
    assert len(run["manifest"]["benchmark_snapshot"]["dataset"]["cases"]) == 25

    # 随机：seed 省略时服务端生成并回填，同一 seed 可复现
    first = client.post("/api/v1/benchmarks/gsm8k/runs", json={
        "model": "reasoner", "scenario": "gsm8k-test-full@1",
        "case_selection": {"mode": "random", "count": 5}}).json()
    seed = first["manifest"]["benchmark_provenance"]["run_selection"]["seed"]
    assert len(first["case_ids"]) == 5 and seed
    replay = client.post("/api/v1/benchmarks/gsm8k/runs", json={
        "model": "reasoner", "scenario": "gsm8k-test-full@1",
        "case_selection": {"mode": "random", "count": 5, "seed": seed}}).json()
    assert replay["case_ids"] == first["case_ids"]

    # 拒绝：未知题目、越界 count、未知思考强度、把 case_ids 塞进 case_selection 之外
    bad_ids = client.post("/api/v1/benchmarks/gsm8k/runs", json={
        "model": "reasoner", "case_selection": {"mode": "ids", "case_ids": ["gsm8k-test-9999"]}})
    assert bad_ids.status_code == 422 and "not in dataset" in bad_ids.json()["error"]["message"]
    bad_count = client.post("/api/v1/benchmarks/gsm8k/runs", json={
        "model": "reasoner", "case_selection": {"mode": "random", "count": 999}})
    assert bad_count.status_code == 422 and "count" in bad_count.json()["error"]["message"]
    bad_level = client.post("/api/v1/benchmarks/gsm8k/runs", json={
        "model": "reasoner", "reasoning_level": "unsupported"})
    assert bad_level.status_code == 422
    bad_mode = client.post("/api/v1/benchmarks/gsm8k/runs", json={
        "model": "reasoner", "case_selection": {"mode": "everything"}})
    assert bad_mode.status_code == 422 and "unsupported" in bad_mode.json()["error"]["message"]


def test_cases_endpoint_pages_searches_and_reports_404(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_DATASET_DIR", str(tmp_path / "datasets"))
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    client = _client()
    assert client.post("/api/v1/benchmarks/gsm8k/import",
                       json={"revision": REVISION, "license": "MIT", "scope": "full"}).status_code == 201
    first = client.get("/api/v1/benchmarks/gsm8k/cases",
                       params={"dataset": "gsm8k-test@1", "limit": 10}).json()
    assert first["total"] == 25 and first["dataset_total"] == 25 and len(first["items"]) == 10
    assert first["items"][0] == {"case_id": "gsm8k-test-0000", "input": "SYNTHETIC 0: compute one.",
                                 "expected": "0", "source_line": 1}
    second = client.get("/api/v1/benchmarks/gsm8k/cases",
                        params={"dataset": "gsm8k-test@1", "offset": 20, "limit": 10}).json()
    assert [item["case_id"] for item in second["items"]] == [
        f"gsm8k-test-{index:04d}" for index in range(20, 25)]
    searched = client.get("/api/v1/benchmarks/gsm8k/cases",
                          params={"dataset": "gsm8k-test@1", "query": "SYNTHETIC 1"}).json()
    assert searched["total"] == 11 and searched["items"][0]["case_id"] == "gsm8k-test-0001"
    by_id = client.get("/api/v1/benchmarks/gsm8k/cases",
                       params={"dataset": "gsm8k-test@1", "query": "0017"}).json()
    assert by_id["total"] == 1 and by_id["items"][0]["expected"] == "17"
    assert client.get("/api/v1/benchmarks/gsm8k/cases",
                      params={"dataset": "nope@9"}).status_code == 404


def test_overview_reports_accuracy_after_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_DATASET_DIR", str(tmp_path / "datasets"))
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    resources = InMemoryResourceStore()
    client = TestClient(create_app(InMemoryRunStore(), resources))
    assert client.post("/api/v1/benchmarks/gsm8k/import",
                       json={"revision": REVISION, "license": "MIT", "scope": "smoke"}).status_code == 201
    resources.providers.put({"name": "local", "kind": "openai_compatible",
                             "base_url": "http://offline.invalid", "max_retries": 0})
    resources.models.put({"id": "m", "provider": "local", "model": "synthetic-model",
                          "max_output_tokens": 2048})
    created = client.post("/api/v1/benchmarks/gsm8k/runs",
                          json={"model": "m", "scenario": "gsm8k-test-smoke@1"}).json()
    service = client.app.state.run_service

    # 合成 gold = 案例序号，fake provider 按序号回答 → 全部 correct
    def invoke(case_id):
        return {"content": f"reasoning\n#### {int(case_id.rsplit('-', 1)[1])}"}

    result = service.execute(created["id"], provider=invoke)
    assert result["status"] == "completed"
    overview = client.get("/api/v1/benchmarks/gsm8k").json()
    run_row = overview["items"][0]["runs"][0]
    assert run_row["id"] == created["id"]
    assert run_row["status"] == "completed" and run_row["accuracy"] == 1.0


def test_run_creation_requires_model_and_validates(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_DATASET_DIR", str(tmp_path / "datasets"))
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    resources = InMemoryResourceStore()
    client = TestClient(create_app(InMemoryRunStore(), resources))
    assert client.post("/api/v1/benchmarks/gsm8k/import",
                       json={"revision": REVISION, "license": "MIT", "scope": "smoke"}).status_code == 201
    # 缺 model → 422
    assert client.post("/api/v1/benchmarks/gsm8k/runs", json={}).status_code == 422
    # 不存在的 model → 422
    missing = client.post("/api/v1/benchmarks/gsm8k/runs", json={"model": "nope"})
    assert missing.status_code == 422
    # 合法 model → 202，manifest 含 benchmark 快照与预设参数
    resources.providers.put({"name": "local", "kind": "openai_compatible",
                             "base_url": "https://offline.invalid", "max_retries": 9})
    resources.models.put({"id": "m", "provider": "local", "model": "synthetic-model",
                          "max_output_tokens": 2048})
    # 省略 scenario 时默认全量场景
    assert client.post("/api/v1/benchmarks/gsm8k/runs", json={"model": "m"}).status_code == 422
    created = client.post("/api/v1/benchmarks/gsm8k/runs", json={"model": "m", "scope": "smoke"})
    assert created.status_code == 202, created.text
    run = created.json()
    assert run["scenario_version"] == "gsm8k-test-smoke@1"
    assert len(run["case_ids"]) == 20
    assert run["manifest"]["benchmark_provenance"]["selected_count"] == 20
    assert run["manifest"]["provider"]["parameters"]["max_output_tokens"] == 1024
    assert run["manifest"]["provider"]["max_retries"] == 0
    # 概览能看到这条 run
    overview = client.get("/api/v1/benchmarks/gsm8k").json()
    assert [r["id"] for r in overview["items"][0]["runs"]] == [run["id"]]
