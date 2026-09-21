"""scripts/m5_failure_diff.py 的集合运算测试：必须按 node ID 比较，不能按总数。

总数相减会把"新增 1 个 + 修好 1 个"误报成"没有变化"，这正是审查指出的问题。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load():
    path = REPO_ROOT / "scripts" / "m5_failure_diff.py"
    spec = importlib.util.spec_from_file_location("m5_failure_diff", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["m5_failure_diff"] = module
    spec.loader.exec_module(module)
    return module


JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="{total}" failures="{failures}" errors="0" skipped="{skipped}">
{body}
  </testsuite>
</testsuites>
"""


def _case(classname: str, name: str, kind: str | None = None) -> str:
    if kind is None:
        return f'    <testcase classname="{classname}" name="{name}" time="0.01" />'
    return (
        f'    <testcase classname="{classname}" name="{name}" time="0.01">'
        f"<{kind}>boom</{kind}></testcase>"
    )


def _write(tmp_path: Path, name: str, cases: list[str], *, total: int | None = None,
           failures: int = 0, skipped: int = 0) -> Path:
    path = tmp_path / name
    path.write_text(
        JUNIT.format(
            total=total if total is not None else len(cases),
            failures=failures, skipped=skipped,
            body="\n".join(cases),
        ),
        encoding="utf-8",
    )
    return path


def test_only_failure_and_error_cases_count(tmp_path):
    module = _load()
    base = _write(tmp_path, "base.xml", [
        _case("tests.a", "test_pass"),
        _case("tests.a", "test_skip"),
        _case("tests.a", "test_fail", "failure"),
        _case("tests.b", "test_err", "error"),
    ], skipped=1)
    assert module.failed_node_ids(base) == {"tests.a::test_fail", "tests.b::test_err"}


def test_total_count_equality_does_not_hide_a_swap(tmp_path):
    """总数相同但集合不同：必须报告 one added + one removed。"""
    module = _load()
    base = _write(tmp_path, "base.xml", [
        _case("tests.a", "test_old", "failure"),
        _case("tests.a", "test_keep", "failure"),
    ], failures=2)
    head = _write(tmp_path, "head.xml", [
        _case("tests.a", "test_new", "failure"),
        _case("tests.a", "test_keep", "failure"),
    ], failures=2)
    result = module.diff_sets(module.failed_node_ids(base), module.failed_node_ids(head))
    assert result["added"] == ["tests.a::test_new"]
    assert result["removed"] == ["tests.a::test_old"]
    assert result["common"] == ["tests.a::test_keep"]


def test_exit_code_is_nonzero_when_a_new_failure_appears(tmp_path):
    module = _load()
    base = _write(tmp_path, "base.xml", [_case("tests.a", "test_keep", "failure")], failures=1)
    head = _write(tmp_path, "head.xml", [
        _case("tests.a", "test_keep", "failure"),
        _case("tests.a", "test_new", "failure"),
    ], failures=2)
    assert module.main(["--base", str(base), "--head", str(head)]) == 1
    assert module.main(["--base", str(base), "--head", str(base)]) == 0


def test_json_report_is_written_with_all_three_sets(tmp_path):
    module = _load()
    import json

    base = _write(tmp_path, "base.xml", [_case("tests.a", "test_old", "failure")], failures=1)
    head = _write(tmp_path, "head.xml", [_case("tests.a", "test_new", "failure")], failures=1)
    out = tmp_path / "report.json"
    module.main(["--base", str(base), "--head", str(head), "--json", str(out)])
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["base_total"] == 1 and payload["head_total"] == 1
    assert payload["added"] == ["tests.a::test_new"]
    assert payload["removed"] == ["tests.a::test_old"]
    assert payload["common"] == []
