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
                if key == "evidence_allowlist" and isinstance(child, list):
                    for token in child:
                        if isinstance(token, str) and token.startswith("artifact:"):
                            record(token.removeprefix("artifact:"))
                elif key in _ARTIFACT_STRING_KEYS and isinstance(child, str):
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


# Closed schema inventory: only read-only key scans, never caller-supplied SQL.
_TRACE_KEYS = {
    "trace_archive_receipts": ("archive_id",),
    "runs": ("id",), "case_runs": ("run_id", "case_id"),
    "case_attempts": ("run_id", "id"), "agent_invocations": ("run_id", "id"),
    "scoring_passes": ("run_id", "id"), "trace_events": ("run_id", "seq"),
    "scores": ("run_id", "case_id"), "run_commands": ("run_id", "id"),
    "trials": ("run_id", "trial_id"), "runtime_sessions": ("run_id", "session_id"),
    "scoring_jobs": ("run_id", "job_id"), "external_jobs": ("run_id", "job_id"),
    "external_job_records": ("job_id", "source_record_key", "parser_version"),
    "baseline_snapshots": ("run_id", "id"),
    "score_sets": ("scoring_pass_id", "case_id", "trial_id", "metric_id", "evaluator_id", "evaluator_version"),
    "experiment_specs": ("experiment_id", "version"),
    "experiment_cells": ("experiment_id", "experiment_version", "cell_id"),
    "benchmark_datasets": ("benchmark_id", "dataset_revision"),
    "m6_baselines": ("baseline_id",), "gate_results": ("gate_result_id",),
    "gate_policies": ("policy_id", "version"), "statistical_reports": ("report_id",),
    "motte_imports": ("import_id",), "motte_import_mappings": ("import_id", "mapping_key"),
    "judge_calibration_versions": ("calibration_id", "version"),
    "judge_calibration_reviews": ("review_id",),
    "judge_calibration_executions": ("execution_id",),
    "judge_calibration_reports": ("report_id",),
    "judge_calibration_qualifications": ("qualification_id",),
}
_TRACE_SOURCES = {"attempts": "case_attempts", "invocations": "agent_invocations",
                  "events": "trace_events", "commands": "run_commands",
                  "baselines": "baseline_snapshots"}


class _TraceCoverage:
    """Prove the ownership traversal omitted no stored row, including orphan edges."""
    def __init__(self):
        self.seen = {table: set() for table in _TRACE_KEYS}

    def observe(self, source, record, owner=None, parent=None):
        table = _TRACE_SOURCES.get(source, source)
        if not isinstance(record, dict):
            raise ValueError("malformed Trace reference record")
        if source == "calibrations":
            from .calibrations import _indices, _KEYS, _model
            kind = ("qualifications" if "qualification" in record else
                    "reports" if "report_id" in record else
                    "executions" if "execution_id" in record else
                    "reviews" if "review_id" in record else "versions")
            self.seen["judge_calibration_" + kind].add(_indices(kind, _model(kind, record))[:_KEYS[kind]])
            return
        fields = dict(record)
        if owner is not None:
            if fields.get("run_id", owner) != owner:
                raise ValueError("Trace reference ownership identity disagrees")
            fields["run_id"] = owner
        if table == "score_sets":
            if fields.get("scoring_pass_id", parent) != parent:
                raise ValueError("ScoreSet ownership identity disagrees")
            fields["scoring_pass_id"] = parent
            for key in ("trial_id", "metric_id", "evaluator_id", "evaluator_version"):
                fields[key] = fields.get(key) or ""
        self.seen[table].add(tuple(fields.get(key) for key in _TRACE_KEYS[table]))

    def verify(self, store):
        from contextlib import closing
        dsn = getattr(store, "dsn", None)
        path = getattr(store.runs, "_path", None)
        if dsn or path:
            if dsn:
                from .postgres import _connect
                context = _connect(dsn)
            else:
                import sqlite3
                from urllib.parse import quote
                uri = "file:" + quote(str(Path(path).resolve()), safe="/") + "?mode=ro"
                context = closing(sqlite3.connect(uri, uri=True))
            with context as connection:
                if dsn:
                    connection.execute("SET TRANSACTION READ ONLY")
                expected = {
                    table: set(connection.execute(
                        "SELECT " + ", ".join(keys) + " FROM " + table).fetchall())
                    for table, keys in _TRACE_KEYS.items()
                }
        else:
            expected = self._memory_keys(store)
        for table in _TRACE_KEYS:
            if expected[table] != self.seen[table]:
                raise ValueError("incomplete Trace reference coverage or orphan ownership: " + table)
        # Even a reserved calibration Pass cannot explain independently stored
        # scores until that actual Pass exists.
        passes = {key[1] for key in self.seen["scoring_passes"]}
        if any(key[0] not in passes for key in self.seen["score_sets"]):
            raise ValueError("orphan ScoreSet ownership")

    @staticmethod
    def _memory_keys(store):
        ledger = _read_only_import_ledger(store)
        actual = _TraceCoverage()
        with store.runs._lock, ledger._lock:
            groups = {
                "runs": store.runs._runs.values(),
                "case_runs": [value[1] for value in store.case_runs._rows.values()],
                "case_attempts": store.attempts._rows.values(),
                "agent_invocations": store.invocations._rows.values(),
                "scoring_passes": store.scoring_passes._passes.values(),
                "run_commands": store.commands.transactions.original._rows.values(),
                "trials": store.trials._rows.values(),
                "runtime_sessions": store.runtime_sessions.transactions.sessions.values(),
                "scoring_jobs": store.scoring_jobs._rows.values(),
                "external_jobs": store.external_jobs._jobs.values(),
                "external_job_records": store.external_jobs._records.values(),
                "baselines": store.baselines._snapshots.values(),
                "benchmark_datasets": store.benchmark_datasets._records.values(),
                "experiment_specs": store.experiments._specs.values(),
                "experiment_cells": store.experiments._cells.values(),
                "m6_baselines": store.baseline_store._snapshots.values(),
                "gate_results": store.gate_store._results.values(),
                "gate_policies": store.gate_store._policies.values(),
                "motte_imports": ledger._imports.values(),
                "motte_import_mappings": ledger._mappings.values(),
            }
            for table, records in groups.items():
                for record in records:
                    actual.observe(table, record)
            for owner, records in store.events._events.items():
                for record in records:
                    actual.observe("trace_events", record, owner)
            for owner, records in store.scores._scores.items():
                for record in records:
                    actual.observe("scores", record, owner)
            for parent, records in store.score_sets._sets.items():
                for record in records:
                    actual.observe("score_sets", record, parent=parent)
            actual.seen["trace_archive_receipts"] = {(key,) for key in store.trace_archives._rows}
            actual.seen["statistical_reports"] = {(key,) for key in store.statistical_reports._rows}
            for kind, records in store.calibrations._rows.items():
                actual.seen["judge_calibration_" + kind] = set(records)
        return actual.seen


