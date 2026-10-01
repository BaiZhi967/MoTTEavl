"""Durable calibration contracts and pure provenance operations.

These envelopes are data, not authority: repositories must verify their source chain,
then reconstruct reports from the actual ledger. Human names and times are declared
provenance within deployment authorization, never independently verified identity.
The pinned spec lives in CalibrationSet.config['judge_spec']; the set digest and the
outer version digest deliberately remain distinct hash domains.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import Field, StrictBool, TypeAdapter, field_validator, model_validator

from motte_contracts.identity import canonical_sha256
from motte_contracts.messages import Contract

from .calibration import (
    CalibrationCall, CalibrationReport, CalibrationSample, CalibrationSet, HumanReviewRequired,
    JudgeQualification, build_calibration_set, review_sample,
)
from .judge import JudgeAuthorisation, JudgeInputSelector, JudgeSpec
from .rubrics import CalibrationPolicy, calibration_policy_sha256, validate_policy

Text = Annotated[str, Field(strict=True, min_length=1, pattern=r"\S")]
Digest = Annotated[str, Field(strict=True, pattern=r"^sha256:[0-9a-f]{64}$")]
NonScoredStatus = Literal[
    "missing_evidence", "refused", "malformed", "forged_evidence", "missing_criterion",
]
SampleExpectedStatus = Literal["scored", "insufficient_evidence", "evaluator_error"]


def _payload(value: Contract | dict[str, Any]) -> dict[str, Any]:
    return value.model_dump(mode="json") if isinstance(value, Contract) else deepcopy(value)


def _normalized(cls: type[Contract], value: dict[str, Any]) -> dict[str, Any]:
    """Fill defaults and validate nested types before calculating an envelope digest."""
    normalized = dict(value)
    for name, field in cls.model_fields.items():
        if name in normalized:
            normalized[name] = TypeAdapter(field.rebuild_annotation()).validate_python(
                normalized[name]
            )
    return {**cls.model_construct(**normalized).model_dump(mode="json"),
            **{key: value for key, value in normalized.items() if key not in cls.model_fields}}


def _time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("time must be an ISO 8601 timezone-qualified timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("time must be a timezone-qualified timestamp")
    return value


def _unique(values: list[str], name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must be unique")


def _sample_status(outcome: ExpectedCalibrationOutcome) -> SampleExpectedStatus:
    if outcome.kind == "scored":
        return "scored"
    return "insufficient_evidence" if outcome.status == "missing_evidence" else "evaluator_error"


class CalibrationRef(Contract):
    calibration_id: Text
    version: Text
    content_sha256: Digest


class ScoredCalibrationOutcome(Contract):
    kind: Literal["scored"]


class NonScoredCalibrationOutcome(Contract):
    kind: Literal["non_scored"]
    status: NonScoredStatus


ExpectedCalibrationOutcome = Annotated[
    ScoredCalibrationOutcome | NonScoredCalibrationOutcome, Field(discriminator="kind"),
]


class PairwiseLabel(Contract):
    kind: Literal["candidate", "tie"]
    candidate_id: Text | None = None

    @model_validator(mode="after")
    def explicit_preference(self) -> Self:
        if (self.kind == "candidate") != (self.candidate_id is not None):
            raise ValueError("candidate labels need a stable ID; only explicit tie has None")
        return self


class CalibrationCandidate(Contract):
    """Importable candidate data, intentionally without Run/Job/owner references."""

    candidate_id: Text
    content: str = Field(strict=True)
    evidence_allowlist: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def evidence_tokens(self) -> Self:
        _unique(self.evidence_allowlist, "evidence allowlist")
        for token in self.evidence_allowlist:
            kind, separator, locator = token.partition(":")
            if not separator or not locator.strip() or kind not in {
                "event", "artifact", "invocation", "tool_call", "file",
            }:
                raise ValueError("evidence allowlist requires explicit kind:locator tokens")
        return self


class CalibrationPair(Contract):
    candidates: tuple[CalibrationCandidate, CalibrationCandidate]

    @model_validator(mode="after")
    def distinct_candidates(self) -> Self:
        if len(set(self.candidate_ids)) != 2:
            raise ValueError("calibration pair requires two distinct stable candidate IDs")
        return self

    @property
    def candidate_ids(self) -> tuple[str, str]:
        return (self.candidates[0].candidate_id, self.candidates[1].candidate_id)


class CalibrationVersion(Contract):
    schema_version: Literal[1] = 1
    calibration: CalibrationSet
    pairs: dict[Text, CalibrationPair] = Field(default_factory=dict)
    pairwise_gold: dict[Text, dict[Text, PairwiseLabel]] = Field(default_factory=dict)
    expected_outcomes: dict[Text, ExpectedCalibrationOutcome] = Field(default_factory=dict)
    review_ids: list[Text] = Field(default_factory=list)
    parent_ref: CalibrationRef | None = None
    content_sha256: Digest

    @field_validator("calibration", mode="before")
    @classmethod
    def revalidate_set(cls, value: Any) -> CalibrationSet:
        return CalibrationSet.model_validate(_payload(value))

    @property
    def spec(self) -> JudgeSpec:
        raw = self.calibration.config.get("judge_spec")
        if not isinstance(raw, dict):
            raise ValueError("calibration config must pin the full judge_spec")
        return JudgeSpec.model_validate(raw)

    @property
    def reference(self) -> CalibrationRef:
        return CalibrationRef(calibration_id=self.calibration.calibration_id,
                              version=self.calibration.version, content_sha256=self.content_sha256)

    @model_validator(mode="after")
    def sealed_provenance(self) -> Self:
        spec = self.spec
        calibration = self.calibration
        if (spec.spec_sha256 != calibration.judge_spec_sha256
                or (spec.rubric_id, spec.rubric_version, spec.model) !=
                (calibration.rubric_id, calibration.rubric_version, calibration.model)):
            raise ValueError("calibration must match its pinned judge spec")
        samples = {sample.sample_id: sample for sample in calibration.samples}
        for sample in samples.values():
            if sample.judge_spec_sha256 != spec.spec_sha256:
                raise ValueError("sample must pin the calibration judge spec")
        if spec.mode == "pairwise":
            if set(self.pairs) != set(samples):
                raise ValueError("pair keys must exactly cover calibration sample IDs")
        elif self.pairs or self.pairwise_gold:
            raise ValueError("single mode cannot carry pairwise inputs or gold")
        reviewed = {key for key, sample in samples.items() if sample.is_human_reviewed}
        if not set(self.expected_outcomes).issubset(reviewed):
            raise ValueError("expected outcomes require a reviewed sample")
        if not set(self.pairwise_gold).issubset(reviewed):
            raise ValueError("pairwise gold requires a reviewed sample")
        if spec.mode == "pairwise" and set(self.expected_outcomes) != reviewed:
            raise ValueError("each reviewed pair needs an explicit expected outcome")
        for sample_id in reviewed:
            sample = samples[sample_id]
            outcome = self.expected_outcomes.get(sample_id)
            gold = self.pairwise_gold.get(sample_id, {})
            if outcome is not None and sample.expected_status != _sample_status(outcome):
                raise ValueError("sample expected status must match its typed expected outcome")
            if outcome is not None and outcome.kind == "non_scored":
                if gold or sample.expected_criteria:
                    raise ValueError("non-scored gold cannot carry preference or Boolean labels")
                if sample_id in self.pairwise_gold:
                    raise ValueError("non-scored gold must omit preference labels")
            elif spec.mode == "pairwise":
                if sample.expected_criteria:
                    raise ValueError("pairwise gold must never be stored as Boolean criteria")
                if set(gold) != set(spec.criteria):
                    raise ValueError("scored gold must exactly cover pinned spec criteria")
                for label in gold.values():
                    if label.kind == "candidate" and label.candidate_id not in self.pairs[sample_id].candidate_ids:
                        raise ValueError("pairwise gold candidate must be in its pair")
        _unique(self.review_ids, "review IDs")
        if reviewed and (self.parent_ref is None or not self.review_ids):
            raise ValueError("reviewed versions require immutable review provenance")
        if (self.parent_ref is None) != (not self.review_ids):
            raise ValueError("parent reference and review IDs must be supplied together")
        if self.parent_ref is not None and (
            self.parent_ref.calibration_id != calibration.calibration_id
            or self.parent_ref.version == calibration.version
        ):
            raise ValueError("review must create a new version of the same calibration")
        expected = self.model_dump(mode="json", exclude={"content_sha256"})
        if self.content_sha256 != canonical_sha256(expected):
            raise ValueError("calibration version content_sha256 does not match its content")
        return self

    @classmethod
    def seal(cls, payload: dict[str, Any]) -> Self:
        data = _normalized(cls, {**payload, "content_sha256": "sha256:" + "0" * 64})
        data["content_sha256"] = canonical_sha256({key: value for key, value in data.items()
                                                   if key != "content_sha256"})
        return cls.model_validate(data)


class CalibrationImport(Contract):
    """Public import shape; reviewed versions and server-owned facts are not inputs."""

    calibration: CalibrationSet
    pairs: dict[Text, CalibrationPair] = Field(default_factory=dict)

    @field_validator("calibration", mode="before")
    @classmethod
    def revalidate_set(cls, value: Any) -> CalibrationSet:
        return CalibrationSet.model_validate(_payload(value))

    @model_validator(mode="after")
    def unreviewed_only(self) -> Self:
        for sample in self.calibration.samples:
            if sample.status != "candidate" or any(
                value is not None for value in (sample.annotator, sample.reviewed_by, sample.reviewed_at)
            ):
                raise ValueError("import accepts only unreviewed human or synthetic candidates")
        self.to_version()
        return self

    def to_version(self) -> CalibrationVersion:
        return CalibrationVersion.seal({"calibration": self.calibration, "pairs": self.pairs})


class HumanReviewInput(Contract):
    sample_id: Text
    annotator: Text
    reviewer: Text
    reviewed_at: Text
    reason: Text
    expected_criteria: dict[Text, StrictBool] = Field(default_factory=dict)
    expected_status: SampleExpectedStatus | None = None
    pairwise_gold: dict[Text, PairwiseLabel] = Field(default_factory=dict)
    expected_outcome: ExpectedCalibrationOutcome | None = None

    _review_time = field_validator("reviewed_at")(_time)

    @model_validator(mode="after")
    def honest_labels(self) -> Self:
        if self.expected_criteria and self.pairwise_gold:
            raise ValueError("Boolean and pairwise gold cannot be mixed")
        if self.expected_outcome is not None:
            mapped = _sample_status(self.expected_outcome)
            if self.expected_status is not None and self.expected_status != mapped:
                raise ValueError("expected_status disagrees with the typed expected outcome")
            if self.expected_outcome.kind == "non_scored" and (
                self.expected_criteria or self.pairwise_gold
            ):
                raise ValueError("non-scored gold cannot carry preference or Boolean labels")
        if self.pairwise_gold and (
            self.expected_outcome is None or self.expected_outcome.kind != "scored"
        ):
            raise ValueError("pairwise gold requires an explicit scored expected outcome")
        if not self.expected_criteria and not self.pairwise_gold and (
            self.expected_outcome is None or self.expected_outcome.kind != "non_scored"
        ):
            raise ValueError("human review requires explicit gold")
        return self


def calibration_review_id(parent_ref: CalibrationRef, review: HumanReviewInput) -> str:
    return "calreview-" + canonical_sha256({"parent_ref": parent_ref, "review": review})[7:]


class HumanReviewRecord(Contract):
    review_id: Text
    review: HumanReviewInput
    parent_ref: CalibrationRef
    child_ref: CalibrationRef
    before_sample_sha256: Digest
    after_sample_sha256: Digest
    recorded_at: Text

    _recorded_time = field_validator("recorded_at")(_time)

    @model_validator(mode="after")
    def stable_id(self) -> Self:
        if self.review_id != calibration_review_id(self.parent_ref, self.review):
            raise ValueError("review ID does not match parent and complete human input")
        if (self.parent_ref.calibration_id != self.child_ref.calibration_id
                or self.parent_ref.version == self.child_ref.version):
            raise ValueError("review child must be a new version of the same calibration")
        return self


def review_calibration_version(
    parent: CalibrationVersion, *, new_version: str, reviews: list[HumanReviewInput],
    recorded_at: str,
) -> tuple[CalibrationVersion, list[HumanReviewRecord]]:
    """Pure, atomic-ready review batch; never mutate the parent or generate human gold."""
    parent = CalibrationVersion.model_validate(parent.model_dump(mode="json"))
    reviews = [HumanReviewInput.model_validate(review.model_dump(mode="json")) for review in reviews]
    if not reviews:
        raise ValueError("a review batch cannot be empty")
    _unique([review.sample_id for review in reviews], "reviewed sample IDs")
    _time(recorded_at)
    samples = {sample.sample_id: sample for sample in parent.calibration.samples}
    gold = deepcopy(parent.pairwise_gold)
    outcomes = deepcopy(parent.expected_outcomes)
    for review in reviews:
        sample = samples.get(review.sample_id)
        if sample is None:
            raise ValueError("review sample is absent from parent version")
        if sample.source != "human":
            raise HumanReviewRequired("synthetic candidates cannot be reviewed as human")
        if parent.spec.mode == "pairwise":
            if review.expected_criteria:
                raise ValueError("pairwise reviews cannot supply Boolean gold")
            if review.expected_outcome is None:
                raise ValueError("pairwise review requires an explicit expected outcome")
        elif review.pairwise_gold:
            raise ValueError("single mode review cannot carry pairwise gold")
        status = (_sample_status(review.expected_outcome) if review.expected_outcome is not None
                  else review.expected_status)
        samples[review.sample_id] = review_sample(
            sample, annotator=review.annotator, reviewer=review.reviewer,
            reviewed_at=review.reviewed_at, expected_criteria=review.expected_criteria,
            expected_status=status, labelling_notes=review.reason,
        )
        gold.pop(review.sample_id, None)
        if review.pairwise_gold:
            gold[review.sample_id] = review.pairwise_gold
        outcomes.pop(review.sample_id, None)
        if review.expected_outcome is not None:
            outcomes[review.sample_id] = review.expected_outcome
    calibration_data = parent.calibration.model_dump(mode="json", exclude={"content_sha256"})
    calibration_data.pop("schema_version")
    calibration_data["version"] = new_version
    calibration_data["samples"] = [samples[sample.sample_id] for sample in parent.calibration.samples]
    calibration = build_calibration_set(**calibration_data)
    review_ids = [calibration_review_id(parent.reference, review) for review in reviews]
    child = CalibrationVersion.seal({
        "calibration": calibration, "pairs": parent.pairs, "pairwise_gold": gold,
        "expected_outcomes": outcomes, "parent_ref": parent.reference,
        "review_ids": [*parent.review_ids, *review_ids],
    })
    before = {sample.sample_id: sample for sample in parent.calibration.samples}
    records = [HumanReviewRecord(
        review_id=review_id, review=review, parent_ref=parent.reference, child_ref=child.reference,
        before_sample_sha256=before[review.sample_id].content_sha256,
        after_sample_sha256=samples[review.sample_id].content_sha256, recorded_at=recorded_at,
    ) for review_id, review in zip(review_ids, reviews, strict=True)]
    return child, records


def verify_review_chain(
    parent: CalibrationVersion, child: CalibrationVersion, records: list[HumanReviewRecord],
) -> None:
    """Read-side verification for one parent→child transition, including audited digests."""
    parent = CalibrationVersion.model_validate(parent.model_dump(mode="json"))
    child = CalibrationVersion.model_validate(child.model_dump(mode="json"))
    records = [HumanReviewRecord.model_validate(record.model_dump(mode="json")) for record in records]
    if not records:
        raise ValueError("review chain is missing review records")
    before = {sample.sample_id: sample for sample in parent.calibration.samples}
    after = {sample.sample_id: sample for sample in child.calibration.samples}
    for record in records:
        if record.parent_ref != parent.reference or record.child_ref != child.reference:
            raise ValueError("review parent or child reference mismatch")
        sample_id = record.review.sample_id
        if (sample_id not in before or sample_id not in after
                or record.before_sample_sha256 != before[sample_id].content_sha256
                or record.after_sample_sha256 != after[sample_id].content_sha256):
            raise ValueError("review audited sample digest mismatch")
    rebuilt, _ = review_calibration_version(
        parent, new_version=child.calibration.version, reviews=[record.review for record in records],
        recorded_at=records[0].recorded_at,
    )
    if rebuilt != child:
        raise ValueError("review records do not reconstruct the child version")


class CalibrationSourceBinding(Contract):
    """Source identity before report/qualification IDs exist; all components are required."""

    judge_spec_sha256: Digest
    rubric_id: Text
    rubric_version: Text
    rubric_sha256: Digest
    model: Text
    provider_snapshot_sha256: Digest
    calibration_id: Text
    calibration_version: Text
    calibration_content_sha256: Digest
    policy_sha256: Digest


class QualificationBinding(CalibrationSourceBinding):
    report_id: Text
    report_sha256: Digest
    qualification_id: Text


class _CalibrationBudgetRequest(Contract):
    max_calls: int = Field(strict=True, ge=1)
    max_prompt_tokens: int = Field(default=0, strict=True, ge=0)
    max_completion_tokens: int = Field(default=0, strict=True, ge=0)
    hard_cost_cap_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class _CalibrationSpecRequest(Contract):
    """Transport-independent equivalent of the published Judge spec request."""

    judge_profile_id: Text
    model: Text
    rubric_id: Text
    rubric_version: Text
    criteria: list[Text] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    input_selector: JudgeInputSelector | None = None
    missing_evidence_policy: Literal["insufficient_evidence", "not_applicable", "fail"] | None = None
    calibration_version: Text | None = None
    budget: _CalibrationBudgetRequest


class CalibrationRunRequest(Contract):
    """Public inputs only; the server owns observations, plans and authorization accounting."""

    request_key: Text
    spec_request: dict[str, Any]
    authorisation: JudgeAuthorisation | None = None
    price_table_version: Text | None = None
    expected_preflight_sha256: Digest | None = None

    @field_validator("spec_request")
    @classmethod
    def published_spec_shape(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _CalibrationSpecRequest.model_validate(value, strict=True).model_dump(
            mode="json", exclude_unset=True,
        )


class CalibrationAllowance(Contract):
    """Exact sum of frozen child reservations; None cost always means unknown."""

    max_calls: int = Field(strict=True, ge=1)
    max_prompt_tokens: int = Field(strict=True, ge=0)
    max_completion_tokens: int = Field(strict=True, ge=0)
    max_cost_usd: float | None = Field(ge=0, allow_inf_nan=False)

    @classmethod
    def from_plans(cls, plans: list[dict[str, Any]]) -> Self:
        reservations = [plan["reservation"] for plan in plans]
        costs = [item["cost_usd"] for item in reservations]
        return cls(
            max_calls=len(plans),
            max_prompt_tokens=sum(item["prompt_tokens"] for item in reservations),
            max_completion_tokens=sum(item["completion_tokens"] for item in reservations),
            max_cost_usd=(round(sum(costs), 8) if costs and all(cost is not None for cost in costs)
                          else None),
        )


def calibration_execution_id(request_key: str) -> str:
    TypeAdapter(Text).validate_python(request_key)
    return "calexec-" + canonical_sha256({
        "namespace": "judge-calibration-execution@1", "request_key": request_key,
    })[7:]


def _provider_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    """Validate the existing SDK snapshot hash convention without importing the SDK.

    The SDK compiler additionally validates the concrete provider contract/config. Only
    frozen_at is a receipt timestamp; unknown semantic fields still enter this hash.
    """
    allowed_fields = {
        "schema_version", "model_resource_id", "model_profile_generation", "model_profile_sha256",
        "provider_connection", "provider_connection_generation", "provider_connection_sha256",
        "adapter_id", "adapter_version", "endpoint", "request_path", "model", "parameters",
        "max_output_tokens", "reasoning", "reasoning_level", "identity_policy", "identity_aliases",
        "identity_alias_version", "credential_ref", "api_key_env", "price_table_version",
        "price_table_sha256", "price_table", "transport", "frozen_at", "snapshot_sha256",
    }
    if set(value) - allowed_fields or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported fields or schema in provider snapshot")
    payload = deepcopy(value)
    declared = payload.pop("snapshot_sha256", None)
    payload.pop("frozen_at", None)
    if declared != canonical_sha256(payload):
        raise ValueError("provider snapshot_sha256 does not match its content")
    secrets = {
        "apikey", "xapikey", "authorization", "password", "token", "secret", "cookie",
        "setcookie", "proxyauthorization", "accesstoken", "refreshtoken", "clientsecret",
        "privatekey", "openaiapikey", "anthropicapikey", "moonshotapikey", "deepseekapikey",
    }

    def check(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if str(key).lower().replace("-", "").replace("_", "") in secrets:
                    raise ValueError("provider snapshot cannot contain credentials or secrets")
                check(child)
        elif isinstance(node, list):
            for child in node:
                check(child)

    # Hash exclusions are not validation exclusions: all retained content must
    # remain secret-free, including audit metadata omitted from the preimage.
    check(value)
    TypeAdapter(str | None).validate_python(value.get("frozen_at"))
    return deepcopy(value)


class CalibrationExecution(Contract):
    schema_version: Literal[1] = 1
    execution_id: Text
    request_key: Text
    request_fingerprint: Digest
    version: CalibrationVersion
    spec: JudgeSpec
    provider_snapshot: dict[str, Any]
    policy: CalibrationPolicy
    plan: list[dict[str, Any]]
    plan_sha256: Digest
    child_job_ids: list[Text]
    # Legacy records omit budget evidence; readers must not interpret None as authorized zero.
    allowance: CalibrationAllowance | None = Field(default=None, exclude_if=lambda value: value is None)
    recorded_at: Text
    content_sha256: Digest

    _recorded_time = field_validator("recorded_at")(_time)

    @field_validator("version", "spec", mode="before")
    @classmethod
    def revalidate_sources(cls, value: Any, info: Any) -> Any:
        model = CalibrationVersion if info.field_name == "version" else JudgeSpec
        return model.model_validate(_payload(value))

    @field_validator("provider_snapshot")
    @classmethod
    def sealed_provider(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _provider_snapshot(value)

    @property
    def source_binding(self) -> CalibrationSourceBinding:
        return CalibrationSourceBinding(
            judge_spec_sha256=self.spec.spec_sha256, rubric_id=self.spec.rubric_id,
            rubric_version=self.spec.rubric_version, rubric_sha256=self.spec.rubric_sha256,
            model=self.spec.model, provider_snapshot_sha256=self.provider_snapshot["snapshot_sha256"],
            calibration_id=self.version.calibration.calibration_id,
            calibration_version=self.version.calibration.version,
            calibration_content_sha256=self.version.content_sha256,
            policy_sha256=calibration_policy_sha256(self.policy),
        )

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"content_sha256", "recorded_at"})

    @model_validator(mode="after")
    def sealed_sources(self) -> Self:
        if self.execution_id != calibration_execution_id(self.request_key):
            raise ValueError("execution ID must derive from the namespaced request key")
        if self.spec != self.version.spec or self.provider_snapshot.get("model") != self.spec.model:
            raise ValueError("execution spec/provider must match the full version snapshot")
        validate_policy(self.policy)
        if (self.policy.rubric_id, self.policy.rubric_version) != (
            self.spec.rubric_id, self.spec.rubric_version,
        ):
            raise ValueError("execution policy must match the rubric")
        if self.plan_sha256 != canonical_sha256(self.plan):
            raise ValueError("execution plan_sha256 does not match its plan")
        if self.allowance is not None:
            if self.allowance != CalibrationAllowance.from_plans(self.plan):
                raise ValueError("execution allowance must equal actual frozen plan reservations")
        _unique(self.child_job_ids, "child job IDs")
        if not self.child_job_ids or not self.plan:
            raise ValueError("execution requires a nonempty plan and child jobs")
        if self.content_sha256 != canonical_sha256(self.identity_payload()):
            raise ValueError("execution content_sha256 does not match its content")
        return self

    @classmethod
    def seal(cls, payload: dict[str, Any]) -> Self:
        data = {**payload, "execution_id": calibration_execution_id(payload["request_key"]),
                "plan_sha256": canonical_sha256(payload["plan"]), "content_sha256": "sha256:" + "0" * 64}
        data = _normalized(cls, data)
        data["content_sha256"] = canonical_sha256({key: value for key, value in data.items()
                                                   if key not in {"content_sha256", "recorded_at"}})
        return cls.model_validate(data)


class PairwiseConfusionCell(Contract):
    expected: PairwiseLabel
    observed: PairwiseLabel
    count: int = Field(strict=True, ge=1)


class PairwiseCalibrationCall(Contract):
    """Stable candidate/tie labels, never encoded as Boolean pass/fail criteria."""

    call: CalibrationCall
    preferences: dict[str, PairwiseLabel] = Field(default_factory=dict)
    winner: PairwiseLabel | None = None

    @model_validator(mode="after")
    def separate_label_domains(self) -> Self:
        if self.call.criteria:
            raise ValueError("pairwise calls cannot carry Boolean criteria")
        if self.winner is not None and (self.call.outcome != "succeeded" or self.call.status != "ok"):
            raise ValueError("a pairwise winner requires a successful complete parsed outcome")
        return self


class PairwiseCriterionConfusion(Contract):
    criterion_id: Text
    cells: list[PairwiseConfusionCell] = Field(default_factory=list)
    missing_evidence: int = Field(default=0, strict=True, ge=0)
    refusals: int = Field(default=0, strict=True, ge=0)
    errors: int = Field(default=0, strict=True, ge=0)

    @model_validator(mode="after")
    def unique_cells(self) -> Self:
        keys = [canonical_sha256({"expected": cell.expected, "observed": cell.observed})
                for cell in self.cells]
        _unique(keys, "confusion cells")
        return self


def _verify_inner_source(inner: CalibrationReport | JudgeQualification,
                         source: CalibrationSourceBinding) -> None:
    for field in ("judge_spec_sha256", "rubric_id", "rubric_version", "model",
                  "calibration_id", "calibration_version", "policy_sha256"):
        if getattr(inner, field) != getattr(source, field):
            raise ValueError(f"inner record differs from source binding: {field}")


class CalibrationReportRecord(Contract):
    schema_version: Literal[1] = 1
    algorithm: Literal["calibration-ledger@1"] = "calibration-ledger@1"
    report_id: Text
    execution_id: Text
    ledger_sha256: Digest
    source: CalibrationSourceBinding
    report: CalibrationReport
    pairwise_confusion: list[PairwiseCriterionConfusion] = Field(default_factory=list)
    pairwise_calls: list[PairwiseCalibrationCall] = Field(default_factory=list,
                                                       exclude_if=lambda value: not value)
    recorded_at: Text
    content_sha256: Digest

    _recorded_time = field_validator("recorded_at")(_time)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"report_id", "content_sha256", "recorded_at"})

    @model_validator(mode="after")
    def sealed_report(self) -> Self:
        _verify_inner_source(self.report, self.source)
        validate_policy(self.report.policy)
        if self.report.policy_sha256 != calibration_policy_sha256(self.report.policy):
            raise ValueError("report policy digest does not match its snapshot")
        if self.report.generated_at is not None:
            raise ValueError("durable report body must omit server timestamps; use recorded_at")
        _unique([row.criterion_id for row in self.pairwise_confusion], "confusion criterion IDs")
        if self.content_sha256 != canonical_sha256(self.identity_payload()):
            raise ValueError("report content_sha256 does not match its content")
        if self.report_id != "calreport-" + self.content_sha256[7:]:
            raise ValueError("report ID must be content addressed")
        return self

    @classmethod
    def seal(cls, payload: dict[str, Any]) -> Self:
        data = _normalized(cls, {**payload, "report_id": "pending",
                                 "content_sha256": "sha256:" + "0" * 64})
        digest = canonical_sha256({key: value for key, value in data.items()
                                   if key not in {"report_id", "content_sha256", "recorded_at"}})
        return cls.model_validate({**data, "content_sha256": digest, "report_id": "calreport-" + digest[7:]})

    def verify_execution(self, execution: CalibrationExecution) -> None:
        execution = CalibrationExecution.model_validate(execution.model_dump(mode="json"))
        record = CalibrationReportRecord.model_validate(self.model_dump(mode="json"))
        if record.execution_id != execution.execution_id or record.source != execution.source_binding:
            raise ValueError("report source does not match the frozen execution")
        if record.report.calibration_sha256 != execution.version.calibration.content_sha256:
            raise ValueError("report inner calibration set digest does not match the execution")


class CalibrationQualificationSource(Contract):
    schema_version: Literal[1] = 1
    qualification: JudgeQualification
    binding: QualificationBinding
    recorded_at: Text
    content_sha256: Digest

    _recorded_time = field_validator("recorded_at")(_time)

    def identity_payload(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude={"content_sha256", "recorded_at"})
        data["qualification"].pop("qualification_id")
        data["qualification"].pop("evaluated_at")
        data["binding"].pop("qualification_id")
        return data

    @model_validator(mode="after")
    def sealed_qualification(self) -> Self:
        _verify_inner_source(self.qualification, self.binding)
        if self.qualification.qualification_id != self.binding.qualification_id:
            raise ValueError("qualification ID differs from the complete binding")
        if self.content_sha256 != canonical_sha256(self.identity_payload()):
            raise ValueError("qualification source content_sha256 does not match its content")
        if self.binding.qualification_id != "calqual-" + self.content_sha256[7:]:
            raise ValueError("qualification ID must be content addressed")
        return self

    @classmethod
    def seal(cls, payload: dict[str, Any]) -> Self:
        qualification = _payload(payload["qualification"])
        binding = _payload(payload["binding"])
        qualification["qualification_id"] = binding["qualification_id"] = "pending"
        data = _normalized(cls, {**payload, "qualification": qualification, "binding": binding,
                                 "content_sha256": "sha256:" + "0" * 64})
        identity = {key: deepcopy(value) for key, value in data.items()
                    if key not in {"content_sha256", "recorded_at"}}
        identity["qualification"].pop("qualification_id")
        identity["qualification"].pop("evaluated_at")
        identity["binding"].pop("qualification_id")
        digest = canonical_sha256(identity)
        data["qualification"]["qualification_id"] = data["binding"]["qualification_id"] = "calqual-" + digest[7:]
        return cls.model_validate({**data, "content_sha256": digest})

    def verify_report(self, report: CalibrationReportRecord) -> None:
        report = CalibrationReportRecord.model_validate(report.model_dump(mode="json"))
        source = CalibrationQualificationSource.model_validate(self.model_dump(mode="json"))
        binding = source.binding
        if binding.report_id != report.report_id or binding.report_sha256 != report.content_sha256:
            raise ValueError("qualification does not bind the exact source report")
        if CalibrationSourceBinding.model_validate(binding.model_dump(exclude={
            "report_id", "report_sha256", "qualification_id",
        })) != report.source:
            raise ValueError("qualification source binding differs from its report")
        for field in ("calibration_sha256", "qualified", "experimental", "gate_eligible", "reasons"):
            if getattr(source.qualification, field) != getattr(report.report, field):
                raise ValueError(f"qualification differs from its source report: {field}")
