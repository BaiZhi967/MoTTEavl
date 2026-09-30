# M8 Trace Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicitly enabled, archive-before-trim Trace retention that preserves evidence, monotonic sequences, and truthful history completeness.

**Architecture:** Keep server write time outside the public Trace payload. A read-only plan selects unprotected terminal prefixes; apply owns the existing maintenance/Worker/artifact locks, durably writes immutable archives, then atomically publishes receipts and trims exactly the revalidated prefixes. Existing reference enumeration and backup verification retain both archives and their nested evidence.

**Tech Stack:** Python 3.12+, existing Pydantic/contracts, SQLite, PostgreSQL/psycopg 3/Alembic, pytest, local CLI/SDK; no new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-30-m8-persistent-closure-design.md`, Section C and delivery gates.

## Global Constraints

- Planning only until the user reviews this plan; do not execute it, mutate product code, or commit now. Preserve the full-source test run at `50b806d`.
- Default `enabled=False`, `retention_days=None`; enablement requires an explicit positive strict integer. No invented 30/90-day policy, scheduler, startup hook, or automatic apply.
- This round's apply runs only in synthetic disposable test databases. No production operation, real deletion outside fixtures, paid calls, private export, merge, or deployment.
- Unknown legacy server timestamps remain NULL; never backfill from payload timestamps. Preserve every Run's highest seq, allocate `max(seq)+1`, and stop at the first unknown/recent/protected/missing sequence.
- Retain active/needs_review/imported Runs, Baseline/Gate/StatisticalReport pins, scoring-event references, and other independent unified references. Any enumeration failure aborts.
- No reason-string authorization, global allow-delete flag, arbitrary SQL/callback maintenance bypass, archive expiry, or archive-to-active-stream restore command.
- Dependencies: StatisticalReport's eager `store.statistical_reports` repository (`put/get/list`, envelope `{report_id,published_at,body}`) and fail-closed unbounded reference traversal must land first; Judge persistence must also enter that traversal. Reserve `0018_trace_retention.py` after Judge revision `0017`; coordinator serializes `_SCHEMA`, `maintenance.py`, API and reference-collector integration.

## Review Focus

- Payload-forged, timezone-naive, future, equal-cutoff, or missing write times must never cause unsafe trimming (Task 1/2).
- Ordinary ownership edges must not pin every Run, while nested scoring references and more than 10,000 independent pins must remain protected (Task 2).
- A stale/fork-inherited owner or second PostgreSQL connection must never authorize maintenance writes or self-deadlock behind SHARE locks (Task 4).
- Disk exhaustion, symlink replacement, same-name/different-bytes, and first-archive power loss before ancestor directory entries are durable must preserve all evidence (Task 3/4).
- Paginated/empty historical reads and backup-restored receipts must not claim complete history or lose nested/hash-only evidence (Task 5/6).

## File Map and Shared Types

- Create `packages/storage/motte_storage/trace_retention_models.py`: frozen validated models shared by planner/archive/commit/read paths; `trace_retention.py`: policy, protection roots, planner and apply orchestration; `trace_archives.py`: receipt readers and archive verification. Keep destructive SQL in the owner-bound maintenance implementation.
- Modify `run_store.py`, `postgres.py`, `scoring_jobs.py`: storage-only timestamps, append unification, consistent event/retention reads. Modify `maintenance.py`, `operation_locks.py`, `artifacts.py`, `artifact_refs.py`, `gc.py`: ownership, durable files, reference closure and backup. All are under `packages/storage/motte_storage/`.
- Create `migrations/versions/0018_trace_retention.py`; extend existing migration/schema tests. Create `packages/sdk-python/motte_sdk/trace_retention.py` and `packages/cli/motte_cli/trace_retention.py`; wire existing CLI `main.py`/`ops.py`. Modify SDK `service.py`/`client.py` and API `apps/api/app/main.py` for history completeness; no destructive HTTP endpoint.
- Create focused tests `tests/storage/test_trace_retention.py`, `test_trace_archives.py`, `test_trace_retention_transactions.py`, `test_trace_retention_pg.py`, `tests/api/test_trace_retention.py`, `tests/cli/test_trace_retention.py`; extend `tests/security/test_gc_retention.py`, `tests/migration/test_import_resume_rollback.py`, `tests/integration/test_m8_pg_restore.py` and existing SDK stream tests in `tests/sdk/test_client_contract.py`.
- Model fields (all in `trace_retention_models.py`, JSON uses UTC ISO timestamps): `TraceRetentionConfig(enabled: bool=False, retention_days: int|None=None)`; `StoredTraceEvent(run_id: str, seq: int, payload: dict[str,Any], stored_at: datetime|None)`; `TracePrefix(run_id: str, run_revision: int, status: str, first_seq: int, last_seq: int, keep_seq: int, event_count: int, events_sha256: str)`.
- `TraceProtection(run_ids: frozenset[str], event_seqs: dict[str,frozenset[int]], sha256: str)`; `TraceRetentionPlan(schema_version: Literal[1], plan_id: str, store_identity_sha256: str, config: TraceRetentionConfig, cutoff: datetime|None, protection_sha256: str, prefixes: list[TracePrefix])`; plan ID hashes its canonical body excluding `plan_id`.
- `TraceArchiveReceipt(archive_id: str, plan_id: str, prefix: TracePrefix, artifact_id: str, sha256: str, bytes: int, cutoff: datetime, artifact_refs: dict[str,str|None], artifact_hashes: list[str], committed_at: datetime|None)`; pending receipts have no `committed_at`, DB assigns it. `TraceEventWindow(events: list[dict[str,Any]], trimmed_through: int)`; `TraceRetentionResult(plan_id: str, trimmed_events: int, receipts: list[TraceArchiveReceipt])`.
- Errors in `trace_retention_models.py`: `TraceRetentionDisabled`, `TraceRetentionPlanChanged`, `TraceArchiveInvalid`, all `ValueError` subclasses; ownership conflicts keep existing `MaintenanceConflict`. Config/model validation rejects extra fields, booleans-as-days, nonfinite values and naive datetimes; internal legacy read converts invalid stored time to unknown.

---

### Task 1: Repository-owned time and monotonic event allocation

**Files:** models above; `run_store.py:_SCHEMA,_append_event,_SQLiteTraceEvents,_InMemoryTraceEvents`; `postgres.py:_append_event,_PgTraceEvents`; `scoring_jobs.py:PgScoringJobs.publish`; migration `0018_trace_retention.py`; tests `test_trace_retention.py`, `test_sqlite_schema_upgrade.py`, `test_postgres_repository.py`, `test_interactive_commands.py`.

**Interfaces:** Keep `append(event: dict[str,Any])->dict[str,Any]` and public payload unchanged. Add all three event repositories' `stored_for_run(run_id: str)->list[StoredTraceEvent]`. Add internal `trace_retention_models.utc_now()->datetime`; test clock patching is internal, not a caller-supplied retention timestamp.

- [ ] **Step 1: Write failing tests** `test_server_time_cannot_be_forged`, `test_legacy_time_stays_unknown`, `test_every_append_path_stamps_time`, `test_memory_sequence_does_not_reuse_trimmed_prefix` (Memory/SQLite plus disposable PG variants):
  ```python
  assert stored.stored_at == fixed_utc_now and stored.payload["stored_at"] == forged_payload_time
  assert migrated.stored_at is None  # even with a plausible old payload timestamp
  assert all(row.stored_at == fixed_utc_now for row in append_path_results)
  assert append_after_removing_fixture_prefix["seq"] == previous_max + 1
  ```
  Cover direct append, Run create/update/transition, attempt/pass publication, and SQLite/Memory/PG ScoringJobs.publish; fixtures alone remove prefixes before Task 4 exists.
- [ ] **Step 2: Red:** `uv run pytest -q tests/storage/test_trace_retention.py tests/storage/test_sqlite_schema_upgrade.py tests/storage/test_postgres_repository.py`; expect missing metadata/models or sequence assertion failure, not unrelated collection failure.
- [ ] **Step 3: Implement:** nullable `trace_events.stored_at TEXT` on SQLite and PG with no default/backfill; timestamps assigned inside shared append helpers. Route SQLite direct append and PG ScoringJobs' inline SQL through their backend helper. Memory stores time separately and replaces `len(events)+1` with `max(seq, default=0)+1`. Update manual PG schema fixtures. Existing SQLite `create_and_upgrade` adds the nullable column without copying payload values.
- [ ] **Step 4: Green:** rerun Step 2 plus `uv run pytest -q tests/storage/test_run_integrity.py tests/storage/test_scoring_jobs.py tests/storage/test_interactive_commands.py`; all selected tests pass; PG skips are reported as missing evidence.
- [ ] **Step 5: Commit after review gate:** `git add` only Task 1 files, then `git commit -m "feat: stamp trace writes with server-owned retention time"`.

### Task 2: Safe prefix plans and complete protection roots

**Files:** create `trace_retention.py`; modify `artifact_refs.py` only for reusable complete reference traversal; test `tests/storage/test_trace_retention.py`.

**Interfaces:** `collect_trace_protection(store: Any)->TraceProtection`; `plan_trace_retention(store: Any, *, config: TraceRetentionConfig)->TraceRetentionPlan`; private `_plan_at_cutoff(store: Any, *, config: TraceRetentionConfig, cutoff: datetime)->TraceRetentionPlan`. The public planner obtains server time; apply revalidates at the saved cutoff, never an advanced cutoff. Consume Task 1 `stored_for_run`, `store.statistical_reports.list(limit=None)`, and complete fail-closed traversal. Disabled plans have `cutoff=None, prefixes=[]`; they do not invent a retention policy.

- [ ] **Step 1: Write failing tests** `test_disabled_and_invalid_policy`, `test_prefix_stops_at_first_unsafe_row`, `test_all_protection_roots`, `test_reference_errors_fail_closed`, `test_ownership_is_not_a_pin`:
  ```python
  assert plan_trace_retention(store, config=TraceRetentionConfig()).prefixes == []
  assert selected_seqs([old, unknown, old, old]) == [1]
  assert selected_seqs([old, at_cutoff, old, old]) == [1]
  assert selected_seqs([old, future, old, old]) == [1]
  assert selected_seqs([old, old, old, old], referenced_seq=2) == [1]
  assert protected_run_ids.isdisjoint({p.run_id for p in plan.prefixes})
  assert ordinary_completed_run_id in {p.run_id for p in unpinned_plan.prefixes}
  ```
  Parametrize enabled missing/0/-1/1.5/True days as validation errors; naive/malformed legacy time as unknown; terminal singleton and missing-seq prefix as no unsafe candidate. Every plan preserves `keep_seq=max(seq)`.
- [ ] **Step 2: Red:** `uv run pytest -q tests/storage/test_trace_retention.py -k 'policy or prefix or protection or reference or ownership'`; expect missing planner or protection assertion failures.
- [ ] **Step 3: Implement planner:** eligible statuses are exactly `completed,failed,cancelled,unsupported,profile_stale`; `needs_review` and all unknown/active states remain protected. Pin imports from both `manifest.import_source` and import ledger mappings, legacy/current Baselines, Gate results, immutable StatisticalReport bodies, Judge provenance, Experiment cells and independent cross-Run references. Decode nested `EvidenceRef(kind="event",run_id,locator=<positive seq>)` from scores/ScoreSets, passes, invocations, jobs and other traversed records into row protection; malformed reference-shaped data aborts. Resolve pass-only refs through `scoring_passes.get`, failing closed when unresolved. Ordinary top-level repository ownership and archive-receipt ownership are not independent pins; do not call `referenced_run_ids(store)` as the whole pin set.
- [ ] **Step 4: Green:** same command; additionally `test_all_protection_roots` must cover 10,001 pins, nested event refs, StatisticalReport drift-resistant pins, import mappings without manifest provenance, failed repository reads and a reference existing only in Judge provenance. Canonical plan digest binds store identity (hashed; no DSN leakage), exact config/cutoff, Run revision/status, event payload+server-time digest and protection digest; no DB/file writes.
- [ ] **Step 5: Commit after review gate:** stage Task 2 files; `git commit -m "feat: plan protected trace retention prefixes"`.

### Task 3: Immutable, durable archive files

**Files:** create `trace_archives.py`; modify `artifacts.py`, `operation_locks.py`, `gc.py`; test `tests/storage/test_trace_archives.py`, `tests/security/test_gc_retention.py`.

**Interfaces:** `ArtifactStore.put_trace_archive(data: bytes, *, maintenance_owner: str)->Artifact`; `build_trace_archive(prefix: TracePrefix, events: list[StoredTraceEvent])->bytes`; `verify_trace_archive(data: bytes, receipt: TraceArchiveReceipt)->None`. Consume existing `collect_artifact_refs(..., hashes=...)` and canonical JSON serializer.

- [ ] **Step 1: Write failing tests** `test_archive_is_canonical_complete_and_immutable`, `test_archive_requires_live_owner`, `test_archive_io_failures_delete_nothing`, `test_first_archive_persists_every_directory_entry`, `test_orphan_archive_has_no_ttl`:
  ```python
  assert archive_bytes == second_encoding and decoded["events"] == complete_original_rows
  assert artifact.id == "trace-archives/sha256/" + sha256(archive_bytes).hexdigest() + ".json"
  assert io_order.index("fsync_file") < io_order.index("fsync_sha256_dir")
  assert io_order.index("fsync_sha256_dir") < io_order.index("fsync_trace_archives_dir") < io_order.index("fsync_artifact_root") < io_order.index("verified")
  assert live_events == before_events and receipts == []
  assert archive_id not in {row["artifact_id"] for row in gc_plan.deletable}
  ```
  Assert stale/wrong-root/fork-inherited owner, ordinary put/delete of reserved paths, symlink escape, disk full, short write, failed fsync and existing unequal bytes all raise; existing identical bytes are reverified/fsynced and idempotent. The first-archive fixture starts with only an existing durable artifact root: inject failure at each mkdir/fsync boundary for `trace-archives/sha256`, and simulate power loss by discarding directory entries not persisted by their parent fsync. Assert zero receipt/trim until every created directory and its parent is durable; after successful DB commit, the simulated durable namespace still resolves the complete archive. Do not treat a process-kill test alone as power-loss proof.
- [ ] **Step 2: Red:** `uv run pytest -q tests/storage/test_trace_archives.py tests/security/test_gc_retention.py`; new tests fail on missing archive API/protection.
- [ ] **Step 3: Implement:** archive schema `1` contains full untouched event payloads, UTC server times, Run ID, seq bounds/count, event digest, nested path+digest refs and hash-only refs. Path derives only from canonical content. Validate live PID/store/root owner in a separate archive-write capability; do not add `trace_retention` to the existing deletion allowlist. Require an already durable artifact root; use no-follow/exclusive-create and safe directory traversal. Flush/fsync the file, then fsync its containing directory and every newly created ancestor directory plus its parent, bottom-up through the existing artifact root (`sha256`, `trace-archives`, root on first use), before readback/hash/schema verification and any DB trim. Reverify this chain on retries; an existing directory or file alone does not prove an earlier interrupted fsync succeeded. Unsupported durability primitives or any directory fsync failure fail closed. Reject generic ArtifactStore overwrite/delete in `trace-archives/`; GC always protects the namespace, including crash orphans.
- [ ] **Step 4: Green:** rerun Step 2 plus `uv run pytest -q tests/storage/test_artifacts.py tests/storage/test_maintenance.py`; all pass without weakening existing mutation barriers.
- [ ] **Step 5: Commit after review gate:** stage Task 3 files; `git commit -m "feat: write immutable durable trace archives"`.

### Task 4: Owner-bound atomic receipt and prefix trim

**Files:** `trace_retention.py`, `trace_archives.py`, `maintenance.py`, `run_store.py` factory/DDL, `postgres.py` factory, migration `0018_trace_retention.py`; create tests `test_trace_retention_transactions.py`, `test_trace_retention_pg.py`.

**Interfaces:** `apply_trace_retention(store: Any, artifacts_root: str|Path, plan: TraceRetentionPlan, *, config: TraceRetentionConfig, confirm: bool=False)->TraceRetentionResult`; `maintenance.commit_trace_retention(store: Any, *, owner: str, plan: TraceRetentionPlan, receipts: list[TraceArchiveReceipt])->TraceRetentionResult`. In `trace_archives.py`, `SQLiteTraceArchives(path: str)`, `PgTraceArchives(dsn: str)`, `MemoryTraceArchives(lock: RLock)` eagerly attach as `store.trace_archives`; each exposes `get(archive_id: str)->TraceArchiveReceipt|None`, `list()->list[TraceArchiveReceipt]`, `list_for_run(run_id: str)->list[TraceArchiveReceipt]`, detached and validated. No public receipt put/update/delete. `archive_id="trace-archive-"+sha256(archive_bytes).hexdigest()`. Memory supports reads/planning and monotonic append; durable apply continues to raise `BackupUnsupported`, matching existing maintenance support.

- [ ] **Step 1: Write failing tests** `test_apply_rechecks_every_plan_precondition`, `test_receipt_and_trim_are_atomic`, `test_pg_uses_lock_owning_connection`, `test_two_processes_cannot_trim_twice`, `test_crash_boundaries_preserve_evidence`:
  ```python
  assert post_apply_seqs == [4] and receipt.prefix.event_count == 3
  assert append_after_apply["seq"] == 5
  assert rollback_seqs == [1, 2, 3, 4] and rollback_receipts == []
  assert trim_connection_backend_pid == maintenance_lock_backend_pid
  assert sorted(contender_outcomes) == ["applied", "conflict"]
  assert (len(receipts), live_seqs) in [(0, [1, 2, 3, 4]), (1, [4])]
  assert io_order.index("fsync_artifact_root") < io_order.index("verified") < io_order.index("db_trim")
  ```
  Disabled/missing-confirm, changed config/store/plan hash/pin/status/revision/payload/time/max-seq, forged future cutoff (even with a recomputed plan hash), unrelated reference-enumeration failure, owner mismatch, archive corruption and incomplete receipt set must cause zero trim. Revalidation uses the saved cutoff, so merely elapsed time does not expand or invalidate a plan. Reapply an already committed exact plan returns the same verified receipts and `trimmed_events=0` before comparing its already-trimmed candidates; partial or mismatched receipts raise.
- [ ] **Step 2: Red:** `uv run pytest -q tests/storage/test_trace_retention_transactions.py tests/storage/test_trace_retention_pg.py`; expect missing apply/receipt operations; PG fixture must use `isolated_pg_database`, never the source database.
- [ ] **Step 3: Implement:** table `trace_archive_receipts(archive_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, run_id TEXT NOT NULL, first_seq BIGINT NOT NULL, last_seq BIGINT NOT NULL, payload TEXT/JSONB NOT NULL, UNIQUE(run_id,first_seq,last_seq))`, Run/plan indexes; downgrade refuses nonempty receipts. Acquire `begin_maintenance(..., reason="trace_retention", artifacts_root=...)`; only its live owner is authority. Reject `plan.cutoff > utc_now()-timedelta(days=config.retention_days)`; hashes are integrity checks, not authorization. Recompute and compare the complete plan at its saved cutoff under the Worker/write barrier before writing archives. Fsync/verify every archive before the first destructive SQL. Reverify bytes, exact row digest/count, receipt and prefix bounds in the commit path; all candidates commit or roll back together.
- [ ] **Step 4: Implement fixed owner transaction:** SQLite uses one `BEGIN IMMEDIATE`; PG reuses the existing live SHARE-lock connection/transaction, never a fresh connection. After validation, temporarily remove only the exact trace-DELETE and receipt-INSERT guard(s) inside this uncommitted, writer-exclusive transaction, execute fixed bound SQL, restore the identical guards before commit, and clear only this owner's maintenance metadata in that same final commit. PG's statement guard requires locking both touched tables before its transactional replacement; unrelated tables retain SHARE locks/guards. No caller connection, callback, SQL or bypass flag is exposed. Exceptions roll back DDL/DML together; resource release is owner-checked and happens after commit/rollback. Never commit to drop SHARE locks and then open a deletion transaction.
- [ ] **Step 5: Green:** rerun Step 2 and maintenance tests. While paused inside archive/trim, second processes attempting ordinary Trace/Run/ScoreSet/Baseline/Gate/report/receipt/artifact writes must block/reject, including a PG repeatable-read transaction begun before acquisition. Fault injection at file fsync, every first-archive ancestor-directory fsync, before receipt, after receipt, after DELETE and before/after commit proves only orphan archives or committed receipt+trim; restart keeps abandoned maintenance fail-closed until explicit owner recovery. Run Task 3's simulated first-archive power-loss test through both SQL apply paths, verifying no committed trim references a disappeared archive namespace. Add explicit CI PG test invocation in `.github/workflows/ci.yml`; PG skips are not acceptance.
- [ ] **Step 6: Commit after review gate:** stage Task 4 files; `git commit -m "feat: atomically archive and trim trace prefixes under maintenance"`.

### Task 5: Archive reference closure and backup verification

**Files:** `artifact_refs.py`, `trace_archives.py`, `maintenance.py`; tests `tests/security/test_gc_retention.py`, `tests/migration/test_import_resume_rollback.py`, `tests/integration/test_m8_pg_restore.py`, `tests/storage/test_trace_archives.py`.

**Interfaces:** `verify_trace_archives(store: Any, artifacts: ArtifactStore)->None`; consume `store.trace_archives.list()` and `verify_trace_archive`. Extend the shared traversal to emit receipts as independent durable audit records, including their archive file and nested/hash-only refs; do not exempt them through rollback-owned Run exclusions.

- [ ] **Step 1: Write failing tests** `test_trimmed_evidence_survives_gc_and_rollback`, `test_backup_restore_validates_archive_receipts`, `test_archive_reference_read_failure_is_fatal`:
  ```python
  assert {archive_id, nested_artifact_id} <= set(referenced_artifact_hashes(store))
  assert hash_only_digest in referenced_hashes and rollback_deleted == []
  assert restored_receipts == original_receipts and restored_live_seqs == [4]
  assert manifest["counts"]["trace_archive_receipts"] == 1
  ```
  Missing/tampered archive, wrong seq/count/event digest/timestamp/ref closure, overlapping receipt ranges, receipt overlapping live rows and receipt beyond retained max fail backup/restore; a truncated receipt repository must not silently pass.
- [ ] **Step 2: Red:** `uv run pytest -q tests/storage/test_trace_archives.py tests/security/test_gc_retention.py tests/migration/test_import_resume_rollback.py tests/integration/test_m8_pg_restore.py`; expect archive closure/count/verification failures.
- [ ] **Step 3: Implement:** include receipts in `_store_counts`, SQLite snapshot schema/fingerprint, PG dump and both staging restore validators; verify canonical archive bytes against receipts and retained event bounds before claiming success. Backup copies receipt-referenced archives and every reachable nested artifact, including resolved hash-only references. Keep restore guard active; restore archives as evidence files only, never replay them into Trace. Existing rollback/GC consume the same shared closure; missing refs fail closed.
- [ ] **Step 4: Green:** rerun Step 2; disposable PG dump/restore must prove parity when available. Document missing Docker/PG/Windows evidence separately rather than weakening assertions.
- [ ] **Step 5: Commit after review gate:** stage Task 5 files; `git commit -m "fix: retain archived trace evidence across gc and backup"`.

### Task 6: Explicit operations and truthful history reads

**Files:** SDK/CLI files in the map; `run_store.py`, `postgres.py`, `service.py`, `apps/api/app/main.py`; tests `tests/api/test_trace_retention.py`, `tests/cli/test_trace_retention.py`, `tests/sdk/test_client_contract.py`; docs `docs/operations/backup-restore.md`, `docs/protocols/sdk-and-migration.md`, new `docs/operations/trace-retention.md`.

**Interfaces:** event repositories `read_window(run_id: str, after: int)->TraceEventWindow` read rows and receipt high-water mark in one consistent DB snapshot; `RunService.events_window(run_id: str, after: int)->TraceEventWindow`. SDK `motte_sdk.trace_retention` reexports Task 2/4 functions/models with identical signatures. CLI `trace_retention_command(args: argparse.Namespace)->int`, `add_trace_retention_parser(sub: argparse._SubParsersAction)->None`.

- [ ] **Step 1: Write failing tests** `test_historical_cursor_is_partial`, `test_empty_and_paginated_history_remain_honest`, `test_sdk_preserves_partial_after_reconciliation`, `test_cli_is_explicit_and_local_only`:
  ```python
  assert snapshot_after_0["partial"] is True and snapshot_after_3["partial"] is False
  assert first_sse_frame == {"type": "gap", "after": 0, "next_seq": 4, "partial": True}
  assert final_stream_state.partial is True
  assert no_config_plan["prefixes"] == [] and missing_confirm_exit == 2
  assert server_mode_exit == 2 and before_events == after_rejected_events
  ```
  Include receipt metadata with no returned events, old Last-Event-ID, page limits, repeated retention, active append after terminal trim, legacy unexplained seq gaps and concurrent trim/read snapshots.
- [ ] **Step 2: Red:** `uv run pytest -q tests/api/test_trace_retention.py tests/cli/test_trace_retention.py tests/sdk/test_client_contract.py`; expect hard-coded `partial=False` and missing CLI failures.
- [ ] **Step 3: Implement:** SSE and snapshot consume one `events_window`; `after < trimmed_through` emits authoritative gap (`next_seq=trimmed_through+1`) and sets `partial=True`, even with no pending row. Retain conservative existing unexplained-first-seq-gap signaling. SDK carries partial through snapshot fallback and terminal reconciliation without resetting it. Public Trace payload stays unchanged, so no new public timestamp field is needed.
- [ ] **Step 4: Implement operations/docs:** `motte trace-retention plan [--config FILE] --output FILE` writes only a canonical plan; omitted config means disabled. `motte trace-retention apply --config FILE --plan FILE --artifacts-root DIR --confirm` requires the saved exact plan/config and local mode. No recompute-and-silently-apply, HTTP mutation endpoint, environment-enable default or scheduler. Document synthetic-only current execution, unknown legacy times, recovery of abandoned ownership, permanent archive retention and no stream-restoration command; update GC's diagnostic string to describe explicit retention availability without invoking it.
- [ ] **Step 5: Green:** rerun Step 2, then `make check`, `uv run python scripts/check_openapi.py`, `make wheels`, and the explicit disposable PG concurrency/backup tests from Tasks 4/5. Confirm no OpenAPI/SDK compatibility drift; regenerate checked-in schema only if intentionally changed. Obtain Windows filesystem durability evidence or retain a named unsupported/fail-closed gate. Do not claim real data retention acceptance from fixtures.
- [ ] **Step 6: Commit after review gate:** stage Task 6 files; `git commit -m "feat: expose explicit trace retention and honest partial history"`.

## Handoff and Stopping Condition

Self-review: Section C is covered by Tasks 1–6; each Review Focus has named assertions, and only Task 4 owns destructive SQL. This plan does not implement archive expiry, replay restoration, automatic scheduling or broad maintenance bypasses. Recommend subagent-driven execution after user plan approval because a receipt/transaction mistake risks evidence loss; use fresh task reviewers and a final cross-backend review. Completion requires reviewed source, all available gates green, explicit missing-platform evidence, and unchanged real-data/production authorization boundaries.
