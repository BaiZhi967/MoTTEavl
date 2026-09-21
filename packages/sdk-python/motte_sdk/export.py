"""Exporter v1（M6-T10）：GateResult 的 JSON 与最小 JUnit XML。

- JSON：GateResult/Comparison 视图的 canonical dump（全部 rule results，
  缺失/unknown/不可比不裁剪）。
- JUnit：每个 rule 一个 testcase；决策映射：pass→通过、quality_fail→failure、
  其余（insufficient/not_comparable/execution_error/safety_block）→ error。
  顶层 testsuite 记录 decision 与退出码摘要；CI 阻断由退出码 + JUnit 状态共同
  表达（协议 §7）。
- M7 只能在同一版本扩展（testsuite 属性/附加 file），不重写 exporter 主权。
  M7-T04 的 v1 内扩展：testsuite 新增 ``policy_id``/``policy_version`` 属性、
  ``write_gate_export`` 平行文件写出（gate-export.json + gate-export.xml）。
  decision→exit_code/JUnit 映射保持不变，不重算分数，零模型/Judge/Runner 调用。
- 只消费已求值结果：零模型/Judge/Runner 调用，无状态修改。
"""
from __future__ import annotations

import hashlib
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping

from motte_contracts.gates import DECISION_EXIT_CODES, GateDecision, GateResult

EXPORTER_VERSION = "gate-exporter@1"

#: JUnit testcase 判定（协议 §7）。
_JUNIT_KIND = {
    GateDecision.PASS: None,
    GateDecision.QUALITY_FAIL: "failure",
    GateDecision.INSUFFICIENT_EVIDENCE: "error",
    GateDecision.NOT_COMPARABLE: "error",
    GateDecision.EXECUTION_ERROR: "error",
    GateDecision.SAFETY_BLOCK: "error",
}


def result_to_json(result: GateResult | Mapping[str, Any]) -> dict[str, Any]:
    """GateResult → canonical JSON dict（保留全部规则与原因）。"""
    if isinstance(result, GateResult):
        payload = result.model_dump()
    else:
        payload = dict(result)
    decision = payload.get("decision")
    exit_code = DECISION_EXIT_CODES.get(
        decision if isinstance(decision, GateDecision)
        else GateDecision(decision),
    )
    return {
        "exporter_version": EXPORTER_VERSION,
        "decision": decision.value if isinstance(decision, GateDecision) else decision,
        "exit_code": exit_code,
        "gate_result_id": payload.get("gate_result_id"),
        "conclusion_hash": payload.get("conclusion_hash"),
        "evaluation_input_hash": payload.get("evaluation_input_hash"),
        "result_semantics_hash": payload.get("result_semantics_hash"),
        "policy": {
            "policy_id": payload.get("policy_id"),
            "policy_version": payload.get("policy_version"),
            "policy_content_hash": payload.get("policy_content_hash"),
        },
        "baseline": payload.get("baseline"),
        "candidates": payload.get("candidates"),
        "rule_results": payload.get("rule_results") or [],
        "suggested_actions": payload.get("suggested_actions") or [],
        "evaluated_at": payload.get("evaluated_at"),
    }


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def result_to_junit(result: GateResult | Mapping[str, Any]) -> str:
    """GateResult → 最小 JUnit XML（每条规则一个 testcase）。

    非通过决策下的通过规则仍记录为通过 testcase（保留全部事实）；整体
    decision 与 exit code 写进 testsuite 属性，CI 读其一即可阻断。
    """
    payload = (
        result.model_dump() if isinstance(result, GateResult) else dict(result)
    )
    decision_value = payload.get("decision")
    decision = (
        decision_value if isinstance(decision_value, GateDecision)
        else GateDecision(decision_value)
    )
    exit_code = DECISION_EXIT_CODES[decision]
    rules = payload.get("rule_results") or []
    suite = ET.Element(
        "testsuite",
        {
            "name": "motte-gate:"
            + str(payload.get("policy_id", "unknown")) + "@"
            + str(payload.get("policy_version", "unknown")),
            # M7-T04 v1 内扩展：testsuite 直接携带政策身份（协议 §4 允许）。
            "policy_id": str(payload.get("policy_id", "unknown")),
            "policy_version": str(payload.get("policy_version", "unknown")),
            "tests": str(len(rules)),
            "failures": str(
                sum(1 for rule in rules if rule.get("status") == "fail")
            ),
            "errors": str(
                sum(
                    1 for rule in rules
                    if rule.get("status") in ("insufficient", "not_applicable")
                )
            ),
            "skipped": str(
                sum(1 for rule in rules if rule.get("status") == "skipped_diagnostic")
            ),
            "time": "0",
        },
    )
    ET.SubElement(
        suite, "properties",
    ).extend([
        ET.Element("property", {"name": "decision", "value": decision.value}),
        ET.Element("property", {"name": "exit_code", "value": str(exit_code)}),
        ET.Element("property", {
            "name": "gate_result_id",
            "value": str(payload.get("gate_result_id", "")),
        }),
        ET.Element("property", {
            "name": "conclusion_hash",
            "value": str(payload.get("conclusion_hash", "")),
        }),
        ET.Element("property", {"name": "exporter_version", "value": EXPORTER_VERSION}),
    ])
    for rule in rules:
        case = ET.SubElement(
            suite, "testcase",
            {"classname": "gate." + str(rule.get("kind", "rule")),
             "name": str(rule.get("rule_id", "rule"))},
        )
        status = rule.get("status")
        reason = str(rule.get("reason", ""))
        if status == "fail":
            kind = (
                "failure" if rule.get("decision") == GateDecision.QUALITY_FAIL.value
                or rule.get("decision") is None
                else "error"
            )
            if rule.get("decision") in (
                GateDecision.EXECUTION_ERROR.value,
                GateDecision.INSUFFICIENT_EVIDENCE.value,
                GateDecision.NOT_COMPARABLE.value,
                GateDecision.SAFETY_BLOCK.value,
            ):
                kind = "error"
            ET.SubElement(
                case, kind, {"message": _escape(reason), "type": str(rule.get("decision") or "fail")},
            ).text = _escape(reason)
        elif status in ("insufficient", "not_applicable"):
            ET.SubElement(
                case, "error",
                {"message": _escape(reason), "type": str(rule.get("decision") or status)},
            ).text = _escape(reason)
        elif status == "skipped_diagnostic":
            ET.SubElement(case, "skipped", {"message": _escape(reason)})
    ET.indent(suite, space="  ")
    return ET.tostring(suite, encoding="unicode")


