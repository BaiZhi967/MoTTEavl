"""opt-in pytest 插件（M7-T04，协议 §4 / A06 / A07）。

**未开启时（A06）插件什么都不做**：没有 ``--motte-gate-report <path>``
（或 ini ``motte_gate_report``）时，除注册 argparse 选项外零行为——
不收集额外条目、不扫数据集、不读凭据、不创建 Run、零网络、零模型调用。

开启后：读取一个**已存在的 M6 导出 JSON**（``motte_sdk.export.result_to_json``
的形状：``exporter_version=="gate-exporter@1"``、``decision``、``exit_code``、
``rule_results[]``），把 Gate 结论映射为 pytest 结果。插件**永不创建 Run、
永不调用模型/Judge/Benchmark**，只消费已求值的导出文件。

映射（协议 §4，与 exporter v1 的 JUnit 映射一致）：

===========================  =========================================
rule status / decision       pytest 结果
===========================  =========================================
pass                         通过（无附加结果）
fail + quality_fail（或 None）failure
fail + execution_error /
    insufficient_evidence /
    not_comparable / safety_block  error
insufficient / not_applicable（含未知 status） error
skipped_diagnostic           skipped
===========================  =========================================

顶层决策 → 合成 item ``test/motte-gate/<policy>@<version>`` 的结局：

- ``pass`` → 通过（前提：规则集非空、exit_code 与决策一致、没有任何
  failure/error/skipped 规则——否则视为不一致导出，fail-closed）；
- ``quality_fail`` → failure（pytest 退出码 1）；
- 其余决策（execution_error / insufficient_evidence / not_comparable /
  safety_block）→ error；
- 文件缺失 / 非 JSON / 非 gate 导出 / 空规则集 → error（A07：**永不假通过**）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pytest

__all__ = [
    "GateAssessment",
    "MotteGateItem",
    "assess_gate_export",
    "rule_outcome",
]

#: 决策 → error 的四类（quality_fail 单独映射 failure）。
_ERROR_DECISIONS = frozenset({
    "execution_error",
    "insufficient_evidence",
    "not_comparable",
    "safety_block",
})

_KNOWN_DECISIONS = frozenset(_ERROR_DECISIONS | {"pass", "quality_fail"})


class MotteGateExportError(Exception):
    """gate 导出不可读/不合法/证据不足 → 合成 item 记为 error（A07）。"""


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("motte")
    group.addoption(
        "--motte-gate-report",
        action="store",
        default=None,
        metavar="PATH",
        help="path to an existing M6 gate export JSON (result_to_json shape); "
        "enables the motte gate pytest item",
    )
    parser.addini(
        "motte_gate_report",
        help="ini equivalent of --motte-gate-report (path to a gate export JSON)",
        default=None,
    )


def _report_path(config: pytest.Config) -> Path | None:
    """开启才返回路径；未开启返回 None（插件全部行为以此为门）。"""
    value = config.getoption("motte_gate_report", None) or config.getini(
        "motte_gate_report"
    )
    if value is None or not str(value).strip():
        return None
    return Path(str(value))


def _load_export(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """读取并做最小形状校验；任何失败都返回 (None, 错误消息)。"""
    from motte_sdk.export import EXPORTER_VERSION

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        return None, f"gate report unreadable: {path} ({error})"
    except ValueError as error:  # json.JSONDecodeError
        return None, f"gate report is not valid JSON: {path} ({error})"
    if not isinstance(raw, dict):
        return None, f"gate report must be a JSON object: {path}"
    version = raw.get("exporter_version")
    if version != EXPORTER_VERSION:
        return None, (
            f"gate report exporter_version {version!r} unsupported "
            f"(expected {EXPORTER_VERSION!r}): {path}"
        )
    return raw, None


class _GateState:
    """一次会话只加载一次的导出状态（挂在 config 上）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.payload, self.load_error = _load_export(path)

    def policy_label(self) -> str:
        if self.payload is not None:
            policy = self.payload.get("policy") or {}
            policy_id = (
                policy.get("policy_id") or self.payload.get("policy_id") or "unknown"
            )
            policy_version = (
                policy.get("policy_version")
                or self.payload.get("policy_version")
                or "unknown"
            )
            return f"{policy_id}@{policy_version}"
        return self.path.stem or "unknown"


def rule_outcome(rule: Mapping[str, Any]) -> str | None:
    """rule result → pytest 结果：failure / error / skipped / None（通过）。

    未知 status 一律 error（fail-closed，A07）。
    """
    status = rule.get("status")
    decision = rule.get("decision")
    if status == "pass":
        return None
    if status == "fail":
        if decision in _ERROR_DECISIONS:
            return "error"
        if decision == "quality_fail" or decision is None:
            return "failure"
        return "error"
    if status in ("insufficient", "not_applicable"):
        return "error"
    if status == "skipped_diagnostic":
        return "skipped"
    return "error"


class GateAssessment:
    """导出 → 合成 item 结局（outcome/message）+ 逐 rule 映射。"""

    def __init__(
        self,
        outcome: str,
        message: str,
        *,
        decision: str | None = None,
        exit_code: int | None = None,
        rule_outcomes: list[tuple[Mapping[str, Any], str | None]] | None = None,
    ) -> None:
        self.outcome = outcome  # "passed" | "failure" | "error"
        self.message = message
        self.decision = decision
        self.exit_code = exit_code
        self.rule_outcomes = rule_outcomes or []


