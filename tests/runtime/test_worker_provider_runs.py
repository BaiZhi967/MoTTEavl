"""openai_compatible Run 经 Worker 执行的集成路径（全部离线，fake opener）。"""
import json
from urllib.error import HTTPError

import apps.worker.motte_worker.runtime as worker_runtime
from apps.worker.motte_worker.runtime import WorkerLoop
from motte_provider.openai_compatible import CaseDrivenProvider, OpenAICompatibleProvider
from motte_provider.pricing import parse_price_table
from motte_provider.transport import HTTPTransport
from motte_sdk.service import RunService
from motte_storage.run_store import SQLiteRunStore

SECRET = "sk-worker-secret"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def _fake_case_provider(monkeypatch, responder):
    def factory(config, cases, **kwargs):
        transport = HTTPTransport("https://api.example.test/v1", SECRET, opener=responder, sleep=lambda _: None)
        provider = OpenAICompatibleProvider(
            transport, config["model"], price_table=parse_price_table(config.get("price_table"))
        )
        return CaseDrivenProvider(provider, cases)

    monkeypatch.setattr(worker_runtime, "build_case_provider", factory)


OPENAI_MANIFEST = {
    "provider": {
        "kind": "openai_compatible",
        "base_url": "https://api.example.test/v1",
        "model": "test-model",
        "api_key_env": "OPENAI_API_KEY",
        "price_table": {"version": "v1", "input_per_million": 1.0, "output_per_million": 2.0},
    },
    "cases": {"case-1": {"prompt": "1+1=?", "expected": "2"}},
}


def _chat_body(content):
    return {
        "model": "test-model",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7},
    }


def test_worker_executes_openai_compatible_run_end_to_end(tmp_path, monkeypatch):
    calls = []

    def responder(request, timeout):
        calls.append(request)
        return FakeResponse(_chat_body("2"))

    _fake_case_provider(monkeypatch, responder)
    service = RunService(SQLiteRunStore(tmp_path / "runs.db"))
    service.create_run("direct-llm@1", OPENAI_MANIFEST, case_ids=["case-1"])

    result = WorkerLoop(RunService(SQLiteRunStore(tmp_path / "runs.db"))).claim_and_execute()
    assert result["status"] == "completed"
    assert calls, "expected at least one transport call"
    persisted = result["cases"][0]["result"]
    assert persisted["content"] == "2"
    assert persisted["cost"]["price_table_version"] == "v1"
    assert persisted["cost"]["total"] == (5 * 1.0 + 7 * 2.0) / 1_000_000
    assert result["scores"] == [{"case_id": "case-1", "passed": True}]
    assert SECRET not in json.dumps(result)


def test_worker_marks_run_unsupported_for_strict_precheck_failure(tmp_path):
    service = RunService(SQLiteRunStore(tmp_path / "runs.db"))
    manifest = {
        "provider": {
            "kind": "openai_compatible",
            "base_url": "https://api.example.test/v1",
            "model": "test-model",
            "parameters": {"temperature": 99.0},
        }
    }
    service.create_run("direct-llm@1", manifest, case_ids=["case-1"])

    result = WorkerLoop(RunService(SQLiteRunStore(tmp_path / "runs.db"))).claim_and_execute()
    assert result["status"] == "unsupported"
    assert result["error"]["code"] == "UNSUPPORTED_PARAMETER"
    assert result["cases"] == []


def test_worker_marks_run_unsupported_for_unknown_provider_kind(tmp_path):
    service = RunService(SQLiteRunStore(tmp_path / "runs.db"))
    service.create_run("direct-llm@1", {"provider": {"kind": "mystery"}}, case_ids=["case-1"])
    result = WorkerLoop(RunService(SQLiteRunStore(tmp_path / "runs.db"))).claim_and_execute()
    assert result["status"] == "unsupported"
    assert result["error"]["code"] == "PROVIDER_CONFIG_INVALID"


def test_provider_failure_records_classified_evidence_on_run(tmp_path, monkeypatch):
    def responder(request, timeout):
        raise HTTPError(request.full_url, 401, "unauthorized", {}, None)

    _fake_case_provider(monkeypatch, responder)
    service = RunService(SQLiteRunStore(tmp_path / "runs.db"))
    service.create_run("direct-llm@1", OPENAI_MANIFEST, case_ids=["case-1"])

    result = WorkerLoop(RunService(SQLiteRunStore(tmp_path / "runs.db"))).claim_and_execute()
    assert result["status"] == "failed"
    assert result["error"]["class"] == "auth"
    evidence = result["error"]["evidence"]
    assert evidence["canonical"]["request"]["body"]["model"] == "test-model"
    assert SECRET not in json.dumps(result["error"])
