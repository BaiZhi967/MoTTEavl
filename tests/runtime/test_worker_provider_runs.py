"""openai_compatible Run 经 Worker 执行的集成路径（全部离线，fake opener）。"""
import json
from urllib.error import HTTPError

import motte_provider.config as provider_config
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

    # 注册表的 build lambda 在调用期从 motte_provider.config 取 build_case_provider，patch 在那里
    monkeypatch.setattr(provider_config, "build_case_provider", factory)


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

    def responder(request, *, timeout):
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


def test_worker_rejects_run_without_provider_instead_of_fabricating_result(tmp_path):
    service = RunService(SQLiteRunStore(tmp_path / "runs.db"))
    service.create_run("direct-llm@1", {}, case_ids=["case-1"])

    result = WorkerLoop(RunService(SQLiteRunStore(tmp_path / "runs.db"))).claim_and_execute()
    assert result["status"] == "unsupported"
    assert result["error"]["code"] == "EXECUTION_BACKEND_REQUIRED"
    assert result["cases"] == []


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
    def responder(request, *, timeout):
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


def test_worker_executes_anthropic_run_with_tools_end_to_end(tmp_path, monkeypatch):
    """anthropic_messages 经 registry 分发端到端（fake opener，零网络）。"""
    from motte_provider.anthropic_messages import AnthropicMessagesProvider
    from motte_provider.transport import HTTPTransport

    captured = []

    def responder(request, *, timeout):
        captured.append(json.loads(request.data))
        return FakeResponse({
            "id": "msg_worker",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 9, "output_tokens": 2},
        })

    def factory(config, cases, **kwargs):
        transport = HTTPTransport(
            "https://api.anthropic.test/v1", "sk-ant-worker",
            opener=responder, sleep=lambda _: None,
            auth="x-api-key", default_headers={"anthropic-version": "2023-06-01"},
        )
        return CaseDrivenProvider(
            AnthropicMessagesProvider(transport, config["model"]),
            cases,
            tools=kwargs.get("tools"),
        )

    monkeypatch.setattr(provider_config, "build_case_provider", factory)
    manifest = {
        "provider": {"kind": "anthropic_messages", "base_url": "https://api.anthropic.test/v1", "model": "claude-sonnet-4-5"},
        "tools": [{"type": "function", "function": {"name": "echo", "parameters": {"type": "object"}}}],
        "cases": {"case-1": {"prompt": "hi", "expected": "ok"}},
    }
    service = RunService(SQLiteRunStore(tmp_path / "runs.db"))
    service.create_run("direct-llm@1", manifest, case_ids=["case-1"])

    result = WorkerLoop(RunService(SQLiteRunStore(tmp_path / "runs.db"))).claim_and_execute()
    assert result["status"] == "completed"
    persisted = result["cases"][0]["result"]
    assert persisted["provider"] == "anthropic_messages"
    assert persisted["content"] == "ok"
    assert persisted["usage"] == {"prompt_tokens": 9, "completion_tokens": 2}
    assert result["scores"] == [{"case_id": "case-1", "passed": True}]
    # manifest 级 tools 注入到了 Anthropic 请求
    assert captured[0]["tools"] == [{"name": "echo", "input_schema": {"type": "object"}, "description": None}]
    assert captured[0]["max_tokens"] == 4096
    assert "sk-ant-worker" not in json.dumps(result)