def _iter_artifact_records_with_owners(
    store: Any, *, exclude_run_ids: Iterable[str] = (), exclude_import_ids: Iterable[str] = (),
    read_only: bool = False, coverage: _TraceCoverage | None = None,
) -> Iterator[tuple[dict[str, Any], str | None, str | None]]:
    """Yield evidence payload plus its owning Run/import, keeping independent refs separate."""
    def observe(source, record, owner=None, parent=None):
        if coverage is not None:
            coverage.observe(source, record, owner, parent)

    excluded_runs = set(exclude_run_ids)
    excluded_imports = set(exclude_import_ids)
    runs_repo = getattr(store, "runs", None)
    if runs_repo is None or not hasattr(runs_repo, "list"):
        raise AttributeError("store.runs.list is required for artifact reference scanning")
    for run in runs_repo.list():
        run_id = run.get("id")
        observe("runs", run)
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
                observe(repo_name, record, run_id)
                if run_id not in excluded_runs or not _is_import_owned_record(
                    record, run_id, excluded_imports,
                ):
                    yield record, run_id, None
                if repo_name == "scoring_passes":
                    score_sets = getattr(store, "score_sets", None)
                    if score_sets is not None and hasattr(score_sets, "list_for_pass"):
                        for score in score_sets.list_for_pass(record.get("id", "")):
                            observe("score_sets", score, parent=record.get("id"))
                            if run_id not in excluded_runs or not _is_import_owned_record(
                                score, run_id, excluded_imports,
                            ):
                                yield score, run_id, None
        external_jobs = getattr(store, "external_jobs", None)
        if external_jobs is not None and hasattr(external_jobs, "jobs_for_run"):
            for job in external_jobs.jobs_for_run(run_id):
                observe("external_jobs", job, run_id)
                yield job, run_id, None
                for record in external_jobs.list_records(job.get("job_id", "")):
                    observe("external_job_records", record)
                    yield record, run_id, None
        baselines = getattr(store, "baselines", None)
        if baselines is not None and hasattr(baselines, "get_for_run"):
            for baseline in baselines.get_for_run(run_id):
                observe("baselines", baseline)
                yield baseline, None, None

    # Durable ScoringJobs are independent of RunStore. Calibration Invocations
    # intentionally have no fabricated Run; list_for_job is their owning edge.
    jobs = getattr(store, "scoring_jobs", None)
    if jobs is None and read_only:
        raise AttributeError("existing ScoringJobs reader is required for read-only scanning")
    if jobs is None and (getattr(store, "dsn", None) or getattr(runs_repo, "_path", None)):
        from .scoring_jobs import scoring_jobs_for

        jobs = scoring_jobs_for(store)
    if jobs is not None and hasattr(jobs, "list_by_status"):
        invocations = getattr(store, "invocations", None)
        for job in jobs.list_by_status():
            observe("scoring_jobs", job)
            calibration_owned = job.get("owner_kind") == "calibration"
            run_id = None if calibration_owned else job.get("run_id")
            if coverage is not None:
                if calibration_owned:
                    owner = job.get("owner") or {}
                    namespace = "calibration:" + str(owner.get("calibration_job_id", ""))
                    if (owner.get("kind") != "calibration" or not owner.get("calibration_job_id")
                            or job.get("run_id") != namespace or job.get("owner_ref") != namespace):
                        raise ValueError("calibration Job ownership identity disagrees")
                else:
                    owner = job.get("owner") or {}
                    if (job.get("owner_kind") != "subject" or owner.get("kind") != "subject"
                            or owner.get("run_id") != run_id or job.get("owner_ref") != "run:" + str(run_id)):
                        raise ValueError("subject Job ownership identity disagrees")
                    if (run_id,) not in coverage.seen["runs"]:
                        raise ValueError("orphan ScoringJob ownership")
            yield job, run_id, None
            if calibration_owned:
                # No actual Run can recover these edges. Every required facade
                # must be readable even for a currently unpublished child.
                readers = {}
                for name, method in (("invocations", "list_for_job"),
                                     ("scoring_passes", "get"),
                                     ("score_sets", "list_for_pass")):
                    reader = getattr(getattr(store, name, None), method, None)
                    if not callable(reader):
                        raise AttributeError(f"store.{name}.{method} is required for calibration evidence scanning")
                    readers[name] = reader
                for invocation in readers["invocations"](job.get("job_id", "")):
                    if coverage is not None:
                        owner = invocation.get("owner") or {}
                        if (invocation.get("job_id") != job["job_id"]
                                or invocation.get("run_id") != job["run_id"]
                                or owner.get("kind") != "calibration"
                                or owner.get("calibration_job_id") != job["owner"]["calibration_job_id"]):
                            raise ValueError("calibration Invocation ownership identity disagrees")
                    observe("agent_invocations", invocation)
                    yield invocation, None, None
                pass_id = job.get("reserved_pass_id")
                if not isinstance(pass_id, str) or not pass_id:
                    raise ValueError("calibration child requires a durable reserved Pass identity")
                scoring_pass = readers["scoring_passes"](pass_id)
                if scoring_pass is not None:
                    if scoring_pass.get("id") != pass_id:
                        raise ValueError("calibration child Pass identity disagrees with its durable edge")
                    if coverage is not None and scoring_pass.get("run_id") != job["run_id"]:
                        raise ValueError("calibration Pass ownership identity disagrees")
                    observe("scoring_passes", scoring_pass)
                    yield scoring_pass, None, None
                # Retain independently stored scores even if the Pass is absent
                # or damaged; a missing facade must never look like zero scores.
                for score in readers["score_sets"](pass_id):
                    observe("score_sets", score, parent=pass_id)
                    yield score, None, None
                if coverage is not None:
                    for event in store.events.list_for_run(job["run_id"]):
                        observe("events", event, job["run_id"])
                        yield event, job["run_id"], None
            elif invocations is not None and callable(getattr(invocations, "list_for_job", None)):
                for invocation in invocations.list_for_job(job.get("job_id", "")):
                    observe("agent_invocations", invocation, run_id)
                    yield invocation, run_id, None

    # Lifecycle sources are independent pins, never owned by a rollback Run.
    calibrations = getattr(store, "calibrations", None)
    if calibrations is None or not callable(getattr(calibrations, "iter_records", None)):
        raise AttributeError("store.calibrations.iter_records is required for reference scanning")
    for record in calibrations.iter_records():
        observe("calibrations", record)
        yield record, None, None

    experiments = getattr(store, "experiments", None)
    if experiments is not None and hasattr(experiments, "list_specs"):
        for spec in experiments.list_specs():
            observe("experiment_specs", spec)
            yield spec, None, None
            for cell in experiments.list_cells(spec["experiment_id"], spec["version"]):
                observe("experiment_cells", cell)
                yield cell, None, None
    baseline_store = getattr(store, "baseline_store", None)
    if baseline_store is not None and hasattr(baseline_store, "list"):
        for baseline in baseline_store.list(limit=None):
            observe("m6_baselines", baseline)
            yield baseline, None, None
    gate_store = getattr(store, "gate_store", None)
    if gate_store is not None and hasattr(gate_store, "list_results"):
        for gate in gate_store.list_results(limit=None):
            observe("gate_results", gate)
            yield gate, None, None
    reports = getattr(store, "statistical_reports", None)
    if reports is None or not hasattr(reports, "list"):
        raise AttributeError("store.statistical_reports.list is required for evidence reference scanning")
    # Publications own their frozen evidence independently of the referenced
    # Runs/imports. Never inherit an exclusion or a finite UI/listing limit.
    for report in reports.list(limit=None):
        observe("statistical_reports", report)
        yield report, None, None
    ledger = _read_only_import_ledger(store) if read_only else platform_for(store).imports
    for batch in ledger.list_imports():
        observe("motte_imports", batch)
        import_id = batch.get("import_id", "")
        if import_id not in excluded_imports:
            yield batch, None, import_id
            for mapping in ledger.mappings_for(import_id):
                observe("motte_import_mappings", mapping)
                yield mapping, None, import_id


