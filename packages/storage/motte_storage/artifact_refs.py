"""Shared, conservative artifact reference decoding for backup, GC and rollback.

Paths and content hashes have distinct meanings: a hash-only legacy reference
protects matching content, but must never be mistaken for a relative file path.
Repository read errors propagate so destructive consumers fail closed.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from .platform import platform_for

_ARTIFACT_STRING_KEYS = frozenset({
    "artifact_id", "artifact_ids", "artifacts", "artifact", "raw_ref", "raw_bundle_artifact",
})
_ALL_LIMIT = 1_000_000


class ArtifactReferenceConflict(ValueError):
    """One immutable path has incompatible content assertions in durable data."""

    def __init__(self, artifact_id: str, hashes: set[str]) -> None:
        self.artifact_id = artifact_id
        self.hashes = frozenset(hashes)
        super().__init__(f"conflicting artifact hashes for {artifact_id}")


def canonical_artifact_id(identifier: str, *, root: str | Path | None = None) -> str:
    """Normalize accepted relative aliases; invalid/out-of-root refs fail closed."""
    path = Path(identifier)
    if path.is_absolute() or path.drive or ".." in path.parts or path.as_posix() == ".":
        raise ValueError("artifact path escapes root or is not a file identity: " + identifier)
    if root is None:
        return path.as_posix()
    resolved_root = Path(root).resolve()
    try:
        return (resolved_root / path).resolve().relative_to(resolved_root).as_posix()
    except ValueError as error:
        raise ValueError("artifact path escapes root: " + identifier) from error


def resolve_artifact_refs(refs: dict[str, str | None], root: str | Path) -> dict[str, str | None]:
    """Resolve in-root symlink aliases as well as lexical aliases for deletion checks."""
    resolved: dict[str, str | None] = {}
    for identifier, digest in refs.items():
        collect_artifact_refs(
            {"artifact_id": canonical_artifact_id(identifier, root=root), "sha256": digest}, resolved,
        )
    return resolved


def collect_artifact_refs(
    value: Any, refs: dict[str, str | None], *, hashes: set[str] | None = None,
) -> None:
    """Collect nested artifact paths and embedded hashes, including legacy forms.

    Only artifact-shaped objects contribute ``id``/``sha256``: unrelated entity
    ids, configuration hashes and event/invocation locators are not file refs.
    """
    def record(identifier: Any, digest: Any = None) -> None:
        digest = digest if isinstance(digest, str) and digest else None
        if isinstance(identifier, str) and identifier:
            identifier = canonical_artifact_id(identifier)
        if isinstance(identifier, str) and identifier and digest is not None:
            previous = refs.get(identifier)
            if previous is not None and previous.removeprefix("sha256:") != digest.removeprefix("sha256:"):
                raise ArtifactReferenceConflict(
                    identifier, {previous.removeprefix("sha256:"), digest.removeprefix("sha256:")},
                )
        if digest is not None and hashes is not None:
            hashes.add(digest.removeprefix("sha256:"))
        if isinstance(identifier, str) and identifier:
            if identifier not in refs or refs[identifier] is None:
                refs[identifier] = digest

    def walk(item: Any, *, artifact_context: bool = False) -> None:
        if isinstance(item, dict):
            is_evidence = item.get("kind") == "artifact"
            is_artifact = artifact_context or is_evidence or (
                isinstance(item.get("id"), str) and "kind" in item and "uri" in item
            )
            digest = item.get("sha256")
            if is_artifact:
                record(item.get("id"), digest)
            if is_evidence:
                record(item.get("locator"), digest)
            if item.get("target_type") == "artifact":
                record(item.get("target_id"), item.get("artifact_sha256"))
            for key, child in item.items():
                if key in _ARTIFACT_STRING_KEYS and isinstance(child, str):
                    record(child, digest)
                elif "artifact" in key.lower() and isinstance(child, (list, dict)):
                    walk(child, artifact_context=True)
                else:
                    walk(child)
        elif isinstance(item, list):
            for child in item:
                if artifact_context and isinstance(child, str):
                    record(child)
                else:
                    walk(child, artifact_context=artifact_context)

    walk(value)


def _is_import_owned_record(record: dict[str, Any], run_id: str, import_ids: set[str]) -> bool:
    """Only importer-provenance children inherit a mapped Run's inactive status."""
    if not import_ids:
        return False
    manifest = record.get("manifest") or {}
    source = (manifest.get("import_source") or {}) if isinstance(manifest, dict) else {}
    if record.get("id") == run_id and isinstance(source, dict) and source.get("import_id") in import_ids:
        return True
    source = record.get("import_source") or {}
    if isinstance(source, dict) and source.get("import_id") in import_ids:
        return True
    details = record.get("details") or {}
    return (
        record.get("outcome") == "imported"
        or record.get("source") == "legacy-import"
        or record.get("type") == "legacy_scores_imported"
        or (isinstance(details, dict) and details.get("source") == "legacy-import"
            and details.get("imported") is True)
    )


