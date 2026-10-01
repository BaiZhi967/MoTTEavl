# M8 Immutable Statistical Reports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish reproducible statistical results once, retrieve/export their stored content, and preserve their evidence across maintenance operations.

**Architecture:** A thin SDK service calls the existing fixed-Pass statistics calculation once and publishes its complete output with the frozen statistical policy. Content-addressed repositories share an immutable envelope across Memory/SQLite/PostgreSQL; existing reference traversal and maintenance barriers protect the report and its Run/Pass/Artifact closure.

**Tech Stack:** Python, Pydantic, FastAPI, existing standard-library statistics/exporters, SQLite, psycopg/PostgreSQL, Alembic, pytest.

**Spec:** `docs/superpowers/specs/2026-09-30-m8-persistent-closure-design.md`, section A and delivery gates.

## Global Constraints

- “保留 GET `/api/v1/comparisons/statistics` 的只读动态视图。”
- “同内容幂等，异内容同 ID 拒绝。无更新/删除接口。”
- “CLI/SDK 提供 publish/get/export；现有 compare --statistics 保持不写入的兼容行为。”
- “没有付费模型、真实人审样本、私有导出、生产操作、合并或部署授权。”
- Planning baseline: `50b806d`; this document does not authorize execution. Obtain user plan review first; do not disturb the source tree while its existing full tests run.
- Reserve migration `0016_statistical_reports`, parent `0015_m7_platform_tables`; Judge owns 0017 and Trace owns 0018. Coordinator serializes edits to common store/schema/maintenance/API/generated files.
- Keep `statistical_policy@1`, seed `20260921`, iterations `2000`, confidence `0.95`, implementation `motte_eval.statistics@1`, and existing missing/applicability semantics unchanged. No new policy overrides, jobs, providers, dependencies, or web UI.

## Review Focus

- Non-finite, negative, Boolean, missing, or currency-less measurements remain visibly invalid/missing after publication and strict JSON serialization (Task 3).
- Simultaneous same-body publication converges on one ID and first server timestamp; hash/input corruption is detected on reads (Tasks 1–2).
- A publication racing GC/rollback cannot create a pin after its evidence was deleted (Tasks 3–4).
- More reports than an ordinary list limit, imported Run exclusions, or reference-reader failure must not silently drop protection (Task 4).
- A later current-Pass, metering, or installed-policy change cannot affect stored JSON/JUnit or require recomputation to export (Tasks 3 and 5).

---

## Locked interfaces and file boundaries

- New `packages/contracts/motte_contracts/statistical_reports.py`: request/envelope models and pure identity/integrity validation; no evaluation or database access.
- New `packages/storage/motte_storage/statistical_reports.py`: three repositories and `statistical_publication_guard(store)`; eager `RunStore.statistical_reports` attachment, not a lazy table factory.
- New `packages/sdk-python/motte_sdk/statistical_reports.py`: publication/get service only. Reuse `ComparisonService.paired_statistics(...)` unchanged; its existing `inputs` already captures the actual selected subject Case/Task metering and fixed refs.
- Envelope is exactly `{report_id: str, published_at: str, body: dict}`. Body is `{schema_version: 1, policy: dict, result: dict}`; `result` is the complete existing statistics output, including `inputs`, `input_digest`, `refs`, Trial qualifications, missing counts, unit/k, algorithm/version and results.
- `report_id = "stat-report-" + canonical_hash(body).removeprefix("sha256:")`; first successful insertion assigns UTC RFC3339 `published_at`. Timestamp and ID are outside the hashed body; clients cannot supply them.
- Canonical body text uses existing `canonical_json` after strict `json.dumps(..., allow_nan=False)` validation. Store canonical TEXT on both SQL backends, avoiding JSONB number normalization changing identity; Memory also stores detached canonical round-trips.
- Request fields: `baseline_run_id: str`, `candidate_run_id: str`, `allowed_factors: list[str] = ["model"]`, `baseline_pass_id: str | None = None`, `candidate_pass_id: str | None = None`, `k: StrictInt = 1` with `k >= 1`; forbid extra fields and blank IDs/factors, normalize factors to sorted unique strings. Never accept client body/results/policy/ID/timestamp.

### Task 1: Define immutable report identity and validation

**Files:** Create contract file above and `tests/contract/test_statistical_reports.py`.

