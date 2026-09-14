import json
from urllib.error import HTTPError

import pytest

from motte_cli.main import main
from motte_cli.smoke import execute_smoke, record_live_smoke, run_live_smoke, smoke_markdown
from motte_provider.pricing import parse_price_table
from motte_provider.transport import HTTPTransport

SECRET = "sk-live-secret-xyz"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def _chat_body():
    return {
        "model": "m",
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
    }


def test_run_live_smoke_produces_redacted_report():
    transport = HTTPTransport("https://api.example.test/v1", SECRET, opener=lambda r, t: FakeResponse(_chat_body()))
    report = run_live_smoke(
        "openai-compatible",
        "m",
        base_url="https://api.example.test/v1",
        api_key=SECRET,
        prompt="ping",
        transport=transport,
    )
    assert report["result"]["content"] == "ok"
    assert report["smoke"]["provider"] == "openai-compatible"
    assert SECRET not in json.dumps(report)


def test_execute_smoke_returns_failure_report_with_error_class():
    def responder(request, timeout):
        raise HTTPError(request.full_url, 429, "busy", {}, None)

    transport = HTTPTransport("https://api.example.test/v1", SECRET, opener=responder, max_retries=0, sleep=lambda _: None)
    report, exit_code = execute_smoke(
        "openai-compatible",
        "m",
        base_url="https://api.example.test/v1",
        api_key=SECRET,
        prompt="ping",
        transport=transport,
    )
    assert exit_code == 1
    assert report["result"]["error"]["class"] == "rate_limit"
    assert report["result"]["metering"]["attempts"] == 1


def test_smoke_markdown_and_record_contain_no_secret(tmp_path):
    transport = HTTPTransport("https://api.example.test/v1", SECRET, opener=lambda r, t: FakeResponse(_chat_body()))
    report = run_live_smoke(
        "openai-compatible",
        "m",
        base_url="https://api.example.test/v1",
        api_key=SECRET,
        prompt="ping",
        price_table=parse_price_table({"version": "v9", "input_per_million": 1, "output_per_million": 1}),
        transport=transport,
    )
    markdown = smoke_markdown(report)
    assert "completed" in markdown
    assert "price_table_version=v9" in markdown
    assert SECRET not in markdown

    log_path = tmp_path / "live-smoke-log.md"
    record_live_smoke(report, log_path)
    record_live_smoke(report, log_path)
    content = log_path.read_text(encoding="utf-8")
    assert content.count("## ") == 2
    assert SECRET not in content


def test_live_smoke_cli_refuses_without_api_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        main(
            [
                "live-smoke",
                "--provider", "openai-compatible",
                "--model", "m",
                "--base-url", "https://api.example.test/v1",
            ]
        )
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def test_run_subcommand_creates_queued_run(tmp_path, capsys):
    spec = {
        "scenario_version": "replay@1",
        "manifest": {"provider": {"kind": "replay", "fixture": {"case-1": {"output": 1, "expected": 1}}}},
        "case_ids": ["case-1"],
    }
    exit_code = main(["run", "--spec", json.dumps(spec), "--db", str(tmp_path / "runs.db")])
    assert exit_code == 0
    created = json.loads(capsys.readouterr().out)
    assert created["status"] == "queued"

    from motte_sdk.service import RunService
    from motte_storage.run_store import SQLiteRunStore

    assert RunService(SQLiteRunStore(tmp_path / "runs.db")).get_run(created["id"])["status"] == "queued"
