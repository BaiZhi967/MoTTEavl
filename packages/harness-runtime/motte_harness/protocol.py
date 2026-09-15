"""Harness 输出协议：JSONL 解析带 parser version（结果溯源）。"""
import json

PARSER_VERSION = "jsonl-v1"


def parse_jsonl(text):
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            out.append({"error": "malformed_line", "raw": line, "parser": PARSER_VERSION})
    return out
