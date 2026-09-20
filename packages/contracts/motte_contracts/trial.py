"""Task / Trial / Verifier 契约（M3：Harbor 与 Terminal-Bench）。

职责边界（需求第 4 节）：

- **TaskIdentity**：稳定身份由 source、dataset revision、规范化相对路径和
  任务内容 hash 组成；展示名单独保存，不参与唯一键。同名不同目录、内容
  变化都是不同身份。
- **TrialPlan**：事前计划的实验重复。``repeat_index`` 在同一 run 的同一
  Task 内唯一，传输重试与操作员 retry 都不改变它——重试不是新的实验样本。
- **TrialResult**：一次计划的结局与证据引用。``source_trial_id`` 保留上游
  Runner 自己的 identity，与平台 ``trial_id`` 不可混用。
- **VerifierObservation**：Verifier 的原始奖励与状态。reward=0 是**有效
  失败**（``scored``），与缺文件（``missing_verifier_evidence``）、畸形
  载荷（``verifier_protocol_error``）、Verifier 崩溃/超时（``verifier_error``）
  语义不同；平台不替 Verifier 猜分。
"""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Literal

from pydantic import Field, StrictStr, field_validator, model_validator

from .messages import Contract

#: 身份与计划的 schema 版本；变更语义必须同时递增并更新消费者。
TASK_IDENTITY_SCHEMA_VERSION = 1
TRIAL_PLAN_SCHEMA_VERSION = 1
TRIAL_RESULT_SCHEMA_VERSION = 1

#: Verifier 奖励的 canonical 维度键：Terminal-Bench 官方任务写单个 ``reward``。
CANONICAL_REWARD_KEY = "reward"


def canonical_hash(payload: Any) -> str:
    """规范 JSON（排序、紧凑、UTF-8）的 sha256，带 ``sha256:`` 前缀。"""
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class VerifierStatus(str, Enum):
    """Verifier 观测状态；每个值对应不同的评分政策（需求第 6 节）。"""

    scored = "scored"
    missing_verifier_evidence = "missing_verifier_evidence"
    verifier_protocol_error = "verifier_protocol_error"
    verifier_error = "verifier_error"


class TrialDisposition(str, Enum):
    """一个计划 Trial 单元的最终处置；每个计划单元都必须落到其中之一。"""

    succeeded = "succeeded"
    failed = "failed"
    not_attempted = "not_attempted"
    cancelled = "cancelled"
    indeterminate = "indeterminate"


class TaskAggregationPolicy(str, Enum):
    """Task 层聚合规则，事前固定在 Profile 里，不默认择优。"""

    first_trial = "first-trial"
    mean_success = "mean-success"


class EvidenceRef(Contract):
    """证据引用：Artifact identity + 原始来源与完整度，不内联大文本。"""

    artifact_id: StrictStr = Field(min_length=1)
    kind: StrictStr = Field(min_length=1)
    sha256: StrictStr | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    media_type: StrictStr | None = None
    #: 相对受控工作目录的来源路径（若已知），用于把引用映射回上游文件。
    source_path: StrictStr | None = None
    #: 该证据是否完整；截断/超预算/不可读时为 False。
    complete: bool = True
    truncated: bool = False
    note: StrictStr | None = None


def compute_task_key(
    *,
    source_id: str,
    dataset_revision: str,
    normalized_relative_path: str,
    task_content_hash: str,
    upstream_task_id: str | None = None,
) -> str:
    """由身份字段派生 ``task_key``（展示名与临时根路径都不参与）。"""
    return canonical_hash({
        "schema_version": TASK_IDENTITY_SCHEMA_VERSION,
        "source_id": source_id,
        "dataset_revision": dataset_revision,
        "normalized_relative_path": normalized_relative_path,
        "task_content_hash": task_content_hash,
        "upstream_task_id": upstream_task_id,
    })


