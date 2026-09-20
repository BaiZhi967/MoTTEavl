"""M4-T07：Codex batch（exec JSONL parser + 受控进程 + 平台链路）。

主断言 test_codex_partial_json_turn_failure_and_unknown_cost：partial
JSONL / turn.failed / exit=0 缺 terminal 不假成功；未回报模型/费用保持
unknown。fixture：codex-exec-events-v1.jsonl 与 fake binary。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from motte_harness.codex import CodexHarness
from motte_harness.parsers.codex import parse_codex_exec_events
from motte_harness.supervisor import SupervisedLimits

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "harness"


def _fixture_lines() -> list[str]:
    return (FIXTURES / "codex-exec-events-v1.jsonl").read_text(
        encoding="utf-8",
    ).splitlines()


class TestParser:
    def test_complete_stream_parses_items_usage_and_terminal(self):
        parsed = parse_codex_exec_events("\n".join(_fixture_lines()))
        assert parsed["status"] == "final"
        assert parsed["thread_ids"] == ["th_fixture_0001"]
        assert parsed["final_output"].startswith("Created hello.txt")
        assert len(parsed["commands"]) == 1
        assert parsed["commands"][0]["exit_code"] == 0
        assert parsed["usage"]["reported"] is True
        assert parsed["usage"]["input_tokens"] == 512
        assert parsed["usage"]["output_tokens"] == 64
        assert parsed["coverage"] == "complete"

    def test_partial_json_retained_with_lowered_coverage(self):
        lines = _fixture_lines()
        stream = "\n".join(lines[:3]) + '\n{"broken json"\n' + "\n".join(lines[3:])
        parsed = parse_codex_exec_events(stream)
        assert parsed["malformed_lines"] == 1
        assert parsed["coverage"] == "partial"
        # 其余事件仍然解析
        assert parsed["status"] == "final"

    def test_turn_failure_without_terminal_is_error(self):
        lines = [
            line for line in _fixture_lines()
            if '"thread.completed"' not in line and '"turn.completed"' not in line
        ]
        lines.append(json.dumps({
            "id": "resp_x", "msg": {"type": "turn.failed", "error": "tool crashed"},
        }))
        parsed = parse_codex_exec_events("\n".join(lines))
        assert parsed["status"] == "error"
        assert "tool crashed" in parsed["turn_failures"][0]

    def test_exit_zero_without_terminal_is_insufficient(self):
        lines = [
            line for line in _fixture_lines()
            if '"thread.completed"' not in line and '"turn.completed"' not in line
        ]
        parsed = parse_codex_exec_events("\n".join(lines))
        assert parsed["status"] == "insufficient"
        assert parsed["coverage"] == "partial"

    def test_missing_usage_stays_unknown_never_zero(self):
        lines = [line for line in _fixture_lines() if '"turn.completed"' not in line]
        lines.append(json.dumps({"id": "x", "msg": {"type": "thread.completed"}}))
        parsed = parse_codex_exec_events("\n".join(lines))
        assert parsed["status"] == "final"
        assert parsed["usage"]["reported"] is False
        assert parsed["usage"]["input_tokens"] is None
        assert parsed["cost_usd"] is None
        # 模型不在流中回报：不得用请求值冒充 observed
        assert parsed["model"] is None

    def test_unknown_message_types_are_retained_not_fatal(self):
        stream = "\n".join(_fixture_lines()) + "\n" + json.dumps({
            "id": "x", "msg": {"type": "future.event", "data": 1},
        })
        parsed = parse_codex_exec_events(stream)
        assert parsed["status"] == "final"
        assert parsed["unknown_msg_types"] == ["future.event"]
        assert parsed["coverage"] == "partial"


def _fake_codex(tmp_path: Path, *, mode: str = "success") -> Path:
    lines = _fixture_lines()
    if mode == "no-terminal":
        lines = [
            line for line in lines
            if '"thread.completed"' not in line and '"turn.completed"' not in line
        ]
    events_file = tmp_path / f"fake-codex-{mode}-events.jsonl"
    events_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script = tmp_path / f"fake-codex-{mode}.py"
    script.write_text(
        "\n".join([
            "import json",
            "import pathlib",
            "import sys",
            "",
            "events_file = pathlib.Path(__file__).with_name(EVENTS_FILE)",
            "events = [json.loads(line) for line in events_file.read_text(encoding='utf-8').splitlines() if line.strip()]",
            "",
            "prompt = ' '.join(sys.argv[2:])",
            "if 'write' in prompt:",
            "    pathlib.Path('answer.txt').write_text('codex-was-here', encoding='utf-8')",
            "for event in events:",
            "    print(json.dumps(event), flush=True)",
        ]).replace("EVENTS_FILE", repr(events_file.name)),
        encoding="utf-8",
    )
    return script


class TestHarnessBatch:
    def test_batch_success_with_fake_binary(self, tmp_path):
        fake = _fake_codex(tmp_path)
        argv = [sys.executable, str(fake), "exec", "--json", "please write answer.txt"]
        result = CodexHarness(binary=str(fake)).run_batch(
            "please write answer.txt", cwd=str(tmp_path), argv=argv,
            limits=SupervisedLimits(total_timeout=30, idle_timeout=10),
        )
        assert result["harness"] == "codex-cli"
        assert result["transport"] == "cli-exec-jsonl"
        assert result["process"]["exit_code"] == 0
        assert result["parsed"]["status"] == "final"
        assert result["parsed"]["commands"][0]["exit_code"] == 0
        assert (tmp_path / "answer.txt").read_text(encoding="utf-8") == "codex-was-here"

    def test_batch_missing_terminal_is_insufficient(self, tmp_path):
        fake = _fake_codex(tmp_path, mode="no-terminal")
        argv = [sys.executable, str(fake), "exec", "--json", "quiet task"]
        result = CodexHarness(binary=str(fake)).run_batch(
            "quiet task", cwd=str(tmp_path), argv=argv,
            limits=SupervisedLimits(total_timeout=30, idle_timeout=10),
        )
        # exit=0 但缺 thread/turn 终态：不假成功（A07）
        assert result["process"]["exit_code"] == 0
        assert result["parsed"]["status"] == "insufficient"

    def test_batch_argv_pins_exec_jsonl_transport(self):
        harness = CodexHarness(binary="codex")
        argv = harness.batch_argv(
            "task", model="gpt-5-codex",
            config_overrides=[("sandbox_mode", "workspace-write")],
            sandbox="workspace-write",
        )
        assert argv[:2] == ["codex", "exec"]
        assert "--json" in argv
        assert "-m" in argv and "gpt-5-codex" in argv
        assert "-c" in argv and "sandbox_mode=workspace-write" in argv
        assert "--sandbox" in argv
        assert "--skip-git-repo-check" in argv
        assert argv[-1] == "task"
