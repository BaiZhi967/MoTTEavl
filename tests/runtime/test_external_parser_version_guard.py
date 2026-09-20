"""Parser upgrades cannot relabel/reparse frozen Jobs or repeat their side effects."""
from copy import deepcopy

import pytest

from motte_contracts.external_job import ExternalJobHandle
from motte_sdk.execution_backends import external_job_spec_from_run
from motte_sdk.external_jobs import DurableExternalJobRunner, ExternalJobSupervisor
from motte_storage.artifacts import ArtifactStore
from motte_storage.external_jobs import SQLiteExternalJobs

OLD = "ceval-opencompass-parser@1"
NEW = "ceval-opencompass-parser@2"


class NoExecutionAdapter:
    parser_version = NEW

    def forbidden(self, *args, **kwargs):
        raise AssertionError("version mismatch must not execute or reparse")

    prepare = start = poll = interrupt = collect = collect_from_files = read_output_files = forbidden


def _setup(tmp_path, *, frozen=OLD, seeded=True, status="active", complete=False):
    run = {"id": "versioned-run", "case_ids": ["case-1", "case-2"], "manifest": {
        "external_benchmark": {
            "adapter_id": "ceval-opencompass", "adapter_version": "1",
            "runner_version": "0.4.2", "dataset_revision": "fixture-rev",
            "environment_digest": "fixture-env", "parser_version": frozen,
            "profile": {"benchmark_id": "ceval", "benchmark_version": "1"},
        },
    }}
    jobs = SQLiteExternalJobs(str(tmp_path / "jobs.db"))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    runner = DurableExternalJobRunner(
        ExternalJobSupervisor(NoExecutionAdapter()), jobs, artifacts=artifacts,
        work_root=tmp_path / "work", parser_version=frozen,
    )
    if seeded:
        handle = ExternalJobHandle(
            run_id=run["id"], job_id="old-job",
            launch_token="old-token", work_dir=str(tmp_path / "deleted"),
            status=status,
        )
        artifact = artifacts.put_bytes("original.json", b'{"files":{"output":{"content":"old"}}}')
        jobs.begin_job({
            "job_id": handle.job_id, "run_id": run["id"], "status": status,
            "launch_token": handle.launch_token, "handle": handle.model_dump(mode="json"),
            "checkpoint": {
                "import_completed": complete, "outcome_status": "settled",
                "outcome_error": {"code": "ORIGINAL_DIAGNOSTIC"},
                "cursor": {"parser_version": OLD},
                "evidence": {"raw_bundle_artifact": artifact.id, "raw_bundle_hash": artifact.sha256},
            },
        })
        jobs.import_record("old-job", "case-1", OLD, "old-hash", {"stable_case_key": "case-1"})
    return run, runner, jobs, artifacts


@pytest.mark.parametrize("status", ["active", "settled", "cancelled"])
def test_partial_old_job_refuses_upgrade_without_changing_facts(tmp_path, status):
    run, runner, jobs, artifacts = _setup(tmp_path, status=status)
    before = deepcopy(jobs.get_job("old-job"))
    records = jobs.list_records("old-job")
    outcome = runner(run)
    assert outcome["job_status"] == ("cancelled" if status == "cancelled" else "indeterminate")
    assert outcome["error"]["code"] == "EXTERNAL_PARSER_VERSION_MISMATCH"
    assert outcome["results"] == []
    assert outcome["import"]["evidence"] == before["checkpoint"]["evidence"]
    assert jobs.get_job("old-job") == before
    assert jobs.list_records("old-job") == records
    assert artifacts.read_bytes("original.json") == b'{"files":{"output":{"content":"old"}}}'


def test_new_run_with_old_frozen_version_does_not_launch(tmp_path):
    run, runner, jobs, _ = _setup(tmp_path, seeded=False)
    outcome = runner(run)
    assert outcome["job_status"] == "indeterminate"
    assert outcome["error"]["code"] == "EXTERNAL_PARSER_VERSION_MISMATCH"
    assert jobs.jobs_for_run(run["id"]) == []


