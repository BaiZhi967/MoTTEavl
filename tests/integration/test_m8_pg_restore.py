"""M8 T09: disposable PostgreSQL dump/restore with frozen evidence references.

This test never restores into the configured source database. Both source and
destination are random task-owned databases on a loopback test cluster.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from motte_sdk.comparisons import ComparisonService
from motte_sdk.service import RunService
from motte_storage.artifacts import ArtifactStore
from motte_storage.factory import create_run_store
from motte_storage.maintenance import (
    MAINTENANCE_FLAG,
    MAINTENANCE_STARTED_AT,
    consistent_backup_postgres,
    maintenance_status,
)
from motte_storage.migrations import upgrade
from motte_storage.platform import platform_for
from apps.worker.motte_worker.runtime import WorkerLoop
from tests.storage.conftest import isolated_database_uri, require_loopback_pg_cluster


@pytest.fixture
def disposable_pg_pair():
    import psycopg
    from psycopg import sql

    raw = os.environ.get("MOTTE_PG_DSN")
    if not raw:
        pytest.skip("MOTTE_PG_DSN absent; no disposable PostgreSQL test cluster")
    try:
        admin_dsn = require_loopback_pg_cluster(raw)
    except ValueError as error:
        pytest.skip(str(error))
    names = ["m8_test_restore_" + uuid4().hex for _ in range(2)]
    created: list[str] = []
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            for name in names:
                admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
                created.append(name)
        targets = tuple(isolated_database_uri(admin_dsn, name) for name in names)
        for name, target_dsn in zip(names, targets, strict=True):
            with psycopg.connect(target_dsn) as target, target.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                assert cursor.fetchone()[0] == name
        yield targets
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            for name in created:
                assert name.startswith("m8_test_restore_")
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.mark.skipif(
    not shutil.which("pg_dump") or not shutil.which("pg_restore"),
    reason="pg_dump and pg_restore required for PostgreSQL recovery exercise",
)
def test_pg_dump_restore_preserves_run_trial_pass_baseline_and_artifact(
    disposable_pg_pair: tuple[str, str], tmp_path: Path,
) -> None:
    source_dsn, staging_dsn = disposable_pg_pair
    upgrade(source_dsn)
    source = create_run_store(storage="postgres", dsn=source_dsn)
    run_id = "m8-restore-run"
    source.runs.create({
        "id": run_id, "schema_version": 2, "revision": 1,
        "scenario_version": "replay@1", "status": "completed",
        "manifest": {"evaluation": {"scorer_id": "deterministic", "scorer_version": "1"}},
        "requested_manifest": {}, "case_ids": ["task-a"],
        "created_at": "2026-09-23T00:00:00Z", "updated_at": "2026-09-23T00:00:00Z",
    })
    source.runs.create({
        "id": "m8-restore-queued", "schema_version": 2, "revision": 1,
        "scenario_version": "replay@1", "status": "queued",
        "manifest": {}, "requested_manifest": {}, "case_ids": ["task-b"],
        "created_at": "2026-09-23T00:00:00Z", "updated_at": "2026-09-23T00:00:00Z",
    })
    source.trials.create_plans([{
        "trial_id": "m8-restore-trial", "run_id": run_id,
        "task_key": "task-a", "repeat_index": 0,
        "agent_config_hash": "sha256:agent", "environment_hash": "sha256:env",
    }])
    source.scoring_passes.append({
        "id": "m8-restore-pass", "run_id": run_id,
        "scorer_id": "deterministic", "scorer_version": "1",
        "created_at": "2026-09-23T00:00:00Z", "source": "initial",
        "source_run_revision": 1, "summary": {},
    }, [{
        "case_id": "task-a", "metric_id": "accuracy",
        "evaluator_id": "deterministic", "evaluator_version": "1",
        "metric_status": "scored", "value": 1.0, "passed": True,
        "denominator": True, "details": {},
    }])
    artifact_root = tmp_path / "artifacts"
    artifact = ArtifactStore(artifact_root).put_bytes("m8/task-a/evidence.txt", b"fixed-evidence")
    source.case_runs.upsert({
        "run_id": run_id, "case_id": "task-a",
        "artifact_refs": [{"id": artifact.id, "kind": "file",
                           "sha256": artifact.sha256, "uri": artifact.uri}],
    })
    baseline = ComparisonService(source).create_baseline(
        "m8-restore-baseline",
        [{"run_id": run_id, "scoring_pass_id": "m8-restore-pass"}],
        policy={"allowed_factors": ["model"]}, created_by="m8-test",
        reason="disposable recovery verification",
    )
    backup_dir = tmp_path / "backup"
    manifest = consistent_backup_postgres(
        source_dsn, backup_dir, artifacts_root=artifact_root, store=source,
    )
    assert manifest["status"] == "complete"
    assert manifest["counts"]["runs"] == 2
    assert manifest["counts"]["scoring_passes"] == 1
    assert manifest["counts"]["baselines"] == 1
    assert maintenance_status(source)["active"] is False
    dump = backup_dir / manifest["database"]["snapshot"]
    completed = subprocess.run(
        ["pg_restore", "--exit-on-error", "--dbname", staging_dsn, str(dump)],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr[:1000]
    restored = create_run_store(storage="postgres", dsn=staging_dsn)
    assert restored.runs.get(run_id) == source.runs.get(run_id)
    assert restored.runs.get("m8-restore-queued")["status"] == "queued"
    assert restored.trials.list_for_run(run_id) == source.trials.list_for_run(run_id)
    assert restored.scoring_passes.list_for_run(run_id) == source.scoring_passes.list_for_run(run_id)
    assert restored.score_sets.list_for_pass("m8-restore-pass") == source.score_sets.list_for_pass("m8-restore-pass")
    assert restored.baseline_store.get("m8-restore-baseline") == baseline
    copied = backup_dir / manifest["artifacts"]["dir"] / artifact.id
    assert copied.read_bytes() == b"fixed-evidence"
    assert manifest["artifacts"]["files"][0]["sha256"] == artifact.sha256
    # pg_restore is currently a manual operation. The test sets the same
    # guard an operator must set before any Worker is allowed near staging.
    meta = platform_for(restored).meta
    meta.set("restored_from_backup", "m8-manual-pg-restore")
    # The dump captures maintenance=active. Remove that inherited flag while
    # the restore guard is held, so the assertion tests the guard itself.
    meta.delete(MAINTENANCE_FLAG)
    meta.delete(MAINTENANCE_STARTED_AT)
    assert meta.get("restored_from_backup") is not None
    worker = WorkerLoop(RunService(restored), execution_lock_held=True, scoring_jobs=None)
    assert worker._platform_block_reason() == "restore_guard_active"
    assert worker.run_once() is None
    assert restored.runs.get("m8-restore-queued")["status"] == "queued"
