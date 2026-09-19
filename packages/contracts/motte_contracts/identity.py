"""Canonical content identities shared by contracts and import orchestration."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def _to_canonical(value: Any, path: str = "$") -> Any:
    """递归转规范 JSON 值；Contract 模型实例按 model_dump(mode="json") 展开。

    NaN/Infinity 在此拒绝：它们不可跨语言稳定序列化。
    """
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise ValueError(f"non-finite float at {path} is not canonical JSON")
    if isinstance(value, dict):
        return {
            str(key): _to_canonical(child, f"{path}.{key}") for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_to_canonical(child, f"{path}[{index}]") for index, child in enumerate(value)]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Deterministic UTF-8 JSON bytes; rejects NaN/Infinity inputs."""
    return json.dumps(
        _to_canonical(value), allow_nan=False, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def dataset_fingerprint(record: dict[str, Any]) -> str:
    """Hash immutable dataset semantics while excluding its storage address."""
    payload = {
        key: value for key, value in record.items()
        if key not in {"name", "version", "dataset_fingerprint"}
    }
    return canonical_sha256(payload)