def assess_gate_export(
    payload: Mapping[str, Any] | None, load_error: str | None
) -> GateAssessment:
    """顶层决策 → (outcome, message)。不可读/不一致/空规则集 → error。"""
    from motte_contracts.gates import DECISION_EXIT_CODES, GateDecision

    if load_error is not None:
        return GateAssessment("error", load_error)
    assert payload is not None
    decision = payload.get("decision")
    if decision not in _KNOWN_DECISIONS:
        return GateAssessment("error", f"unknown gate decision: {decision!r}")
    assert isinstance(decision, str)
    expected_exit = DECISION_EXIT_CODES[GateDecision(decision)]
    reported_exit = payload.get("exit_code")
    if reported_exit != expected_exit:
        return GateAssessment(
            "error",
            f"gate export exit_code {reported_exit!r} inconsistent with "
            f"decision {decision!r} (expected {expected_exit})",
            decision=decision,
        )
    rules = payload.get("rule_results")
    if not isinstance(rules, (list, tuple)) or not rules:
        return GateAssessment(
            "error",
            "gate export has an empty rule set (A07: never fake pass)",
            decision=decision,
            exit_code=expected_exit,
        )
    mapped: list[tuple[Mapping[str, Any], str | None]] = []
    for rule in rules:
        if not isinstance(rule, Mapping):
            return GateAssessment(
                "error",
                "gate export rule_results contains a non-object entry "
                "(A07: never fake pass)",
                decision=decision,
                exit_code=expected_exit,
            )
        mapped.append((rule, rule_outcome(rule)))
    bad = [(r, o) for r, o in mapped if o in ("failure", "error")]
    detail = "; ".join(
        f"{r.get('rule_id')}: {r.get('reason', '')}" for r, _ in bad
    )
    if decision == "pass":
        if bad or any(o == "skipped" for _, o in mapped):
            return GateAssessment(
                "error",
                "gate export says decision=pass but rules report "
                f"failure/error/skipped ({detail}) — inconsistent export, "
                "refusing to fake pass (A07)",
                decision=decision,
                exit_code=expected_exit,
                rule_outcomes=mapped,
            )
        return GateAssessment(
            "passed", f"gate pass: {len(mapped)} rule(s) satisfied",
            decision=decision, exit_code=expected_exit, rule_outcomes=mapped,
        )
    if decision == "quality_fail":
        return GateAssessment(
            "failure",
            f"gate quality_fail (exit {expected_exit}): {detail}",
            decision=decision, exit_code=expected_exit, rule_outcomes=mapped,
        )
    return GateAssessment(
        "error",
        f"gate {decision} (exit {expected_exit}): {detail}",
        decision=decision, exit_code=expected_exit, rule_outcomes=mapped,
    )


class MotteGateItem(pytest.Item):
    """合成 gate item：结局在运行时由导出内容决定（A07：永不假通过）。"""

    def __init__(self, *, gate_state: _GateState, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.gate_state = gate_state

    def _assess(self) -> GateAssessment:
        return assess_gate_export(self.gate_state.payload, self.gate_state.load_error)

    def setup(self) -> None:
        # error 类结局在 setup 抛出 → pytest 记为 ERROR（区别于 failure）。
        assessment = self._assess()
        if assessment.outcome == "error":
            raise MotteGateExportError(assessment.message)

    def runtest(self) -> None:
        assessment = self._assess()
        if assessment.outcome == "failure":
            pytest.fail(assessment.message, pytrace=False)

    def repr_failure(self, excinfo: pytest.ExceptionInfo[BaseException]) -> str:
        if excinfo.errisinstance(MotteGateExportError):
            return str(excinfo.value)
        return super().repr_failure(excinfo)  # type: ignore[return-value]

    def reportinfo(self) -> tuple[Path, int | None, str]:
        return self.path, None, self.name


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """开启时附加一个合成 gate item；未开启直接返回（A06）。"""
    path = _report_path(config)
    if path is None:
        return
    state = _GateState(path)
    config._motte_gate_state = state
    items.append(MotteGateItem.from_parent(
        session, name="test/motte-gate/" + state.policy_label(), gate_state=state,
    ))


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: pytest.ExitCode | int,
    config: pytest.Config,
) -> None:
    """开启时把 gate 结论写进会话汇总（stdout 一次；未开启不写）。"""
    state: _GateState | None = getattr(config, "_motte_gate_state", None)
    if state is None:
        return
    reporter = terminalreporter
    reporter.write_sep("=", f"motte gate report: {state.path}")
    if state.load_error is not None:
        reporter.write_line(f"ERROR {state.load_error}")
        return
    assessment = assess_gate_export(state.payload, None)
    reporter.write_line(
        f"decision={assessment.decision} exit_code={assessment.exit_code}"
        f" -> {assessment.outcome}"
    )
    for rule, mapped in assessment.rule_outcomes:
        reporter.write_line(
            f"[{mapped or 'pass':>7}] {rule.get('rule_id')}: "
            f"{rule.get('reason', '')}"
        )
    if assessment.outcome != "passed":
        reporter.write_line(f"gate verdict: {assessment.outcome} — {assessment.message}")
