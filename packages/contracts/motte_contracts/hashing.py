"""Canonical serialization 与 hash 规则（M6 协议 §8）。

所有 M6 身份（cell_id / snapshot_id / policy hash / evaluation_input_hash /
result_semantics_hash / conclusion_hash）共用这一份实现；实现细节变更必须
同步协议文档并升版本。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """canonical JSON：sort_keys、紧凑分隔符、非 ASCII 原样、UTF-8。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_hash(obj: Any) -> str:
    """canonical JSON 的 sha256 十六进制，前缀 ``sha256:``。"""
    digest = hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
    return "sha256:" + digest


def content_hash_with_id(payload: dict[str, Any], id_field: str) -> str:
    """计算带自指 id 字段对象的 content hash：id 字段排除在哈希外。"""
    stripped = {key: value for key, value in payload.items() if key != id_field}
    return canonical_hash(stripped)
