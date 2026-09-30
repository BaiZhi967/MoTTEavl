"""Durable deletion intent around non-transactional filesystem mutations.

An intent is committed before unlink. Only an observed successful unlink gets a
completion timestamp. A crash between those steps remains explicitly uncertain;
replaying an absent file cannot retrospectively prove who deleted it.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


def audit_missing_artifact(tombstones: Any, entry: dict[str, Any]) -> str:
    """Preserve confirmed completion, or record observed absence without inventing it."""
    previous = next((
        row for row in tombstones.list(limit=1_000_000)
        if row.get("gc_run_id") == entry["gc_run_id"]
        and row.get("artifact_id") == entry["artifact_id"]
    ), None)
    if previous is not None and (
        previous.get("deletion_status") == "deleted"
        or ("deletion_status" not in previous and previous.get("deleted_at"))
    ):
        return "already_deleted"
    status = "absence_unconfirmed" if previous is not None else "missing_unobserved"
    audit = {**(previous or entry), "deletion_status": status, "observed_absent_at": _now()}
    audit.pop("deleted_at", None)
    tombstones.append([audit])
    return status


def delete_with_audit(
    tombstones: Any, entry: dict[str, Any], *, delete: Callable[[], None],
    exists: Callable[[], bool],
) -> dict[str, Any]:
    """Write intent, perform deletion, then separately persist the observed outcome.

    If intent persistence fails, no deletion occurs. If completion persistence
    fails or the process exits, the already committed intent remains. Exceptions
    deriving directly from BaseException intentionally leave that intent pending.
    """
    intent = {**entry, "deletion_status": "deleting", "deletion_started_at": _now()}
    intent.pop("deleted_at", None)
    tombstones.append([intent])
    try:
        delete()
        if exists():
            raise OSError("artifact still exists after deletion returned")
    except Exception as error:
        failed = {
            **intent, "deletion_status": "failed", "failure_observed_at": _now(),
            "error_type": type(error).__name__,
        }
        tombstones.append([failed])
        raise
    completed = {**intent, "deletion_status": "deleted", "deleted_at": _now()}
    tombstones.append([completed])
    return completed