**Interfaces:** Produce `StatisticalReportPublishRequest`, `StatisticalReport`; `statistical_report_id(body: dict[str, Any]) -> str`; `validate_statistical_report(report: dict[str, Any]) -> dict[str, Any]`. Validation returns a detached envelope or raises `ValueError`; no lookup of the currently installed statistical policy.

- [ ] **Red:** Add `test_body_identity_excludes_publication_time`: reordered mapping keys yield equal IDs, changing metering/refs/k/policy/results changes ID, and two valid timestamps do not change ID. Assert ID equals the formula above.
- [ ] **Red:** Add `test_integrity_checks_are_self_contained`: wrong report ID, wrong `result.input_digest`, mismatch between `result.refs` and `result.inputs.refs`, or mismatch between stored policy hash/ref/seed/iterations/implementation and result raises `ValueError`; old internally consistent stored policy remains readable. Missing required result fields and raw NaN/Infinity reject; existing named non-finite input markers survive.
- [ ] **Red:** Add `test_request_rejects_client_publication_fields`: extra `body`, `qualified`, `published_at`, `report_id`, `policy`, Boolean/zero k and blank refs reject; duplicate factors normalize.
- [ ] Run `uv run pytest -q tests/contract/test_statistical_reports.py`; expect import/missing-interface failures before implementation.
- [ ] Implement the exact models/helpers; validate both RunReportRef objects using the existing contract, complete required result fields, strict JSON, body digest and internal input/policy bindings. Preserve result dictionaries without filtering unknown diagnostic fields. Do not change global hashing behavior.
- [ ] Run the same command; require all PASS. Scoped review: identity boundaries, strict values, no client-supplied conclusion. Commit only these files after review: `feat(contracts): define immutable statistical reports`.

### Task 2: Persist reports identically on three backends

**Files:** Create storage file above, `migrations/versions/0016_statistical_reports.py`, `tests/storage/test_statistical_reports.py`, `tests/storage/test_statistical_reports_pg.py`; modify `packages/storage/motte_storage/run_store.py` (`_SCHEMA`, `RunStore`, SQLite/Memory factories), `packages/storage/motte_storage/postgres.py` (factory), `tests/storage/test_sqlite_schema_upgrade.py`, `tests/storage/test_postgres_repository.py` (explicit revision-chain assertion).

**Interfaces:** `MemoryStatisticalReports(lock: RLock)`, `SQLiteStatisticalReports(path: str)`, `PgStatisticalReports(dsn: str)` each expose `put(report_id: str, body: dict[str, Any]) -> dict[str, Any]`, `get(report_id: str) -> dict[str, Any] | None`, `list(*, limit: int | None = None) -> list[dict[str, Any]]`. No delete/update methods. Order list by `report_id`; `None` means all; zero means empty; negative/Boolean limit rejects. All returned envelopes are detached and integrity-validated.

- [ ] **Red:** Add backend-shared `test_put_replay_and_detachment`: `first == replay`, same `published_at`, one row, mutations of inputs/get/list results do not mutate storage; missing ID returns `None`; deterministic unbounded/limited list behavior.
- [ ] **Red:** Add `test_conflict_and_corruption_fail_closed`: claimed ID for changed body rejects without changing old row; raw persisted body/input-digest/ID corruption raises on get/list/replay, never silently repairs. Add SQL reopen persistence and `-0.0`/`1.0` canonical-text round-trip cases.
- [ ] **Red:** Add `test_parallel_publication_converges`: independent SQLite connections/processes and independent PG connections submit identical body; all envelopes equal, exactly one row. Memory threads obey the same assertion. Use `isolated_pg_database`, never the source DSN's database.
- [ ] **Red:** Add migration tests: pre-0016 SQLite rows preserved after two upgrades; `revision_ids()` appends exactly `0016_statistical_reports` while this slice is isolated; PG 0015→0016→0015 empty→0016 works; populated downgrade refuses with named `statistical_reports` blocker and preserves bytes; `DOWN_STATEMENTS` exists for existing isolated fixture cleanup.
- [ ] Run `uv run pytest -q tests/storage/test_statistical_reports.py tests/storage/test_statistical_reports_pg.py tests/storage/test_sqlite_schema_upgrade.py tests/storage/test_postgres_repository.py`; expect missing repository/table failures (PG skips are not PG proof).
- [ ] Implement table `statistical_reports(report_id TEXT PRIMARY KEY, body TEXT NOT NULL, published_at TEXT NOT NULL)` in both DDL paths. SQLite uses `BEGIN IMMEDIATE`; PG uses transactional insert-on-conflict-do-nothing then reads/compares the winner; Memory uses shared RLock. Validate submitted and existing content, preserve first timestamp, and never upsert over a row. Export `StatisticalReportConflict(ValueError)` and `StatisticalReportCorrupt(ValueError)` for service mapping.
- [ ] Run the same command with a verified disposable loopback PG cluster; require PASS on every backend. Scoped review: SQL race/rollback, migration downgrade safety and exact canonical bytes. Commit: `feat(storage): persist immutable statistical reports`.