def _iter_artifact_records_with_owners(
    store: Any, *, exclude_run_ids: Iterable[str] = (), exclude_import_ids: Iterable[str] = (),
) -> Iterator[tuple[dict[str, Any], str | None, str | None]]:
    """Yield evidence payload plus its owning Run/import, keeping independent refs separate."""
    excluded_runs = set(exclude_run_ids)
    excluded_imports = set(exclude_import_ids)
    runs_repo = getattr(store, "runs", None)
    if runs_repo is None or not hasattr(runs_repo, "list"):
        raise AttributeError("store.runs.list is required for artifact reference scanning")
    for run in runs_repo.list():
        run_id = run.get("id")
        if run_id not in excluded_runs:
            yield run, run_id, None
        for repo_name in (
            "case_runs", "attempts", "invocations", "scoring_passes", "events", "scores",
            "commands", "trials", "runtime_sessions", "scoring_jobs",
        ):
            repo = getattr(store, repo_name, None)
            if repo is None or not hasattr(repo, "list_for_run"):
                continue
            for record in repo.list_for_run(run_id):
                if run_id not in excluded_runs or not _is_import_owned_record(
                    record, run_id, excluded_imports,
                ):
                    yield record, run_id, None
                if repo_name == "scoring_passes":
                    score_sets = getattr(store, "score_sets", None)
                    if score_sets is not None and hasattr(score_sets, "list_for_pass"):
                        for score in score_sets.list_for_pass(record.get("id", "")):
                            if run_id not in excluded_runs or not _is_import_owned_record(
                                score, run_id, excluded_imports,
                            ):
                                yield score, run_id, None
        external_jobs = getattr(store, "external_jobs", None)
        if external_jobs is not None and hasattr(external_jobs, "jobs_for_run"):
            for job in external_jobs.jobs_for_run(run_id):
                yield job, run_id, None
                for record in external_jobs.list_records(job.get("job_id", "")):
                    yield record, run_id, None
        baselines = getattr(store, "baselines", None)
        if baselines is not None and hasattr(baselines, "get_for_run"):
            for baseline in baselines.get_for_run(run_id):
                yield baseline, None, None

    # Durable ScoringJobs are independent of RunStore. Calibration Invocations
    # intentionally have no fabricated Run; list_for_job is their owning edge.
    jobs = getattr(store, "scoring_jobs", None)
    if jobs is None and (getattr(store, "dsn", None) or getattr(runs_repo, "_path", None)):
        from .scoring_jobs import scoring_jobs_for

        jobs = scoring_jobs_for(store)
    if jobs is not None and hasattr(jobs, "list_by_status"):
        invocations = getattr(store, "invocations", None)
        for job in jobs.list_by_status():
            run_id = job.get("run_id")
            yield job, run_id, None
            if invocations is not None and hasattr(invocations, "list_for_job"):
                for invocation in invocations.list_for_job(job.get("job_id", "")):
                    yield invocation, run_id, None

    experiments = getattr(store, "experiments", None)
    if experiments is not None and hasattr(experiments, "list_specs"):
        for spec in experiments.list_specs():
            yield spec, None, None
            for cell in experiments.list_cells(spec["experiment_id"], spec["version"]):
                yield cell, None, None
    baseline_store = getattr(store, "baseline_store", None)
    if baseline_store is not None and hasattr(baseline_store, "list"):
        for baseline in baseline_store.list(limit=_ALL_LIMIT):
            yield baseline, None, None
    gate_store = getattr(store, "gate_store", None)
    if gate_store is not None and hasattr(gate_store, "list_results"):
        for gate in gate_store.list_results(limit=_ALL_LIMIT):
            yield gate, None, None
    ledger = platform_for(store).imports
    for batch in ledger.list_imports():
        import_id = batch.get("import_id", "")
        if import_id not in excluded_imports:
            yield batch, None, import_id
            for mapping in ledger.mappings_for(import_id):
                yield mapping, None, import_id


def iter_artifact_records(
    store: Any, *, exclude_run_ids: Iterable[str] = (), exclude_import_ids: Iterable[str] = (),
) -> Iterator[dict[str, Any]]:
    """Traverse complete evidence payloads without suppressing repository failures."""
    for record, _, _ in _iter_artifact_records_with_owners(
        store, exclude_run_ids=exclude_run_ids, exclude_import_ids=exclude_import_ids,
    ):
        yield record


