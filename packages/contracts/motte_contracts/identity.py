"""Canonical content identities shared by contracts and import orchestration."""
from __future__ import annotations

import hashlib
import json
from typing import Any

def _reject_non_finite(value: Any, path: str = "$") -> None:
    """NaN/Infinity 在进入规范 JSON / hash 前拒绝：它们不可跨语言稳定序列化。"""
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise ValueError(f"non-finite float at {path} is not canonical JSON")
    if isinstance(value, dict):
        for key, child in value.items():
            _reject_non_finite(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_non_finite(child, f"{path}[{index}]")


def canonical_json_bytes(value: Any) -> bytes:
    """Deterministic UTF-8 JSON bytes; rejects NaN/Infinity inputs."""
    _reject_non_finite(value)
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
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
