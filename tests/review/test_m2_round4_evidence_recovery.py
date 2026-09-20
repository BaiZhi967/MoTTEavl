"""M2-R4-02: interrupted imports retain frozen evidence across durable recovery."""
import hashlib
import json
import shutil
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from motte_benchmark.fake_runner import self_argv
from motte_benchmark.opencompass.adapter import CevalJobAdapter
from motte_benchmark.opencompass.parser import PARSER_VERSION
from motte_contracts.external_job import ExternalJobHandle
from motte_sdk.benchmark_catalog import prepare_external_dataset, prepare_external_run_inputs
from motte_sdk.external_jobs import DurableExternalJobRunner, ExternalJobSupervisor
from motte_storage.artifacts import ArtifactStore
from motte_storage.external_jobs import SQLiteExternalJobs


def _run_input():
    rows = [
        {"id": f"logic-{i}", "subject": "logic", "question": "Which answer?",
         "A": "one", "B": "two", "C": "three", "D": "four", "answer": "B",
         "split": "val"}
        for i in range(1, 5)
    ]
    dataset = prepare_external_dataset(
        files={"data.jsonl": "\n".join(json.dumps(row) for row in rows).encode()},
        dataset_revision="r4-evidence-recovery",
    )
    inputs = prepare_external_run_inputs(
        dataset, benchmark_id="ceval", model_id="offline-model",
        model_record={"id": "offline-model", "provider": "openai", "model": "mock-model",
                      "parameters": {}, "lifecycle": "published", "context_window": 8192},
    )
    return {"id": "run-r4-evidence", "manifest": inputs["manifest"],
            "case_ids": inputs["case_ids"]}


def _runner(tmp_path, mode):
    jobs = SQLiteExternalJobs(str(tmp_path / "jobs.db"))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    runner = DurableExternalJobRunner(
        ExternalJobSupervisor(CevalJobAdapter(
            argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": mode},
        ), poll_interval_seconds=0.01),
        jobs, artifacts=artifacts, work_root=tmp_path / "jobs",
        parser_version=PARSER_VERSION,
    )
    runner.bind_store(SimpleNamespace(external_jobs=jobs))
    return runner, jobs, artifacts


@pytest.mark.parametrize("mode, expected_status", [
    ("opencompass_ok", "settled"),
    ("opencompass_partial", "failed"),
])
def test_interrupted_import_preserves_evidence_and_replays_after_restart(
    tmp_path, monkeypatch, mode, expected_status,
):
    run = _run_input()
    runner, jobs, artifacts = _runner(tmp_path, mode)
    real_import = jobs.import_record
    calls = 0

    def interrupt_second_import(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("interrupted import")
        return real_import(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(jobs, "import_record", interrupt_second_import)
        with pytest.raises(OSError, match="interrupted import"):
            runner(run)

    job = jobs.jobs_for_run(run["id"])[0]
    checkpoint = job["checkpoint"]
    evidence = deepcopy(checkpoint["evidence"])
    assert checkpoint["import_completed"] is False
    assert len(jobs.list_records(job["job_id"])) == 1
    assert evidence["complete"] is True
    assert evidence["frozen_before_parse"] is True
    assert evidence["raw_files"]
    original_bytes = {
        key: artifacts.read_bytes(evidence[key])
        for key in ("raw_bundle_artifact", "outcome_artifact")
    }
    frozen_outcome = json.loads(original_bytes["outcome_artifact"])
    assert frozen_outcome["job_status"] == expected_status
    if expected_status == "failed":
        assert frozen_outcome["error"]["code"] == "JOB_NONZERO_EXIT"
    shutil.rmtree(job["handle"]["work_dir"])

    # New storage and runner instances must recover solely through frozen artifacts.
    recovery, jobs, artifacts = _runner(tmp_path, mode)
    recovered = recovery(run)
    assert recovered["recovered_from"] == "frozen-artifact"
    assert recovered["import"]["evidence"] == evidence
    assert recovered["import"]["artifact"] == evidence["outcome_artifact"]
    assert recovered["job_status"] == expected_status
    assert recovered["error"] == frozen_outcome["error"]
    assert recovered["results"] == frozen_outcome["results"]
    assert recovered["import"]["imported"] == len(frozen_outcome["results"]) - 1
    finalized = jobs.get_job(job["job_id"])
    assert finalized["status"] == expected_status
    assert finalized["checkpoint"]["import_completed"] is True
    assert finalized["checkpoint"]["evidence"] == evidence
    assert finalized["checkpoint"]["outcome_error"] == frozen_outcome["error"]

    again, jobs, artifacts = _runner(tmp_path, mode)
    replay = again(run)
    assert replay["recovered_from"] == "job-store"
    assert replay["import"]["imported"] == 0
    assert replay["import"]["evidence"] == evidence
    assert replay["error"] == frozen_outcome["error"]
    assert replay["results"] == recovered["results"]
    assert jobs.get_job(job["job_id"])["checkpoint"]["evidence"] == evidence
    assert len(jobs.jobs_for_run(run["id"])) == 1
    assert not Path(job["handle"]["work_dir"]).exists()

    for key, content in original_bytes.items():
        assert artifacts.read_bytes(replay["import"]["evidence"][key]) == content
    assert hashlib.sha256(original_bytes["outcome_artifact"]).hexdigest() == evidence["outcome_sha256"]
    bundle = json.loads(original_bytes["raw_bundle_artifact"])
    canonical = json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert "sha256:" + hashlib.sha256(canonical).hexdigest() == evidence["raw_bundle_hash"]
    files = {rel: entry["content"] for rel, entry in bundle["files"].items()}
    reparsed, _ = again.supervisor.adapter.collect_from_files(
        ExternalJobHandle.model_validate(job["handle"]), {}, files,
    )
    assert [record.model_dump(mode="json") for record in reparsed] == recovered["results"]


def test_completed_failed_job_preserves_error_on_job_store_recovery(tmp_path):
    run = _run_input()
    runner, jobs, _ = _runner(tmp_path, "opencompass_partial")
    outcome = runner(run)
    assert outcome["job_status"] == "failed"
    assert outcome["error"]["code"] == "JOB_NONZERO_EXIT"
    job = jobs.jobs_for_run(run["id"])[0]
    assert job["checkpoint"].get("outcome_error") == outcome["error"]
    shutil.rmtree(job["handle"]["work_dir"])
    recovery, _, _ = _runner(tmp_path, "opencompass_partial")
    recovered = recovery(run)
    assert recovered["recovered_from"] == "job-store"
    assert recovered["error"] == outcome["error"]
    assert recovered["import"]["evidence"] == outcome["import"]["evidence"]