def referenced_run_ids(
    store: Any, *, exclude_run_ids: Iterable[str] = (), exclude_import_ids: Iterable[str] = (),
) -> set[str]:
    """Collect report edges and repository ownership, including payloads without run_id."""
    found: set[str] = set()
    for record, run_id, _ in _iter_artifact_records_with_owners(
        store, exclude_run_ids=exclude_run_ids, exclude_import_ids=exclude_import_ids,
    ):
        if run_id is not None:
            found.add(run_id)
        collect_referenced_run_ids(record, found)
    return found


def _archived_deletions(store: Any, records) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Identify inactive own evidence with a positive, completed rollback audit.

    This only changes backup/retention requirements. Audit rows and inactive Run
    payloads remain untouched. Foreign references, independent pins and uncertain
    deletion intents still require their evidence to exist.
    """
    platform = platform_for(store)
    retired_imports = {
        batch["import_id"] for batch in platform.imports.list_imports()
        if batch.get("status") == "rolled_back"
    }
    retired_runs: dict[str, str] = {}
    for run in store.runs.list():
        retired = run.get("rolled_back_import") or {}
        source = (run.get("manifest") or {}).get("import_source") or {}
        import_id = retired.get("import_id")
        if import_id in retired_imports and source.get("import_id") == import_id:
            retired_runs[run["id"]] = import_id
    external_run_refs: set[str] = set()
    for record, run_id, import_id in records:
        own_retired_run = run_id in retired_runs and _is_import_owned_record(
            record, run_id, {retired_runs[run_id]},
        )
        if not own_retired_run and import_id not in retired_imports:
            if run_id is not None:
                external_run_refs.add(run_id)
            collect_referenced_run_ids(record, external_run_refs)
    retired_runs = {run_id: batch for run_id, batch in retired_runs.items()
                    if run_id not in external_run_refs}
    confirmed: dict[str, dict[str, str]] = {}
    for audit in platform.tombstones.list(limit=_ALL_LIMIT):
        import_id = audit.get("import_id")
        if import_id not in retired_imports:
            continue
        if audit.get("gc_run_id") != "import-rollback:" + import_id:
            continue
        if audit.get("deletion_status") != "deleted":
            continue
        identifier, digest = audit.get("artifact_id"), audit.get("sha256")
        if isinstance(identifier, str) and isinstance(digest, str) and digest:
            confirmed.setdefault(import_id, {})[canonical_artifact_id(identifier)] = digest.removeprefix("sha256:")
    return retired_runs, confirmed


def referenced_artifact_hashes(
    store: Any, *, exclude_run_ids: Iterable[str] = (), exclude_import_ids: Iterable[str] = (),
    hashes: set[str] | None = None,
) -> dict[str, str | None]:
    """Collect live refs; retain audit-only refs except confirmed own rollback deletions."""
    records = list(_iter_artifact_records_with_owners(
        store, exclude_run_ids=exclude_run_ids, exclude_import_ids=exclude_import_ids,
    ))
    retired_runs, confirmed = _archived_deletions(store, records)
    refs: dict[str, str | None] = {}
    for record, run_id, import_id in records:
        own_import = import_id
        retired_import = retired_runs.get(run_id)
        if retired_import is not None and _is_import_owned_record(record, run_id, {retired_import}):
            own_import = retired_import
        archived = confirmed.get(own_import, {})
        local_refs: dict[str, str | None] = {}
        local_hashes: set[str] = set()
        collect_artifact_refs(record, local_refs, hashes=local_hashes)
        live_refs = {
            identifier: digest for identifier, digest in local_refs.items()
            if identifier not in archived or (digest is not None
                and digest.removeprefix("sha256:") != archived[identifier])
        }
        for identifier, digest in live_refs.items():
            collect_artifact_refs({"artifact_id": identifier, "sha256": digest}, refs)
        if hashes is not None:
            hashes.update(local_hashes - set(archived.values()))
            hashes.update(digest.removeprefix("sha256:") for digest in live_refs.values() if digest)
    return refs


def collect_referenced_run_ids(value: Any, found: set[str]) -> None:
    """Find independent report/Run edges before excluding rollback-owned records."""
    if isinstance(value, dict):
        if value.get("target_type") == "run" and isinstance(value.get("target_id"), str):
            found.add(value["target_id"])
        for key, child in value.items():
            if (key == "run_id" or key.endswith("_run_id")) and isinstance(child, str):
                found.add(child)
            elif (key == "run_ids" or key.endswith("_run_ids")) and isinstance(child, list):
                found.update(item for item in child if isinstance(item, str))
            collect_referenced_run_ids(child, found)
    elif isinstance(value, list):
        for child in value:
            collect_referenced_run_ids(child, found)
