"""Explicit Trace retention: read-only plans and owner-bound durable apply."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from motte_contracts.evaluation import EvidenceRef
from motte_contracts.identity import canonical_sha256

from . import trace_retention_models as models
from .artifact_refs import collect_artifact_refs, iter_trace_reference_records
from .maintenance import _store_identity

_TERMINAL = frozenset({"completed", "failed", "cancelled", "unsupported", "profile_stale"})
_PASS_KEYS = frozenset({"scoring_pass_id", "source_pass_id", "previous_pass_id",
                        "current_scoring_pass_id"})


def _identity(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"reference {name} must be a nonempty string")
    return value


def _protection(run_ids: set[str], event_seqs: dict[str, set[int]]) -> models.TraceProtection:
    body = {"run_ids": sorted(run_ids),
            "event_seqs": {key: sorted(value) for key, value in sorted(event_seqs.items())}}
    return models.TraceProtection(**body, sha256=canonical_sha256(body))


def collect_trace_protection(store: Any) -> models.TraceProtection:
    """Retain independent sources and explicit event evidence, excluding owning edges."""
    runs: set[str] = set()
    events: dict[str, set[int]] = {}
    pass_owners: dict[str, str] = {}
    artifact_refs: dict[str, str | None] = {}

    def pass_run(pass_id: Any) -> str:
        pass_id = _identity(pass_id, "scoring_pass_id")
        if pass_id not in pass_owners:
            record = store.scoring_passes.get(pass_id)
            if not isinstance(record, dict) or record.get("id") != pass_id:
                raise ValueError("unresolved scoring Pass reference: " + pass_id)
            pass_owners[pass_id] = _identity(record.get("run_id"), "Pass run_id")
        return pass_owners[pass_id]

    def walk(value: Any, owner: str | None, *, top: bool = False, ownership: bool = False) -> None:
        if isinstance(value, (list, tuple)):
            for child in value:
                walk(child, owner)
            return
        if not isinstance(value, dict):
            return
        kind = value.get("kind")
        evidence = (kind == "event" or ("locator" in value
                    and (kind in {"artifact", "invocation"} or "run_id" in value)))
        if evidence:
            ref = EvidenceRef.model_validate(value)
            if ref.kind == "event":
                events.setdefault(ref.run_id, set()).add(int(ref.locator))
            else:
                runs.add(ref.run_id)
            return
        if value.get("scoring_pass_id") is not None and value.get("run_id") is not None:
            if pass_run(value["scoring_pass_id"]) != _identity(value["run_id"], "run_id"):
                raise ValueError("coupled Run/Pass reference identity disagrees")
        target_type = value.get("target_type")
        if target_type == "run":
            runs.add(_identity(value.get("target_id"), "target_id"))
        elif target_type == "scoring_pass":
            runs.add(pass_run(value.get("target_id")))
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("reference record requires string keys")
            if (key == "run_id" or key.endswith("_run_id")) and child is not None:
                target = _identity(child, key)
                if not (key == "run_id" and (top or ownership) and target == owner):
                    runs.add(target)
            elif (key == "run_ids" or key.endswith("_run_ids")) and child is not None:
                if not isinstance(child, list):
                    raise ValueError("reference Run ids must be a list")
                runs.update(_identity(item, key) for item in child)
            elif key in _PASS_KEYS and child is not None:
                target = pass_run(child)
                if not (top and key in {"scoring_pass_id", "current_scoring_pass_id"}
                        and target == owner):
                    runs.add(target)
            else:
                walk(child, owner, ownership=top and key == "owner")

    # Status/provenance protection is independent of timestamp eligibility.
    for run in store.runs.list():
        if not isinstance(run, dict):
            raise ValueError("malformed Run record")
        run_id = _identity(run.get("id"), "Run id")
        manifest = run.get("manifest")
        if manifest is not None and not isinstance(manifest, dict):
            raise ValueError("malformed Run manifest")
        if run.get("status") not in _TERMINAL or (
            isinstance(manifest, dict) and manifest.get("import_source") is not None
        ):
            runs.add(run_id)
    for record, owner in iter_trace_reference_records(store):
        collect_artifact_refs(record, artifact_refs)
        walk(record, owner, top=True)
    return _protection(runs, events)


def _plan(store: Any, config: models.TraceRetentionConfig, cutoff: datetime | None,
          protection: models.TraceProtection, prefixes: list[models.TracePrefix]
          ) -> models.TraceRetentionPlan:
    body = {
        "schema_version": 1, "store_identity_sha256": canonical_sha256(_store_identity(store)),
        "config": config.model_dump(mode="json"),
        "cutoff": None if cutoff is None else cutoff.isoformat().replace("+00:00", "Z"),
        "protection_sha256": protection.sha256,
        "prefixes": [prefix.model_dump(mode="json") for prefix in sorted(prefixes, key=lambda p: p.run_id)],
    }
    return models.TraceRetentionPlan(**body, plan_id=canonical_sha256(body))


def plan_trace_retention(store: Any, *, config: models.TraceRetentionConfig) -> models.TraceRetentionPlan:
    """Use repository server time once. A disabled plan has no policy cutoff or candidates."""
    config = models.TraceRetentionConfig.model_validate(config)
    if not config.enabled:
        return _plan(store, config, None, _protection(set(), {}), [])
    try:
        cutoff = models._utc_datetime(models.utc_now()) - timedelta(days=config.retention_days)
    except OverflowError as error:
        raise ValueError("retention cutoff is outside the supported datetime range") from error
    return _plan_at_cutoff(store, config=config, cutoff=cutoff)


def _plan_at_cutoff(store: Any, *, config: models.TraceRetentionConfig,
                    cutoff: datetime) -> models.TraceRetentionPlan:
    """Reconstruct at the exact saved cutoff; later apply must hold its maintenance owner."""
    config = models.TraceRetentionConfig.model_validate(config)
    if not config.enabled:
        raise models.TraceRetentionDisabled("Trace retention is disabled")
    cutoff = models._utc_datetime(cutoff)
    receipts = getattr(store, 'trace_archives', None)
    if receipts is None or not callable(getattr(receipts, 'list', None)):
        raise AttributeError('store.trace_archives.list is required for Trace protection')
    if receipts.list():
        raise models.TraceRetentionPlanChanged(
            'subsequent retention requires verified archived reference closure (Task 5)')
    protection = collect_trace_protection(store)
    prefixes = []
    seen = set()
    for run in store.runs.list():
        run_id = _identity(run.get("id"), "Run id")
        if run_id in seen:
            raise ValueError("duplicate Run in retention source")
        seen.add(run_id)
        if run_id in protection.run_ids or run.get("status") not in _TERMINAL:
            continue
        rows = [models.StoredTraceEvent.model_validate(row) for row in store.events.stored_for_run(run_id)]
        previous = 0
        for row in rows:
            if (row.run_id != run_id or row.seq <= previous
                    or row.payload.get("run_id") != run_id
                    or type(row.payload.get("seq")) is not int or row.payload["seq"] != row.seq):
                raise ValueError("Trace row identity or sequence order is corrupt")
            previous = row.seq
        if len(rows) < 2:
            continue
        selected = []
        expected = 1
        for row in rows[:-1]:
            if (row.seq != expected or row.stored_at is None or row.stored_at >= cutoff
                    or row.seq in protection.event_seqs.get(run_id, ())):
                break
            selected.append(row)
            expected += 1
        if selected:
            prefixes.append(models.TracePrefix(
                run_id=run_id, run_revision=run["revision"], status=run["status"],
                first_seq=selected[0].seq, last_seq=selected[-1].seq,
                keep_seq=rows[-1].seq, event_count=len(selected),
                events_sha256=canonical_sha256([row.model_dump(mode="json") for row in selected]),
            ))
    return _plan(store, config, cutoff, protection, prefixes)


def _validate_apply_plan(store: Any, plan: models.TraceRetentionPlan,
                         config: models.TraceRetentionConfig) -> models.TraceRetentionPlan:
    config = models.TraceRetentionConfig.model_validate(config)
    if not config.enabled:
        raise models.TraceRetentionDisabled('Trace retention is disabled')
    plan = models.TraceRetentionPlan.model_validate(plan)
    if plan.config != config or plan.store_identity_sha256 != canonical_sha256(_store_identity(store)):
        raise models.TraceRetentionPlanChanged('retention config or store changed')
    if plan.cutoff is None or plan.cutoff > models.utc_now() - timedelta(days=config.retention_days):
        raise models.TraceRetentionPlanChanged('retention cutoff is newer than the authorized policy')
    return plan


def _replay_receipts(store, artifacts, plan, receipts):
    """Exact, fully verified replay, before comparing now-trimmed candidates."""
    from .trace_archives import verify_trace_archive
    matching = [receipt for receipt in receipts if receipt.plan_id == plan.plan_id]
    if not matching:
        return None
    if ([row.prefix for row in matching] != plan.prefixes or
            any(row.cutoff != plan.cutoff for row in matching)):
        raise models.TraceRetentionPlanChanged('partial or mismatched committed receipt set')
    for receipt in matching:
        verify_trace_archive(artifacts.read_bytes(receipt.artifact_id), receipt)
        rows = store.events.stored_for_run(receipt.prefix.run_id)
        if (not rows or max(row.seq for row in rows) < receipt.prefix.keep_seq
                or any(row.seq <= receipt.prefix.last_seq for row in rows)):
            raise models.TraceRetentionPlanChanged('committed prefix disagrees with retained Trace')
    return models.TraceRetentionResult(plan_id=plan.plan_id, trimmed_events=0, receipts=matching)


def apply_trace_retention(store: Any, artifacts_root: str | Path, plan: models.TraceRetentionPlan, *,
                          config: models.TraceRetentionConfig, confirm: bool = False
                          ) -> models.TraceRetentionResult:
    """Explicit durable archive-before-trim. No public callback or SQL capability."""
    import hashlib
    import json
    from . import maintenance
    from .artifacts import ArtifactStore
    from .operation_locks import trace_archive_write_capability
    from .trace_archives import build_trace_archive, verify_trace_archive

    if confirm is not True:
        raise ValueError('Trace retention apply requires confirm=True')
    plan = _validate_apply_plan(store, plan, config)
    lease = maintenance.begin_maintenance(store, reason='trace_retention', artifacts_root=artifacts_root)
    owner = lease['owner']
    try:
        maintenance._require_trace_retention_owner(store, owner)
        with trace_archive_write_capability(store, artifacts_root=artifacts_root, maintenance_owner=owner):
            artifacts = ArtifactStore(artifacts_root)
            existing = store.trace_archives.list()
            replay = _replay_receipts(store, artifacts, plan, existing)
            if replay is not None:
                return replay
            if _plan_at_cutoff(store, config=config, cutoff=plan.cutoff) != plan:
                raise models.TraceRetentionPlanChanged('retention plan inputs changed')
            receipts = []
            for prefix in plan.prefixes:
                rows = [row for row in store.events.stored_for_run(prefix.run_id)
                        if prefix.first_seq <= row.seq <= prefix.last_seq]
                data = build_trace_archive(prefix, rows)
                artifact = artifacts.put_trace_archive(data, maintenance_owner=owner)
                body = json.loads(data)
                receipt = models.TraceArchiveReceipt(
                    archive_id='trace-archive-' + hashlib.sha256(data).hexdigest(),
                    plan_id=plan.plan_id, prefix=prefix, artifact_id=artifact.id,
                    sha256=artifact.sha256, bytes=len(data), cutoff=plan.cutoff,
                    artifact_refs=body['artifact_refs'], artifact_hashes=body['artifact_hashes'])
                verify_trace_archive(artifacts.read_bytes(receipt.artifact_id), receipt)
                receipts.append(receipt)
            return maintenance.commit_trace_retention(store, owner=owner, plan=plan, receipts=receipts)
    finally:
        if owner in maintenance._ACTIVE_MAINTENANCE:
            maintenance.end_maintenance(store, owner=owner, reason='trace_retention')
