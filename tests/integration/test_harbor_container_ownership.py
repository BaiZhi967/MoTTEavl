"""M3 review R04：容器所有权（标签注入、定向停止、残留核查）。

复现/静态（review 原文）：``motte.job=...`` 只写进 handle 的
``owned_resources``，没有注入真实容器；interrupt/cleanup 仅委托
``ProcessJobAdapter``，没有检查或停止本 Job 的 Docker 容器，于是 cleanup 能
在没核验任何容器的情况下报告 ``clean``。

覆盖：
1. Runner 侧所有权 overlay 的内容与 compose project 名规则（离线）；
2. 所有权判定只认「本 Job 标签」或「本 Job 记录的 compose project」，**不碰**
   无关容器（诱饵）；daemon 不可达时状态是 ``unknown`` 而不是 ``clean``（假客户端）；
3. 受控真实 Docker：interrupt 停止本 Job 容器、cleanup 删除并保留诱饵容器。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from motte_benchmark.harbor import entry as harbor_entry
from motte_benchmark.harbor.adapter import ADAPTER_ID, HarborJobAdapter
from motte_benchmark.harbor.containers import (
    COMPOSE_PROJECT_LABEL,
    OWNER_LABEL_JOB,
    OWNER_LABEL_OWNER,
    OWNER_LABEL_RUN,
    ContainerOwnership,
    owner_label_value,
    sanitize_compose_project_name,
)
from motte_benchmark.protocol import BenchmarkRuntimeError
from motte_contracts.external_job import ExternalJobHandle, ExternalJobStatus

TASKS_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "benchmarks" / "harbor" / "tasks"
#: 本地已有的小镜像（真实 Docker 层不拉网络）。
TEST_IMAGE = "alpine:3.20"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        capture_output=True, text=True, check=False,
    )
    return probe.returncode == 0


# ------------------------------------------------------------------ 假客户端


class FakeContainer:
    def __init__(self, *, container_id: str, name: str, labels: dict[str, str],
                 status: str = "running") -> None:
        self.id = container_id
        self.short_id = container_id[:12]
        self.name = name
        self.labels = labels
        self.status = status
        self.stopped = False
        self.removed = False
        self.stop_calls = 0

    def stop(self, timeout: int | None = None) -> None:
        self.stop_calls += 1
        self.stopped = True
        self.status = "exited"

    def remove(self, force: bool = False) -> None:
        self.removed = True


class FakeContainers:
    def __init__(self, containers: list[FakeContainer]) -> None:
        self._containers = containers

    def list(self, all: bool = True, filters: dict[str, Any] | None = None) -> list[FakeContainer]:
        label = str((filters or {}).get("label") or "")
        if "=" not in label:
            return []
        key, value = label.split("=", 1)
        return [
            item for item in self._containers
            if not item.removed and str(item.labels.get(key) or "") == value
        ]

    def get(self, identifier: str) -> FakeContainer:
        for item in self._containers:
            if item.id == identifier:
                return item
        raise RuntimeError(f"no such container: {identifier}")


class FakeClient:
    def __init__(self, containers: list[FakeContainer]) -> None:
        self.containers = FakeContainers(containers)


def _fixture_handle(tmp_path: Path, *, job_id: str = "job-owner") -> ExternalJobHandle:
    work_dir = tmp_path / "work"
    (work_dir / "harbor").mkdir(parents=True, exist_ok=True)
    return ExternalJobHandle(
        job_id=job_id, run_id="run-owner", launch_token="token-owner-1",
        owned_resources={
            "kind": "process", "pids": [], "exit_code": None,
            "owner_token": "token-owner-1", "job_id": job_id, "run_id": "run-owner",
            "container_label": f"motte.job={job_id}",
            "container_owner_label": owner_label_value("token-owner-1"),
            "resources": [{"kind": "job_dir", "path": str(work_dir), "owner_token": "token-owner-1"}],
        },
        launch_identity={}, work_dir=str(work_dir), created_at="2026-09-20T00:00:00+00:00",
        status=ExternalJobStatus.active,
    )


def _ownership(client: Any, *, job_id: str = "job-owner", projects: tuple[str, ...] = ()) -> ContainerOwnership:
    return ContainerOwnership(
        job_id=job_id, run_id="run-owner", owner_token="token-owner-1",
        projects=projects, client_factory=lambda: client, stop_grace_seconds=1.0,
    )


def test_ownership_only_matches_this_job_and_never_touches_decoys() -> None:
    """只认本 Job 的标签/project；诱饵容器绝不停止、绝不删除。"""
    owned = FakeContainer(
        container_id="a" * 64, name="owned",
        labels={OWNER_LABEL_JOB: "job-owner", OWNER_LABEL_RUN: "run-owner",
                OWNER_LABEL_OWNER: owner_label_value("token-owner-1")},
    )
    by_project = FakeContainer(
        container_id="b" * 64, name="owned-by-project",
        labels={COMPOSE_PROJECT_LABEL: "hello-pass__abc1234__env"},
    )
    decoy = FakeContainer(
        container_id="c" * 64, name="decoy",
        labels={OWNER_LABEL_JOB: "another-job", COMPOSE_PROJECT_LABEL: "other__env"},
    )
    client = FakeClient([owned, by_project, decoy])
    ownership = _ownership(client, projects=("hello-pass__abc1234__env",))

    listed = ownership.list_owned()
    assert listed["state"] == "residual"
    assert sorted(item["name"] for item in listed["containers"]) == [
        "owned", "owned-by-project",
    ]

    outcome = ownership.stop_owned(remove=True)
    assert outcome["state"] == "clean"
    assert owned.stopped and owned.removed
    assert by_project.stopped and by_project.removed
    assert not decoy.stopped and not decoy.removed, "诱饵容器不得被触碰"
    assert sorted(item["name"] for item in outcome["removed"]) == [
        "owned", "owned-by-project",
    ]


def test_daemon_unreachable_is_unknown_never_clean() -> None:
    """daemon 不可达：状态必须是 unknown（附原因），绝不报 clean。"""
    def _boom() -> Any:
        raise BenchmarkRuntimeError("HARBOR_DOCKER_UNAVAILABLE", "cannot reach daemon")

    ownership = ContainerOwnership(
        job_id="job-owner", run_id="run-owner", owner_token="token-owner-1",
        client_factory=_boom,
    )
    assert ownership.state()["state"] == "unknown"
    assert "cannot reach daemon" in str(ownership.state()["reason"])
    outcome = ownership.stop_owned(remove=True)
    assert outcome["state"] == "unknown"
    assert outcome["stopped"] == [] and outcome["removed"] == []
    assert outcome["errors"][0]["code"] == "HARBOR_DOCKER_UNAVAILABLE"


def test_container_without_ownership_evidence_is_not_acted_on() -> None:
    """定位到但核验不通过（标签被改）的容器：不停止，如实记录。"""
    mismatched = FakeContainer(
        container_id="d" * 64, name="mismatched",
        labels={COMPOSE_PROJECT_LABEL: "hello-pass__abc1234__env"},
    )
    # 先按 project 命中，随后标签被改成别的 Job（核验不一致）。
    client = FakeClient([mismatched])
    ownership = _ownership(client, projects=("hello-pass__abc1234__env",))
    assert ownership.list_owned()["state"] == "residual"
    mismatched.labels[COMPOSE_PROJECT_LABEL] = "other__env"
    outcome = ownership.stop_owned(remove=True)
    assert not mismatched.stopped
    assert outcome["errors"] == [], "不再命中就不再动作，也不谎报错误"


def test_ownership_conflict_refuses_cleanup_and_reports_unknown() -> None:
    """M3-R2-08 反例：job 标签匹配但 run/owner 明确冲突的容器绝不能被动。

    review 复现：假 client 返回一个 ``motte.job`` 匹配、``motte.run`` 与
    ``motte.owner`` 都不同的容器，``stop_owned(remove=True)`` 仍停止/删除并报告
    ``clean``。构造器保存的 run_id/owner_label 必须参与判定：明确冲突 → 拒绝
    动作、状态 unknown 并附冲突明细，绝不用 project fallback 覆盖。
    """
    consistent = FakeContainer(
        container_id="a" * 64, name="consistent",
        labels={OWNER_LABEL_JOB: "job-owner", OWNER_LABEL_RUN: "run-owner",
                OWNER_LABEL_OWNER: owner_label_value("token-owner-1")},
    )
    owner_conflict = FakeContainer(
        container_id="b" * 64, name="owner-conflict",
        labels={OWNER_LABEL_JOB: "job-owner", OWNER_LABEL_RUN: "run-owner",
                OWNER_LABEL_OWNER: owner_label_value("someone-elses-token")},
    )
    run_conflict = FakeContainer(
        container_id="c" * 64, name="run-conflict",
        labels={OWNER_LABEL_JOB: "job-owner", OWNER_LABEL_RUN: "another-run",
                OWNER_LABEL_OWNER: owner_label_value("token-owner-1")},
    )
    foreign_job = FakeContainer(
        container_id="d" * 64, name="foreign-job-same-project",
        labels={OWNER_LABEL_JOB: "another-job",
                COMPOSE_PROJECT_LABEL: "hello-pass__abc1234__env"},
    )
    project_only = FakeContainer(
        container_id="e" * 64, name="project-only",
        labels={COMPOSE_PROJECT_LABEL: "hello-pass__abc1234__env"},
    )
    client = FakeClient([consistent, owner_conflict, run_conflict, foreign_job, project_only])
    ownership = _ownership(client, projects=("hello-pass__abc1234__env",))

    listed = ownership.list_owned()
    # 冲突项不出现在"可动作"列表里，但必须被显式登记。
    assert sorted(item["name"] for item in listed["containers"]) == [
        "consistent", "project-only",
    ]
    assert sorted(item["name"] for item in listed["conflicts"]) == [
        "foreign-job-same-project", "owner-conflict", "run-conflict",
    ]
    assert listed["state"] == "unknown", listed

    outcome = ownership.stop_owned(remove=True)
    assert consistent.stopped and consistent.removed
    assert project_only.stopped and project_only.removed, "只有 project 证据、无冲突时仍可清理"
    assert not owner_conflict.stopped and not owner_conflict.removed
    assert not run_conflict.stopped and not run_conflict.removed
    assert not foreign_job.stopped and not foreign_job.removed, "别的 Job 标签不得用 project 覆盖"
    assert outcome["state"] == "unknown", outcome
    assert outcome["reason"], "unknown 必须附原因"
    assert sorted(item["name"] for item in outcome["conflicts"]) == [
        "foreign-job-same-project", "owner-conflict", "run-conflict",
    ]
    codes = {
        conflict["code"]
        for item in outcome["conflicts"] for conflict in item["conflicts"]
    }
    assert codes == {
        "HARBOR_CONTAINER_FOREIGN_JOB_LABEL",
        "HARBOR_CONTAINER_OWNER_CONFLICT",
        "HARBOR_CONTAINER_RUN_CONFLICT",
    }, codes

    after = ownership.state()
    assert after["state"] == "unknown", after
    assert sorted(item["name"] for item in after["conflicts"]) == [
        "foreign-job-same-project", "owner-conflict", "run-conflict",
    ]


def test_conflict_detected_at_action_time_refuses_that_container() -> None:
    """定位后标签被改：动作前复验必须拒绝（错误里带冲突明细）。"""
    container = FakeContainer(
        container_id="f" * 64, name="relabelled",
        labels={COMPOSE_PROJECT_LABEL: "hello-pass__abc1234__env"},
    )
    client = FakeClient([container])
    ownership = _ownership(client, projects=("hello-pass__abc1234__env",))
    assert ownership.list_owned()["state"] == "residual"

    # 列出来之后、动作之前被改成"另一个 Job 带本 Job 的 project 标签"。
    container.labels[OWNER_LABEL_JOB] = "another-job"
    outcome = ownership.stop_owned(remove=True)
    assert not container.stopped and not container.removed
    conflicts = [
        error for error in outcome["errors"]
        if error["code"] == "HARBOR_CONTAINER_OWNERSHIP_CONFLICT"
    ]
    assert len(conflicts) == 1, outcome["errors"]
    assert conflicts[0]["conflicts"][0]["code"] == "HARBOR_CONTAINER_FOREIGN_JOB_LABEL"
    assert outcome["reason"], outcome


@pytest.mark.parametrize("conflict_labels, expected_codes", [
    ({OWNER_LABEL_RUN: "another-run"}, {"HARBOR_CONTAINER_RUN_CONFLICT"}),
    ({OWNER_LABEL_OWNER: "sha256:another-owner"}, {"HARBOR_CONTAINER_OWNER_CONFLICT"}),
    ({OWNER_LABEL_RUN: "another-run", OWNER_LABEL_OWNER: "sha256:another-owner"},
     {"HARBOR_CONTAINER_RUN_CONFLICT", "HARBOR_CONTAINER_OWNER_CONFLICT"}),
])
def test_project_fallback_never_overrides_existing_identity_conflicts(
    conflict_labels: dict[str, str], expected_codes: set[str],
) -> None:
    """R3-03: missing job label cannot erase explicit run or owner conflicts."""
    project = "hello-pass__abc1234__env"
    decoy = FakeContainer(
        container_id="f" * 64, name="foreign-project-match",
        labels={COMPOSE_PROJECT_LABEL: project, **conflict_labels},
    )
    ownership = _ownership(FakeClient([decoy]), projects=(project,))
    verdict, conflicts = ownership.classify({"labels": decoy.labels})
    assert verdict == "conflict"
    assert {item["code"] for item in conflicts} == expected_codes
    outcome = ownership.stop_owned(remove=True)
    assert not decoy.stopped and not decoy.removed
    assert outcome["state"] == "unknown"
    assert outcome["reason"]
    assert outcome["stopped"] == outcome["removed"] == []
    assert outcome["conflicts"][0]["id"] == decoy.id


def test_project_identity_conflict_introduced_after_listing_prevents_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = "hello-pass__abc1234__env"
    container = FakeContainer(
        container_id="f" * 64, name="relabelled-after-list",
        labels={COMPOSE_PROJECT_LABEL: project},
    )
    client = FakeClient([container])
    ownership = _ownership(client, projects=(project,))
    original_get = client.containers.get

    def get_after_relabel(identifier: str) -> FakeContainer:
        result = original_get(identifier)
        result.labels[OWNER_LABEL_RUN] = "another-run"
        return result

    monkeypatch.setattr(client.containers, "get", get_after_relabel)
    outcome = ownership.stop_owned(remove=True)
    assert not container.stopped and not container.removed
    assert outcome["state"] == "unknown"
    assert any(
        conflict["code"] == "HARBOR_CONTAINER_RUN_CONFLICT"
        for error in outcome["errors"] for conflict in error.get("conflicts", [])
    )


# ------------------------------------------------------------------ Runner 侧


def test_runner_ownership_overlay_and_project_names(tmp_path: Path) -> None:
    """Runner 写出的 overlay 含 job/run/owner 标签；project 名按 Harbor 规则。"""
    overlay = harbor_entry.write_ownership_overlay(
        tmp_path, job_id="job-owner", run_id="run-owner", owner_token="token-owner-1",
    )
    assert overlay.name == "ownership-overlay.yaml"
    import yaml

    document = yaml.safe_load(overlay.read_text(encoding="utf-8"))
    labels = document["services"]["main"]["labels"]
    assert labels[OWNER_LABEL_JOB] == "job-owner"
    assert labels[OWNER_LABEL_RUN] == "run-owner"
    # owner 标签是 token 摘要，原始 token 绝不落进容器元数据。
    assert labels[OWNER_LABEL_OWNER] == owner_label_value("token-owner-1")
    assert "token-owner-1" not in overlay.read_text(encoding="utf-8")

    assert sanitize_compose_project_name("hello-pass__5RmqnD2__env") == "hello-pass__5rmqnd2__env"
    assert harbor_entry.compose_project_name("hello-pass__5RmqnD2") == (
        sanitize_compose_project_name("hello-pass__5RmqnD2__env")
    )
    # 首字符非字母数字 → 补 0；非法字符 → '-'。
    assert sanitize_compose_project_name("__env") == "0__env"
    assert sanitize_compose_project_name("a/b c__env") == "a-b-c__env"


def test_adapter_reads_trial_projects_from_the_location_file(tmp_path: Path) -> None:
    """适配器从定位文件取 compose project 集合（Runner 才知道 trial 名）。"""
    handle = _fixture_handle(tmp_path)
    (Path(handle.work_dir) / "harbor" / "job-location.json").write_text(
        json.dumps({
            "job_dir": str(Path(handle.work_dir) / "jobs" / "job-owner"),
            "jobs_dir": str(Path(handle.work_dir) / "jobs"),
            "job_name": "job-owner",
            "started": True,
            "trials": [],
            "trial_names": ["hello-pass__AbC1234"],
            "compose_projects": ["hello-pass__abc1234__env"],
        }),
        encoding="utf-8",
    )
    adapter = HarborJobAdapter(argv=["/bin/true"], data_root=str(TASKS_ROOT))
    ownership = adapter.ownership(handle)
    assert ownership.job_id == "job-owner"
    assert ownership.owner_label == owner_label_value("token-owner-1")
    assert "hello-pass__abc1234__env" in ownership.projects


# ------------------------------------------------------------------ 真实 Docker


@pytest.mark.live
def test_real_docker_interrupt_and_cleanup_keep_decoys(tmp_path: Path) -> None:
    """受控真实 Docker：定向停止/删除本 Job 容器，诱饵与冲突项原样保留。

    诱饵两类：无关 Job 的容器（标签完全不同）与**冒充本 Job** 的容器（``motte.job``
    是我们、``motte.run``/``motte.owner`` 却是别人的）——后者必须被拒绝动作，
    并把状态记成 unknown 且附冲突明细（M3-R2-08）。
    """
    if not _docker_available():
        pytest.skip("real Docker daemon required")
    import docker

    client = docker.from_env()
    stamp = f"{int(time.time())}-{tmp_path.name[:8]}"
    job_id = f"job-real-{stamp}"
    project = f"hello-pass__real{stamp}__env".lower().replace(".", "-")
    owned = client.containers.run(
        TEST_IMAGE, ["sleep", "600"], detach=True,
        name=f"motte-owned-{stamp}",
        labels={OWNER_LABEL_JOB: job_id, OWNER_LABEL_RUN: "run-real",
                OWNER_LABEL_OWNER: owner_label_value("token-real")},
        remove=False,
    )
    decoy = client.containers.run(
        TEST_IMAGE, ["sleep", "600"], detach=True,
        name=f"motte-decoy-{stamp}",
        labels={OWNER_LABEL_JOB: f"other-{job_id}", COMPOSE_PROJECT_LABEL: f"other-{stamp}__env"},
        remove=False,
    )
    by_project = client.containers.run(
        TEST_IMAGE, ["sleep", "600"], detach=True,
        name=f"motte-project-{stamp}",
        labels={COMPOSE_PROJECT_LABEL: project},
        remove=False,
    )
    impostor = client.containers.run(
        TEST_IMAGE, ["sleep", "600"], detach=True,
        name=f"motte-impostor-{stamp}",
        labels={OWNER_LABEL_JOB: job_id, OWNER_LABEL_RUN: "someone-elses-run",
                OWNER_LABEL_OWNER: owner_label_value("someone-elses-token"),
                COMPOSE_PROJECT_LABEL: project},
        remove=False,
    )
    handle = _fixture_handle(tmp_path, job_id=job_id)
    # 容器标签与平台记录必须一致（run/owner 都是所有权证据的一部分）。
    handle = handle.model_copy(update={
        "run_id": "run-real",
        "launch_token": "token-real",
        "owned_resources": {
            **handle.owned_resources,
            "run_id": "run-real",
            "container_owner_label": owner_label_value("token-real"),
        },
    })
    (Path(handle.work_dir) / "harbor" / "job-location.json").write_text(
        json.dumps({
            "job_dir": str(Path(handle.work_dir) / "jobs" / job_id),
            "jobs_dir": str(Path(handle.work_dir) / "jobs"),
            "job_name": job_id, "started": True, "trials": [],
            "trial_names": ["hello-pass__Real001"], "compose_projects": [project],
        }),
        encoding="utf-8",
    )
    adapter = HarborJobAdapter(argv=["/bin/true"], data_root=str(TASKS_ROOT))
    try:
        ownership = adapter.ownership(handle)
        listed = ownership.list_owned()
        # 冲突项（冒充本 Job）不进可动作列表，但被显式登记 → 状态 unknown。
        assert listed["state"] == "unknown", listed
        names = sorted(item["name"] for item in listed["containers"])
        assert names == sorted([owned.name, by_project.name]), names
        assert [item["name"] for item in listed["conflicts"]] == [impostor.name]
        assert all(item["project"] or item["labels"] for item in listed["containers"])

        # interrupt：停止（不删除）本 Job 容器；冲突项与诱饵都不动。
        interrupted = adapter.interrupt(handle)
        assert interrupted.owned_resources["container_state"] == "unknown"
        owned.reload()
        by_project.reload()
        decoy.reload()
        impostor.reload()
        assert owned.status != "running", "本 Job 容器必须被停止"
        assert by_project.status != "running", "按 compose project 命中的容器必须被停止"
        assert decoy.status == "running", "诱饵容器不得被停止"
        assert impostor.status == "running", "run/owner 冲突的容器不得被停止"

        # cleanup：删除本 Job 容器；诱饵与冲突项仍在，状态如实为 unknown。
        cleanup = adapter.cleanup(handle)
        assert cleanup["container_state"] == "unknown", cleanup["container_errors"]
        assert cleanup["state"] in ("residual", "unknown")
        remaining = [item.name for item in cleanup["containers"]]
        assert remaining == [], remaining
        conflict_errors = [
            error for error in cleanup["container_errors"]
            if error["code"] == "HARBOR_CONTAINER_OWNERSHIP_CONFLICT"
        ]
        assert len(conflict_errors) == 1, cleanup["container_errors"]
        assert conflict_errors[0]["container"]["name"] == impostor.name
        assert any(item["kind"] == "container" for item in cleanup["known_resources"]) is False
        assert any(
            item["kind"] == "container"
            for item in cleanup["owned_resources"]["resources"]
        ), "清理结果必须把观察到的容器写进资源账本"
        with pytest.raises(docker.errors.NotFound):
            client.containers.get(owned.id)
        with pytest.raises(docker.errors.NotFound):
            client.containers.get(by_project.id)
        assert client.containers.get(decoy.id).status == "running"
        assert client.containers.get(impostor.id).status == "running"
        # 冲突仍在 → 残留核查不许报 clean。
        residual = adapter.ownership(handle).state()
        assert residual["state"] == "unknown", residual
        assert [item["name"] for item in residual["conflicts"]] == [impostor.name]
    finally:
        for container in (owned, by_project, decoy, impostor):
            try:
                container.remove(force=True)
            except Exception:  # noqa: BLE001 - 清理失败不影响断言结论
                pass
