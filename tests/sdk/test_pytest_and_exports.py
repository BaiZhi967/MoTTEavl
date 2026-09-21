"""M7-T04：tracing 单执行语义、opt-in pytest 插件（A06/A07）、exporter v1 文件写出。

全部离线：零网络、零真实模型/Judge/Runner。内嵌 pytest 运行用 pytester，
插件一律经 ``-p motte_sdk.pytest_plugin`` 显式加载并关闭 autoload，
避免与安装态 entry point 重复注册；另用少量子进程运行验证真实进程行为。
"""
from __future__ import annotations

import builtins
import hashlib
import json
import socket
import urllib.request
import xml.etree.ElementTree as ET

import pytest

pytest_plugins = ["pytester"]

import motte_sdk.export as sdk_export  # noqa: E402
import motte_sdk.pytest_plugin as gate_plugin  # noqa: E402
import motte_sdk.tracing as tracing  # noqa: E402

# ------------------------------------------------------------------ helpers


def rule(
    rule_id: str = "acc",
    *,
    status: str = "pass",
    decision: str | None = None,
    reason: str = "synthetic rule",
    kind: str = "metric_threshold",
) -> dict:
    return {
        "rule_id": rule_id,
        "kind": kind,
        "status": status,
        "severity": "block",
        "decision": decision,
        "reason": reason,
        "diagnostic_skipped": status == "skipped_diagnostic",
    }


def gate_mapping(
    decision: str, rules: list[dict], *, policy_id: str = "pol-m7"
) -> dict:
    """与 CLI 存储侧同形的 GateResult 映射（result_to_json 的输入）。"""
    return {
        "gate_result_id": "sha256:" + "1" * 64,
        "policy_id": policy_id,
        "policy_version": "1",
        "policy_content_hash": "sha256:" + "2" * 64,
        "baseline": None,
        "candidates": (
            {"run_id": "run-m7", "report_semantics_hash": "sha256:" + "3" * 64},
        ),
        "decision": decision,
        "rule_results": tuple(rules),
        "evaluation_input_hash": "sha256:" + "4" * 64,
        "result_semantics_hash": "sha256:" + "5" * 64,
        "conclusion_hash": "sha256:" + "6" * 64,
        "evaluated_at": "2026-09-22T00:00:00+00:00",
        "suggested_actions": (),
    }


