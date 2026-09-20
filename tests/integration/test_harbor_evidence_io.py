"""M3 review R11/R12：二进制工件冻结与外层文件受控读取。

R12 复现（review 原文）：把 ``work/harbor/config.json`` 替换成指向临时目录外
合成秘密文件的 symlink，采集跟随链接并把其文本加入 bundle。内层 Trial 用
``TrustedDir``，外层 config/lock/result/plan/location 却用普通
``is_file``/``read_text``，缺少锚定、拒 symlink 与大小限额。

R11 复现：非 UTF-8 文件只登记 size/hash/encoding，字节留在原工作目录；没有
独立 Artifact、没有可恢复内容引用，而 bundle 仍可被标为"冻结完成"。工作目录
清理后无法还原证据。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from motte_benchmark.harbor.adapter import ADAPTER_ID, HarborJobAdapter
from motte_benchmark.protocol import BenchmarkRuntimeError
from motte_contracts.external_job import ExternalJobHandle, ExternalJobStatus
from motte_sdk import terminalbench as tb
from motte_sdk.service import build_run_service
from motte_storage.artifacts import ArtifactStore

TASKS_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "benchmarks" / "harbor" / "tasks"
#: 合成哨兵（不是真实凭据）：用来证明外部文件的文本没有被采集进 bundle。
SYNTHETIC_SENTINEL = "SYNTHETIC-OUTSIDE-FILE-CONTENT"
#: 非 UTF-8 字节（含 NUL 与非法 UTF-8 序列），必须逐字节 roundtrip。
BINARY_BLOB = b"\xff\xfe\x00\x01PK\x03\x04binary-payload\x00\x80"


def _prepared(tmp_path: Path) -> tuple[HarborJobAdapter, ExternalJobHandle]:
    service = build_run_service(tmp_path / "runs.db")
    record = tb.prepare_terminal_bench_dataset(
        service.store, task_root=str(TASKS_ROOT), source_id="review-r11",
        dataset_revision="r11-1",
    )
    inputs = tb.build_run_inputs(
        record=record, run_id="run-r11", job_id="job-r11",
        profile=tb.terminal_bench_profile(n_trials=1),
    )
    external = inputs["manifest"]["external_benchmark"]
    from motte_contracts.external_job import ExternalJobSpec

    spec = ExternalJobSpec(
        run_id="run-r11", adapter_id=ADAPTER_ID, adapter_version="1",
        runner_version="harbor-0.23.0", execution_config_hash=inputs["manifest_hash"],
        dataset_revision="r11-1", selected_case_ids=inputs["case_ids"],
        profile=external["profile"], work_root=str(tmp_path / "jobs"),
        environment_digest=external["environment_digest"],
        limits=external["limits"], runner_config=external["runner_config"],
    )
    adapter = HarborJobAdapter(argv=["/bin/true"], data_root=str(TASKS_ROOT))
    return adapter, adapter.prepare(spec)


def _stage_binary_artifact(work_dir: Path, *, rel: str = "artifacts/blob.bin") -> Path:
    """在 Job 目录的某个 Trial 下放一个非 UTF-8 工件。"""
    plan = json.loads((work_dir / "harbor" / "plan.json").read_text(encoding="utf-8"))
    task = plan["tasks"][0]
    trial_dir = work_dir / "jobs" / "job-r11" / "hello-pass__Binary1"
    target = trial_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(BINARY_BLOB)
    (trial_dir / "result.json").write_text(json.dumps({
        "id": "binary-trial-1",
        "trial_name": "hello-pass__Binary1",
        "task_id": {"path": f"/data/{task['normalized_relative_path']}"},
        "exception_info": None,
        "verifier_result": {"rewards": {"reward": 1.0}},
    }), encoding="utf-8")
    (trial_dir / "verifier").mkdir(parents=True, exist_ok=True)
    (trial_dir / "verifier" / "reward.txt").write_text("1.0\n", encoding="utf-8")
    (work_dir / "harbor" / "job-location.json").write_text(json.dumps({
        "job_dir": str(work_dir / "jobs" / "job-r11"),
        "jobs_dir": str(work_dir / "jobs"),
        "job_name": "job-r11", "started": True, "trials": ["hello-pass__Binary1"],
        "trial_names": ["hello-pass__Binary1"], "compose_projects": [],
    }), encoding="utf-8")
    return target


# ------------------------------------------------------------------ R12


def test_outer_config_symlink_is_refused_not_followed(tmp_path: Path) -> None:
    """R12 反例：外层 config.json 是 symlink 时拒绝，绝不跟随并采集其内容。"""
    adapter, handle = _prepared(tmp_path)
    work_dir = Path(handle.work_dir)
    outside = tmp_path / "outside-secret.json"
    outside.write_text(json.dumps({"note": SYNTHETIC_SENTINEL}), encoding="utf-8")
    config_path = work_dir / "harbor" / "config.json"
    config_path.unlink()
    config_path.symlink_to(outside)

    with pytest.raises(BenchmarkRuntimeError) as refused:
        adapter.read_output_files(handle)
    assert refused.value.code == "HARBOR_OUTPUT_UNREADABLE"
    assert "symlink" in str(refused.value)

    # 换成 plan.json 同样拒绝（外层文件一视同仁）。
    plan_path = work_dir / "harbor" / "plan.json"
    plan_path.unlink()
    plan_path.symlink_to(outside)
    with pytest.raises(BenchmarkRuntimeError):
        adapter.read_output_files(handle)


def test_outer_file_over_limit_is_refused(tmp_path: Path) -> None:
    """R12：外层文件超限同样拒绝（不再无限 read_text）。"""
    adapter, handle = _prepared(tmp_path)
    work_dir = Path(handle.work_dir)
    (work_dir / "harbor" / "result.json").write_text("x" * (4 * 1024 * 1024 + 1), encoding="utf-8")
    with pytest.raises(BenchmarkRuntimeError) as too_large:
        adapter.read_output_files(handle)
    assert too_large.value.code == "HARBOR_OUTPUT_TOO_LARGE"


def test_outer_files_are_registered_with_hash_size_and_encoding(tmp_path: Path) -> None:
    """外层文件的 sha256/size/encoding 进入 evidence-index（可审计）。"""
    adapter, handle = _prepared(tmp_path)
    work_dir = Path(handle.work_dir)
    _stage_binary_artifact(work_dir)
    files = adapter.read_output_files(handle)
    index = json.loads(files["harbor/evidence-index.json"])
    for name in ("harbor/config.json", "harbor/plan.json", "harbor/job-location.json"):
        entry = index["files"][name]
        assert entry["scope"] == "outer"
        assert entry["encoding"] == "utf-8"
        assert entry["sha256"].startswith("sha256:")
        assert entry["size_bytes"] == len(files[name].encode("utf-8"))
    assert index["schema_version"] >= 2
    assert index["job_location"]["started"] is True


def test_outer_file_replacement_is_visible_in_the_index(tmp_path: Path) -> None:
    """R12：外层文件被替换后，登记的是**实际读到的**字节 hash（可发现替换）。"""
    adapter, handle = _prepared(tmp_path)
    work_dir = Path(handle.work_dir)
    config_path = work_dir / "harbor" / "config.json"
    first = adapter.read_output_files(handle)
    first_entry = json.loads(first["harbor/evidence-index.json"])["files"]["harbor/config.json"]

    replaced = json.loads(config_path.read_text(encoding="utf-8"))
    replaced["job"]["job_name"] = "replaced-job-name"
    config_path.write_text(json.dumps(replaced), encoding="utf-8")
    second = adapter.read_output_files(handle)
    second_entry = json.loads(second["harbor/evidence-index.json"])["files"]["harbor/config.json"]

    assert second_entry["sha256"] != first_entry["sha256"]
    import hashlib

    assert second_entry["sha256"] == (
        "sha256:" + hashlib.sha256(second["harbor/config.json"].encode("utf-8")).hexdigest()
    )
    assert second_entry["size_bytes"] == len(second["harbor/config.json"].encode("utf-8"))
    assert json.loads(second["harbor/config.json"])["job"]["job_name"] == "replaced-job-name"


def test_location_file_symlink_does_not_escape(tmp_path: Path) -> None:
    """定位文件被替换成 symlink：拒绝采集、不跟随；所有权核验不越界。"""
    adapter, handle = _prepared(tmp_path)
    work_dir = Path(handle.work_dir)
    outside = tmp_path / "outside-location.json"
    outside.write_text(json.dumps({
        "job_dir": str(tmp_path / "outside-job"), "started": True,
    }), encoding="utf-8")
    location = work_dir / "harbor" / "job-location.json"
    location.unlink(missing_ok=True)  # 适配器只在 Runner 写过定位文件后才需要它
    location.symlink_to(outside)
    with pytest.raises(BenchmarkRuntimeError) as refused:
        adapter.read_output_files(handle)
    assert refused.value.code == "HARBOR_OUTPUT_UNREADABLE"
    assert "job-location.json" in str(refused.value)
    # 所有权路径也不跟随链接：拿不到 job_dir/compose project 就不做任何容器动作。
    assert adapter._location_payload(handle) == {}
    assert adapter._job_dir(handle) is None


# ------------------------------------------------------------------ R11


def test_binary_artifact_is_frozen_with_content_and_recoverable(tmp_path: Path) -> None:
    """R11：非 UTF-8 工件冻结为独立 Artifact，删除工作目录后仍可读回。"""
    adapter, handle = _prepared(tmp_path)
    work_dir = Path(handle.work_dir)
    _stage_binary_artifact(work_dir)
    store = ArtifactStore(str(tmp_path / "artifacts"))
    published: list[tuple[str, bytes]] = []

    def _sink(path: str, data: bytes) -> Any:
        published.append((path, data))
        return store.put_bytes(path, data)

    adapter.binary_sink = _sink
    files, binaries = adapter.read_output_bytes(handle)
    assert len(binaries) == 1, binaries
    entry = binaries[0]
    assert entry["encoding"] == "binary"
    assert entry["media_type"] == "application/octet-stream"
    assert entry["artifact_id"], entry
    assert entry["note"] is None
    assert entry["sha256"] == "sha256:" + __import__("hashlib").sha256(BINARY_BLOB).hexdigest()
    assert entry["size_bytes"] == len(BINARY_BLOB)
    # Artifact 路径形状由接口固定（主 agent 的详情读取链路依赖它）。
    artifact_path = published[0][0]
    assert artifact_path.startswith(f"external-jobs/run-r11/{handle.job_id}/evidence/bin-")
    assert artifact_path.endswith(".bin")
    assert artifact_path.split("bin-")[1].split(".")[0] == entry["sha256"].split(":")[1][:16]
    assert published[0][1] == BINARY_BLOB

    index = json.loads(files["harbor/evidence-index.json"])
    indexed = index["files"]["harbor/trials/hello-pass__Binary1/artifacts/blob.bin"]
    assert indexed["encoding"] == "binary"
    assert indexed["artifact_id"] == entry["artifact_id"]
    assert index["binaries"] == [{"path": "harbor/trials/hello-pass__Binary1/artifacts/blob.bin",
                                  **{k: v for k, v in entry.items() if k != "path"}}]

    # 工作目录被清理后：内容仍可从 Artifact 读回（逐字节相等）。
    shutil.rmtree(work_dir)
    recovered = store.read_bytes(str(entry["artifact_id"]))
    assert recovered == BINARY_BLOB

    # cursor 与 evidence-index 覆盖同一批二进制条目。
    results, cursor = adapter.collect_from_files(handle, {}, files, binaries)
    assert cursor["binary_count"] == 1
    assert cursor["binaries_frozen"] is True
    assert cursor["binaries"][0]["artifact_id"] == entry["artifact_id"]
    assert [item.output["disposition"] for item in results] == ["succeeded", "not_attempted"]


def test_missing_or_failing_sink_is_reported_honestly(tmp_path: Path) -> None:
    """没有 sink / sink 抛错：如实记 artifact_id=null + note，不谎报完整冻结。"""
    adapter, handle = _prepared(tmp_path)
    work_dir = Path(handle.work_dir)
    _stage_binary_artifact(work_dir)

    # 1. 未接线：只能从工作目录读，明确说明"没有冻结为 Artifact"。
    files, binaries = adapter.read_output_bytes(handle)
    assert binaries[0]["artifact_id"] is None
    assert "NOT frozen as an artifact" in str(binaries[0]["note"])
    results, cursor = adapter.collect_from_files(handle, {}, files, binaries)
    assert cursor["binaries_frozen"] is False

    # 2. sink 抛错：记录错误，不冒充成功。
    def _boom(path: str, data: bytes) -> Any:
        raise RuntimeError("artifact store unavailable")

    adapter.binary_sink = _boom
    _files, binaries = adapter.read_output_bytes(handle)
    assert binaries[0]["artifact_id"] is None
    assert "artifact store unavailable" in str(binaries[0]["note"])

    # 3. sink 返回无 id 的对象：同样不冒充。
    adapter.binary_sink = lambda path, data: object()
    _files, binaries = adapter.read_output_bytes(handle)
    assert binaries[0]["artifact_id"] is None
    assert binaries[0]["note"] == "binary sink returned no artifact id"


def test_utf8_files_are_never_sent_to_the_binary_sink(tmp_path: Path) -> None:
    """只有非 UTF-8 字节走二进制通道；文本文件不重复冻结。"""
    adapter, handle = _prepared(tmp_path)
    work_dir = Path(handle.work_dir)
    _stage_binary_artifact(work_dir)
    calls: list[str] = []
    adapter.binary_sink = lambda path, data: calls.append(path) or "artifact-1"
    files, binaries = adapter.read_output_bytes(handle)
    assert len(calls) == 1
    assert binaries[0]["encoding"] == "binary"
    # 文本（result.json / reward.txt）仍然进入文本 bundle，可被 Parser 读取。
    assert "harbor/trials/hello-pass__Binary1/result.json" in files
    assert "harbor/trials/hello-pass__Binary1/verifier/reward.txt" in files