@pytest.mark.parametrize("frozen", [OLD, NEW])
def test_completed_old_job_remains_read_only_without_reparse(tmp_path, frozen):
    run, runner, jobs, _ = _setup(tmp_path, frozen=frozen, status="settled", complete=True)
    before = deepcopy(jobs.get_job("old-job"))
    records = jobs.list_records("old-job")
    outcome = runner(run)
    assert outcome["job_status"] == "settled"
    assert outcome["error"] == {"code": "ORIGINAL_DIAGNOSTIC"}
    assert outcome["recovered_from"] == "job-store"
    assert outcome["results"] == [{"stable_case_key": "case-1"}]
    assert outcome["import"]["imported"] == 0
    assert outcome["import"]["parser_version"] == OLD
    assert outcome["import"]["evidence"] == before["checkpoint"]["evidence"]
    assert jobs.get_job("old-job") == before
    assert jobs.list_records("old-job") == records


@pytest.mark.parametrize("actual", [NEW, None])
def test_actual_cursor_cannot_label_new_results_as_old_parser(tmp_path, actual):
    run, runner, jobs, _ = _setup(tmp_path)
    runner.supervisor.adapter.parser_version = OLD
    before = deepcopy(jobs.get_job("old-job"))
    records = jobs.list_records("old-job")
    spec = external_job_spec_from_run(run, work_root=str(tmp_path / "work"))
    outcome = runner._settle(spec, {
        "job_status": "settled", "handle": before["handle"],
        "results": [{"stable_case_key": "case-2"}],
        "cursor": {"parser_version": actual},
    })
    assert outcome["job_status"] == "indeterminate"
    assert outcome["error"]["code"] == "EXTERNAL_PARSER_VERSION_MISMATCH"
    assert jobs.get_job("old-job") == before
    assert jobs.list_records("old-job") == records


def test_checkpoint_parser_mismatch_blocks_reparse_even_if_adapter_matches(tmp_path):
    run, runner, jobs, _ = _setup(tmp_path, frozen=NEW)
    before = deepcopy(jobs.get_job("old-job"))
    outcome = runner(run)
    assert outcome["job_status"] == "indeterminate"
    assert outcome["error"]["code"] == "EXTERNAL_PARSER_VERSION_MISMATCH"
    assert jobs.get_job("old-job") == before


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_empty_results_without_cursor_version_preserve_error_and_cancellation(tmp_path, status):
    run, runner, jobs, _ = _setup(tmp_path, status=status)
    runner.supervisor.adapter.parser_version = OLD
    before = jobs.get_job("old-job")
    error = {"code": "JOB_COLLECT_FAILED", "message": "nothing parseable"}
    outcome = runner._settle(
        external_job_spec_from_run(run, work_root=str(tmp_path / "work")),
        {"job_status": status, "error": error, "results": [], "cursor": {},
         "handle": before["handle"], "recovered_from": "frozen-artifact"},
    )
    assert outcome["job_status"] == status
    assert outcome["error"] == error
    assert outcome["import"]["imported"] == 0
    assert jobs.get_job("old-job")["status"] == status


def test_legacy_adapter_without_declared_parser_accepts_explicit_version(tmp_path):
    from motte_benchmark.fake_runner import self_argv
    from motte_benchmark.process import ProcessJobAdapter

    run, runner, jobs, _ = _setup(tmp_path, frozen="fixture@1", seeded=False)
    run["case_ids"] = ["s-a:1", "s-a:2", "s-a:3", "s-a:4"]
    runner.supervisor = ExternalJobSupervisor(
        ProcessJobAdapter(argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": "ok"}),
        poll_interval_seconds=0.01,
    )
    outcome = runner(run)
    assert outcome["job_status"] == "settled"
    records = jobs.list_records(outcome["handle"]["job_id"])
    assert len(records) == 4
    assert {record["parser_version"] for record in records} == {"fixture@1"}