### Task 3: Publish one captured calculation under maintenance exclusion

**Files:** Create SDK service above, `tests/sdk/test_statistical_reports.py`; modify storage file from Task 2 for the guard; reuse fixtures from `tests/sdk/test_m8_statistics_exports.py` and `tests/sdk/test_m6_comparison_service.py`.

**Interfaces:** `StatisticalReportService(store: Any)` exposes `publish(baseline_run_id: str, candidate_run_id: str, *, allowed_factors: list[str] | tuple[str, ...], baseline_pass_id: str | None = None, candidate_pass_id: str | None = None, k: int = 1) -> dict[str, Any]` and `get(report_id: str) -> dict[str, Any]` (`KeyError` when absent). `statistical_publication_guard(store: Any) -> ContextManager[None]` raises existing `MaintenanceConflict` on exclusion/active metadata.

- [ ] **Red:** Add `test_publish_captures_once_and_get_never_recomputes`: spy sees exactly one `paired_statistics` call; body result equals that returned snapshot; publish twice unchanged returns identical envelope. Move current and change Case metering, then replace calculator with a raising stub: `service.get(id) == original`. Publishing changed metering with fixed old passes produces a different ID.
- [ ] **Red:** Add parameterized `test_invalid_metering_remains_visible` for `None`, NaN, ±Infinity, negative, Boolean, numeric string, missing currency, all-missing and mixed currencies; stored result equals existing calculation, missing counts unchanged, strict JSON succeeds, no inferred USD/zero/Judge cost. Add Terminal-Bench `k=2` qualified/missing/reused-source-Trial fixtures preserving `trial_aggregation`, not counting retries.
- [ ] **Red:** Add `test_current_moves_during_capture`: existing switching-current fixture still binds both refs and calculation to the first resolved Pass pair; invalid Pass ownership/missing Pass/k produces no row. Add `test_publication_guard_releases_on_exception` and maintenance-vs-publication two-process exclusion tests.
- [ ] Run `uv run pytest -q tests/sdk/test_statistical_reports.py`; expect missing-service failures.
- [ ] Implement publication as request validation → guard → one existing calculation → deep-copy stored policy/result → identity/validation → repository put. Reads only call repository get. Guard uses existing shared `file_lock(db + ".maintenance.lock")` on SQLite, a live dedicated connection holding `pg_try_advisory_lock_shared(hashtext('motteavl:maintenance'))` on PG, and shared RunStore RLock on Memory. Check active maintenance metadata after acquiring; release in `finally`. Hold the guard through insertion so GC/rollback cannot finish between capture and pinning; do not call `begin_maintenance` or waive write triggers.
- [ ] Run this file plus `tests/sdk/test_m8_statistics_exports.py tests/sdk/test_m6_comparison_service.py tests/cli/test_compare_statistics_consistency.py`; require PASS. Scoped review: fixed refs, unchanged mathematics, guard race ordering, zero Runner/Judge calls. Commit: `feat(sdk): publish frozen statistical calculations`.

### Task 4: Include publication pins in the unified evidence closure

**Files:** Modify `packages/storage/motte_storage/artifact_refs.py`, `packages/storage/motte_storage/maintenance.py` (`_store_counts`), `tests/security/test_gc_retention.py`, `tests/migration/test_import_resume_rollback.py`, `tests/integration/test_backup_restore_consistency.py`, `tests/storage/test_maintenance.py`; create `tests/storage/test_statistical_report_references.py`.

