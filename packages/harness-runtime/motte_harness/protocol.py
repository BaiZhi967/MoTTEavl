"""Harness 输出协议：JSONL 解析带 parser version（结果溯源）。

M4-T05 起 PARSER 族共享标准事件 envelope 语义（与 motte_agent/protocol.py
的 bridge 协议对齐）：run/case/session/operation 身份、source_id/source_seq
（原生事件自身序号）、platform_seq（平台持久化 seq，入 trace 时填）、type、
parser_version、coverage（完整度：未知/截断/缺失事件时降级，不冒充完整）。
"""
import json

PARSER_VERSION = "jsonl-v1"

# 归一化事件的必需字段（payload 之外的身份与溯源骨架）
EVENT_ENVELOPE_FIELDS = (
    "run_id", "case_id", "session_id", "operation_id",
    "source_id", "source_seq", "type", "parser_version",
)
OPTIONAL_ENVELOPE_FIELDS = ("platform_seq", "payload_ref", "coverage", "raw_ref")

# coverage 取值：complete=全部已知事件且未截断；partial=未知事件/截断/
# 缺终态——评分与展示层必须把 partial 当证据不完整处理（M4-G08）。
COVERAGE_COMPLETE = "complete"
COVERAGE_PARTIAL = "partial"


def parse_jsonl(text):
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            out.append({"error": "malformed_line", "raw": line, "parser": PARSER_VERSION})
    return out
