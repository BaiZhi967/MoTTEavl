"""M2-T01：外部 Job 数据契约与 sample/job 分派隔离。

反例与期望：
- 旧 sample manifest（无 execution_mode）保持可读、可分派，逐题 invoke。
- job 模式 Run 只调用一次 Job 入口，携带全部 selected case，不逐题 invoke。
- 缺 runner/profile/environment 版本的外部配置在创建/分派前被拒绝，0 次 Job 调用。
"""
import pytest
from pydantic import ValidationError

from motte_contracts.external_job import (
    ExternalJobHandle,
    ExternalJobSpec,
    ExternalJobStatus,
    ImportBatch,
    NormalizedCaseResult,
    new_launch_token,
)
from motte_contracts.run import ExecutionSpec
from motte_sdk.dispatcher import RunDispatcher
from motte_sdk.execution_backends import (
    ExecutionBackendError,
    ExecutionBackendSpec,
    ExecutionHandle,
    external_job_spec_from_run,
    legacy_execution,
    register_backend,
    resolve_execution,
    unregister_backend,
    validate_external_job_manifest,
)
from motte_sdk.service import RunService
from motte_storage.run_store import InMemoryRunStore


COMPLETE_EXTERNAL_CONFIG = {
    "adapter_id": "fake-job-benchmark",
    "adapter_version": "1",
    "runner_version": "fake-runner-1.0.0",
    "dataset_revision": "ceval-fixture-rev-1",
    "environment_digest": "sha256:" + "0" * 64,
    "profile": {"benchmark_id": "fake-bench", "benchmark_version": "1"},
    "retry_policy": {"runner": 0, "provider_transport": 0, "operator": 0},
    "limits": {"max_wall_seconds": 600},
}

REPLAY_MANIFEST = {
    "provider": {"kind": "replay", "fixture": {"case-1": {"output": 1, "expected": 1}}},
    "execution": {
        "backend_id": "replay",
        "backend_version": "1",
        "capabilities": {"interactive": False, "safe_to_repeat": True},
    },
}


def _job_manifest(config: dict) -> dict:
    return {
        "execution": {"backend_id": "fake-job-benchmark", "backend_version": "1"},
        "external_benchmark": config,
    }


def _fake_job_backend(job_calls: list, invoke_calls: list) -> ExecutionBackendSpec:
    def build(run: dict) -> ExecutionHandle:
        def run_job(dispatched: dict) -> dict:
            job_calls.append({
                "run_id": dispatched["id"],
                "case_ids": list(dispatched.get("case_ids") or []),
            })
            return {
                "job_status": "settled",
                "results": [
                    {"case_id": "s-a:1", "status": "succeeded",
                     "output": {"prediction": "A"}},
                    {"case_id": "s-a:2", "status": "succeeded",
                     "output": {"prediction": "B"}},
                    {"case_id": "s-a:3", "status": "failed",
                     "error": {"code": "RUNNER_NONZERO_EXIT", "message": "exit 1"}},
                ],
            }

        return ExecutionHandle(
            backend_id="fake-job-benchmark",
            backend_version="1",
            invoke=lambda case_id: invoke_calls.append(case_id),
            execution_mode="job",
            run_job=run_job,
        )

    return ExecutionBackendSpec(
        id="fake-job-benchmark",
        version="1",
        validate=validate_external_job_manifest,
        build=build,
        capabilities={"interactive": False, "safe_to_repeat": False},
        execution_mode="job",
    )