**Interfaces:** Extend `_iter_artifact_records_with_owners` to yield every report as independently owned `(report, None, None)`, with `statistical_reports.list(limit=None)`; a missing repository on a supported RunStore is an error, not an empty set. Add `collect_referenced_pass_ids(value: Any, found: set[str]) -> None` and `referenced_pass_ids(store: Any, *, exclude_run_ids: Iterable[str] = (), exclude_import_ids: Iterable[str] = ()) -> set[str]`; detect `scoring_pass_id`, `source_pass_id`, `previous_pass_id`, and `{target_type: "scoring_pass", target_id: ...}`. Existing Run and Artifact scanners remain the common authority.

- [ ] **Red:** Add `test_report_pins_survive_owned_import_exclusion`: report refs retain both Run IDs and selected Pass IDs even when those Runs/import mappings are excluded; rollback returns the referenced Run/artifact as blocked and neither deactivates nor deletes them. Unreferenced imported control evidence still follows existing rollback policy.
- [ ] **Red:** Add `test_all_reports_scanned_and_failure_aborts`: >100 reports and a sentinel report beyond the previous list cap remain protected; a list/get integrity/read failure makes GC/rollback/backup fail with zero deletions and no successful backup. Nested artifact path/hash conflicts propagate.
- [ ] **Red:** Add `test_publication_blocks_gc_apply_race`: pause calculation while guard is held, maintenance acquisition refuses; after publication GC scan protects its evidence. Reverse ordering: maintenance owns lock first, publication rejects with zero new rows. Exercise real SQLite processes and PG connections.
- [ ] **Red:** Add `test_report_backup_restore_and_barrier`: report count appears in manifest; restored envelope and associated evidence match exactly; missing/mismatched pinned artifact rejects backup/restore. Direct repository and raw SQL inserts during SQLite/PG maintenance cannot write; report get remains readable. No maintenance bypass or new mutable metadata is added.
- [ ] Run `uv run pytest -q tests/storage/test_statistical_report_references.py tests/security/test_gc_retention.py tests/migration/test_import_resume_rollback.py tests/integration/test_backup_restore_consistency.py tests/storage/test_maintenance.py`; expect pin/count assertions to fail.
- [ ] Implement traversal and `statistical_reports` count. The eager table from Task 2 must be included in existing table-enumerated triggers/PG locks and SQLite backup/PG dump without a special allow flag. Propagate repository failures; do not catch and return an empty pin set. Reuse existing rollback Run pin logic instead of creating a second deletion policy. Trace's later plan consumes the exported Run/Pass collectors; do not implement Trace retention here.
- [ ] Run the same command plus Task 2 PG tests; require PASS. Scoped review: independent ownership, scan completeness, backup closure and maintenance bypass attempts. Commit: `fix(storage): preserve statistical publication evidence`.

### Task 5: Expose explicit API, SDK and CLI publication/export

**Files:** Modify `apps/api/app/main.py`, `packages/sdk-python/motte_sdk/client.py`, `packages/sdk-python/motte_sdk/export.py`, `packages/cli/motte_cli/main.py`; create `tests/api/test_statistical_reports.py`, `tests/cli/test_statistical_reports.py`; extend `tests/sdk/test_client_contract.py`; update `docs/protocols/experiments-and-comparison.md`, `docs/protocols/sdk-and-migration.md`, `README.md`, generated `api/openapi.json` and `apps/web/src/api/schema.d.ts`.

**Interfaces:** POST `/api/v1/statistical-reports` takes Task 1 request and returns stored envelope (HTTP 200 for initial publication and replay); GET `/api/v1/statistical-reports/{report_id}?format=json|junit` reads the same envelope. No public list/update/delete route. SDK `publish_statistical_report(baseline_run_id, candidate_run_id, *, allowed_factors=("model",), baseline_pass_id=None, candidate_pass_id=None, k=1) -> dict`, `get_statistical_report(report_id: str) -> dict`, `export_statistical_report(report_id: str, format: str = "json") -> dict | str`; keep existing transport/error/retry conventions.

