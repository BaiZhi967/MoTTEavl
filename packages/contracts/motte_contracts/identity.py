"""Canonical content identities shared by contracts and import orchestration."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_sha256(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def dataset_fingerprint(record: dict[str, Any]) -> str:
    """Hash immutable dataset semantics while excluding its storage address."""
    payload = {
        key: value for key, value in record.items()
        if key not in {"name", "version", "dataset_fingerprint"}
    }
    return canonical_sha256(payload)