def test_sample_and_job_dispatch_are_distinct():
    job_calls: list = []
    job_invoke_calls: list = []
    sample_invoke_calls: list = []
    register_backend(_fake_job_backend(job_calls, job_invoke_calls))
    register_backend(ExecutionBackendSpec(
        id="fake-sample",
        version="1",
        validate=lambda manifest: None,
        build=lambda run: ExecutionHandle(
            backend_id="fake-sample",
            backend_version="1",
            invoke=lambda case_id: (
                sample_invoke_calls.append(case_id), {"output": case_id},
            )[1],
        ),
    ))
    try:
        service = RunService(InMemoryRunStore())
        run = service.create_run(
            "fake-suite@1",
            _job_manifest(dict(COMPLETE_EXTERNAL_CONFIG)),
            case_ids=["s-a:1", "s-a:2", "s-a:3", "s-a:4"],
        )
        # API 只创建不启动：排队中、Job 未启动。
        assert service.get_run(run["id"])["status"] == "queued"
        assert job_calls == []

        completed = RunDispatcher(service).dispatch(run["id"])
        assert [call["run_id"] for call in job_calls] == [run["id"]]
        assert job_calls[0]["case_ids"] == ["s-a:1", "s-a:2", "s-a:3", "s-a:4"]
        assert job_invoke_calls == []
        by_case = {row["case_id"]: row["outcome"] for row in completed["cases"]}
        assert by_case == {
            "s-a:1": "responded",
            "s-a:2": "responded",
            "s-a:3": "call_failed",
            "s-a:4": "not_attempted",
        }
        # 非零退出 + 部分结果：终态 failed，但每个 selected case 都有处置状态。
        assert completed["status"] == "failed"

        # sample 模式保持逐题 invoke，不触碰 Job 入口。
        sample = service.create_run(
            "fake-suite@1",
            {"execution": {"backend_id": "fake-sample", "backend_version": "1"}},
            case_ids=["c-1", "c-2"],
        )
        sample_done = RunDispatcher(service).dispatch(sample["id"])
        assert sample_invoke_calls == ["c-1", "c-2"]
        assert sample_done["status"] == "completed"
        assert len(job_calls) == 1
    finally:
        unregister_backend("fake-job-benchmark", "1")
        unregister_backend("fake-sample", "1")


def test_job_dispatch_rejects_unpinned_config_without_starting():
    job_calls: list = []
    register_backend(_fake_job_backend(job_calls, []))
    try:
        service = RunService(InMemoryRunStore())
        broken = {
            key: value for key, value in COMPLETE_EXTERNAL_CONFIG.items()
            if key != "runner_version"
        }
        run = service.create_run(
            "fake-suite@1", _job_manifest(broken), case_ids=["s-a:1"],
        )
        result = RunDispatcher(service).dispatch(run["id"])
        assert result["status"] == "unsupported"
        assert result["error"]["code"] == "EXTERNAL_JOB_VERSION_REQUIRED"
        assert "runner_version" in result["error"]["message"]
        assert job_calls == []
    finally:
        unregister_backend("fake-job-benchmark", "1")


def test_external_job_manifest_requires_pinned_runner_profile_environment():
    manifest = _job_manifest(dict(COMPLETE_EXTERNAL_CONFIG))
    validate_external_job_manifest(manifest)
    for field in ("runner_version", "environment_digest", "dataset_revision", "profile"):
        broken = _job_manifest({
            key: value for key, value in COMPLETE_EXTERNAL_CONFIG.items() if key != field
        })
        with pytest.raises(ExecutionBackendError) as excinfo:
            validate_external_job_manifest(broken)
        assert excinfo.value.code == "EXTERNAL_JOB_VERSION_REQUIRED"
    with pytest.raises(ExecutionBackendError, match="benchmark_version"):
        validate_external_job_manifest(_job_manifest({
            **COMPLETE_EXTERNAL_CONFIG,
            "profile": {"benchmark_id": "fake-bench"},
        }))
    with pytest.raises(ExecutionBackendError, match="adapter_id"):
        validate_external_job_manifest(
            _job_manifest({k: v for k, v in COMPLETE_EXTERNAL_CONFIG.items()
                           if k not in ("adapter_id", "adapter_version")})
        )


def test_external_job_spec_requires_pinned_versions_and_selection():
    base = dict(
        run_id="run-1",
        adapter_id="a",
        adapter_version="1",
        runner_version="runner-1.0.0",
        execution_config_hash="hash-1",
        dataset_revision="rev-1",
        selected_case_ids=["c1", "c2"],
        profile={"benchmark_id": "b", "benchmark_version": "1"},
        work_root="/tmp/work",
        environment_digest="sha256:" + "0" * 64,
        limits={"max_wall_seconds": 60},
        retry_policy={"runner": 0, "provider_transport": 0, "operator": 0},
    )
    assert ExternalJobSpec(**base).selected_case_ids == ["c1", "c2"]
    rejects = [
        {**base, "runner_version": ""},
        {**base, "environment_digest": ""},
        {**base, "dataset_revision": ""},
        {**base, "execution_config_hash": ""},
        {**base, "profile": {"benchmark_id": "b"}},
        {**base, "selected_case_ids": []},
        {**base, "selected_case_ids": ["c1", "c1"]},
        # 占位 revision/digest 不允许（M2-G06）。
        {**base, "runner_version": "latest"},
        {**base, "dataset_revision": "TBD"},
        {**base, "environment_digest": "placeholder"},
    ]
    for payload in rejects:
        with pytest.raises(ValidationError):
            ExternalJobSpec(**payload)


