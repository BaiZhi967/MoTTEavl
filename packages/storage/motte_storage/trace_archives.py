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
        if receipt.reference_document is not None:
            from .trace_references import extract_reference_document
            document = extract_reference_document((row['payload'], receipt.prefix.run_id) for row in body['events'])
            if document != receipt.reference_document:
                raise ValueError('archive reference document disagrees with canonical payloads')
        if any(StoredTraceEvent.model_validate(row).stored_at >= receipt.cutoff
               for row in body["events"]):
            raise ValueError("archive contains an event at or after the retention cutoff")
    except (ValueError, TypeError, OverflowError) as error:
        raise TraceArchiveInvalid("Trace archive receipt verification failed: " + str(error)) from error


def _receipt_from_row(row) -> TraceArchiveReceipt:
    """Validate both the immutable payload and every independently indexed field."""
    archive_id, plan_id, run_id, first_seq, last_seq, payload = row
    receipt = TraceArchiveReceipt.model_validate(json.loads(payload) if isinstance(payload, str) else payload)
    if (receipt.committed_at is None or
            (archive_id, plan_id, run_id, first_seq, last_seq) != (
                receipt.archive_id, receipt.plan_id, receipt.prefix.run_id,
                receipt.prefix.first_seq, receipt.prefix.last_seq)
            or receipt.archive_id != 'trace-archive-' + receipt.sha256.removeprefix('sha256:')):
        raise TraceArchiveInvalid('stored Trace receipt identity disagrees')
    return receipt


_RECEIPT_COLUMNS = 'archive_id, plan_id, run_id, first_seq, last_seq, payload'


def _receipts_on_connection(connection) -> list[TraceArchiveReceipt]:
    return [_receipt_from_row(row) for row in connection.execute(
        'SELECT ' + _RECEIPT_COLUMNS + ' FROM trace_archive_receipts ORDER BY run_id, first_seq, archive_id'
    ).fetchall()]


class SQLiteTraceArchives:
    """Read existing receipt storage; construction and reads never create schema."""
    def __init__(self, path: str):
        self._path = str(path)

    def list(self) -> list[TraceArchiveReceipt]:
        import sqlite3
        from contextlib import closing
        from urllib.parse import quote
        uri = 'file:' + quote(str(Path(self._path).resolve()), safe='/') + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            return _receipts_on_connection(connection)

    def get(self, archive_id: str) -> TraceArchiveReceipt | None:
        return next((row for row in self.list() if row.archive_id == archive_id), None)

    def list_for_run(self, run_id: str) -> list[TraceArchiveReceipt]:
        return [row for row in self.list() if row.prefix.run_id == run_id]


class PgTraceArchives(SQLiteTraceArchives):
    def __init__(self, dsn: str):
        self._dsn = dsn

    def list(self) -> list[TraceArchiveReceipt]:
        from .postgres import _connect
        with _connect(self._dsn) as connection:
            connection.execute('SET TRANSACTION READ ONLY')
            return _receipts_on_connection(connection)


class MemoryTraceArchives(SQLiteTraceArchives):
    def __init__(self, lock):
        self._lock = lock
        self._rows: dict[str, TraceArchiveReceipt] = {}

    def list(self) -> list[TraceArchiveReceipt]:
        with self._lock:
            rows = [_receipt_from_row((key, row.plan_id, row.prefix.run_id,
                                       row.prefix.first_seq, row.prefix.last_seq, row.model_dump()))
                    for key, row in self._rows.items()]
            return sorted(rows, key=lambda row: (row.prefix.run_id, row.prefix.first_seq, row.archive_id))