def _read_only_import_ledger(store: Any) -> Any:
    """Bind existing storage without running SQLite schema creation/upgrade."""
    from .platform import _SQLiteImportLedger, _PgImportLedger

    if getattr(store, "dsn", None):
        return _PgImportLedger(store.dsn)
    path = getattr(store.runs, "_path", None)
    if path is not None:
        return _SQLiteImportLedger(str(path))
    platform = getattr(store, "_motte_platform_stores", None)
    ledger = getattr(platform, "imports", None)
    if ledger is None:
        raise AttributeError("existing import ledger is required for read-only reference scanning")
    return ledger


def iter_trace_reference_records(store: Any) -> Iterator[tuple[dict[str, Any], str | None]]:
    """Complete read-only traversal, retaining ownership rather than treating it as a pin."""
    required = {
        "runs": ("list",), "events": ("list_for_run", "stored_for_run"),
        "case_runs": ("list_for_run",), "attempts": ("list_for_run",),
        "invocations": ("list_for_run", "list_for_job"),
        "scoring_passes": ("list_for_run", "get"), "score_sets": ("list_for_pass",),
        "scores": ("list_for_run",), "commands": ("list_for_run",),
        "trials": ("list_for_run",), "runtime_sessions": ("list_for_run",),
        "scoring_jobs": ("list_for_run", "list_by_status"),
        "external_jobs": ("jobs_for_run", "list_records", "list_conflicts"),
        "baselines": ("get_for_run",), "baseline_store": ("list",),
        "benchmark_datasets": ("list",),
        "gate_store": ("list_results", "list_policies"),
        "experiments": ("list_specs", "list_cells"),
        "statistical_reports": ("list",), "calibrations": ("iter_records",),
        "trace_archives": ("list",),
    }
    for name, methods in required.items():
        for method in methods:
            if not callable(getattr(getattr(store, name, None), method, None)):
                raise AttributeError(f"store.{name}.{method} is required for Trace protection")
    ledger = _read_only_import_ledger(store)
    if any(not callable(getattr(ledger, name, None)) for name in ("list_imports", "mappings_for")):
        raise AttributeError("complete import ledger readers are required for Trace protection")
    coverage = _TraceCoverage()
    # Task 5 must preserve archived cross-Run/Pass/event reference closure before
    # planning later prefixes. Empty/truncated facades cannot hide physical rows.
    if store.trace_archives.list():
        raise ValueError('Trace receipts require verified archived reference closure')
    for record, owner, _ in _iter_artifact_records_with_owners(store, read_only=True, coverage=coverage):
        if not isinstance(record, dict):
            raise ValueError("reference repository returned a malformed record")
        yield record, owner
    for source, reader in (("gate_policies", store.gate_store.list_policies),
                           ("benchmark_datasets", store.benchmark_datasets.list),
                           (None, store.external_jobs.list_conflicts)):
        for record in reader():
            if not isinstance(record, dict):
                raise ValueError("reference repository returned a malformed record")
            if source is not None:
                coverage.observe(source, record)
            yield record, None
    coverage.verify(store)


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


def referenced_pass_ids(
    store: Any, *, exclude_run_ids: Iterable[str] = (), exclude_import_ids: Iterable[str] = (),
) -> set[str]:
    """Collect fixed Pass edges from the same complete, ownership-aware traversal."""
    found: set[str] = set()
    for record in iter_artifact_records(
        store, exclude_run_ids=exclude_run_ids, exclude_import_ids=exclude_import_ids,
    ):
        collect_referenced_pass_ids(record, found)
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


def collect_referenced_pass_ids(value: Any, found: set[str]) -> None:
    """Find selected, source, previous and mapped scoring Pass references."""
    if isinstance(value, dict):
        if value.get("target_type") == "scoring_pass" and isinstance(value.get("target_id"), str):
            found.add(value["target_id"])
        for key, child in value.items():
            if key in {"scoring_pass_id", "source_pass_id", "previous_pass_id"} and isinstance(child, str):
                found.add(child)
            collect_referenced_pass_ids(child, found)
    elif isinstance(value, list):
        for child in value:
            collect_referenced_pass_ids(child, found)