def test_job_handle_launch_token_and_result_contracts():
    token_a, token_b = new_launch_token(), new_launch_token()
    assert token_a and token_b and token_a != token_b
    handle = ExternalJobHandle(
        job_id="job-1",
        run_id="run-1",
        launch_token=token_a,
        work_dir="/tmp/work/job-1",
        status=ExternalJobStatus.prepared,
        owned_resources={"pids": [123]},
        launch_identity={"host": "worker-1", "pid": 123},
    )
    with pytest.raises((TypeError, ValidationError)):
        handle.launch_token = "reused"
    with pytest.raises(ValidationError):
        ExternalJobHandle(
            job_id="job-1", run_id="run-1", launch_token="",
            work_dir="d", status=ExternalJobStatus.prepared,
        )
    with pytest.raises(ValidationError):
        NormalizedCaseResult(stable_case_key="", source_case_id="x", status="succeeded")
    batch = ImportBatch(
        job_id="job-1", source_artifact_hash="h-1", parser_version="p-1",
        record_keys=["k1", "k2"],
    )
    assert batch.conflicts == []
    with pytest.raises(ValidationError):
        ImportBatch(
            job_id="job-1", source_artifact_hash="h-1", parser_version="p-1",
            record_keys=[],
        )


def test_legacy_manifests_default_to_sample_mode():
    assert ExecutionSpec(backend_id="replay", backend_version="1").execution_mode == "sample"
    legacy_run = {
        "scenario_version": "replay@1",
        "manifest": {"provider": REPLAY_MANIFEST["provider"]},
    }
    projected = legacy_execution(legacy_run)
    assert projected["execution"]["backend_id"] == "replay"
    assert projected["execution"]["execution_mode"] == "sample"
    resolved = resolve_execution("replay@1", {"provider": REPLAY_MANIFEST["provider"]})
    assert resolved["execution"]["execution_mode"] == "sample"


def test_execution_mode_is_recorded_and_mismatch_rejected():
    register_backend(_fake_job_backend([], []))
    try:
        resolved = resolve_execution(
            "fake-suite@1", _job_manifest(dict(COMPLETE_EXTERNAL_CONFIG)),
        )
        assert resolved["execution"]["execution_mode"] == "job"
        with pytest.raises(ExecutionBackendError):
            resolve_execution("fake-suite@1", _job_manifest({
                **COMPLETE_EXTERNAL_CONFIG,
            }) | {"execution": {
                "backend_id": "fake-job-benchmark",
                "backend_version": "1",
                "execution_mode": "sample",
            }})
    finally:
        unregister_backend("fake-job-benchmark", "1")


def test_external_job_spec_from_run_freezes_selection_and_config_hash():
    service = RunService(InMemoryRunStore())
    run = service.create_run(
        "fake-suite@1",
        _job_manifest(dict(COMPLETE_EXTERNAL_CONFIG)),
        case_ids=["s-a:1", "s-a:2"],
    )
    spec = external_job_spec_from_run(run, work_root="/tmp/w")
    assert spec.run_id == run["id"]
    assert spec.selected_case_ids == ["s-a:1", "s-a:2"]
    assert spec.execution_config_hash == external_job_spec_from_run(
        run, work_root="/tmp/w",
    ).execution_config_hash
    assert spec.retry_policy.runner == 0
    with pytest.raises(ExecutionBackendError):
        external_job_spec_from_run({**run, "case_ids": []}, work_root="/tmp/w")
    with pytest.raises(ExecutionBackendError):
        external_job_spec_from_run(
            {**run, "manifest": {**run["manifest"], "external_benchmark": {}}},
            work_root="/tmp/w",
        )
