"""Deterministic audit records for atomic dataset/scenario publication."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from motte_contracts.identity import canonical_sha256

_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_RECEIPT_HASH = re.compile(r"[0-9a-f]{64}")
_PUBLICATION_ID = re.compile(r"publication-[0-9a-f]{64}")
_PUBLICATION_FIELDS = frozenset({
    "id", "dataset", "scenario", "dataset_fingerprint", "receipt", "receipt_sha256",
    "actor", "entrypoint", "published_at",
})
_FORBIDDEN_RECEIPT_FIELDS = frozenset({"artifact_paths", "body", "secret", "secrets"})


def canonical_publication_time(value: Any) -> str:
    """Normalize an aware ISO timestamp to a canonical UTC RFC3339 string."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("published_at must be a non-empty timezone-aware ISO timestamp")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as error:
        raise ValueError("published_at must be a timezone-aware ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("published_at must be a timezone-aware ISO timestamp")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _resource_ref(record: dict[str, Any], label: str) -> str:
    name = record.get("name")
    version = record.get("version")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{label} name must be a non-empty string")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"{label} version must be a non-empty string")
    return f"{name}@{version}"


def _forbidden_receipt_path(value: Any, path: str = "receipt") -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _FORBIDDEN_RECEIPT_FIELDS:
                return f"{path}.{key}"
            nested = _forbidden_receipt_path(item, f"{path}.{key}")
            if nested is not None:
                return nested
    elif isinstance(value, list):
        for index, item in enumerate(value):
            nested = _forbidden_receipt_path(item, f"{path}[{index}]")
            if nested is not None:
                return nested
    return None


def _portable_receipt(receipt: Any, fingerprint: str) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise ValueError("publication receipt must be an object")
    receipt_fingerprint = receipt.get("dataset_fingerprint")
    if receipt_fingerprint is not None and receipt_fingerprint != fingerprint:
        raise ValueError("publication receipt dataset_fingerprint does not match the dataset")
    portable = deepcopy(receipt)
    portable.pop("artifact_paths", None)
    if any(not isinstance(key, str) for key in portable):
        raise ValueError("publication receipt keys must be strings")
    forbidden_path = _forbidden_receipt_path(portable)
    if forbidden_path is not None:
        raise ValueError(f"publication receipt contains non-portable field: {forbidden_path}")
    try:
        json.dumps(
            portable,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("publication receipt must contain canonical JSON values") from error
    return portable


def _receipt_sha256(receipt: Any, fingerprint: str) -> str:
    portable = _portable_receipt(receipt, fingerprint)
    return canonical_sha256(portable).removeprefix("sha256:")


def _publication_id(
    *,
    dataset_ref: str,
    scenario_ref: str,
    dataset_fingerprint: str,
    receipt_sha256: str,
    actor: str,
    entrypoint: str,
) -> str:
    identity = canonical_sha256({
        "dataset": dataset_ref,
        "scenario": scenario_ref,
        "dataset_fingerprint": dataset_fingerprint,
        "receipt_sha256": f"sha256:{receipt_sha256}",
        "actor": actor,
        "entrypoint": entrypoint,
    })
    return f"publication-{identity.removeprefix('sha256:')}"


def _audit_record(
    dataset: dict[str, Any],
    scenario: dict[str, Any],
    *,
    receipt: dict[str, Any],
    receipt_sha256: str,
    actor: str,
    entrypoint: str,
    published_at: str,
) -> dict[str, Any]:
    fingerprint = dataset.get("dataset_fingerprint")
    if not isinstance(fingerprint, str) or not _HASH.fullmatch(fingerprint):
        raise ValueError("dataset_fingerprint is required for audited publication")
    if not isinstance(receipt_sha256, str) or not _RECEIPT_HASH.fullmatch(receipt_sha256):
        raise ValueError("receipt_sha256 must be 64 lowercase hexadecimal characters")
    portable_receipt = _portable_receipt(receipt, fingerprint)
    if "artifact_paths" in receipt:
        raise ValueError("publication audit receipt must not contain local artifact_paths")
    if _receipt_sha256(portable_receipt, fingerprint) != receipt_sha256:
        raise ValueError("publication audit receipt_sha256 does not match receipt")
    normalized_text: dict[str, str] = {}
    for field, value in (("actor", actor), ("entrypoint", entrypoint)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        normalized_text[field] = value.strip()
    dataset_ref = _resource_ref(dataset, "dataset")
    scenario_ref = _resource_ref(scenario, "scenario")
    timestamp = canonical_publication_time(published_at)
    publication_id = _publication_id(
        dataset_ref=dataset_ref,
        scenario_ref=scenario_ref,
        dataset_fingerprint=fingerprint,
        receipt_sha256=receipt_sha256,
        actor=normalized_text["actor"],
        entrypoint=normalized_text["entrypoint"],
    )
    return {
        "id": publication_id,
        "dataset": dataset_ref,
        "scenario": scenario_ref,
        "dataset_fingerprint": fingerprint,
        "receipt": portable_receipt,
        "receipt_sha256": receipt_sha256,
        "actor": normalized_text["actor"],
        "entrypoint": normalized_text["entrypoint"],
        "published_at": timestamp,
    }


def validate_publication_audit(record: Any) -> dict[str, Any]:
    """Validate that an audit is canonical and its ID matches its immutable fields."""
    if not isinstance(record, dict) or set(record) != _PUBLICATION_FIELDS:
        raise ValueError("publication audit must contain exactly the canonical fields")
    for field in ("dataset", "scenario"):
        value = record[field]
        if not isinstance(value, str) or value != value.strip():
            raise ValueError(f"publication audit {field} must be canonical")
        name, separator, version = value.rpartition("@")
        if not separator or not name or not version:
            raise ValueError(f"publication audit {field} must be a name@version reference")
    fingerprint = record["dataset_fingerprint"]
    if not isinstance(fingerprint, str) or not _HASH.fullmatch(fingerprint):
        raise ValueError("publication audit dataset_fingerprint must be a canonical sha256")
    receipt_sha256 = record["receipt_sha256"]
    if not isinstance(receipt_sha256, str) or not _RECEIPT_HASH.fullmatch(receipt_sha256):
        raise ValueError("publication audit receipt_sha256 must be lowercase hexadecimal")
    receipt = record["receipt"]
    if isinstance(receipt, dict) and "artifact_paths" in receipt:
        raise ValueError("publication audit receipt must not contain local artifact_paths")
    if _receipt_sha256(receipt, fingerprint) != receipt_sha256:
        raise ValueError("publication audit receipt_sha256 does not match receipt")
    for field in ("actor", "entrypoint"):
        value = record[field]
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"publication audit {field} must be canonical")
    timestamp = canonical_publication_time(record["published_at"])
    if record["published_at"] != timestamp:
        raise ValueError("publication audit published_at must be canonical UTC RFC3339")
    expected_id = _publication_id(
        dataset_ref=record["dataset"],
        scenario_ref=record["scenario"],
        dataset_fingerprint=fingerprint,
        receipt_sha256=receipt_sha256,
        actor=record["actor"],
        entrypoint=record["entrypoint"],
    )
    publication_id = record["id"]
    if not isinstance(publication_id, str) or not _PUBLICATION_ID.fullmatch(publication_id):
        raise ValueError("publication audit id must be a canonical publication identity")
    if publication_id != expected_id:
        raise ValueError("publication audit id does not match its immutable fields")
    return deepcopy(record)


def publication_audit(
    dataset: dict[str, Any],
    scenario: dict[str, Any],
    receipt: dict[str, Any],
    *,
    actor: str,
    entrypoint: str,
    published_at: str,
) -> dict[str, Any]:
    """Build an idempotent audit event whose identity covers portable receipt evidence."""
    fingerprint = dataset.get("dataset_fingerprint")
    if not isinstance(fingerprint, str) or not _HASH.fullmatch(fingerprint):
        raise ValueError("dataset_fingerprint is required for audited publication")
    portable_receipt = _portable_receipt(receipt, fingerprint)
    receipt_sha256 = _receipt_sha256(portable_receipt, fingerprint)
    return _audit_record(
        dataset,
        scenario,
        receipt=portable_receipt,
        receipt_sha256=receipt_sha256,
        actor=actor,
        entrypoint=entrypoint,
        published_at=published_at,
    )


def retarget_publication_audit(
    publication: dict[str, Any],
    dataset: dict[str, Any],
    scenario: dict[str, Any],
) -> dict[str, Any]:
    """Retarget a valid audit to a newly allocated immutable resource version."""
    validated = validate_publication_audit(publication)
    return _audit_record(
        dataset,
        scenario,
        receipt=validated["receipt"],
        receipt_sha256=validated["receipt_sha256"],
        actor=validated["actor"],
        entrypoint=validated["entrypoint"],
        published_at=validated["published_at"],
    )


__all__ = [
    "canonical_publication_time",
    "publication_audit",
    "retarget_publication_audit",
    "validate_publication_audit",
]