def write_export(pytester, decision: str, rules: list[dict]) -> object:
    path = pytester.path / "gate-export.json"
    path.write_text(
        json.dumps(
            sdk_export.result_to_json(gate_mapping(decision, rules)),
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _enabled_run(pytester, monkeypatch, gate_path, *args):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    pytester.makepyfile(test_user="def test_user():\n    assert True\n")
    return pytester.runpytest(
        "-p", "motte_sdk.pytest_plugin",
        f"--motte-gate-report={gate_path}",
        *args,
    )


# ================================================================ tracing


def test_decorator_executes_once_on_success():
    calls: list[int] = []

    @tracing.motte_trace("once-ok", attributes={"run": "r1"})
    def work(value: int) -> int:
        calls.append(value)
        return value * 2

    assert work(21) == 42
    assert calls == [21]
    root = tracing.last_trace_span()
    assert root is not None
    assert root.status == "ok"
    assert root.flush_error is None
    assert root.record["attributes"] == {"run": "r1"}


def test_decorator_executes_once_on_exception():
    calls: list[int] = []

    @tracing.motte_trace("once-err")
    def boom() -> None:
        calls.append(1)
        raise ValueError("boom sk-abcdefghijklmnop leak")

    with pytest.raises(ValueError):
        boom()
    assert calls == [1]
    record = tracing.last_trace_span().record
    assert record["status"] == "error"
    assert record["exception"]["type"] == "ValueError"
    assert "sk-abcdefghijklmnop" not in record["exception"]["message"]
    assert "[REDACTED-SECRET]" in record["exception"]["message"]


def test_decorator_executes_once_when_flush_raises():
    calls: list[str] = []

    def bad_flush(records: list[dict]) -> None:
        calls.append("flush")
        raise RuntimeError("sink down")

    @tracing.motte_trace("once-flush", flush=bad_flush)
    def work() -> str:
        calls.append("run")
        return "done"

    assert work() == "done"  # flush 失败绝不传播
    assert calls == ["run", "flush"]  # callable 只执行一次
    root = tracing.last_trace_span()
    assert root.flush_error is not None
    assert "RuntimeError" in root.flush_error
    assert root.record["flush_error"] == root.flush_error


def test_nested_spans_parent_child_and_records():
    with tracing.trace_span("outer", flush=lambda records: None) as outer:
        with tracing.trace_span("inner") as inner:
            inner.add_event("tick", {"note": "n"})

    assert outer.parent_id is None
    assert inner.parent_id == outer.span_id
    records = outer.records
    assert len(records) == 2  # 子先父后
    inner_record = next(
        r for r in records if r["span_id"] == inner.span_id
    )
    assert inner_record["parent_id"] == outer.span_id
    assert any(e["name"] == "tick" for e in inner_record["events"])
    assert [r["name"] for r in records] == ["inner", "outer"]
    assert tracing.last_trace_span() is outer


def test_nested_flush_only_outermost_runs():
    seen: list[list[str]] = []

    def root_flush(records: list[dict]) -> None:
        seen.append([r["name"] for r in records])

    def child_flush(records: list[dict]) -> None:
        raise AssertionError("nested span flush must not run")

    with tracing.trace_span("root", flush=root_flush):
        with tracing.trace_span("child", flush=child_flush):
            pass

    assert seen == [["child", "root"]]


def test_attribute_redaction_and_set_attribute():
    with tracing.trace_span(
        "red",
        attributes={"note": "leak sk-abcdefghijklmnop token", "api_key": "zzz"},
    ) as span:
        span.set_attribute("authorization", "Bearer abcdefghijklmnop")
        span.set_attribute("headers", {"authorization": "plain-text"})
    attributes = span.record["attributes"]
    assert attributes["api_key"] == "[REDACTED]"
    # 值形状命中（Bearer …）→ 整段替换
    assert attributes["authorization"] == "[REDACTED-SECRET]"
    # 键名命中（dict 内层 authorization）→ 整段替换
    assert attributes["headers"] == {"authorization": "[REDACTED]"}
    assert "sk-abcdefghijklmnop" not in attributes["note"]
    assert "[REDACTED-SECRET]" in attributes["note"]


def test_oversized_attribute_truncated_under_cap():
    big = "x" * (tracing.SPAN_RECORD_LIMIT_BYTES + 4096)
    with tracing.trace_span("big", attributes={"payload": big}) as span:
        pass
    stored = span.record["attributes"]["payload"]
    assert isinstance(stored, dict)
    assert stored["truncated"] is True
    assert 0 < len(stored["value"]) < len(big)
    serialized = json.dumps(span.record, default=str).encode("utf-8")
    assert len(serialized) <= tracing.SPAN_RECORD_LIMIT_BYTES


def test_flush_error_captured_and_swallowed_in_context_manager():
    def boom(records: list[dict]) -> None:
        raise OSError("disk full")

    with tracing.trace_span("f", flush=boom) as span:
        span.add_event("work")

    assert "OSError" in span.flush_error
    assert "disk full" in span.flush_error
    assert span.record["flush_error"] == span.flush_error
    assert len(span.records) == 1


def test_import_tracing_has_no_side_effects(tmp_path):
    # 零副作用：模块已 import（文件顶部），这里验证再次 import 不产生任何文件/
    # 环境写入，也不抛错。
    import importlib

    before = set(tmp_path.iterdir())
    importlib.reload(tracing)
    assert set(tmp_path.iterdir()) == before


# ============================================================ pytest plugin


def test_rule_outcome_mapping_matrix():
    assert gate_plugin.rule_outcome(rule(status="pass")) is None
    assert gate_plugin.rule_outcome(
        rule(status="fail", decision="quality_fail")) == "failure"
    assert gate_plugin.rule_outcome(rule(status="fail", decision=None)) == "failure"
    for decision in (
        "execution_error", "insufficient_evidence", "not_comparable", "safety_block",
    ):
        assert gate_plugin.rule_outcome(
            rule(status="fail", decision=decision)) == "error"
    assert gate_plugin.rule_outcome(rule(status="insufficient")) == "error"
    assert gate_plugin.rule_outcome(rule(status="not_applicable")) == "error"
    assert gate_plugin.rule_outcome(rule(status="skipped_diagnostic")) == "skipped"
    assert gate_plugin.rule_outcome(rule(status="bogus")) == "error"  # fail-closed


def test_assess_gate_export_fail_closed():
    ok = sdk_export.result_to_json(gate_mapping("pass", [rule()]))
    assert gate_plugin.assess_gate_export(ok, None).outcome == "passed"
    empty = sdk_export.result_to_json(gate_mapping("pass", []))
    assert gate_plugin.assess_gate_export(empty, None).outcome == "error"
    inconsistent = dict(ok)
    inconsistent["exit_code"] = 5
    assert gate_plugin.assess_gate_export(inconsistent, None).outcome == "error"
    unknown = dict(ok)
    unknown["decision"] = "weird"
    assert gate_plugin.assess_gate_export(unknown, None).outcome == "error"
    assert gate_plugin.assess_gate_export(None, "gone").outcome == "error"
    # 决策通过但规则带 failure/skipped → 拒绝假通过
    mixed = sdk_export.result_to_json(gate_mapping("pass", [
        rule(), rule(status="skipped_diagnostic"),
    ]))
    assert gate_plugin.assess_gate_export(mixed, None).outcome == "error"


def test_plugin_inert_when_not_enabled(pytester, monkeypatch):
    """A06：插件加载但未开启 → 只跑用户测试、零额外条目、零网络。"""
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    pytester.makepyfile(
        test_user="def test_one():\n    assert True\n\n"
        "def test_two():\n    assert True\n",
    )

    class NoSocket:
        def __init__(self, *args, **kwargs):
            raise AssertionError("network is forbidden while plugin is inert")

    def no_urlopen(*args, **kwargs):
        raise AssertionError("urllib open is forbidden while plugin is inert")

    monkeypatch.setattr(socket, "socket", NoSocket)
    monkeypatch.setattr(urllib.request, "urlopen", no_urlopen)
    result = pytester.runpytest("-p", "motte_sdk.pytest_plugin")
    assert result.ret == 0
    result.assert_outcomes(passed=2, failed=0, errors=0)
    result.stdout.no_fnmatch_line("*motte-gate*")
    result.stdout.no_fnmatch_line("*motte gate report*")


def test_collection_identical_with_and_without_plugin(pytester, monkeypatch):
    """A06（子进程）：collect-only 输出与插件加载与否逐行一致。"""
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    pytester.makepyfile(
        test_user="def test_one():\n    pass\n\ndef test_two():\n    pass\n",
    )

    def collected(result):
        lines = []
        for line in result.stdout.str().splitlines():
            if "::" in line:
                lines.append(line)
            elif "tests collected" in line:
                # 去掉 "in 0.03s" 计时尾巴，只比内容。
                lines.append(line.split(" in ")[0].strip())
        return lines

    without = pytester.runpytest_subprocess("--collect-only", "-q")
    with_plugin = pytester.runpytest_subprocess(
        "--collect-only", "-q", "-p", "motte_sdk.pytest_plugin")
    assert without.ret == 0
    assert with_plugin.ret == 0
    assert collected(without) == collected(with_plugin)
    assert collected(without) == [
        "test_user.py::test_one",
        "test_user.py::test_two",
        "2 tests collected",
    ]


def test_plain_subprocess_run_is_inert(pytester):
    """A06（子进程、真实 autoload）：装了 entry point 也不改变普通运行。"""
    pytester.makepyfile(test_user="def test_user():\n    assert True\n")
    result = pytester.runpytest_subprocess()
    assert result.ret == 0
    result.assert_outcomes(passed=1, failed=0, errors=0)
    result.stdout.no_fnmatch_line("*motte gate report*")


def test_enabled_passing_export_exits_zero(pytester, monkeypatch):
    path = write_export(pytester, "pass", [
        rule(reason="acc 0.9 >= 0.5"), rule("cov", reason="coverage ok"),
    ])
    result = _enabled_run(pytester, monkeypatch, path, "-v")
    assert result.ret == 0
    result.assert_outcomes(passed=2, failed=0, errors=0)  # 用户测试 + gate item
    result.stdout.fnmatch_lines(["*motte gate report*"])
    result.stdout.fnmatch_lines(["*decision=pass exit_code=0*passed*"])
    result.stdout.fnmatch_lines(["*test/motte-gate/pol-m7@1*PASSED*"])


def test_enabled_quality_fail_exits_one(pytester, monkeypatch):
    path = write_export(pytester, "quality_fail", [
        rule(status="fail", decision="quality_fail", reason="acc 0.33 < 0.5"),
        rule("cov", reason="coverage ok"),
    ])
    result = _enabled_run(pytester, monkeypatch, path)
    assert result.ret == 1
    result.assert_outcomes(passed=1, failed=1, errors=0)
    result.stdout.fnmatch_lines(["*decision=quality_fail exit_code=1*failure*"])
    result.stdout.fnmatch_lines(["*acc 0.33 < 0.5*"])


def test_enabled_execution_error_is_error(pytester, monkeypatch):
    path = write_export(pytester, "execution_error", [
        rule(status="fail", decision="execution_error", reason="run failed"),
    ])
    result = _enabled_run(pytester, monkeypatch, path)
    assert result.ret == 1
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*decision=execution_error exit_code=3*error*"])


def test_enabled_insufficient_evidence_is_error(pytester, monkeypatch):
    path = write_export(pytester, "insufficient_evidence", [
        rule(status="insufficient", decision="insufficient_evidence",
             reason="coverage 0.4 < 1.0"),
    ])
    result = _enabled_run(pytester, monkeypatch, path)
    assert result.ret == 1
    result.assert_outcomes(passed=1, errors=1)


def test_enabled_skipped_diagnostic_rule_maps_to_skipped(pytester, monkeypatch):
    path = write_export(pytester, "quality_fail", [
        rule(status="fail", decision="quality_fail", reason="acc 0.2 < 0.5"),
        rule("diag", status="skipped_diagnostic", reason="diagnostic skip"),
        rule("ev", status="insufficient", reason="no evidence"),
    ])
    result = _enabled_run(pytester, monkeypatch, path)
    assert result.ret == 1
    # 注意：fnmatch 的 [] 是字符类，模式里不能写字面方括号。
    result.stdout.fnmatch_lines(["*skipped] diag: diagnostic skip*"])
    result.stdout.fnmatch_lines(["*error] ev: no evidence*"])


def test_enabled_missing_file_errors_not_fake_pass(pytester, monkeypatch):
    result = _enabled_run(pytester, monkeypatch, pytester.path / "missing.json")
    assert result.ret == 1
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*ERROR*gate report unreadable*missing.json*"])


def test_enabled_invalid_json_errors(pytester, monkeypatch):
    bad = pytester.path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    result = _enabled_run(pytester, monkeypatch, bad)
    assert result.ret == 1
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*not valid JSON*"])


def test_enabled_empty_rule_set_errors(pytester, monkeypatch):
    path = write_export(pytester, "pass", [])
    result = _enabled_run(pytester, monkeypatch, path)
    assert result.ret == 1
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*empty rule set*never fake pass*"])


def test_enabled_ini_option_equivalent(pytester, monkeypatch):
    path = write_export(pytester, "pass", [rule()])
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    pytester.makepyfile(test_user="def test_user():\n    assert True\n")
    pytester.makeini(
        "[pytest]\nmotte_gate_report = {}\n".format(path),
    )
    result = pytester.runpytest("-p", "motte_sdk.pytest_plugin")
    assert result.ret == 0
    result.assert_outcomes(passed=2, errors=0)


# ================================================================ exporter


def test_write_gate_export_roundtrip_and_stable_sha256(tmp_path):
    payload = gate_mapping("quality_fail", [
        rule(status="fail", decision="quality_fail", reason="acc 0.33 < 0.5"),
    ])
    out_dir = tmp_path / "out"
    first = sdk_export.write_gate_export(payload, out_dir=out_dir)
    second = sdk_export.write_gate_export(payload, out_dir=out_dir)
    assert first["sha256s"] == second["sha256s"]
    json_path = tmp_path / "out" / "gate-export.json"
    xml_path = tmp_path / "out" / "gate-export.xml"
    assert json_path.is_file() and xml_path.is_file()
    assert first["paths"]["json"] == str(json_path.resolve())
    assert first["sha256s"]["json"] == hashlib.sha256(
        json_path.read_bytes()).hexdigest()
    assert first["sha256s"]["junit"] == hashlib.sha256(
        xml_path.read_bytes()).hexdigest()
    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["exporter_version"] == "gate-exporter@1"
    assert document["decision"] == "quality_fail"
    assert document["exit_code"] == 1
    root = ET.parse(xml_path).getroot()
    assert root.tag == "testsuite"
    assert root.attrib["policy_id"] == "pol-m7"
    assert root.attrib["policy_version"] == "1"
    assert root.findall("testcase/failure")
    properties = {
        p.attrib["name"]: p.attrib["value"]
        for p in root.findall("properties/property")
    }
    for name in (
        "decision", "exit_code", "gate_result_id", "conclusion_hash",
        "exporter_version",
    ):
        assert name in properties, name
    assert properties["exporter_version"] == "gate-exporter@1"


def test_write_gate_export_format_subset_and_rejection(tmp_path):
    payload = gate_mapping("pass", [rule()])
    result = sdk_export.write_gate_export(
        payload, out_dir=tmp_path, formats=("json",))
    assert set(result["paths"]) == {"json"}
    assert not (tmp_path / "gate-export.xml").exists()
    with pytest.raises(ValueError, match="unknown gate export format"):
        sdk_export.write_gate_export(
            payload, out_dir=tmp_path, formats=("html",))


def test_write_gate_export_imports_no_execution_modules(tmp_path, monkeypatch):
    """纯函数断言：写出过程不得 import 任何 provider/runner/网络模块。"""
    denied = {
        "motte_provider", "motte_agent", "motte_harness", "motte_skill",
        "motte_sandbox", "httpx", "docker", "celery", "urllib3", "requests",
        "urllib",
    }
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in denied:
            raise AssertionError(f"unexpected import during export: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    sdk_export.write_gate_export(
        gate_mapping("pass", [rule()]), out_dir=tmp_path)