def checked_trace_receipts(store) -> list[TraceArchiveReceipt]:
    """Require complete immutable reader coverage, including parentless receipts."""
    reader = getattr(getattr(store, 'trace_archives', None), 'list', None)
    if not callable(reader):
        raise AttributeError('store.trace_archives.list is required for archive reference scanning')
    observed = [TraceArchiveReceipt.model_validate(row) for row in reader()]
    if getattr(store, 'dsn', None):
        expected = PgTraceArchives(store.dsn).list()
    elif getattr(store.runs, '_path', None):
        expected = SQLiteTraceArchives(store.runs._path).list()
    else:
        repo = store.trace_archives
        with repo._lock:
            expected = [_receipt_from_row((key, row.plan_id, row.prefix.run_id, row.prefix.first_seq,
                                          row.prefix.last_seq, row.model_dump())) for key, row in repo._rows.items()]
        expected.sort(key=lambda row: (row.prefix.run_id, row.prefix.first_seq, row.archive_id))
    if observed != expected:
        raise TraceArchiveInvalid('incomplete or mismatched Trace receipt reader coverage')
    return observed


def trace_receipt_boundaries(store, receipts) -> dict[str, int]:
    """Prove contiguous archived prefixes end strictly before retained live rows."""
    boundaries, keep = {}, {}
    for receipt in receipts:
        prefix = receipt.prefix
        if prefix.first_seq != boundaries.get(prefix.run_id, 0) + 1:
            raise TraceArchiveInvalid('Trace receipt ranges overlap or leave a gap')
        boundaries[prefix.run_id] = prefix.last_seq
        keep[prefix.run_id] = max(keep.get(prefix.run_id, 0), prefix.keep_seq)
    for run_id, last_seq in boundaries.items():
        if store.runs.get(run_id) is None:
            raise TraceArchiveInvalid('Trace receipt has no owning Run')
        rows = store.events.stored_for_run(run_id)
        if (not rows or rows[0].seq <= last_seq or rows[-1].seq < keep[run_id]
                or any(row.seq <= last_seq for row in rows)):
            raise TraceArchiveInvalid('Trace receipt overlaps live rows or exceeds retained bounds')
    return boundaries


def receipt_reference_record(store, receipt, *, trace_protection=False):
    """Independent audit root; only Trace planning suppresses the own Run edge."""
    from .trace_references import resolve_reference_document
    document = receipt.reference_document
    if document is None:
        raise TraceArchiveInvalid('legacy receipt lacks archived reference document')
    runs, events = resolve_reference_document(document, store.scoring_passes.get)
    record = {
        'archive_id': receipt.archive_id, 'run_id': receipt.prefix.run_id,
        'artifact_id': receipt.artifact_id, 'sha256': receipt.sha256,
        'artifacts': [{'artifact_id': key, 'sha256': digest} for key, digest in receipt.artifact_refs.items()]
                     + [{'sha256': digest} for digest in receipt.artifact_hashes],
        'referenced_run_ids': sorted(runs),
        'event_refs': [{'kind': 'event', 'run_id': run_id, 'locator': str(seq)}
                       for run_id, seqs in sorted(events.items()) for seq in sorted(seqs)],
    }
    if not trace_protection:
        # Rollback keeps even an archived owning Pass. Trace protection already
        # resolved these tokens with their original own-edge suppression above.
        record['pass_refs'] = [{'source_pass_id': ref.pass_id} for ref in document.pass_references]
    return record


def verify_trace_archives(store, artifacts) -> None:
    """Verify every complete receipt/file/reference closure and retained boundary."""
    from .trace_references import resolve_reference_document
    receipts = checked_trace_receipts(store)
    trace_receipt_boundaries(store, receipts)
    for receipt in receipts:
        if receipt.reference_document is None:
            raise TraceArchiveInvalid('legacy receipt lacks archived reference document')
        data = artifacts.read_bytes(receipt.artifact_id)
        verify_trace_archive(data, receipt)
        body = _decode_trace_archive(data)
        if any(row['payload'].get('run_id') != receipt.prefix.run_id
               or type(row['payload'].get('seq')) is not int or row['payload']['seq'] != row['seq']
               for row in body['events']):
            raise TraceArchiveInvalid('archived event payload identity disagrees')
        resolve_reference_document(receipt.reference_document, store.scoring_passes.get)
