"""M4-T06：Claude batch（原生 parser + 受控进程 + 平台链路）。

主断言 test_claude_terminal_missing_and_config_drift：缺 final/未知 schema
不假成功；宿主配置发现不可证明时 reproducibility=partial。fixture 为
tests/fixtures/harness/claude-batch-result-v1.json 与 fake binary。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from motte_harness.claude import ClaudeHarness
from motte_harness.parsers.claude import parse_claude_batch
from motte_harness.supervisor import SupervisedLimits

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "harness"


def _fixture_payload() -> dict:
    payload = json.loads(
        (FIXTURES / "claude-batch-result-v1.json").read_text(encoding="utf-8"),
    )
    # _fixture 是夹具元数据（provenance 标注），不属于 CLI 输出 schema。
    return {key: value for key, value in payload.items() if key != "_fixture"}


class TestParser:
    def test_success_result_parses_with_usage_cost_and_model(self):
        payload = _fixture_payload()
        parsed = parse_claude_batch(json.dumps(payload))
        assert parsed["status"] == "final"
        assert parsed["final_output"].startswith("I created hello.txt")
        assert parsed["session_id"] == "fixture-session-0001"
        assert parsed["usage"]["reported"] is True
        assert parsed["usage"]["input_tokens"] == 420
        assert parsed["usage"]["output_tokens"] == 96
        assert parsed["cost_usd"] == 0.0123
        assert parsed["model"] == "claude-sonnet-4-5"
        assert parsed["coverage"] == "complete"

    def test_error_subtype_is_error_not_success(self):
        payload = {**_fixture_payload(), "subtype": "error_during_execution",
                   "is_error": True}
        parsed = parse_claude_batch(json.dumps(payload))
        assert parsed["status"] == "error"

    def test_missing_final_and_unknown_schema_are_insufficient(self):
        assert parse_claude_batch("")["status"] == "insufficient"
        assert parse_claude_batch("not json")["status"] == "insufficient"
        assert parse_claude_batch("[]")["status"] == "insufficient"
        assert parse_claude_batch(json.dumps({"type": "stream"}))["status"] == "insufficient"
        success = _fixture_payload()
        del success["result"]
        assert parse_claude_batch(json.dumps(success))["reason"] == "missing_result_text"
        unknown_subtype = {**_fixture_payload(), "subtype": "brand-new"}
        parsed = parse_claude_batch(json.dumps(unknown_subtype))
        assert parsed["status"] == "insufficient"
        assert parsed["coverage"] == "partial"

    def test_missing_usage_stays_unknown_never_zero(self):
        payload = _fixture_payload()
        del payload["usage"]
        del payload["modelUsage"]
        del payload["total_cost_usd"]
        parsed = parse_claude_batch(json.dumps(payload))
        assert parsed["usage"]["reported"] is False
        assert parsed["usage"]["input_tokens"] is None
        assert parsed["cost_usd"] is None
        assert parsed["model"] is None

    def test_unknown_extra_fields_lower_coverage_but_keep_result(self):
        payload = {**_fixture_payload(), "future_field": {"x": 1}}
        parsed = parse_claude_batch(json.dumps(payload))
        assert parsed["status"] == "final"
        assert parsed["coverage"] == "partial"
        assert parsed["unknown_fields"] == ["future_field"]


def _fake_claude(tmp_path: Path, *, mode: str = "success") -> Path:
    script = tmp_path / f"fake-claude-{mode}.py"
    result = _fixture_payload()
    if mode == "error":
        result = {**result, "subtype": "error_during_execution", "is_error": True}
    elif mode == "no-final":
        result = {**result, "result": None}
    script.write_text(
        "\n".join([
            "import json",
            "import pathlib",
            "import sys",
            "",
            f"payload = {result!r}",
            "",
            "argv = sys.argv[1:]",
            "if '-p' in argv:",
            "    prompt = argv[argv.index('-p') + 1]",
            "    if 'write' in prompt:",
            "        pathlib.Path('answer.txt').write_text('claude-was-here', encoding='utf-8')",
            "print(json.dumps(payload))",
        ]),
        encoding="utf-8",
    )
    return script


class TestHarnessBatch:
    def test_batch_success_with_fake_binary(self, tmp_path):
        fake = _fake_claude(tmp_path)
        harness = ClaudeHarness(binary=str(fake))
        argv = [sys.executable, str(fake), "-p", "please write answer.txt",
                "--output-format", "json"]
        result = harness.run_batch(
            "please write answer.txt", cwd=str(tmp_path), argv=argv,
            limits=SupervisedLimits(total_timeout=30, idle_timeout=10),
        )
        assert result["harness"] == "claude-cli"
        assert result["transport"] == "cli-batch-json"
        assert result["process"]["exit_code"] == 0
        assert result["parsed"]["status"] == "final"
        assert (tmp_path / "answer.txt").read_text(encoding="utf-8") == "claude-was-here"

    def test_batch_error_subtype_maps_to_error(self, tmp_path):
        fake = _fake_claude(tmp_path, mode="error")
        argv = [sys.executable, str(fake), "-p", "boom", "--output-format", "json"]
        result = harness_result = ClaudeHarness(binary=str(fake)).run_batch(
            "boom", cwd=str(tmp_path), argv=argv,
            limits=SupervisedLimits(total_timeout=30, idle_timeout=10),
        )
        del harness_result
        assert result["parsed"]["status"] == "error"

    def test_batch_missing_final_is_insufficient_not_success(self, tmp_path):
        fake = _fake_claude(tmp_path, mode="no-final")
        argv = [sys.executable, str(fake), "-p", "quiet", "--output-format", "json"]
        result = ClaudeHarness(binary=str(fake)).run_batch(
            "quiet", cwd=str(tmp_path), argv=argv,
            limits=SupervisedLimits(total_timeout=30, idle_timeout=10),
        )
        # exit=0 且有输出，但缺 final：不得判成功（A07）
        assert result["process"]["exit_code"] == 0
        assert result["parsed"]["status"] == "insufficient"

    def test_batch_argv_maps_native_flags_or_rejects(self):
        harness = ClaudeHarness(binary="claude")
        argv = harness.batch_argv(
            "do it", model="claude-sonnet-4-5", max_turns=4,
            permission_mode="acceptEdits", settings_file="/tmp/s.json",
        )
        assert argv[:2] == ["claude", "-p"]
        assert "--model" in argv and "claude-sonnet-4-5" in argv
        assert "--max-turns" in argv and "4" in argv
        assert "--permission-mode" in argv
        assert "--settings" in argv
        assert "--output-format" in argv and "json" in argv
