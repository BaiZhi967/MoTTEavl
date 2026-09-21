"""M5-T09b/T09c：ScoringJob 服务（持久作业、调用账本、原子发布）。

职责边界：
- 提交只做预检与持久化：request key（幂等键）-> job -> 预留 ScoringPass id 一次
  写完。没有授权或预算不可执行时**一个调用都不发**，也不留下 job。
- 执行必须由 WorkerLoop / 执行锁领取（claim_next / run_claimed）。API handler
  只提交与读取；任何 GET 都不会领取或触发 job。
- 每次 Judge 调用都有 prepared -> dispatching -> settled 边界，先落库再发出动作；
  本服务不做任何自动模型重试。响应已落地但解析失败时，只允许显式的确定性
  重解析（retry_parse），不会再次调用 Provider。
- 结果先留在 job 上；只有发布政策允许时才在同一个事务里提交 ScoreSet +
  ScoringPass + job receipt + current 指针。
- 取消由本服务自己处理（幂等 + revision CAS），不复用 RunService.cancel；
  任何路径都不修改原 subject Run 的状态或证据。

Worker 接线（见模块末尾 WorkerIntegration 说明）：
    self.scoring_jobs = ScoringJobService(self.service.store, provider_factory=...)
    job = self.scoring_jobs.claim_and_run()          # 在 _claim_and_execute_unlocked 内
    self.scoring_jobs.recover_interrupted()          # 在 _recover_interrupted_unlocked 内
"""
from __future__ import annotations

import hashlib
import os
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Callable, Literal

from pydantic import Field, model_validator

from motte_contracts.evaluation import MetricResult, MetricStatus
from motte_contracts.identity import canonical_sha256
from motte_contracts.messages import Contract
from motte_eval.judge import (
    JUDGE_PURPOSE,
    JudgeAuthorisation,
    JudgeBudgetError,
    JudgeError,
    JudgeInputBundle,
    JudgeInputError,
    JudgeNotAuthorised,
    JudgePairwiseInput,
    JudgeSpec,
    build_judge_input,
    build_judge_request,
    declared_output_ceiling,
    judge_job_fingerprint,
    judge_metrics,
    pairwise_metrics,
    parse_judge_output,
    parse_pairwise_output,
    preflight_judge,
    price_view,
    prompt_token_upper_bound,
)
from motte_provider.errors import is_indeterminate_error
from motte_eval.observation import metric_result_to_score
from motte_storage.integrity import RunConflictError
from motte_storage.scoring_jobs import (
    PUBLISH_POLICIES,
    ScoringJobConflict,
    ScoringJobError,
    job_view,
    new_job_id,
    new_reserved_pass_id,
    scoring_jobs_for,
)

__all__ = [
    "FrozenProviderFactory",
    "JudgeEvidenceError",
    "JudgeProviderPolicy",
    "JudgeProviderSnapshot",
    "JudgeProviderSnapshotError",
    "ScoringJobError",
    "ScoringJobConflict",
    "ScoringJobRequest",
    "ScoringJobService",
    "build_judge_submission",
    "default_artifact_reader",
    "freeze_judge_provider_snapshot",
    "resolve_saved_observations",
]

#: 一次调用的 usage 累计维度；任一已结算调用缺失就整项保持未知。
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")

#: 快照里只作审计、不参与身份的时间戳：重复提交必须得到同一 snapshot hash。
_SNAPSHOT_AUDIT_FIELDS = frozenset({"frozen_at"})


def default_artifact_reader(root: str | None = None) -> Callable[[str], bytes | None]:
    """默认 Artifact 读取器：只读受控 artifact root，路径穿越交给 ArtifactStore 拒绝。"""
    from motte_storage.artifacts import ArtifactStore

    store = ArtifactStore(root or os.environ.get("ARTIFACT_ROOT", "var/artifacts"))

    def read(artifact_id: str) -> bytes | None:
        try:
            return store.read_bytes(artifact_id)
        except Exception:  # noqa: BLE001 - 读取失败按不可得处理，由 judge 层拒绝
            return None

    return read


class JudgeProviderSnapshotError(JudgeError):
    """冻结的 Provider 快照缺失、不完整或与冻结 spec 不一致。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class JudgeEvidenceError(JudgeInputError):
    """服务端保存的 Observation 缺失或归属不符；零调用、零发布。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _verify_saved_observation(run_id: str, case_id: str, raw: Any) -> dict[str, Any]:
    """校验保存的 Observation：契约、evidence_hash 与 run/case 归属。"""
    from motte_contracts.evaluation import FrozenObservation, observation_evidence_hash

    if not isinstance(raw, dict):
        raise JudgeEvidenceError(
            "JUDGE_EVIDENCE_INVALID", f"saved observation is not an object: {case_id}"
        )
    try:
        frozen = FrozenObservation.model_validate(raw)
    except Exception as error:  # noqa: BLE001 - 损坏的观察按不可评证据处理
        raise JudgeEvidenceError(
            "JUDGE_EVIDENCE_INVALID", f"saved observation is invalid: {case_id}: {error}"
        ) from error
    payload = {
        key: value for key, value in raw.items()
        if key not in {"evidence_hash", "recorded_at"}
    }
    if observation_evidence_hash(payload) != raw.get("evidence_hash"):
        raise JudgeEvidenceError(
            "JUDGE_EVIDENCE_INVALID",
            f"saved observation hash does not match its content: {case_id}",
        )
    if frozen.run_id != run_id:
        raise JudgeEvidenceError(
            "JUDGE_EVIDENCE_INVALID",
            f"saved observation belongs to another run: {frozen.run_id!r} != {run_id!r}",
        )
    if frozen.case_id != case_id:
        raise JudgeEvidenceError(
            "JUDGE_EVIDENCE_INVALID",
            f"saved observation belongs to another case: {frozen.case_id!r} != {case_id!r}",
        )
    return raw


def resolve_saved_observations(
    store: Any, run_id: str, case_ids: Any = None,
) -> dict[str, dict[str, Any]]:
    """服务端解析 Judge 证据：只读**保存过的** CaseRun 结果，客户端不提供内容。

    sample_count 因此来自这里解析出的证据数量，而不是客户端填写的计数。
    任何缺失/归属不符都在提交期拒绝：零调用、零发布。
    """
    run = store.runs.get(run_id)
    if run is None:
        raise JudgeEvidenceError("JUDGE_EVIDENCE_MISSING", f"subject run is missing: {run_id}")
    selected = [str(case_id) for case_id in (case_ids or run.get("case_ids") or [])]
    if not selected:
        raise JudgeEvidenceError(
            "JUDGE_EVIDENCE_MISSING", "judge submission requires at least one saved case"
        )
    run_cases = set(run.get("case_ids") or [])
    resolved: dict[str, dict[str, Any]] = {}
    for case_id in selected:
        if case_id in resolved:
            raise JudgeEvidenceError(
                "JUDGE_EVIDENCE_INVALID", f"duplicate case id in the judge request: {case_id}"
            )
        if run_cases and case_id not in run_cases:
            raise JudgeEvidenceError(
                "JUDGE_EVIDENCE_MISSING", f"case is not part of the saved run: {case_id}"
            )
        row = store.case_runs.get(run_id, case_id)
        raw = None
        if isinstance(row, dict):
            result = row.get("result")
            if isinstance(result, dict):
                raw = result.get("observation")
        if not isinstance(raw, dict):
            raise JudgeEvidenceError(
                "JUDGE_EVIDENCE_MISSING",
                f"no saved observation for case: {case_id}",
            )
        resolved[case_id] = _verify_saved_observation(run_id, case_id, raw)
    return resolved


