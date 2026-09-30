"""Canonical, complete Trace archive bytes and receipt verification.

Archive content is independent of a particular apply/receipt: immutable identity
is the digest of the full canonical document. Plan/receipt publication and all DB
mutation belong to the owner-bound retention transaction, not this module.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from motte_contracts.identity import canonical_json_bytes, canonical_sha256

from .artifact_refs import collect_artifact_refs
from .trace_retention_models import (
    Sha256, StoredTraceEvent, TraceArchiveInvalid, TraceArchiveReceipt, TracePrefix,
)
from pydantic import TypeAdapter

TRACE_ARCHIVE_NAMESPACE = "trace-archives"
_DIGEST = TypeAdapter(Sha256)


def is_trace_archive_id(artifact_id: str) -> bool:
    """Reserve the entire namespace, including incomplete and unreferenced files."""
    parts = Path(artifact_id).parts
    return bool(parts) and (parts[0].casefold() if os.name == "nt" else parts[0]) == TRACE_ARCHIVE_NAMESPACE


def trace_archive_artifact_id(data: bytes) -> str:
    return f"{TRACE_ARCHIVE_NAMESPACE}/sha256/{hashlib.sha256(data).hexdigest()}.json"


def build_trace_archive(prefix: TracePrefix, events: list[StoredTraceEvent]) -> bytes:
    """Encode the complete ordered stored rows, refusing stale or unchecked input.

    ``events_sha256`` is the canonical SHA-256 of the full stored-row JSON list,
    including each untouched payload and UTC server timestamp. It is not a hash
    of a shortened/public Trace view. Hash references normalize to ``sha256:``;
    ArtifactStore's existing Artifact contract still exposes a bare hex digest.
    """
    try:
        prefix = TracePrefix.model_validate(prefix)
        if not isinstance(events, list):
            raise ValueError("archive events must be a list")
        rows = [StoredTraceEvent.model_validate(event) for event in events]
        if len(rows) != prefix.event_count:
            raise ValueError("archive event count does not match prefix")
        for seq, row in enumerate(rows, start=prefix.first_seq):
            if row.run_id != prefix.run_id or row.seq != seq or row.stored_at is None:
                raise ValueError("archive requires the complete known-time prefix in sequence order")
        encoded_rows = [row.model_dump(mode="json") for row in rows]
        if canonical_sha256(encoded_rows) != prefix.events_sha256:
            raise ValueError("archive event digest does not match prefix")
        refs: dict[str, str | None] = {}
        hashes: set[str] = set()
        collect_artifact_refs(encoded_rows, refs, hashes=hashes)
        refs = {key: _DIGEST.validate_python(value) if value is not None else None
                for key, value in sorted(refs.items())}
        normalized_hashes = sorted({_DIGEST.validate_python(value) for value in hashes})
        return canonical_json_bytes({
            "schema_version": 1, **prefix.model_dump(mode="json"), "events": encoded_rows,
            "artifact_refs": refs, "artifact_hashes": normalized_hashes,
        })
    except (ValueError, TypeError, OverflowError) as error:
        raise TraceArchiveInvalid("invalid Trace archive evidence: " + str(error)) from error


def _decode_trace_archive(data: bytes) -> dict[str, Any]:
    """Validate the exact schema and encoding, including duplicate/unknown fields."""
    try:
        if not isinstance(data, bytes):
            raise ValueError("archive content must be bytes")
        body = json.loads(data)
        if not isinstance(body, dict) or type(body.get("schema_version")) is not int:
            raise ValueError("archive schema_version must be integer 1")
        if body["schema_version"] != 1:
            raise ValueError("unsupported Trace archive schema")
        prefix = TracePrefix.model_validate({key: body[key] for key in TracePrefix.model_fields})
        rebuilt = build_trace_archive(prefix, body["events"])
        if rebuilt != data:
            raise ValueError("archive schema or bytes are not canonical and complete")
        return body
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        raise TraceArchiveInvalid("invalid Trace archive bytes: " + str(error)) from error


def verify_trace_archive(data: bytes, receipt: TraceArchiveReceipt) -> None:
    """Bind a pending or committed receipt to the canonical immutable evidence.

    The archive cannot independently prove plan_id or archive_id: those identify
    the DB transaction. Receipt publication must additionally revalidate its plan.
    """
    try:
        receipt = TraceArchiveReceipt.model_validate(receipt)
        body = _decode_trace_archive(data)
        if len(data) != receipt.bytes or "sha256:" + hashlib.sha256(data).hexdigest() != receipt.sha256:
            raise ValueError("archive byte length or digest does not match receipt")
        if receipt.artifact_id != trace_archive_artifact_id(data):
            raise ValueError("archive artifact identity does not match receipt")
        if build_trace_archive(receipt.prefix, body["events"]) != data:
            raise ValueError("archive prefix does not match receipt")
        if body["artifact_refs"] != receipt.artifact_refs or body["artifact_hashes"] != receipt.artifact_hashes:
            raise ValueError("archive nested evidence does not match receipt")
        if any(StoredTraceEvent.model_validate(row).stored_at >= receipt.cutoff
               for row in body["events"]):
            raise ValueError("archive contains an event at or after the retention cutoff")
    except (ValueError, TypeError, OverflowError) as error:
        raise TraceArchiveInvalid("Trace archive receipt verification failed: " + str(error)) from error