- [ ] **Red:** Add `test_api_publication_and_errors`: POST→GET equality; invalid/extra body fields, k, format and bad ownership map 422; absent Run/Pass/report map 404; immutable conflict 409; corruption 409 with `STATISTICAL_REPORT_CORRUPT`; maintenance 503. PUT/PATCH/DELETE are 405; no public arbitrary-body import. Existing dynamic GET/compare leave report count zero and incur no provider calls.
- [ ] **Red:** Add `test_json_junit_have_identical_stored_envelope`: `json.loads(ET.fromstring(xml).findtext("system-out")) == json_export == published`; inapplicable remains skipped, no quality Gate assertion; JUnit properties retain original statistical fields plus report ID/schema/time. Repeat after current/metering drift and replace calculator with raising stub; API/SDK/CLI exports still match.
- [ ] **Red:** Add local `--db` and remote `--server` CLI tests for `motte statistical-report publish --baseline B --candidate C [--factors model] [--baseline-pass P] [--candidate-pass Q] [--k 1]`, `get ID`, and `export ID --format json|junit [--output PATH]`. Assert stdout/file parity, persisted restart retrieval, exit 0 even for inapplicable valid reports, existing `_error` exit 2 for invalid requests, with no partially written export file. SDK POST is not silently retried after response loss; explicit same fixed-input replay returns original envelope.
- [ ] Run `uv run pytest -q tests/api/test_statistical_reports.py tests/cli/test_statistical_reports.py tests/sdk/test_client_contract.py`; expect missing route/client/parser/export failures.
- [ ] Implement service wiring and request/response models, pure `statistical_report_to_json(report: Mapping[str, Any]) -> dict[str, Any]` and `statistical_report_to_junit(report: Mapping[str, Any]) -> str`. JUnit reuses `statistics_to_junit(body.result)`, replacing system-out with the full validated envelope and appending publication properties. SDK export GETs the stored envelope then uses these exporters; no evaluator or current lookup. CLI uses the same service/exporters locally and client remotely; publish defaults match existing compare factors/k.
- [ ] Update protocol sections that currently say persistent reports are unimplemented, document identity/timestamp/request/error/export semantics and no automatic publication; leave unrelated M8 live acceptance qualifications intact. Run `make openapi` and inspect only expected route/schema/type changes.
- [ ] Run the same tests plus `tests/cli/test_m8_statistics_junit.py tests/cli/test_compare_statistics_consistency.py` and `make openapi-check`; require PASS. Scoped review: read/write separation, source integrity, HTTP contracts, byte/semantic export parity. Commit: `feat(api): expose statistical report publication and export`.

### Task 6: Verify the integrated persistent publication slice

**Files:** Existing tests/docs above; update evidence notes in `docs/superpowers/plans/2026-09-23-m8-delivery-closure-execution.md` only with observed results.

**Interfaces:** Consumes Tasks 1–5; produces a verified source commit and precise remaining-environment list, not a claim that all M8 work is accepted.

- [ ] Run all new contract/storage/service/API/CLI tests on one stable HEAD; run real PG tests with a verified disposable loopback cluster. Record exact commands, HEAD, counts and any skips; unavailable PG is a blocker until CI supplies real PG evidence.
- [ ] Run `make check`, `make wheels`, `uv run pytest -q tests/packaging`, and `make audit`; use the project's existing environment recovery policy for available tooling, never suppress a failing gate or call a skipped backend verified.
- [ ] Independently review immutable-body/identity logic, PG races, rollback/backup pin closure, maintenance guard lifetime, and unchanged dynamic read-only paths. Re-run affected tests after fixes and the full gates after the last source change.
- [ ] With parent authorization, include scoped commits in the draft PR, verify remote HEAD equals reviewed HEAD, and wait for that HEAD's CI to reach terminal results. Do not merge, deploy, call paid models, export private data, or equate this slice with real Judge/runner acceptance.

## Handoff and dependencies

- Dependency order: 1 → 2 → 3 → 4 → 5 → 6. No source implementation until user plan review; task commits described above are future execution steps, not part of writing this plan.
- Statistical reports do not depend on Judge/Trace implementations. Judge 0017 depends on migration 0016; Trace can consume `store.statistical_reports`, full report enumeration and `referenced_pass_ids` after Task 4. Common-file edits are coordinator-owned integration points.
- Self-review complete: section A covers immutable publication, full stored inputs/policy/results, all backends/migration, API/SDK/CLI export, unified protection, replay/concurrency/corruption/drift/invalid data and backup/rollback. Each Review Focus condition has explicit test assertions above.
- Recommended execution: subagent-driven, with the six scoped review gates above and one final integrated review; the data-retention and multi-backend race boundaries warrant independent review.