class TaskIdentity(Contract):
    """受控任务根目录内一个任务的稳定身份（需求 4.1）。

    ``task_key`` 是派生字段，但作为**存储字段**保存：每次反序列化都会重算
    并比对，因此持久化身份不可能与内容 hash 悄悄脱节——篡改或版本漂移都
    会在验证时报错，而不是静默产生两个"看起来合法"的身份。
    """

    source_id: StrictStr = Field(min_length=1)
    dataset_revision: StrictStr = Field(min_length=1)
    normalized_relative_path: StrictStr = Field(min_length=1)
    task_content_hash: StrictStr = Field(min_length=1)
    task_key: StrictStr = Field(min_length=1)
    upstream_task_id: StrictStr | None = None

    @field_validator("normalized_relative_path")
    @classmethod
    def _must_be_posix_and_relative(cls, value: str) -> str:
        return _validate_normalized_path(value)

    @model_validator(mode="after")
    def _task_key_must_match_identity(self) -> TaskIdentity:
        expected = compute_task_key(
            source_id=self.source_id,
            dataset_revision=self.dataset_revision,
            normalized_relative_path=self.normalized_relative_path,
            task_content_hash=self.task_content_hash,
            upstream_task_id=self.upstream_task_id,
        )
        if self.task_key != expected:
            raise ValueError(
                f"task_key does not match the canonical identity hash (expected {expected})",
            )
        return self


class TaskManifest(Contract):
    """一次只读准备（prepare）的产物：任务清单 + 来源治理记录。"""

    source_id: StrictStr = Field(min_length=1)
    dataset_revision: StrictStr = Field(min_length=1)
    tasks: list[TaskIdentity] = Field(min_length=1)
    display_names: dict[str, StrictStr] = Field(default_factory=dict)
    file_hashes: dict[str, dict[str, StrictStr]] = Field(default_factory=dict)
    #: 每个 task_key 的非 secret 事实：文件数、字节数、是否带 tests/gold。
    task_facts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: 被识别为目录但不符合任务结构的候选（显式登记，不静默跳过）。
    invalid_tasks: list[dict[str, Any]] = Field(default_factory=list)
    license_id: StrictStr | None = None
    license_evidence: StrictStr | None = None
    source_kind: Literal["local", "pinned-source"] = "local"
    prepared_at: StrictStr | None = None
    limits: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _keys_must_be_unique(self) -> TaskManifest:
        keys = [task.task_key for task in self.tasks]
        if len(keys) != len(set(keys)):
            raise ValueError("task manifest requires unique task_key per task")
        for task in self.tasks:
            if task.task_content_hash not in self.file_hashes:
                raise ValueError(
                    "task manifest requires the file hash map of every task"
                )
        if self.display_names.keys() - set(keys):
            raise ValueError("display names must reference tasks in this manifest")
        if self.task_facts.keys() - set(keys):
            raise ValueError("task facts must reference tasks in this manifest")
        return self

    @property
    def manifest_hash(self) -> str:
        """整个清单的内容 hash：同一字节重复准备得到同一 hash。"""
        return canonical_hash({
            "source_id": self.source_id,
            "dataset_revision": self.dataset_revision,
            "tasks": sorted(
                (
                    task.model_dump(mode="json") | {"task_key": task.task_key}
                    for task in self.tasks
                ),
                key=lambda item: item["task_key"],
            ),
            "file_hashes": self.file_hashes,
            "task_facts": self.task_facts,
            "invalid_tasks": self.invalid_tasks,
            "license_id": self.license_id,
            "source_kind": self.source_kind,
        })


class TrialPlan(Contract):
    """事前冻结的一次实验重复；``trial_id`` 由计划身份派生（需求 4.2）。"""

    trial_id: StrictStr = Field(min_length=1)
    run_id: StrictStr = Field(min_length=1)
    task_key: StrictStr = Field(min_length=1)
    repeat_index: int = Field(ge=0, strict=True)
    seed: int | None = Field(default=None, strict=True)
    agent_config_hash: StrictStr = Field(min_length=1)
    environment_hash: StrictStr = Field(min_length=1)
    schema_version: int = Field(default=TRIAL_PLAN_SCHEMA_VERSION, ge=1, strict=True)

    @property
    def plan_key(self) -> str:
        """计划身份 hash：同一 run/task/repeat/配置得到同一 trial_id。"""
        return canonical_hash({
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "task_key": self.task_key,
            "repeat_index": self.repeat_index,
            "seed": self.seed,
            "agent_config_hash": self.agent_config_hash,
            "environment_hash": self.environment_hash,
        })


