"""M4-T11：Inspect EvalLog 只读 parser（官方 .json 完整格式子集）。

支持格式（inspect-eval-log-v2）：官方 ``.json`` EvalLog 是**单个 JSON
对象**（``--full`` 时含 ``samples`` 列表；每个 sample 有 id/epoch/scores
等）。``.eval`` 二进制格式不支持（INSPECT_FORMAT_UNSUPPORTED 由导入层
给出可区分错误）。只有 aggregate（无 samples）不制造样本；未知 schema
拒绝；解析绝不执行日志内容（无 pickle/插件/命令加载路径）。

工具/样本上限照旧（MAX_*）；sample.scores 的 scorer 数量有界。
"""
from __future__ import annotations

import json
from typing import Any

PARSER_VERSION = "inspect-json-v3"
SUPPORTED_SCHEMA = "inspect-eval-log-v2"

MAX_LOG_BYTES = 64_000_000
MAX_SAMPLES = 100_000
MAX_SAMPLE_SCORES = 64

# EvalLog 顶层必须具备的运行元数据字段（官方 schema 子集）
_REQUIRED_TOP_FIELDS = ("version", "plan", "eval")
_KNOWN_TOP_FIELDS = frozenset({
    "version", "plan", "results", "samples", "status", "created",
    "model", "eval", "dataset", "scorer", "config", "bundle", "started_at",
    "completed_at", "error", "statistics", "transcript", "stats", "reductions",
    "invalidated", "log_updates", "config_updates", "tags", "metadata",
})
_KNOWN_STATUSES = frozenset({
    "started", "success", "completed", "failed", "stopped", "cancelled", "error",
})


class InspectLogError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _extract_sample(entry: Any, index: int) -> dict[str, Any]:
    if not isinstance(entry, dict) or "id" not in entry:
        raise InspectLogError(
            "UNKNOWN_SCHEMA", f"samples[{index}] is missing an id"
        )
    scores_payload = entry.get("scores") or {}
    if not isinstance(scores_payload, dict):
        raise InspectLogError(
            "UNKNOWN_SCHEMA", f"samples[{index}].scores must be an object"
        )
    if len(scores_payload) > MAX_SAMPLE_SCORES:
        raise InspectLogError(
            "SCORE_LIMIT", f"samples[{index}] has too many scorers"
        )
    scores: dict[str, Any] = {}
    for scorer, value in scores_payload.items():
        if isinstance(value, dict):
            scores[str(scorer)] = {
                "name": value.get("name") or str(scorer),
                "value": value.get("value"),
                "answer": value.get("answer"),
                "explanation": value.get("explanation"),
            }
        else:
            scores[str(scorer)] = {"name": str(scorer), "value": value}
    epoch = entry.get("epoch", 1)
    if type(epoch) is not int or epoch < 1 or type(entry["id"]) not in (str, int):
        raise InspectLogError("UNKNOWN_SCHEMA", "sample id/epoch has invalid type")
    if not str(entry["id"]):
        raise InspectLogError("UNKNOWN_SCHEMA", "sample id must not be empty")
    return {
        "id": str(entry["id"]),
        "epoch": epoch,
        "scores": scores,
        "output": entry.get("output"),
        "error": entry.get("error"),
    }


def parse_inspect_log(text: str) -> dict[str, Any]:
    """解析并归一化；返回 {header, samples, schema, parser_version, coverage}。"""
    if len(text.encode("utf-8")) > MAX_LOG_BYTES:
        raise InspectLogError("SIZE_LIMIT", f"inspect log exceeds {MAX_LOG_BYTES} bytes")
    stripped = text.strip()
    if not stripped:
        raise InspectLogError("EMPTY_LOG", "inspect log is empty")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as error:
        raise InspectLogError(
            "MALFORMED_JSON",
            f"log is not a JSON object (official .json EvalLog expected; "
            f"binary .eval is unsupported): {error}",
        ) from error
    if not isinstance(payload, dict):
        raise InspectLogError("MALFORMED_JSON", "inspect log must be a JSON object")
    missing = [field for field in _REQUIRED_TOP_FIELDS if field not in payload]
    if missing:
        raise InspectLogError(
            "UNKNOWN_SCHEMA",
            f"EvalLog object is missing required fields: {missing}",
        )
    if type(payload["version"]) is not int or payload["version"] != 2:
        raise InspectLogError("UNKNOWN_SCHEMA", "only Inspect EvalLog version 2 is supported")
    identity = _log_identity(payload)
    unknown_fields = sorted(set(payload) - _KNOWN_TOP_FIELDS)
    status = payload.get("status")
    if status is not None and (not isinstance(status, str) or status not in _KNOWN_STATUSES):
        raise InspectLogError("UNKNOWN_SCHEMA", f"unknown EvalLog status: {status!r}")
    samples_payload = payload.get("samples")
    if samples_payload is not None and not isinstance(samples_payload, list):
        raise InspectLogError("UNKNOWN_SCHEMA", "samples must be a list")
    if samples_payload is not None and len(samples_payload) > MAX_SAMPLES:
        raise InspectLogError(
            "SAMPLE_LIMIT", f"inspect log exceeds {MAX_SAMPLES} samples"
        )
    if not samples_payload:
        raise InspectLogError(
            "AGGREGATE_ONLY",
            "log contains aggregate results without samples (summary-only "
            ".json or binary .eval); the platform does not fabricate samples "
            "from aggregates — export with --full",
        )
    samples = [
        _extract_sample(entry, index)
        for index, entry in enumerate(samples_payload)
    ]
    keys = [(sample["id"], sample["epoch"]) for sample in samples]
    if len(keys) != len(set(keys)):
        raise InspectLogError("DUPLICATE_SAMPLE", "duplicate sample id/epoch in EvalLog")
    header = {
        "version": payload.get("version"),
        "status": payload.get("status"),
        "created": payload["eval"].get("created"),
        "model": payload["eval"].get("model"),
        "eval": payload.get("eval"),
        "dataset": payload["eval"].get("dataset"),
        "results": payload.get("results"),
    }
    coverage = "partial" if unknown_fields else "complete"
    return {
        "schema": SUPPORTED_SCHEMA,
        "parser_version": PARSER_VERSION,
        "header": header,
        "samples": samples,
        "identity": identity,
        "unknown_fields": unknown_fields,
        "coverage": coverage,
    }


def _log_identity(payload: dict[str, Any]) -> dict[str, Any]:
    """Official source IDs remain stable when samples/scores change."""
    eval_info = payload.get("eval")
    if not isinstance(eval_info, dict):
        raise InspectLogError("UNKNOWN_SCHEMA", "eval must be an EvalSpec object")
    for key in ("eval_id", "run_id", "created", "model", "task"):
        if not isinstance(eval_info.get(key), str) or not eval_info[key]:
            raise InspectLogError("UNKNOWN_SCHEMA", f"eval.{key} is required")
    return {
        "eval_id": eval_info["eval_id"],
        "run_id": eval_info["run_id"],
    }