class JudgeProviderSnapshot(Contract):
    """提交期冻结的非秘密 Provider 身份：引用、hash、adapter、端点与凭据引用。

    执行期只按这份快照构造 Provider，不再按可变名称重新解析资源；凭据引用
    只是一个**名字**（凭据文件 profile / 环境变量名），秘密在构造 Provider 时
    才解析，绝不进入 job / manifest / event / log。
    """

    schema_version: Literal[1] = 1
    model_resource_id: str | None = None
    model_profile_generation: int | None = None
    model_profile_sha256: str | None = None
    provider_connection: str | None = None
    provider_connection_generation: int | None = None
    provider_connection_sha256: str | None = None
    adapter_id: str | None = None
    adapter_version: str | None = None
    endpoint: str | None = None
    request_path: str | None = None
    model: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    max_output_tokens: int | None = Field(default=None, gt=0)
    reasoning: dict[str, Any] | None = None
    reasoning_level: str | None = None
    identity_policy: str | None = None
    identity_aliases: dict[str, str] | None = None
    identity_alias_version: str | None = None
    #: 非秘密凭据引用：profile 名与 env 变量名，绝不是密钥本身。
    credential_ref: str | None = None
    api_key_env: str | None = None
    price_table_version: str | None = None
    price_table_sha256: str | None = None
    price_table: dict[str, Any] | None = None
    transport: dict[str, Any] = Field(default_factory=dict)
    frozen_at: str | None = None
    snapshot_sha256: str = Field(min_length=71, max_length=71)

    @model_validator(mode="after")
    def sealed_and_secret_free(self) -> "JudgeProviderSnapshot":
        from .resolve import find_secret_paths

        payload = self.model_dump(mode="json")
        declared = payload.pop("snapshot_sha256")
        for audit in _SNAPSHOT_AUDIT_FIELDS:
            payload.pop(audit, None)
        if declared != canonical_sha256(payload):
            raise ValueError("judge provider snapshot_sha256 does not match its content")
        leaked = find_secret_paths(payload)
        if leaked:
            raise ValueError(
                "judge provider snapshot must only carry non-secret references: "
                + ", ".join(leaked)
            )
        return self

    @classmethod
    def seal(cls, payload: dict[str, Any]) -> "JudgeProviderSnapshot":
        """按内容封存快照：snapshot_sha256 由**契约归一化后**的内容决定。"""
        data = {key: value for key, value in payload.items() if key != "snapshot_sha256"}
        # 先按契约补全默认值，再对归一化视图取 hash，保证校验与封存同一公式。
        data["snapshot_sha256"] = "sha256:" + "0" * 64
        view = cls.model_construct(**data).model_dump(mode="json")
        view.pop("snapshot_sha256")
        # 审计时间戳不进身份：同一资源内容的重复提交必须得到同一快照 hash。
        for audit in _SNAPSHOT_AUDIT_FIELDS:
            view.pop(audit, None)
        data["snapshot_sha256"] = canonical_sha256(view)
        return cls.model_validate(data)

    @classmethod
    def minimal(cls, model: str) -> "JudgeProviderSnapshot":
        """没有提交期快照时的最小身份（库内直连路径，仅用于向后兼容）。"""
        return cls.seal({"model": model})

    def provider_config(self) -> dict[str, Any]:
        """还原成 motte_provider 的 provider 配置（凭据仍只是引用）。"""
        if not self.adapter_id or not self.endpoint:
            raise JudgeProviderSnapshotError(
                "JUDGE_SNAPSHOT_INCOMPLETE",
                "a frozen judge provider snapshot requires adapter_id and endpoint",
            )
        config: dict[str, Any] = {
            "kind": self.adapter_id,
            "implementation_version": self.adapter_version,
            "base_url": self.endpoint,
            "model": self.model,
            "parameters": dict(self.parameters or {}),
            "credentials": self.credential_ref,
            "api_key_env": self.api_key_env,
            "price_table": self.price_table,
            "max_output_tokens": self.max_output_tokens,
            "reasoning": self.reasoning,
            "reasoning_level": self.reasoning_level,
            "identity_policy": self.identity_policy or "report_only",
            "identity_aliases": self.identity_aliases,
            "identity_alias_version": self.identity_alias_version,
            **{key: value for key, value in (self.transport or {}).items()},
        }
        if self.request_path:
            config["request_path"] = self.request_path
        return {key: value for key, value in config.items() if value is not None}


def freeze_judge_provider_snapshot(
    *,
    model_resource_id: str,
    resources: Any,
    price_table_version: str | None = None,
    frozen_at: str | None = None,
) -> JudgeProviderSnapshot:
    """提交期解析已发布 ModelProfile / ProviderConnection / 价格版本并封存快照。

    解析复用既有 resolve_manifest（生命周期、enabled、adapter 与价格解析都在
    这里发生），因此 API 与 CLI 得到同一份证据；执行期不再读资源仓库。
    """
    from motte_provider.config import provider_class_for

    from .resolve import resolve_manifest

    manifest: dict[str, Any] = {"model": model_resource_id}
    if price_table_version:
        manifest["price_table_version"] = price_table_version
    resolved = resolve_manifest(manifest, resources)
    provider = resolved.get("provider")
    if not isinstance(provider, dict):
        raise JudgeProviderSnapshotError(
            "JUDGE_PROVIDER_UNRESOLVED",
            f"model {model_resource_id} did not resolve to a provider connection",
        )
    adapter_id = provider.get("adapter_id") or provider.get("kind")
    if not isinstance(adapter_id, str) or not adapter_id:
        raise JudgeProviderSnapshotError(
            "JUDGE_PROVIDER_UNRESOLVED", "resolved provider has no adapter identity"
        )
    if provider_class_for(adapter_id) is None:
        # replay / 脚本 adapter 没有 .complete：不能作为 Judge Provider。
        raise JudgeProviderSnapshotError(
            "JUDGE_ADAPTER_UNSUPPORTED",
            f"provider adapter {adapter_id!r} cannot serve judge completions",
        )
    wire_model = provider.get("model")
    if not isinstance(wire_model, str) or not wire_model:
        raise JudgeProviderSnapshotError(
            "JUDGE_PROVIDER_UNRESOLVED", "resolved provider has no model"
        )
    snapshots = resolved.get("resource_snapshots") or {}
    model_snapshot = snapshots.get("model_profile") or {}
    connection_snapshot = snapshots.get("provider_connection") or {}
    price_snapshot = snapshots.get("price_table") or {}
    price_record = provider.get("price_table")
    price_record = price_record if isinstance(price_record, dict) else None
    transport = {
        key: provider[key]
        for key in ("timeout", "max_retries", "backoff_initial_ms", "backoff_max_ms")
        if provider.get(key) is not None
    }
    ceiling = provider.get("max_output_tokens")
    return JudgeProviderSnapshot.seal({
        "model_resource_id": model_resource_id,
        "model_profile_generation": model_snapshot.get("generation"),
        "model_profile_sha256": (
            model_snapshot.get("content_hash") or model_snapshot.get("profile_hash")
        ),
        "provider_connection": connection_snapshot.get("name") or provider.get("name"),
        "provider_connection_generation": connection_snapshot.get("generation"),
        "provider_connection_sha256": connection_snapshot.get("content_hash"),
        "adapter_id": adapter_id,
        "adapter_version": (
            provider.get("adapter_version") or provider.get("implementation_version")
        ),
        "endpoint": provider.get("base_url"),
        "request_path": provider.get("request_path"),
        "model": wire_model,
        "parameters": dict(provider.get("parameters") or {}),
        "max_output_tokens": ceiling if type(ceiling) is int and ceiling > 0 else None,
        "reasoning": provider.get("reasoning") or None,
        "reasoning_level": provider.get("reasoning_level"),
        "identity_policy": provider.get("identity_policy"),
        "identity_aliases": provider.get("identity_aliases") or None,
        "identity_alias_version": provider.get("identity_alias_version"),
        "credential_ref": provider.get("credentials") or provider.get("name"),
        "api_key_env": provider.get("api_key_env"),
        "price_table_version": (
            (price_record or {}).get("version") or price_snapshot.get("version")
        ),
        "price_table_sha256": price_snapshot.get("content_hash"),
        "price_table": price_record,
        "transport": transport,
        "frozen_at": frozen_at or _now(),
    })


class FrozenProviderFactory:
    """按冻结快照构造 Judge Provider；秘密引用只在**构造期**解析一次。

    这是 Worker 的 provider_factory 契约：参数是 JudgeProviderSnapshot，不是
    可变的模型名字。快照缺失 adapter/endpoint 时明确拒绝，绝不回落到按名字
    重新解析资源。
    """

    def __call__(self, snapshot: Any) -> Any:
        frozen = (
            snapshot if isinstance(snapshot, JudgeProviderSnapshot)
            else JudgeProviderSnapshot.model_validate(snapshot)
        )
        from motte_provider.config import build_case_provider

        try:
            built = build_case_provider(frozen.provider_config(), {})
        except (KeyError, ValueError, TypeError) as error:
            raise JudgeProviderSnapshotError(
                "JUDGE_PROVIDER_BUILD_FAILED", str(error)
            ) from error
        provider = getattr(built, "provider", built)
        if not hasattr(provider, "complete"):
            raise JudgeProviderSnapshotError(
                "JUDGE_PROVIDER_BUILD_FAILED",
                f"provider adapter {frozen.adapter_id!r} exposes no completion call",
            )
        return provider


class JudgeProviderPolicy(Contract):
    """Judge Provider 的付费安全策略；默认关闭一切自动重试。

    token 硬承诺来自两个可证明的 Provider 能力：
    - prompt_token_bound：能否给出 prompt 的 token 上界。冻结请求的 UTF-8
      字节数就是字节级 BPE 词表的 token 上界；None 表示无法证明。
    - enforces_output_limit：Provider 是否真会按请求里的 max_output_tokens
      截断输出；只有它成立时输出才是可证明的上界。
    任一能力缺失时，任何金额硬上限请求都在提交期被拒绝，而不是把估算
    叫做硬上限。
    """

    automatic_model_retries: bool = False
    max_calls_per_job: int = Field(default=32, ge=1)
    prompt_token_bound: Literal["rendered_request_utf8_bytes"] | None = (
        "rendered_request_utf8_bytes"
    )
    enforces_output_limit: bool = True

    @model_validator(mode="after")
    def never_rebills(self) -> "JudgeProviderPolicy":
        if self.automatic_model_retries:
            raise ValueError(
                "automatic model retries can re-bill a judge call; "
                "parse retries are explicit and deterministic only"
            )
        return self


