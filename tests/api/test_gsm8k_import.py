"""Offline tests for the GSM8K benchmark console endpoints (download is monkeypatched)."""
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


def _fake_urlopen(request, timeout):
    assert request.full_url.startswith(
        f"https://raw.githubusercontent.com/openai/grade-school-math/{REVISION}/"
    )

    class Response:
        status = 200

        def read(self):
            return FAKE_ROWS

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return Response()


def _client(store=None, resources=None):
    return TestClient(create_app(store, resources))


def test_import_idempotent_and_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_DATASET_DIR", str(tmp_path / "datasets"))
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    client = _client()
    body = {"revision": REVISION, "license": "MIT"}
    first = client.post("/api/v1/benchmarks/gsm8k/import", json=body)
    assert first.status_code == 201, first.text
    payload = first.json()
    assert payload["imported"] == "gsm8k-test@1"
    assert payload["scenario"] == "gsm8k-test-smoke@1"
    assert payload["cases"] == 20
    # 同内容重复导入幂等
    assert client.post("/api/v1/benchmarks/gsm8k/import", json=body).json() == payload
    # 源文件按 revision 持久化到受控目录
    assert list((tmp_path / "datasets").glob("test-*.jsonl"))
    # 概览可见场景与题数
    overview = client.get("/api/v1/benchmarks/gsm8k").json()
    assert [item["scenario"] for item in overview["items"]] == ["gsm8k-test-smoke@1"]
    assert overview["items"][0]["cases"] == 20
    assert overview["items"][0]["runs"] == []
    # 校验错误
    assert client.post("/api/v1/benchmarks/gsm8k/import", json={"revision": "short"}).status_code == 422
    assert client.post("/api/v1/benchmarks/gsm8k/import", json={}).status_code == 422


def test_import_conflict_on_same_version(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_DATASET_DIR", str(tmp_path / "datasets"))
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    resources = InMemoryResourceStore()
    client = TestClient(create_app(InMemoryRunStore(), resources))
    assert client.post("/api/v1/benchmarks/gsm8k/import", json={"revision": REVISION, "license": "MIT"}).status_code == 201
    # 同版本不同内容（换 license 不改 cases；改 revision 会改变 source_sha256 但 cases 相同，仍幂等）
    conflict = client.post(
        "/api/v1/benchmarks/gsm8k/import",
        json={"revision": REVISION, "license": "Apache-2.0", "version": "1"},
    )
    assert conflict.status_code == 409


def test_gsm8k_accuracy_helper():
    from apps.api.app.main import _gsm8k_accuracy

    scores = [{"outcome": "correct"}] * 18 + [{"outcome": "wrong_answer"}] * 2
    assert _gsm8k_accuracy({"scores": scores}) == 0.9
    # run 行内无 scores（SQLite 完成态分数存 scores 表）或为空 → None
    assert _gsm8k_accuracy({"id": "run-1"}) is None
    assert _gsm8k_accuracy({"scores": []}) is None
    # 显式传入分数列表（概览路径从 scores 表读取）
    assert _gsm8k_accuracy({}, scores) == 0.9
    assert _gsm8k_accuracy({}, scores[:10]) is None


def test_overview_reports_accuracy_after_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_DATASET_DIR", str(tmp_path / "datasets"))
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    resources = InMemoryResourceStore()
    client = TestClient(create_app(InMemoryRunStore(), resources))
    assert client.post("/api/v1/benchmarks/gsm8k/import", json={"revision": REVISION, "license": "MIT"}).status_code == 201
    resources.providers.put({"name": "local", "kind": "openai_compatible",
                             "base_url": "http://offline.invalid", "max_retries": 0})
    resources.models.put({"id": "m", "provider": "local", "model": "synthetic-model",
                          "max_output_tokens": 2048})
    created = client.post("/api/v1/benchmarks/gsm8k/runs", json={"model": "m"}).json()
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
    assert client.post("/api/v1/benchmarks/gsm8k/import", json={"revision": REVISION, "license": "MIT"}).status_code == 201
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
    created = client.post("/api/v1/benchmarks/gsm8k/runs", json={"model": "m"})
    assert created.status_code == 202, created.text
    run = created.json()
    assert len(run["case_ids"]) == 20
    assert run["manifest"]["benchmark_provenance"]["selected_count"] == 20
    assert run["manifest"]["provider"]["parameters"]["max_output_tokens"] == 1024
    assert run["manifest"]["provider"]["max_retries"] == 0
    # 概览能看到这条 run
    overview = client.get("/api/v1/benchmarks/gsm8k").json()
    assert [r["id"] for r in overview["items"][0]["runs"]] == [run["id"]]
