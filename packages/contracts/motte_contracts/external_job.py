"""外部 Job 执行数据契约（job-based benchmark 运行）。

一个 job-based Run 只启动一个外部 Job：本模块固定 JobSpec / JobHandle /
NormalizedCaseResult / ImportBatch 的字段与不变量，并以 ``ExternalJobAdapter``
Protocol 声明 prepare/start/poll/interrupt/collect/cleanup 操作。adapter 只执行
作业，不直接修改 Run 表；持久化、启动边界与采集检查点由应用层负责（见需求
第 4 节）。launch_token 每次启动生成、不可复用；恢复时只能凭 token 重新观察，
不能凭空重启。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import Field, StrictStr, field_validator

from .messages import Contract


class ExecutionMode(str, Enum):
    """执行后端的分派模式。

    ``sample``：沿用逐 Case invoke 的既有路径（direct/replay/agent）。
    ``job``：一次 Run 只启动一个外部 Job，由 Job 覆盖全部 selected case。
    """

    sample = "sample"
    job = "job"


class ExternalJobStatus(str, Enum):
    """Job 状态描述外部作业的子过程，不替代 Run 状态。"""

    prepared = "prepared"
    launching = "launching"
    active = "active"
    collecting = "collecting"
    settled = "settled"
    failed = "failed"
    cancelled = "cancelled"
    indeterminate = "indeterminate"


class CaseResultStatus(str, Enum):
    """单个 selected case 在外部 Job 中的处置状态。"""

    succeeded = "succeeded"
    failed = "failed"
    not_attempted = "not_attempted"
    unscored = "unscored"


# 占位值不允许出现在需要“固定版本”的字段里（M2-G06：没有占位 revision/digest）。
_PLACEHOLDER_VALUES = {"", "latest", "tbd", "todo", "placeholder", "unpinned", "unknown", "n/a"}


def _reject_placeholder(value: str, field_name: str) -> str:
    if value.strip().lower() in _PLACEHOLDER_VALUES:
        raise ValueError(f"{field_name} must be pinned to a real value, not a placeholder")
    return value


class ExternalRetryPolicy(Contract):
    """Runner / Provider transport / 操作员三种重试分开记录（需求第 6 节）。

    配置转换不能额外叠加默认重试；三层各自计数，缺省均为 0。
    """

    runner: int = Field(default=0, ge=0)
    provider_transport: int = Field(default=0, ge=0)
    operator: int = Field(default=0, ge=0)


class ExternalJobSpec(Contract):
    """一次外部 Job 的冻结执行配置（需求 4.1 表）。"""

    run_id: StrictStr = Field(min_length=1)
    adapter_id: StrictStr = Field(min_length=1)
    adapter_version: StrictStr = Field(min_length=1)
    runner_version: StrictStr = Field(min_length=1)
    execution_config_hash: StrictStr = Field(min_length=1)
    dataset_revision: StrictStr = Field(min_length=1)
    selected_case_ids: list[StrictStr] = Field(min_length=1)
    profile: dict[str, Any]
    work_root: StrictStr = Field(min_length=1)
    environment_digest: StrictStr = Field(min_length=1)
    limits: dict[str, Any] = Field(default_factory=dict)
    retry_policy: ExternalRetryPolicy = Field(default_factory=ExternalRetryPolicy)
    # Runner 可消费的完整配置（review R01）：模型快照、逐题 prompt、few-shot、
    # 凭据引用与配置 hash；由创建入口经 build_opencompass_config 冻结。
    runner_config: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "runner_version", "dataset_revision", "environment_digest", "execution_config_hash",
    )
    @classmethod
    def _versions_must_be_pinned(cls, value: str, info: Any) -> str:
        return _reject_placeholder(value, info.field_name)

    @field_validator("selected_case_ids")
    @classmethod
    def _selection_must_be_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("selected_case_ids must be unique")
        return value

    @field_validator("profile")
    @classmethod
    def _profile_must_pin_benchmark(cls, value: dict[str, Any]) -> dict[str, Any]:
        for key in ("benchmark_id", "benchmark_version"):
            pinned = value.get(key)
            if not isinstance(pinned, str) or not pinned.strip():
                raise ValueError(f"profile.{key} is required and must be a nonempty string")
        return value


class ExternalJobHandle(Contract):
    """外部 Job 的持久句柄（需求 4.1 表 + 4.2 启动身份）。

    ``launch_token`` 每次启动生成一次、不可复用；``owned_resources`` 记录本 Job
    拥有的进程/容器引用，``launch_identity`` 保存与 PID 同时落库的启动身份——
    恢复时不能仅凭 PID 判定是原任务。
    """

    job_id: StrictStr = Field(min_length=1)
    run_id: StrictStr = Field(min_length=1)
    launch_token: StrictStr = Field(min_length=1)
    external_id: StrictStr | None = None
    owned_resources: dict[str, Any] = Field(default_factory=dict)
    launch_identity: dict[str, Any] = Field(default_factory=dict)
    work_dir: StrictStr = Field(min_length=1)
    created_at: StrictStr | None = None
    status: ExternalJobStatus
    collection_cursor: dict[str, Any] = Field(default_factory=dict)


class NormalizedCaseResult(Contract):
    """Parser 归一化后的单 case 结果（需求 4.1 表）。

    ``output``/``error`` 是导入层消费的原始载荷；结果冻结为受控 Artifact 后
    改用 ``output_ref`` 引用（M2-T03）。``usage``/``evidence_coverage`` 中
    未观测到的量（费用、模型身份）如实标记 unknown，不补填为已核验。
    """

    stable_case_key: StrictStr = Field(min_length=1)
    source_case_id: StrictStr = Field(min_length=1)
    output_ref: StrictStr | None = None
    output: Any = None
    error: dict[str, Any] | None = None
    native_score_refs: list[StrictStr] = Field(default_factory=list)
    status: CaseResultStatus
    error_category: StrictStr | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    evidence_coverage: dict[str, Any] = Field(default_factory=dict)


class ImportBatch(Contract):
    """一次幂等导入的批次记录（需求 4.3）。

    幂等键为 job_id + source_record_key + parser_version；同键同内容重复导入
    no-op，同键不同内容 conflict——两份来源摘要都保留在 ``conflicts``。
    """

    job_id: StrictStr = Field(min_length=1)
    source_artifact_hash: StrictStr = Field(min_length=1)
    parser_version: StrictStr = Field(min_length=1)
    record_keys: list[StrictStr] = Field(min_length=1)
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("record_keys")
    @classmethod
    def _record_keys_must_be_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("record_keys must be unique")
        return value


def new_launch_token() -> str:
    """生成一次性的 launch token；每次启动必须使用新 token（需求 4.2）。"""
    return f"launch-{uuid4().hex}"


@runtime_checkable
class ExternalJobAdapter(Protocol):
    """拟议 adapter 操作（需求 4.1）：只执行作业，不修改 Run 表。

    - ``prepare(spec)``：校验并创建受控工作目录，无执行副作用。
    - ``start(spec, handle)``：产生执行副作用；应用层先持久化启动意图与
      launch_token，再调用本操作，并把 token 传给受控 wrapper/资源标签。
    - ``poll(handle)``：刷新 Job 状态。
    - ``interrupt(handle)``：中断本 Job 拥有的进程树/容器。
    - ``collect(handle, cursor)``：从 cursor 起采集归一化结果并返回新 cursor。
    - ``cleanup(handle)``：清理本 Job 资源并列出残留。
    """

    def prepare(self, spec: ExternalJobSpec) -> ExternalJobHandle: ...

    def start(self, spec: ExternalJobSpec, handle: ExternalJobHandle) -> ExternalJobHandle: ...

    def poll(self, handle: ExternalJobHandle) -> ExternalJobHandle: ...

    def interrupt(self, handle: ExternalJobHandle) -> ExternalJobHandle: ...

    def collect(
        self, handle: ExternalJobHandle, cursor: dict[str, Any],
    ) -> tuple[list[NormalizedCaseResult], dict[str, Any]]: ...

    def cleanup(self, handle: ExternalJobHandle) -> dict[str, Any]: ...
