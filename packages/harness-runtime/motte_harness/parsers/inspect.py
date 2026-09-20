"""M4-T11：Inspect eval-log 只读 parser（固定 schema 子集）。

支持格式（inspect-eval-log-v1 子集）：首行 header 对象（含 model/task
等运行元数据），后续行为 sample 记录（{"sample": {id, epoch, scores…}}）。
未知 schema 拒绝；只有 aggregate（header+results 无 sample）不制造样本。
解析绝不执行日志内容（无 pickle/插件/命令加载路径）。
"""
from __future__ import annotations

import json
from typing import Any

PARSER_VERSION = "inspect-jsonl-v1"
SUPPORTED_SCHEMA = "inspect-eval-log-v1"

MAX_LOG_BYTES = 64_000_000
MAX_LINES = 200_000
MAX_SAMPLE_SCORES = 64


class InspectLogError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _load_lines(text: str) -> list[dict[str, Any]]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise InspectLogError("EMPTY_LOG", "inspect log has no lines")
    if len(lines) > MAX_LINES:
        raise InspectLogError("LINE_LIMIT", f"inspect log exceeds {MAX_LINES} lines")
    parsed: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise InspectLogError(
                "MALFORMED_JSON", f"line {index + 1} is not JSON",
            ) from error
        if not isinstance(value, dict):
            raise InspectLogError("MALFORMED_JSON", f"line {index + 1} is not an object")
        parsed.append(value)
    return parsed


def parse_inspect_log(text: str) -> dict[str, Any]:
    """解析并归一化；返回 {header, samples, schema, parser_version, coverage}。"""
    if len(text.encode("utf-8")) > MAX_LOG_BYTES:
        raise InspectLogError("SIZE_LIMIT", f"inspect log exceeds {MAX_LOG_BYTES} bytes")
    events = _load_lines(text)
    header = events[0]
    # header 必须是运行元数据（非 sample/event 行）
    if "sample" in header or "event" in header or "error" in header:
        raise InspectLogError("UNKNOWN_SCHEMA", "first line must be the run header")

    samples: list[dict[str, Any]] = []
    unknown_lines = 0
    results_only = False
    for event in events[1:]:
        if "sample" in event:
            sample = event["sample"]
            if not isinstance(sample, dict) or "id" not in sample:
                raise InspectLogError("UNKNOWN_SCHEMA", "sample record missing id")
            scores_payload = sample.get("scores") or {}
            if not isinstance(scores_payload, dict):
                raise InspectLogError("UNKNOWN_SCHEMA", "sample scores must be an object")
            if len(scores_payload) > MAX_SAMPLE_SCORES:
                raise InspectLogError("SCORE_LIMIT", "sample has too many scorers")
            scores = {}
            for scorer, entry in scores_payload.items():
                if isinstance(entry, dict):
                    scores[str(scorer)] = {
                        "name": entry.get("name"),
                        "value": entry.get("value"),
                    }
                else:
                    scores[str(scorer)] = {"name": str(scorer), "value": entry}
            samples.append({
                "id": str(sample["id"]),
                "epoch": sample.get("epoch"),
                "scores": scores,
            })
        elif "results" in event:
            results_only = True
        elif "event" in event or "error" in event or "model" in event:
            continue  # 已知事件类别：保留为日志内容，不制造样本
        else:
            unknown_lines += 1

    if results_only and not samples:
        raise InspectLogError(
            "AGGREGATE_ONLY",
            "log contains aggregate results without samples; the platform "
            "does not fabricate samples from aggregates",
        )
    if unknown_lines:
        raise InspectLogError(
            "UNKNOWN_SCHEMA",
            f"{unknown_lines} log lines have unknown schema; refusing import",
        )
    return {
        "schema": SUPPORTED_SCHEMA,
        "parser_version": PARSER_VERSION,
        "header": header,
        "samples": samples,
        "coverage": "complete" if samples else "partial",
    }
