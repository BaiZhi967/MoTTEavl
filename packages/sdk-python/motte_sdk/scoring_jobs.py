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
    judge_job_fingerprint,
    judge_metrics,
    pairwise_metrics,
    parse_judge_output,
    parse_pairwise_output,
    preflight_judge,
)
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
    "JudgeProviderPolicy",
    "ScoringJobError",
    "ScoringJobConflict",
    "ScoringJobRequest",
    "ScoringJobService",
    "default_artifact_reader",
]

_INDETERMINATE_ERROR_CLASSES = (
    "network", "timeout", "protocol", "connection", "transport", "server_error",
)


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


class JudgeProviderPolicy(Contract):
    """Judge Provider 的付费安全策略；默认关闭一切自动重试。"""

    automatic_model_retries: bool = False
    max_calls_per_job: int = Field(default=32, ge=1)

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

    # ------------------------------------------------------------- 提交
    def submit(self, request: ScoringJobRequest) -> dict[str, Any]:
        spec = request.judge_spec
        inputs, plans = self._prepare_inputs(request)
        preflight = preflight_judge(
            spec,
            mode=request.mode,
            sample_count=request.sample_count,
            repeats=request.repeats,
            orderings=request.orderings,
            authorisation=request.authorisation,
            price_table=request.price_table,
            estimated_prompt_tokens=request.estimated_prompt_tokens,
            estimated_completion_tokens=request.estimated_completion_tokens,
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
        if self.provider_factory is None:
            raise JudgeError(
                "no judge provider factory is configured; submitting would create an "
                "unexecutable job"
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
        )
        job_id = new_job_id()
        reserved_pass_id = new_reserved_pass_id()
        record = {
            "schema_version": 1,
            "job_id": job_id,
            "request_key": request.request_key,
            "fingerprint": fingerprint,
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
            "inputs": {key: value.model_dump(mode="json") for key, value in inputs.items()},
            "plans": plans,
            "input_digests": {
                plan["case_id"]: plan["input_sha256"] for plan in plans
            },
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
        return result

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
            request = self._request_for(current, plan)
            invocation = self._invocation_record(current, plan)
            call = {
                "call_id": plan["call_id"],
                "case_id": plan["case_id"],
                "mode": plan["mode"],
                "candidate_ids": plan.get("candidate_ids") or [],
                "presentation_order": plan.get("presentation_order") or [],
                "input_sha256": plan["input_sha256"],
                "ordinal": index + 1,
            }
            current = self.jobs.begin_call(
                current["job_id"], expected_revision=current["revision"],
                invocation=invocation, call=call,
            )
            try:
                envelope = self._complete(current["judge_spec"]["model"], request)
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
            total = sum(
                item.get("cost_usd") or 0.0 for item in latest.get("calls") or []
            ) + (summary.get("cost_usd") or 0.0)
            last_call = index == len(job["plans"]) - 1
            current = self.jobs.settle_call(
                latest["job_id"], expected_revision=latest["revision"],
                call_id=plan["call_id"], outcome="succeeded",
                result_summary=summary,
                job_status="settled" if last_call else "dispatching",
                job_changes={
                    "cost_total_usd": total if summary.get("cost_usd") is not None
                    else latest.get("cost_total_usd"),
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
        return self.jobs.recover_interrupted()

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
        if request.mode == "pairwise":
            plans: list[dict[str, Any]] = []
            orders: list[list[str]] = request.presentation_orders or []
            if not orders:
                orders = [
                    list(pair.presentation_order) for pair in request.pairwise_pairs
                ]
            index = 0
            for pair in request.pairwise_pairs:
                for order in orders:
                    if sorted(order) != sorted(pair.presentation_order):
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
                    index += 1
                    plans.append({
                        "call_id": f"call-{index}",
                        "case_id": reordered.task_ref,
                        "mode": "pairwise",
                        "pair_id": reordered.pair_id,
                        "candidate_ids": [
                            reordered.candidate_a.candidate.candidate_id,
                            reordered.candidate_b.candidate.candidate_id,
                        ],
                        "presentation_order": list(order),
                        "input_sha256": reordered.input_sha256,
                        "pair": reordered.model_dump(mode="json"),
                    })
            return {}, plans

        inputs: dict[str, JudgeInputBundle] = {}
        plans = []
        for index, (case_id, observation) in enumerate(request.observations.items()):
            bundle = build_judge_input(
                request.judge_spec, observation,
                artifact_reader=self._artifact_reader,
            )
            inputs[case_id] = bundle
            plans.append({
                "call_id": f"call-{index + 1}",
                "case_id": case_id,
                "mode": "single",
                "observation_id": bundle.observation_id,
                "input_sha256": bundle.input_sha256,
            })
        return inputs, plans

    def _request_for(self, job: dict[str, Any], plan: dict[str, Any]) -> Any:
        spec = JudgeSpec.model_validate(job["judge_spec"])
        if plan["mode"] == "pairwise":
            pair = JudgePairwiseInput.model_validate(plan["pair"])
            return build_judge_request(spec, pair)
        bundle = JudgeInputBundle.model_validate(job["inputs"][plan["case_id"]])
        return build_judge_request(spec, bundle)

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
                "price_table_version": (
                    job["budget"].get("price_table_version")
                ),
            },
            "prepared_at": self._clock(),
        }

    # ------------------------------------------------------------- 内部：调用
    def _complete(self, model: str, request: Any) -> dict[str, Any]:
        if self.provider_factory is None:
            raise JudgeError("no judge provider is configured")
        provider = self.provider_factory(model)
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
        error_class = getattr(error, "error_class", None) or type(error).__name__
        indeterminate = str(error_class).lower() in _INDETERMINATE_ERROR_CLASSES
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
        updated = self.jobs.settle_call(
            job["job_id"], expected_revision=job["revision"],
            call_id=plan["call_id"], outcome=outcome, result_summary=summary,
            job_status="indeterminate" if indeterminate else "failed",
            job_changes={"failure": failure},
        )
        return job_view(updated)

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
                "publish_policy": job["publish_policy"],
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