class ScoringJobRequest(Contract):
    """一次 Judge 评分请求；request_key 与内容 fingerprint 分离。"""

    request_key: str = Field(min_length=1)
    judge_spec: JudgeSpec
    mode: Literal["single", "pairwise"] = "single"
    run_id: str | None = None
    source_pass_id: str | None = None
    observations: dict[str, dict[str, Any]] = Field(default_factory=dict)
    pairwise_pairs: list[JudgePairwiseInput] = Field(default_factory=list)
    calibration_job_id: str | None = None
    sample_ids: dict[str, str] = Field(default_factory=dict)
    authorisation: JudgeAuthorisation | None = None
    publish_policy: Literal["all_scored", "allow_non_scored"] = "all_scored"
    repeats: int = Field(default=1, ge=1)
    presentation_orders: list[list[str]] = Field(default_factory=list)
    estimated_prompt_tokens: int = Field(default=0, ge=0)
    estimated_completion_tokens: int = Field(default=0, ge=0)
    price_table: dict[str, Any] | None = None
    #: 提交期冻结的非秘密 Provider 身份；存在时执行只按它构造 Provider。
    provider_snapshot: JudgeProviderSnapshot | None = None

    @model_validator(mode="after")
    def shape(self) -> "ScoringJobRequest":
        if self.mode != self.judge_spec.mode:
            raise ValueError("request mode must match the frozen judge spec mode")
        if self.calibration_job_id is None and not self.run_id:
            raise ValueError("scoring job requires run_id or calibration_job_id")
        if self.calibration_job_id is not None and self.run_id is not None:
            raise ValueError(
                "calibration scoring jobs must not reference a subject Run"
            )
        if self.mode == "single":
            if not self.observations:
                raise ValueError("single-mode scoring job requires observations")
            if self.pairwise_pairs:
                raise ValueError("single-mode scoring job cannot carry pairwise pairs")
        else:
            if not self.pairwise_pairs:
                raise ValueError("pairwise scoring job requires fixed candidate pairs")
            if self.observations:
                raise ValueError("pairwise scoring job cannot carry single observations")
        if self.calibration_job_id is not None:
            missing = set(self.observations or {}) - set(self.sample_ids)
            if missing:
                raise ValueError(
                    "calibration samples must be owned by calibration_job/sample: "
                    f"{sorted(missing)}"
                )
        return self

    @property
    def owner(self) -> dict[str, Any]:
        if self.calibration_job_id is not None:
            return {
                "kind": "calibration",
                "calibration_job_id": self.calibration_job_id,
                "sample_ids": dict(self.sample_ids),
            }
        return {"kind": "subject", "run_id": self.run_id}

    @property
    def sample_count(self) -> int:
        if self.mode == "single":
            return len(self.observations)
        return len(self.pairwise_pairs)

    @property
    def orderings(self) -> int:
        # 每个候选对都会按 presentation_orders 逐个展示；每次展示都是一个独立 plan。
        if self.mode == "single":
            return 1
        return len(self.presentation_orders) or 1


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _accumulate_cost(
    calls: list[dict[str, Any]] | None, current_cost: float | None,
) -> float | None:
    """累计已结算调用的实际费用；任一次费用未知 → 总额保持未知，绝不补 0。"""
    settled = [
        item.get("cost_usd")
        for item in calls or []
        if item.get("status") == "settled"
        and item.get("outcome") in {"succeeded", "indeterminate"}
    ]
    if any(cost is None for cost in settled) or current_cost is None:
        return None
    return round(sum(float(cost) for cost in settled) + float(current_cost), 8)