def trial_id_for(
    *,
    run_id: str,
    task_key: str,
    repeat_index: int,
    agent_config_hash: str,
    environment_hash: str,
    seed: int | None = None,
) -> str:
    """由冻结计划身份派生稳定 ``trial_id``（计划重复不改变它）。"""
    return "trial-" + canonical_hash({
        "schema_version": TRIAL_PLAN_SCHEMA_VERSION,
        "run_id": run_id,
        "task_key": task_key,
        "repeat_index": repeat_index,
        "seed": seed,
        "agent_config_hash": agent_config_hash,
        "environment_hash": environment_hash,
    }).removeprefix("sha256:")[:32]


class VerifierObservation(Contract):
    """Verifier 观测（需求第 6 节）：原始 reward、来源证据与错误分别保存。"""

    status: VerifierStatus
    rewards: dict[str, float] = Field(default_factory=dict)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    #: 上游 reward 文件的原始来源路径与内容 hash（缺失时为空）。
    source_path: StrictStr | None = None
    source_hash: StrictStr | None = None

    @field_validator("rewards", mode="before")
    @classmethod
    def _rewards_must_be_finite_numbers(cls, value: dict[str, float]) -> dict[str, float]:
        # mode="before"：先看原始 JSON 值，否则 Pydantic 会把 ``True`` 强制
        # 转成 1.0，bool 奖励就会冒充成合法数字。
        for key, reward in value.items():
            # bool 是 int 的子类：``True`` 不是数字奖励，必须显式拒绝。
            if isinstance(reward, bool) or not isinstance(reward, (int, float)):
                raise ValueError(f"reward {key!r} must be a number, not {type(reward).__name__}")
            if reward != reward or reward in (float("inf"), float("-inf")):
                raise ValueError(f"reward {key!r} must be finite")
        return {key: float(reward) for key, reward in value.items()}

    @model_validator(mode="after")
    def _scored_requires_reward(self) -> VerifierObservation:
        if self.status is VerifierStatus.scored and not self.rewards:
            raise ValueError("scored verifier observation requires at least one reward")
        if self.status is not VerifierStatus.scored and self.rewards:
            raise ValueError(
                "non-scored verifier observation must not carry rewards; "
                "keep the raw payload in evidence instead of guessing a score"
            )
        return self

    @property
    def reward(self) -> float | None:
        """canonical 主奖励（``reward`` 维度）；没有时返回 None，不填 0。"""
        return self.rewards.get(CANONICAL_REWARD_KEY)


class TrialResult(Contract):
    """一次计划 Trial 的结果契约；不嵌入秘密或巨大文本。"""

    trial_id: StrictStr = Field(min_length=1)
    source_trial_id: StrictStr | None = None
    disposition: TrialDisposition
    termination: dict[str, Any] = Field(default_factory=dict)
    verifier_observation: VerifierObservation
    artifact_refs: list[EvidenceRef] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    #: 未被观测到的量（cost/model identity/trajectory…）逐项标注。
    coverage: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = Field(default=TRIAL_RESULT_SCHEMA_VERSION, ge=1, strict=True)

    @model_validator(mode="after")
    def _disposition_matches_verifier(self) -> TrialResult:
        scored = self.verifier_observation.status is VerifierStatus.scored
        if scored and self.disposition not in (
            TrialDisposition.succeeded, TrialDisposition.failed,
        ):
            raise ValueError("a scored verifier observation requires succeeded/failed disposition")
        if not scored and self.disposition in (
            TrialDisposition.succeeded, TrialDisposition.failed,
        ):
            raise ValueError(
                "succeeded/failed disposition requires a scored verifier observation; "
                "missing or errored verifier evidence is not a quality judgement"
            )
        return self

    @property
    def passed(self) -> bool | None:
        """质量通过：只有 scored 且 reward 明确 > 0 才为 True。"""
        if self.verifier_observation.status is not VerifierStatus.scored:
            return None
        reward = self.verifier_observation.reward
        if reward is None:
            return None
        return reward > 0


def _validate_normalized_path(value: str) -> str:
    """规范化相对路径必须是 POSIX、相对、无 ``..``、无盘符（需求 4.1）。"""
    if "\\" in value:
        raise ValueError("normalized path must use POSIX separators")
    if value.startswith("/"):
        raise ValueError("normalized path must be relative to the controlled root")
    if len(value) >= 2 and value[1] == ":":
        raise ValueError("normalized path must not carry a drive letter")
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if not parts:
        raise ValueError("normalized path must not be empty")
    if any(part == ".." for part in parts):
        raise ValueError("normalized path must not escape the controlled root")
    return "/".join(parts)