def comparison_to_json(view: Mapping[str, Any]) -> dict[str, Any]:
    """比较视图 → canonical JSON（三级结论 + 结构性/指标级原因 + case diff）。"""
    return {
        "exporter_version": EXPORTER_VERSION,
        "level": view.get("level"),
        "eligible": view.get("eligible"),
        "structural_reasons": list(view.get("structural_reasons") or ()),
        "metric_reasons": list(view.get("metric_reasons") or ()),
        "metric_eligibility": dict(view.get("metric_eligibility") or {}),
        "case_diff": dict(view.get("case_diff") or {}),
        "allowed_differences": list(view.get("allowed_differences") or ()),
    }


def _stable_json_line(payload: Mapping[str, Any]) -> str:
    """确定性的 JSON 文本（排序键、UTF-8、尾随换行），供 sha256 稳定。"""
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, indent=2
    ) + "\n"


#: ``formats`` 兞际支持的取值 → 输出文件名。
_EXPORT_FILES: dict[str, str] = {
    "json": "gate-export.json",
    "junit": "gate-export.xml",
}


def write_gate_export(
    result: GateResult | Mapping[str, Any],
    *,
    out_dir: str | os.PathLike[str],
    formats: tuple[str, ...] | list[str] = ("json", "junit"),
) -> dict[str, dict[str, str]]:
    """把一个已求值的 GateResult 写成平行导出文件（M7-T04，v1 内扩展）。

    - ``gate-export.json``（``result_to_json`` 内容）与
      ``gate-export.xml``（``result_to_junit`` 内容，与 CLI 落盘字节一致）；
    - 纯函数：只消费传入结果，零模型/Judge/Runner 调用、零网络、零状态修改；
    - 返回 ``{"paths": {format: 绝对路径}, "sha256s": {format: hex}}``；
      同一输入重复写出内容字节稳定，故 sha256 稳定。
    不新建 exporter 版本、不改变 decision→exit_code/JUnit 映射（协议 §4）。
    """
    contents: dict[str, str] = {
        "json": _stable_json_line(result_to_json(result)),
        "junit": result_to_junit(result) + "\n",
    }
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    sha256s: dict[str, str] = {}
    for fmt in formats:
        if fmt not in _EXPORT_FILES:
            known = ",".join(sorted(_EXPORT_FILES))
            raise ValueError(f"unknown gate export format: {fmt!r} (known: {known})")
        path = target / _EXPORT_FILES[fmt]
        # write_bytes 而非 write_text：Windows 文本模式会做 \n→\r\n 换行翻译，
        # 破坏"内容字节 == sha256 输入"的稳定性。
        path.write_bytes(contents[fmt].encode("utf-8"))
        resolved = str(path.resolve())
        paths[fmt] = resolved
        sha256s[fmt] = hashlib.sha256(
            contents[fmt].encode("utf-8")
        ).hexdigest()
    return {"paths": paths, "sha256s": sha256s}