def _accumulate_usage(
    calls: list[dict[str, Any]] | None, current_usage: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """累计已结算调用的实际 usage；任一维度缺失就整项保持未知。"""
    settled = [
        item.get("usage") or {}
        for item in calls or []
        if item.get("status") == "settled" and item.get("outcome") == "succeeded"
    ]
    usages = [*settled, current_usage or {}]
    totals: dict[str, Any] = {}
    for key in _USAGE_KEYS:
        values = [entry.get(key) for entry in usages]
        totals[key] = (
            sum(int(value) for value in values)
            if values and all(
                type(value) is int and value >= 0 for value in values
            )
            else None
        )
    return totals


def build_judge_submission(
    *,
    store: Any,
    resources: Any,
    request_key: str,
    run_id: str,
    mode: str,
    spec_request: dict[str, Any],
    case_ids: Any = None,
    source_pass_id: str | None = None,
    authorisation: Any = None,
    publish_policy: str = "all_scored",
    repeats: int = 1,
    presentation_orders: Any = None,
    price_table_version: str | None = None,
) -> ScoringJobRequest:
    """API 与 CLI 共用的提交编译：服务端解析资源与证据，客户端只给引用。

    - spec_request.model 是**已发布 ModelProfile id**；线路模型、adapter、端点与
      价格版本都在这里解析并冻结；
    - 预算的 price_known / price_table_version 由服务端从解析出的价格表填写，
      客户端声明不了"价格已知"；
    - 证据只从保存过的 CaseRun 结果解析，客户端不提供 observation 内容，
      sample_count 因此也不是客户端能填的。
    """
    from motte_eval.judge import JudgeAuthorisation, JudgeBudget, build_judge_spec

    if mode != "single":
        raise JudgeInputError(
            "the public judge submission path accepts single-mode jobs only"
        )
    model_resource_id = spec_request.get("model")
    if not isinstance(model_resource_id, str) or not model_resource_id:
        raise JudgeInputError("judge submission requires a published model resource id")
    snapshot = freeze_judge_provider_snapshot(
        model_resource_id=model_resource_id,
        resources=resources,
        price_table_version=price_table_version,
    )
    price = price_view(snapshot.price_table)
    budget_request = spec_request.get("budget") or {}
    budget = JudgeBudget(
        max_calls=budget_request.get("max_calls"),
        max_prompt_tokens=budget_request.get("max_prompt_tokens", 0),
        max_completion_tokens=budget_request.get("max_completion_tokens", 0),
        price_known=bool(price["known"]),
        price_table_version=(price["version"] if price["known"] else None),
        hard_cost_cap_usd=budget_request.get("hard_cost_cap_usd"),
    )
    spec = build_judge_spec(
        judge_profile_id=str(spec_request.get("judge_profile_id") or ""),
        model=snapshot.model,
        rubric_id=str(spec_request.get("rubric_id") or ""),
        rubric_version=str(spec_request.get("rubric_version") or ""),
        criteria=list(spec_request.get("criteria") or []) or None,
        parameters=dict(spec_request.get("parameters") or {}),
        input_selector=spec_request.get("input_selector"),
        missing_evidence_policy=spec_request.get("missing_evidence_policy"),
        calibration_version=spec_request.get("calibration_version"),
        budget=budget,
        mode=mode,
    )
    observations = resolve_saved_observations(store, run_id, case_ids)
    auth = authorisation
    if isinstance(auth, dict):
        auth = JudgeAuthorisation.model_validate(auth)
    return ScoringJobRequest(
        request_key=request_key,
        judge_spec=spec,
        mode=mode,
        run_id=run_id,
        source_pass_id=source_pass_id,
        observations=observations,
        authorisation=auth,
        publish_policy=publish_policy,
        repeats=repeats,
        presentation_orders=[list(order) for order in (presentation_orders or [])],
        price_table=snapshot.price_table,
        provider_snapshot=snapshot,
    )


class ScoringJobService:
    """ScoringJob 的提交、领取、执行、取消与发布。"""

    def __init__(
        self,
        store: Any,
        *,
        provider_factory: Callable[[str], Any] | None = None,
        jobs: Any | None = None,
        artifact_reader: Callable[[str], bytes | None] | None = None,
        policy: JudgeProviderPolicy | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.jobs = jobs if jobs is not None else scoring_jobs_for(store)
        self.provider_factory = provider_factory
        self._artifact_reader = artifact_reader
        self.policy = policy or JudgeProviderPolicy()
        self._clock = clock or _now

    # ------------------------------------------------------------- 只读
    def get(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job_view(job)

    def get_by_request_key(self, request_key: str) -> dict[str, Any] | None:
        job = self.jobs.get_by_request_key(request_key)
        return job_view(job) if job is not None else None

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return [job_view(job) for job in self.jobs.list_for_run(run_id)]

    def history(self, run_id: str) -> dict[str, Any]:
        """评分历史读取：零模型调用，不领取、不触发任何 job。"""
        return {"run_id": run_id, "jobs": self.list_for_run(run_id)}

    def get_public(self, job_id: str) -> dict[str, Any]:
        """公共读取视图：不含输入原文与响应正文。"""
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return self.public_view(job)

    def history_public(self, run_id: str) -> list[dict[str, Any]]:
        return [self.public_view(job) for job in self.jobs.list_for_run(run_id)]

    # ------------------------------------------------------------- 提交
    def _compile(self, request: ScoringJobRequest) -> dict[str, Any]:
        """预检与提交共用的唯一编译路径：计划、预算、fingerprint 与冻结快照。

        预检不落库、不构造 Provider；提交只多一步持久化。两条路径消费同一份
        编译结果，因此不存在第二个调用数公式。
        """
        spec = request.judge_spec
        snapshot = request.provider_snapshot
        if snapshot is not None and snapshot.model != spec.model:
            raise JudgeProviderSnapshotError(
                "JUDGE_SNAPSHOT_MODEL_MISMATCH",
                "frozen provider snapshot model differs from the judge spec model: "
                f"{snapshot.model!r} != {spec.model!r}",
            )
        inputs, plans = self._prepare_inputs(request)
        # 先编译完整请求计划，再算预算与 fingerprint：预检的调用数就是计划长度，
        # 不存在第二个公式。
        plans = self._freeze_plans(spec, inputs, plans, request.price_table)
        if snapshot is not None and snapshot.max_output_tokens is not None:
            for plan in plans:
                value = plan.get("output_tokens")
                if value is not None and value > snapshot.max_output_tokens:
                    raise JudgeProviderSnapshotError(
                        "JUDGE_OUTPUT_CEILING_UNSUPPORTED",
                        f"frozen plan output ceiling {value} exceeds the frozen model "
                        f"ceiling {snapshot.max_output_tokens}",
                    )
        allowance = self._allowance(plans)
        preflight = preflight_judge(
            spec,
            mode=request.mode,
            sample_count=request.sample_count,
            repeats=request.repeats,
            orderings=request.orderings,
            max_calls=len(plans),
            authorisation=request.authorisation,
            price_table=request.price_table,
            estimated_prompt_tokens=request.estimated_prompt_tokens,
            estimated_completion_tokens=request.estimated_completion_tokens,
            prompt_token_upper_bound=(
                allowance["max_prompt_tokens"]
                if self.policy.prompt_token_bound else None
            ),
            output_token_ceiling=plans[0].get("output_tokens") if plans else None,
            output_bound_provable=self.policy.enforces_output_limit,
        )
        if not preflight.authorised:
            raise JudgeNotAuthorised(
                "judge scoring requires an explicit authorisation; zero calls issued: "
                + "; ".join(preflight.reasons)
            )
        if not preflight.budget_executable:
            raise JudgeBudgetError(
                "judge budget is not executable; zero calls issued: "
                + "; ".join(preflight.reasons)
            )
        if preflight.max_calls > self.policy.max_calls_per_job:
            raise JudgeBudgetError(
                f"job would issue {preflight.max_calls} calls, above the provider "
                f"policy limit of {self.policy.max_calls_per_job}"
            )

        owner = request.owner
        owner_ref = (
            f"run:{owner['run_id']}" if owner["kind"] == "subject"
            else f"calibration:{owner['calibration_job_id']}"
        )
        fingerprint = judge_job_fingerprint(
            owner_ref=owner_ref,
            source_pass_id=request.source_pass_id,
            observation_digests=sorted(
                plan["input_sha256"] for plan in plans
            ),
            case_ids=sorted(plan["case_id"] for plan in plans),
            spec=spec,
            mode=request.mode,
            publish_policy=request.publish_policy,
            repeats=request.repeats,
            presentation_order=(
                list(request.presentation_orders[0])
                if request.mode == "pairwise" and request.presentation_orders
                else None
            ),
            calibration_version=spec.calibration_version,
            plans=plans,
        )
        if snapshot is not None:
            # Provider 身份也是请求身份的一部分：资源变了，同一 request_key 就是
            # 不同内容（409），而不是静默复用旧快照。
            fingerprint = canonical_sha256({
                "job_fingerprint": fingerprint,
                "provider_snapshot_sha256": snapshot.snapshot_sha256,
            })
        return {
            "spec": spec,
            "inputs": inputs,
            "plans": plans,
            "allowance": allowance,
            "preflight": preflight,
            "owner": owner,
            "owner_ref": owner_ref,
            "fingerprint": fingerprint,
            "snapshot": (
                snapshot.model_dump(mode="json") if snapshot is not None else None
            ),
        }

    def preflight(self, request: ScoringJobRequest) -> dict[str, Any]:
        """零费用预检：不落作业、不构造 Provider、不调用模型。"""
        compiled = self._compile(request)
        report = compiled["preflight"].model_dump(mode="json")
        report["executed"] = False
        report["provider_factory_available"] = self.provider_factory is not None
        report["provider_snapshot"] = deepcopy(compiled["snapshot"])
        report["model_resource_id"] = (
            (compiled["snapshot"] or {}).get("model_resource_id")
        )
        return report

    def submit(self, request: ScoringJobRequest) -> dict[str, Any]:
        compiled = self._compile(request)
        if self.provider_factory is None:
            raise JudgeError(
                "no judge provider factory is configured; submitting would create an "
                "unexecutable job"
            )
        spec = compiled["spec"]
        owner = compiled["owner"]
        owner_ref = compiled["owner_ref"]
        plans = compiled["plans"]
        preflight = compiled["preflight"]
        job_id = new_job_id()
        reserved_pass_id = new_reserved_pass_id()
        record = {
            "schema_version": 1,
            "job_id": job_id,
            "request_key": request.request_key,
            "fingerprint": compiled["fingerprint"],
            "owner": deepcopy(owner),
            "owner_kind": owner["kind"],
            "owner_ref": owner_ref,
            "run_id": (
                owner["run_id"] if owner["kind"] == "subject" else owner_ref
            ),
            "source_pass_id": request.source_pass_id,
            "status": "queued",
            "revision": 1,
            "reserved_pass_id": reserved_pass_id,
            "mode": request.mode,
            "publish_policy": request.publish_policy,
            "repeats": request.repeats,
            "presentation_orders": [list(item) for item in request.presentation_orders],
            "judge_spec": spec.model_dump(mode="json"),
            "judge_spec_sha256": spec.spec_sha256,
            "budget": spec.budget.model_dump(mode="json"),
            "preflight": preflight.model_dump(mode="json"),
            "authorisation": (
                request.authorisation.model_dump(mode="json")
                if request.authorisation is not None else None
            ),
            "calibration_version": spec.calibration_version,
            "inputs": {
                key: value.model_dump(mode="json")
                for key, value in compiled["inputs"].items()
            },
            # 冻结的唯一调用计划：call_id / owner / mode / repeat_index /
            # presentation_order / input_sha256 每项各有一份，执行只消费这一份。
            "plans": plans,
            "allowance": compiled["allowance"],
            "input_digests": {
                plan["case_id"]: plan["input_sha256"] for plan in plans
            },
            "price_table": deepcopy(request.price_table),
            # 提交期冻结的 Provider 身份：执行期只按它构造 Provider。
            "provider_snapshot": deepcopy(compiled["snapshot"]),
            "usage_total": None,
            "calls": [],
            "cost_total_usd": 0.0 if preflight.price_coverage["known"] else None,
            "price_coverage": deepcopy(preflight.price_coverage),
            "created_at": self._clock(),
            "purpose": JUDGE_PURPOSE,
        }
        outcome = self.jobs.submit(record)
        result = job_view(outcome["job"])
        result["reused"] = not outcome["created"]
        result["preflight"] = deepcopy(preflight.model_dump(mode="json"))
        result["provider_snapshot"] = deepcopy(compiled["snapshot"])
        return result

    def public_view(self, job: dict[str, Any]) -> dict[str, Any]:
        """公共作业视图：身份、计划、账本与结果；不含输入原文与响应正文。"""
        view = job_view(job)
        view.pop("inputs", None)
        view["plans"] = [
            {key: value for key, value in plan.items() if key != "pair"}
            for plan in view.get("plans") or []
        ]
        calls = []
        for item in view.get("calls") or []:
            raw = item.get("raw_response")
            calls.append({
                **item,
                "raw_response": (
                    {
                        "response_id": raw.get("response_id"),
                        "provider": raw.get("provider"),
                        "model": raw.get("model"),
                    }
                    if isinstance(raw, dict) else None
                ),
            })
        view["calls"] = calls
        return view

    # ------------------------------------------------------------- 领取与执行
    def claim_next(self) -> dict[str, Any] | None:
        return self.jobs.claim_next()

    def run_claimed(self, job: dict[str, Any]) -> dict[str, Any]:
        if job["status"] == "completed":
            # 通知/HTTP 响应丢失后的重复调用：只返回原 receipt。
            view = job_view(job)
            view["published"] = False
            view["publish_outcome"] = "already_completed"
            view["receipt"] = deepcopy(job.get("receipt"))
            return view
        if job["status"] == "cancelled":
            view = job_view(job)
            view["published"] = False
            view["publish_outcome"] = "already_cancelled"
            view["receipt"] = None
            return view
        if job["status"] == "settled":
            # 崩溃窗口「响应已 settled、解析前」：只重做确定性解析，零新调用。
            return self._finalize(job)
        if job["status"] == "indeterminate":
            return job_view(job)
        if job["status"] != "prepared":
            raise ScoringJobError(
                f"job must be claimed before execution, got {job['status']!r}"
            )
        current = job
        for index, plan in enumerate(job["plans"]):
            latest = self.jobs.get(job["job_id"])
            if latest is None:
                raise ScoringJobError(f"scoring job disappeared: {job['job_id']}")
            if (latest.get("cancellation") or {}).get("requested"):
                return self._finalize_cancellation(latest)
            existing = next(
                (
                    item for item in latest.get("calls") or []
                    if item.get("call_id") == plan["call_id"]
                ),
                None,
            )
            if existing is not None:
                # 已经 dispatch 过的调用绝不重发：已结算的跳过，未结算的保留不确定。
                if existing.get("status") == "dispatching":
                    return self._call_indeterminate(
                        latest, plan,
                        "a dispatched judge call was never settled; it is never resent",
                    )
                continue
            request = self._request_for(current, plan)
            invocation = self._invocation_record(current, plan)
            call = {
                "call_id": plan["call_id"],
                "case_id": plan["case_id"],
                "mode": plan["mode"],
                "repeat_index": plan.get("repeat_index", 0),
                "candidate_ids": plan.get("candidate_ids") or [],
                "presentation_order": plan.get("presentation_order") or [],
                "input_sha256": plan["input_sha256"],
                "ordinal": index + 1,
                # dispatch 前原子预留额度：已发出未结算的调用占用这份额度。
                "reservation": deepcopy(plan.get("reservation")),
            }
            current = self.jobs.begin_call(
                current["job_id"], expected_revision=current["revision"],
                invocation=invocation, call=call,
            )
            try:
                envelope = self._complete(current, request)
            except Exception as error:  # noqa: BLE001 - 调用失败即持久边界
                return self._call_failed(current, plan, error)
            summary = self._response_summary(envelope)
            # 响应落地后重新读取 job：取消请求可能已经推进过 revision。
            latest = self.jobs.get(current["job_id"])
            if latest is None:
                raise ScoringJobError(
                    f"scoring job disappeared: {current['job_id']}"
                )
            if latest["status"] != "dispatching":
                return self._finalize_cancellation(latest)
            last_call = index == len(job["plans"]) - 1
            current = self.jobs.settle_call(
                latest["job_id"], expected_revision=latest["revision"],
                call_id=plan["call_id"], outcome="succeeded",
                result_summary=summary,
                job_status="settled" if last_call else "dispatching",
                job_changes={
                    "cost_total_usd": _accumulate_cost(
                        latest.get("calls"), summary.get("cost_usd"),
                    ),
                    "usage_total": _accumulate_usage(
                        latest.get("calls"), summary.get("usage"),
                    ),
                    "returns": (latest.get("returns") or 0) + 1,
                },
            )
        return self._finalize(current)

    def claim_and_run(self, job_id: str | None = None) -> dict[str, Any] | None:
        if job_id is not None:
            claimed = self.jobs.claim(job_id)
        else:
            claimed = self.jobs.claim_next()
        if claimed is None:
            return None
        return self.run_claimed(claimed)

    def recover_interrupted(self) -> list[str]:
        """崩溃恢复：prepared 回队列、dispatching 标不确定（绝不自动重发）。

        已 dispatch 但结果未知时费用必须保持未知：把 0.0 的"暂无支出"改回 None，
        否则不确定作业会假装自己没有产生费用（F17）。
        """
        touched = self.jobs.recover_interrupted()
        for job_id in touched:
            job = self.jobs.get(job_id)
            if job is None or job.get("status") != "indeterminate":
                continue
            if job.get("cost_total_usd") is None:
                continue
            self.jobs.transition(
                job_id, expected_revision=job["revision"],
                expected_status="indeterminate", status="indeterminate",
                allow_same_status=True, changes={"cost_total_usd": None},
            )
        return touched

    def retry_parse(self, job_id: str) -> dict[str, Any]:
        """确定性重解析：响应已落地的 failed job 可以零费用重试解析。"""
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] == "completed":
            return job_view(job)
        if job["status"] != "failed":
            raise ScoringJobError(
                "deterministic parse retry requires a failed job with settled responses"
            )
        if job.get("failure", {}).get("code") != "JUDGE_PARSE_FAILED":
            raise ScoringJobError(
                "only parse failures can be retried without another model call"
            )
        reopened = self.jobs.transition(
            job_id, expected_revision=job["revision"], expected_status="failed",
            status="settled", changes={"failure": None, "reopened_at": self._clock()},
        )
        return self._finalize(reopened)

    # ------------------------------------------------------------- 取消
    def cancel(self, job_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        outcome = self.jobs.cancel(job_id, actor=actor, reason=reason)
        return {
            "job": job_view(outcome["job"]),
            "outcome": outcome["outcome"],
            "receipt": deepcopy(outcome.get("receipt")),
            "billed_calls": outcome.get("billed_calls"),
            "in_flight": bool(outcome.get("in_flight")),
            "note": outcome.get("note"),
        }

    # ------------------------------------------------------------- 内部：输入
    def _prepare_inputs(
        self, request: ScoringJobRequest,
    ) -> tuple[dict[str, JudgeInputBundle], list[dict[str, Any]]]:
        """编译唯一的调用计划：每个 pair/order/repeat 各有一个稳定 call_id。

        - 显式 presentation_orders 才做换序扩展；未显式给出时每个 pair 只按
          自己的 presentation_order 评一次，绝不笛卡尔扩展成 N x N 次调用。
        - repeats 每个重复都是一次独立调用：有自己的 call_id、repeat_index 与
          账本条目，因此也有自己的 ScoreSet 行（trial_id = call_id）。
        """
        self._verify_evidence_ownership(request)
        if request.mode == "pairwise":
            plans: list[dict[str, Any]] = []
            explicit = [list(order) for order in request.presentation_orders]
            index = 0
            for pair in request.pairwise_pairs:
                self._verify_pairwise_candidates(request, pair)
                orders = explicit or [list(pair.presentation_order)]
                for order in orders:
                    if len(order) != 2 or sorted(order) != sorted(pair.presentation_order):
                        raise JudgeInputError(
                            "presentation order must permute the same candidate pair"
                        )
                    reordered = pair.model_copy(update={
                        "presentation_order": list(order),
                        "input_sha256": canonical_sha256({
                            **{
                                key: value
                                for key, value in pair.model_dump(mode="json").items()
                                if key != "input_sha256"
                            },
                            "presentation_order": list(order),
                        }),
                    })
                    for repeat_index in range(request.repeats):
                        index += 1
                        plans.append({
                            "call_id": f"call-{index}",
                            "case_id": reordered.task_ref,
                            "mode": "pairwise",
                            "pair_id": reordered.pair_id,
                            "repeat_index": repeat_index,
                            "candidate_ids": [
                                reordered.candidate_a.candidate.candidate_id,
                                reordered.candidate_b.candidate.candidate_id,
                            ],
                            "presentation_order": list(order),
                            "input_sha256": reordered.input_sha256,
                            "pair": reordered.model_dump(mode="json"),
                        })
            if not plans:
                raise JudgeInputError("pairwise scoring job requires at least one plan")
            return {}, plans

        inputs: dict[str, JudgeInputBundle] = {}
        plans = []
        index = 0
        for case_id, observation in request.observations.items():
            self._verify_observation(request, case_id, observation)
            bundle = build_judge_input(
                request.judge_spec, observation,
                artifact_reader=self._artifact_reader,
            )
            inputs[case_id] = bundle
            for repeat_index in range(request.repeats):
                index += 1
                plans.append({
                    "call_id": f"call-{index}",
                    "case_id": case_id,
                    "mode": "single",
                    "repeat_index": repeat_index,
                    "presentation_order": [],
                    "observation_id": bundle.observation_id,
                    "input_sha256": bundle.input_sha256,
                })
        return inputs, plans

    # ------------------------------------------------------------- 内部：证据归属
    def _subject_run(self, request: ScoringJobRequest) -> dict[str, Any]:
        """subject owner 必须绑定一个**保存过的** Run。"""
        if request.run_id is None:
            raise JudgeInputError("subject scoring job requires a run_id")
        run = self.store.runs.get(request.run_id)
        if run is None:
            raise JudgeInputError(f"subject run is missing: {request.run_id}")
        if request.source_pass_id:
            source = self.store.scoring_passes.get(request.source_pass_id)
            if source is None:
                raise JudgeInputError(
                    f"source scoring pass is missing: {request.source_pass_id}"
                )
            if source.get("run_id") != request.run_id:
                raise JudgeInputError(
                    "source scoring pass does not belong to the subject run"
                )
        return run

    def _verify_case(self, run: dict[str, Any], case_id: str, *, what: str) -> None:
        case_ids = run.get("case_ids")
        if isinstance(case_ids, list) and case_ids and case_id not in case_ids:
            raise JudgeInputError(f"{what} is not part of the saved run: {case_id}")

    def _verify_evidence_ownership(self, request: ScoringJobRequest) -> None:
        """提交前核对 owner/case/证据身份；不匹配时零调用、零发布。

        calibration 使用自己的命名空间，不能冒充 subject：它没有 Run 可绑定，
        样本 key 必须由 sample_ids 拥有（模型层已校验）。subject 则必须绑定
        保存过的 Run 与同属该 Run 的 source pass。
        """
        if request.owner["kind"] == "calibration":
            return
        self._subject_run(request)

    def _verify_observation(
        self, request: ScoringJobRequest, case_id: str, observation: Any,
    ) -> None:
        raw = (
            observation.model_dump(mode="json")
            if isinstance(observation, Contract) else dict(observation)
        )
        if raw.get("case_id") != case_id:
            raise JudgeInputError(
                "observation case_id does not match the request case key: "
                f"{raw.get('case_id')!r} != {case_id!r}"
            )
        if request.owner["kind"] == "calibration":
            return
        run = self._subject_run(request)
        if raw.get("run_id") != request.run_id:
            raise JudgeInputError(
                "observation belongs to another run: "
                f"{raw.get('run_id')!r} != {request.run_id!r}"
            )
        self._verify_case(run, case_id, what="observation case")
        attempt_id = raw.get("attempt_id")
        if attempt_id:
            attempt = self.store.attempts.get(str(attempt_id))
            if attempt is None or attempt.get("run_id") != request.run_id:
                raise JudgeInputError(
                    f"observation attempt does not belong to the run: {attempt_id}"
                )

    def _verify_pairwise_candidates(
        self, request: ScoringJobRequest, pair: JudgePairwiseInput,
    ) -> None:
        if request.owner["kind"] == "calibration":
            return
        run = self._subject_run(request)
        self._verify_case(run, pair.task_ref, what="pairwise task_ref")
        for side in (pair.candidate_a, pair.candidate_b):
            ref = side.candidate
            if ref.owner_kind != "subject":
                raise JudgeInputError(
                    "a subject scoring job cannot judge calibration candidates"
                )
            if ref.run_id != request.run_id:
                raise JudgeInputError(
                    f"candidate {ref.candidate_id} belongs to another run: {ref.run_id}"
                )
            self._verify_case(run, str(ref.case_id), what="candidate case")

    # ------------------------------------------------------------- 内部：预算
    def _output_ceiling(self, spec: JudgeSpec, calls: int) -> int | None:
        """单次调用的输出上限：冻结参数与预算额度的较小值。

        没有可执行的上限（spec 没声明、预算没有 completion 额度，或 Provider
        不强制执行该参数）时返回 None：此时任何金额硬上限都不可证明。
        """
        if not self.policy.enforces_output_limit:
            return None
        candidates: list[int] = []
        declared = declared_output_ceiling(spec)
        if declared is not None:
            candidates.append(declared)
        if spec.budget.max_completion_tokens > 0 and calls > 0:
            candidates.append(max(1, spec.budget.max_completion_tokens // calls))
        if not candidates:
            return None
        return min(candidates)

    def _freeze_plans(
        self,
        spec: JudgeSpec,
        inputs: dict[str, JudgeInputBundle],
        plans: list[dict[str, Any]],
        price_table: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """把每个调用要预留的额度写进冻结计划（真实渲染请求的可证明上界）。"""
        ceiling = self._output_ceiling(spec, len(plans))
        price = price_view(price_table)
        frozen: list[dict[str, Any]] = []
        for plan in plans:
            bundle = self._bundle_for(spec, inputs, plan)
            prompt_tokens = (
                prompt_token_upper_bound(spec, bundle)
                if self.policy.prompt_token_bound else 0
            )
            cost = None
            if price["known"]:
                cost = round(
                    (
                        prompt_tokens * float(price["input_per_million"] or 0.0)
                        + (ceiling or 0) * float(price["output_per_million"] or 0.0)
                    ) / 1_000_000,
                    8,
                )
            frozen.append({
                **plan,
                "budget_sha256": canonical_sha256(spec.budget.model_dump(mode="json")),
                "output_tokens": ceiling,
                "prompt_tokens_upper_bound": prompt_tokens,
                "reservation": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": ceiling or 0,
                    "cost_usd": cost,
                },
            })
        return frozen

    def _bundle_for(
        self,
        spec: JudgeSpec,
        inputs: dict[str, JudgeInputBundle],
        plan: dict[str, Any],
    ) -> JudgeInputBundle | JudgePairwiseInput:
        if plan["mode"] == "pairwise":
            return JudgePairwiseInput.model_validate(plan["pair"])
        return inputs[plan["case_id"]]

    def _allowance(self, plans: list[dict[str, Any]]) -> dict[str, Any]:
        """提交期冻结的额度：每次 dispatch 都从这个总额度里原子扣除。"""
        reservations = [plan.get("reservation") or {} for plan in plans]
        costs = [item.get("cost_usd") for item in reservations]
        return {
            "max_calls": len(plans),
            "max_prompt_tokens": sum(
                int(item.get("prompt_tokens") or 0) for item in reservations
            ),
            "max_completion_tokens": sum(
                int(item.get("completion_tokens") or 0) for item in reservations
            ),
            "max_cost_usd": (
                round(sum(float(cost) for cost in costs), 8)
                if costs and all(cost is not None for cost in costs)
                else None
            ),
        }

    def _request_for(self, job: dict[str, Any], plan: dict[str, Any]) -> Any:
        spec = JudgeSpec.model_validate(job["judge_spec"])
        ceiling = plan.get("output_tokens")
        if plan["mode"] == "pairwise":
            pair = JudgePairwiseInput.model_validate(plan["pair"])
            return build_judge_request(spec, pair, max_output_tokens=ceiling)
        bundle = JudgeInputBundle.model_validate(job["inputs"][plan["case_id"]])
        return build_judge_request(spec, bundle, max_output_tokens=ceiling)

    @staticmethod
    def _snapshot_identity(job: dict[str, Any]) -> dict[str, Any]:
        """冻结 Provider 的非秘密身份摘要（Invocation 与 pass 共用同一份）。"""
        raw = job.get("provider_snapshot")
        if not isinstance(raw, dict) or not raw:
            return {}
        adapter = raw.get("adapter_id")
        return {
            "provider_snapshot_sha256": raw.get("snapshot_sha256"),
            "model_resource_id": raw.get("model_resource_id"),
            "provider_connection": raw.get("provider_connection"),
            "provider_adapter": (
                f"{adapter}@{raw.get('adapter_version')}" if adapter else None
            ),
            "provider_endpoint": raw.get("endpoint"),
        }

    def _invocation_record(
        self, job: dict[str, Any], plan: dict[str, Any],
    ) -> dict[str, Any]:
        owner = job["owner"]
        if owner["kind"] == "subject":
            run_id = owner["run_id"]
            case_id = plan["case_id"]
            owner_payload: dict[str, Any] = {
                "kind": "subject", "run_id": run_id, "case_id": case_id,
            }
        else:
            sample_id = owner["sample_ids"].get(plan["case_id"])
            if not sample_id:
                raise JudgeInputError(
                    f"calibration sample is not owned by calibration_job: "
                    f"{plan['case_id']}"
                )
            run_id = job["owner_ref"]
            case_id = sample_id
            owner_payload = {
                "kind": "calibration",
                "calibration_job_id": owner["calibration_job_id"],
                "sample_id": sample_id,
            }
        call_key = f"{job['job_id']}:{plan['call_id']}"
        return {
            "id": "inv-" + hashlib.sha256(call_key.encode("utf-8")).hexdigest()[:24],
            "run_id": run_id,
            "case_id": case_id,
            "kind": "model",
            "step": plan.get("ordinal", 1),
            "status": "prepared",
            "purpose": "judge",
            "job_id": job["job_id"],
            "scoring_pass_id": None,
            "criterion_id": None,
            "owner": owner_payload,
            "model": job["judge_spec"]["model"],
            "request_summary": {
                "purpose": JUDGE_PURPOSE,
                "judge_spec_sha256": job["judge_spec_sha256"],
                "rubric": job["judge_spec"]["rubric_id"],
                "mode": plan["mode"],
                "input_sha256": plan["input_sha256"],
                "presentation_order": plan.get("presentation_order") or [],
                "candidate_ids": plan.get("candidate_ids") or [],
                "repeat_index": plan.get("repeat_index", 0),
                "output_tokens": plan.get("output_tokens"),
                "reservation": deepcopy(plan.get("reservation")),
                "price_table_version": (
                    job["budget"].get("price_table_version")
                ),
                **self._snapshot_identity(job),
            },
            "prepared_at": self._clock(),
        }

    # ------------------------------------------------------------- 内部：调用
    def _snapshot_for(self, job: dict[str, Any]) -> JudgeProviderSnapshot:
        """执行期的 Provider 身份：只读提交期冻结的快照，绝不按名字重选模型。"""
        raw = job.get("provider_snapshot")
        spec = JudgeSpec.model_validate(job["judge_spec"])
        if isinstance(raw, dict) and raw:
            frozen = JudgeProviderSnapshot.model_validate(raw)
            if frozen.model != spec.model:
                raise JudgeProviderSnapshotError(
                    "JUDGE_SNAPSHOT_MODEL_MISMATCH",
                    "frozen provider snapshot model differs from the frozen judge spec "
                    f"model: {frozen.model!r} != {spec.model!r}",
                )
            return frozen
        # 库内直连路径（无提交期快照）保留最小身份，仅携带冻结 spec 的模型名。
        return JudgeProviderSnapshot.minimal(spec.model)

    def _complete(self, job: dict[str, Any], request: Any) -> dict[str, Any]:
        if self.provider_factory is None:
            raise JudgeError("no judge provider is configured")
        provider = self.provider_factory(self._snapshot_for(job))
        envelope = provider.complete(request)
        if not isinstance(envelope, dict):
            raise JudgeError("judge provider must return an envelope object")
        return envelope

    def _response_summary(self, envelope: dict[str, Any]) -> dict[str, Any]:
        cost = envelope.get("cost") or {}
        metering = envelope.get("metering") or {}
        content = envelope.get("content")
        return {
            "usage": deepcopy(envelope.get("usage") or {}),
            "cost_usd": cost.get("total"),
            "price_table_version": cost.get("price_table_version"),
            "response_id": envelope.get("response_id"),
            "provider": envelope.get("provider"),
            "model": envelope.get("model"),
            "attempts": metering.get("attempts"),
            "unsafe_retry_observed": bool(
                (metering.get("attempts") or 1) > 1
            ),
            "raw_response": {
                "content": content if isinstance(content, str) else None,
                "response_id": envelope.get("response_id"),
                "provider": envelope.get("provider"),
            },
        }

    def _call_failed(
        self, job: dict[str, Any], plan: dict[str, Any], error: Exception,
    ) -> dict[str, Any]:
        """已 dispatch 的调用失败：与 Provider 的唯一错误分类共用判定。"""
        error_class = getattr(error, "error_class", None) or type(error).__name__
        indeterminate = is_indeterminate_error(error)
        outcome = "indeterminate" if indeterminate else "failed"
        summary = {
            "error_class": error_class,
            "message": str(error),
            "raw_response": None,
            "billable": None if indeterminate else False,
        }
        failure = {
            "code": (
                "CALL_OUTCOME_INDETERMINATE" if indeterminate
                else "JUDGE_CALL_FAILED"
            ),
            "message": str(error),
            "call_id": plan["call_id"],
            "error_class": error_class,
        }
        changes: dict[str, Any] = {"failure": failure}
        if indeterminate:
            # 无法证明请求未被处理：费用保持未知，绝不补 0 或宣称未计费。
            changes["cost_total_usd"] = None
        updated = self.jobs.settle_call(
            job["job_id"], expected_revision=job["revision"],
            call_id=plan["call_id"], outcome=outcome, result_summary=summary,
            job_status="indeterminate" if indeterminate else "failed",
            job_changes=changes,
        )
        return job_view(updated)

    def _call_indeterminate(
        self, job: dict[str, Any], plan: dict[str, Any], message: str,
    ) -> dict[str, Any]:
        """已 dispatch 但未结算：不重发，标不确定。"""
        return self._call_failed(
            job, plan,
            JudgeError(message, code="CALL_OUTCOME_INDETERMINATE"),
        )

    def _finalize_cancellation(self, job: dict[str, Any]) -> dict[str, Any]:
        """兑现取消请求：已发出的调用保留在账本里，只是不再发布新 pass。"""
        calls = job.get("calls") or []
        if job["status"] == "dispatching" and all(
            item.get("status") == "settled" for item in calls
        ):
            cancellation = deepcopy(job.get("cancellation") or {})
            cancellation["billed_calls"] = job.get("billed_calls") or 0
            updated = self.jobs.transition(
                job["job_id"], expected_revision=job["revision"],
                expected_status="dispatching", status="cancelled",
                changes={
                    "cancellation": cancellation,
                    "cancelled_at": self._clock(),
                    "publish_outcome": "cancelled_before_publish",
                },
            )
            return {
                "job": job_view(updated), "outcome": "cancelled_before_publish",
                "receipt": None, "billed_calls": updated.get("billed_calls") or 0,
            }
        outcome = self.jobs.cancel(
            job["job_id"], actor="scoring-job", reason="cancellation requested",
        )
        return {
            "job": job_view(outcome["job"]),
            "outcome": outcome["outcome"],
            "receipt": deepcopy(outcome.get("receipt")),
            "billed_calls": outcome.get("billed_calls"),
        }

    # ------------------------------------------------------------- 内部：发布
    def _finalize(self, job: dict[str, Any]) -> dict[str, Any]:
        if (job.get("cancellation") or {}).get("requested") and job["status"] != "cancelled":
            return self._finalize_cancellation(job)
        spec = JudgeSpec.model_validate(job["judge_spec"])
        run_id = job["owner"]["run_id"] if job["owner"]["kind"] == "subject" else (
            job["owner_ref"]
        )
        metrics: list[MetricResult] = []
        pairs: list[tuple[str, MetricResult]] = []
        parse_failures: list[str] = []
        for plan in job["plans"]:
            call = next(
                (item for item in job.get("calls") or []
                 if item.get("call_id") == plan["call_id"]),
                None,
            )
            if call is None or call.get("outcome") != "succeeded":
                for metric in self._missing_call_metrics(spec, plan, call):
                    metrics.append(metric)
                    pairs.append((plan["case_id"], metric))
                continue
            content = ((call.get("raw_response") or {}).get("content")) or ""
            response_ref = (call.get("raw_response") or {}).get("response_id")
            if plan["mode"] == "pairwise":
                pair = JudgePairwiseInput.model_validate(plan["pair"])
                outcome = parse_pairwise_output(spec, content, pair, response_ref=response_ref)
                produced = pairwise_metrics(
                    spec, outcome, pair, run_id=run_id,
                    input_sha256=plan["input_sha256"],
                    extra_details={"job_id": job["job_id"], "call_id": plan["call_id"]},
                )
            else:
                bundle = JudgeInputBundle.model_validate(job["inputs"][plan["case_id"]])
                outcome = parse_judge_output(spec, content, bundle, response_ref=response_ref)
                produced = judge_metrics(
                    spec, outcome, case_id=plan["case_id"], run_id=run_id,
                    input_sha256=plan["input_sha256"],
                    extra_details={"job_id": job["job_id"], "call_id": plan["call_id"]},
                )
            for metric in produced:
                metrics.append(metric)
                pairs.append((plan["case_id"], metric))
            if outcome.status != "ok":
                parse_failures.append(f"{plan['call_id']}:{outcome.status}")

        non_scored = [item for item in metrics if item.status is not MetricStatus.scored]
        result = {
            "metrics": [item.model_dump(mode="json") for item in metrics],
            "parse_failures": parse_failures,
            "non_scored": len(non_scored),
            "mode": job["mode"],
        }
        if non_scored and job["publish_policy"] == "all_scored":
            updated = self.jobs.transition(
                job["job_id"], expected_revision=job["revision"],
                expected_status=job["status"], status="failed",
                changes={
                    "failure": {
                        "code": "PUBLISH_BLOCKED_NON_SCORED",
                        "message": (
                            f"{len(non_scored)} metric(s) are not scored; the explicit "
                            "publish policy forbids publishing a non-scored result set"
                        ),
                        "non_scored": [
                            item.metric_id for item in non_scored
                        ],
                    },
                    "result": result,
                },
            )
            return job_view(updated)

        # 同一个 case 被评分多次（重复 / 换序）时，ScoreSet 的唯一键需要显式重复维度：
        # 每次调用成为一个 trial 行，而不是覆盖或丢掉其中一次结果。
        repeated = len({plan["case_id"] for plan in job["plans"]}) < len(job["plans"])
        scores = []
        for case_id, metric in pairs:
            score = metric_result_to_score(metric, case_id)
            if repeated:
                score["trial_id"] = str(metric.details.get("call_id") or "")
            scores.append(score)
        previous = self._previous_pass(job)
        record = {
            "id": job["reserved_pass_id"],
            "run_id": run_id,
            "scorer_id": f"judge:{spec.judge_profile_id}",
            "scorer_version": spec.evaluator_version,
            "created_at": self._clock(),
            "source": "judge",
            "source_run_revision": self._subject_run_revision(job),
            "source_snapshot_hash": None,
            "previous_pass_id": previous.get("id") if previous else None,
            "purpose": JUDGE_PURPOSE,
            "job_id": job["job_id"],
            "judge": {
                **spec.as_summary(),
                "mode": job["mode"],
                "input_digests": deepcopy(job.get("input_digests") or {}),
                # 每个冻结调用的证据身份：重复/换序的多次评分各自保留一行。
                "calls": [
                    {
                        "call_id": plan["call_id"],
                        "case_id": plan["case_id"],
                        "repeat_index": plan.get("repeat_index", 0),
                        "presentation_order": list(plan.get("presentation_order") or []),
                        "input_sha256": plan["input_sha256"],
                    }
                    for plan in job["plans"]
                ],
                # 保存政策：重复评分逐行追加（trial_id = call_id），后一次运行
                # 只能产生新 pass 并保留 previous_pass_id，绝不覆盖已有评分。
                "save_policy": "append_pass_append_trials",
                "publish_policy": job["publish_policy"],
                # 冻结 Provider 身份：历史评分可核验它当时用的是哪个连接/端点。
                "provider_snapshot_sha256": self._snapshot_identity(job).get(
                    "provider_snapshot_sha256"
                ),
                "model_resource_id": self._snapshot_identity(job).get("model_resource_id"),
                "provider_connection": self._snapshot_identity(job).get("provider_connection"),
                "provider_adapter": self._snapshot_identity(job).get("provider_adapter"),
                "endpoint": self._snapshot_identity(job).get("provider_endpoint"),
                "owner": deepcopy(job["owner"]),
                "source_pass_id": job.get("source_pass_id"),
                "calibration_version": job.get("calibration_version"),
                "input_selector": deepcopy(spec.input_selector.model_dump(mode="json")),
                "missing_evidence_policy": spec.missing_evidence_policy,
            },
            "summary": self._pass_summary(job, metrics, non_scored),
            "scores": deepcopy(scores),
        }
        receipt = {
            "job_id": job["job_id"],
            "scoring_pass_id": record["id"],
            "run_id": run_id,
            "owner": deepcopy(job["owner"]),
            "fingerprint": job["fingerprint"],
            "judge_spec_sha256": spec.spec_sha256,
            "rubric": spec.rubric_reference,
            "mode": job["mode"],
            "billed_calls": job.get("billed_calls") or 0,
            "cost": {
                "known": job.get("cost_total_usd") is not None,
                "total_usd": job.get("cost_total_usd"),
                "price_table_version": self._observed_price_version(job),
            },
            "scores": len(scores),
            "non_scored": len(non_scored),
            "published_at": self._clock(),
        }
        events: list[dict[str, Any]] = []
        if job["owner"]["kind"] == "subject":
            events.append({
                "run_id": run_id,
                "type": "scoring_pass_created",
                "scoring_pass_id": record["id"],
                "source": "judge",
                "scorer_id": record["scorer_id"],
                "scorer_version": record["scorer_version"],
                "judge_profile_id": spec.judge_profile_id,
                "job_id": job["job_id"],
            })
            for score in scores:
                events.append({
                    "run_id": run_id,
                    "type": "score",
                    "scoring_pass_id": record["id"],
                    "case_id": score["case_id"],
                    "metric_id": score.get("metric_id"),
                    "passed": score.get("passed"),
                    "metric_status": score.get("metric_status"),
                })
            events.append({
                "run_id": run_id,
                "type": "judge_scoring_completed",
                "scoring_pass_id": record["id"],
                "job_id": job["job_id"],
                "billed_calls": job.get("billed_calls") or 0,
            })
        expected_run_revision = None
        expected_run_status = None
        run_advance = None
        if job["owner"]["kind"] == "subject":
            run = self.store.runs.get(run_id)
            if run is None:
                raise ScoringJobError(f"subject run is missing: {run_id}")
            expected_run_revision = run["revision"]
            expected_run_status = run["status"]
            run_advance = {"updated_at": self._clock()}
        try:
            outcome = self.jobs.publish(
                job["job_id"], expected_revision=job["revision"],
                pass_record=record, scores=scores, events=events,
                run_advance=run_advance,
                expected_run_revision=expected_run_revision,
                expected_run_status=expected_run_status,
                receipt=receipt, result=result,
            )
        except RunConflictError as error:
            # 全有或全无：CAS 竞争失败时不留下半个 current pass，job 留在 settled，
            # 下一次领取会重新读取 Run revision 后再试。
            conflicted = self.jobs.transition(
                job["job_id"], expected_revision=job["revision"],
                expected_status=job["status"], status=job["status"],
                allow_same_status=True,
                changes={
                    "publish_conflict": {
                        "at": self._clock(), "reason": str(error),
                        "attempts": int(job.get("publish_conflict", {}).get("attempts") or 0) + 1,
                    },
                    "result": result,
                },
            )
            view = job_view(conflicted)
            view["published"] = False
            view["publish_outcome"] = "publish_conflict"
            view["receipt"] = None
            return view
        view = job_view(outcome["job"])
        view["published"] = outcome["published"]
        view["publish_outcome"] = outcome["outcome"]
        view["receipt"] = deepcopy(outcome["receipt"])
        return view

    def _pass_summary(
        self, job: dict[str, Any], metrics: list[MetricResult], non_scored: list[MetricResult],
    ) -> dict[str, Any]:
        metrics_summary: dict[str, dict[str, int]] = {}
        for item in metrics:
            bucket = metrics_summary.setdefault(item.metric_id, {
                "scored": 0, "passed": 0, "failed": 0, "insufficient_evidence": 0,
                "evaluator_error": 0, "not_applicable": 0,
            })
            bucket[item.status.value if item.status is not MetricStatus.scored
                   else "scored"] += 1
            if item.status is MetricStatus.scored:
                if item.passed is True:
                    bucket["passed"] += 1
                elif item.passed is False:
                    bucket["failed"] += 1
        return {
            "scores": len(metrics),
            "passed": sum(1 for item in metrics if item.passed is True),
            "multi_metric": True,
            "metrics": metrics_summary,
            "judge": True,
            "purpose": JUDGE_PURPOSE,
            "job_id": job["job_id"],
            "non_scored": len(non_scored),
            "billed_calls": job.get("billed_calls") or 0,
            "cost": {
                "known": job.get("cost_total_usd") is not None,
                "total_usd": job.get("cost_total_usd"),
            },
            "interventions": [],
        }

    def _observed_price_version(self, job: dict[str, Any]) -> str | None:
        """费用账本用实际观察到的价格表版本；没有观察值时才回落到预算声明。"""
        for item in reversed(job.get("calls") or []):
            if item.get("price_table_version"):
                return str(item["price_table_version"])
        return job["budget"].get("price_table_version")

    def _previous_pass(self, job: dict[str, Any]) -> dict[str, Any] | None:
        if job["owner"]["kind"] != "subject":
            return None
        passes = self.store.scoring_passes
        current = passes.current(job["owner"]["run_id"])
        if current is not None:
            return current
        records = passes.list_for_run(job["owner"]["run_id"])
        return records[-1] if records else None

    def _subject_run_revision(self, job: dict[str, Any]) -> int | None:
        if job["owner"]["kind"] != "subject":
            return None
        run = self.store.runs.get(job["owner"]["run_id"])
        return run["revision"] if run else None

    def _missing_call_metrics(
        self, spec: JudgeSpec, plan: dict[str, Any], call: dict[str, Any] | None,
    ) -> list[MetricResult]:
        """没有成功响应时逐 criterion 给出非 scored 占位，绝不补 0 或 pass。"""
        status = (
            MetricStatus.insufficient_evidence
            if call is None
            else MetricStatus.evaluator_error
        )
        reason = (
            "judge call never succeeded; no score is fabricated"
            if call is None
            else f"judge call outcome: {call.get('outcome')}"
        )
        metric_ids = (
            ["pairwise_preference"] if plan["mode"] == "pairwise" else list(spec.criteria)
        )
        return [
            MetricResult(
                metric_id=metric_id,
                status=status,
                evaluator_id=f"judge:{spec.judge_profile_id}",
                evaluator_version=spec.evaluator_version,
                reason=reason,
                denominator=False,
                details={
                    "call_id": plan["call_id"],
                    "judge_outcome": (call or {}).get("outcome"),
                    "no_response": True,
                },
            )
            for metric_id in metric_ids
        ]
