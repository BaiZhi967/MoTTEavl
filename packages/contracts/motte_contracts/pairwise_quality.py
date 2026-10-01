"""Content-addressed pairwise quality evidence, separate from Boolean accuracy.

These pure contracts validate shape, source digests and internal consistency.
They cannot establish trusted Job/Invocation ownership or Judge qualification;
the server's ledger reconstruction must independently verify those facts.
"""

from __future__ import annotations

from copy import deepcopy
from math import fsum, isclose
from typing import Annotated, Any, Generator, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from .hashing import canonical_hash
from .messages import Contract

_Text = Annotated[str, Field(strict=True, min_length=1)]
_Digest = Annotated[str, Field(strict=True, pattern=r"^sha256:[0-9a-f]{64}$")]
_Count = Annotated[int, Field(strict=True, ge=0)]
_Ratio = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]


class _DetachedPairwiseContract(Contract):
    """Defensive public field/iteration reads, local to the new contracts only.

    Frozen model fields may contain mutable containers or other models. Return
    detached values on supported readback paths; model_copy remains Pydantic's
    explicitly unchecked operation and every model instance is revalidated.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    def __getattribute__(self, name: str) -> Any:
        value = super().__getattribute__(name)
        return deepcopy(value) if name in type(self).model_fields else value

    def __iter__(self) -> Generator[tuple[str, Any], None, None]:
        for name, value in super().__iter__():
            yield name, deepcopy(value)


class PairwiseRoleBinding(_DetachedPairwiseContract):
    """Explicit stable roles; A/B presentation and lexical order choose neither."""

    case_id: _Text
    pair_id: _Text
    challenger_candidate_id: _Text
    reference_candidate_id: _Text
    challenger_attempt_id: _Text
    reference_attempt_id: _Text
    candidate_input_sha256: dict[_Text, _Digest]

    @model_validator(mode="before")
    @classmethod
    def _detach_input(cls, value: Any) -> Any:
        return deepcopy(value)

    @model_validator(mode="after")
    def _roles_are_explicit(self) -> Self:
        if self.challenger_candidate_id == self.reference_candidate_id:
            raise ValueError("challenger and reference candidates must differ")
        if self.challenger_attempt_id == self.reference_attempt_id:
            raise ValueError("challenger and reference attempts must differ")
        if set(self.candidate_input_sha256) != {
            self.challenger_candidate_id,
            self.reference_candidate_id,
        }:
            raise ValueError("candidate input digests must bind exactly the two role candidates")
        return self


class _PairwiseQualityFields(_DetachedPairwiseContract):
    schema_version: Literal[1] = 1
    metric_id: Literal["pairwise_challenger_score"] = "pairwise_challenger_score"
    metric_version: Literal["1"] = "1"
    algorithm: Literal["pairwise-quality@1"] = "pairwise-quality@1"
    source_pass_id: _Text
    job_id: _Text | None
    # A required null is honest unavailable evidence, never a fabricated digest.
    plan_sha256: _Digest | None
    ledger_sha256: _Digest | None
    roles_sha256: _Digest
    roles: tuple[PairwiseRoleBinding, ...]
    planned_pairs: _Count
    valid_pairs: _Count
    planned_calls: _Count
    settled_calls: _Count
    pair_values: dict[_Text, _Ratio | None]
    value: _Ratio | None
    coverage: _Ratio | None
    reasons: tuple[_Text, ...]

    @model_validator(mode="before")
    @classmethod
    def _detach_input(cls, value: Any) -> Any:
        if (
            isinstance(value, dict)
            and "schema_version" in value
            and type(value["schema_version"]) is not int
        ):
            raise ValueError("schema_version must be the integer 1, not a coerced Boolean or float")
        return deepcopy(value)

    @model_validator(mode="after")
    def _evidence_is_coherent(self) -> Self:
        role_pairs = {role.pair_id for role in self.roles}
        role_cases = {role.case_id for role in self.roles}
        if len(role_pairs) != len(self.roles) or len(role_cases) != len(self.roles):
            raise ValueError(
                "exactly one unique role binding is allowed per pair and selected case"
            )
        candidates = [
            candidate
            for role in self.roles
            for candidate in (role.challenger_candidate_id, role.reference_candidate_id)
        ]
        attempts = [
            attempt
            for role in self.roles
            for attempt in (role.challenger_attempt_id, role.reference_attempt_id)
        ]
        if len(set(candidates)) != len(candidates) or len(set(attempts)) != len(attempts):
            raise ValueError("saved candidates and attempts cannot be reused across selected cases")
        if len(self.pair_values) != self.planned_pairs or not role_pairs <= set(self.pair_values):
            raise ValueError("pair values must retain all independently planned pair identities")
        if self.roles_sha256 != canonical_hash(
            [role.model_dump(mode="json") for role in self.roles]
        ):
            raise ValueError("roles_sha256 must hash the actual explicit role bindings")
        if len(self.roles) < self.planned_pairs and "missing_pairwise_roles" not in self.reasons:
            raise ValueError("incomplete historical roles require an explicit missing-role reason")
        valid_ids = {key for key, value in self.pair_values.items() if value is not None}
        if not valid_ids <= role_pairs or len(valid_ids) != self.valid_pairs:
            raise ValueError("valid pair counts/values require explicit roles for those pairs")
        if (
            self.planned_calls < self.planned_pairs
            or self.settled_calls > self.planned_calls
            or self.valid_pairs > self.settled_calls
        ):
            raise ValueError(
                "planned pairs and settled planned calls must retain separate valid counts"
            )
        if not self.planned_pairs and (self.planned_calls or self.settled_calls):
            raise ValueError("an empty pair plan cannot claim planned or settled pairwise calls")
        coverage = self.valid_pairs / self.planned_pairs if self.planned_pairs else None
        if self.coverage != coverage:
            raise ValueError(
                "coverage must be valid_pairs/planned_pairs, or null for an empty plan"
            )
        for digest, reason in (
            (self.plan_sha256, "plan_digest_unavailable"),
            (self.ledger_sha256, "ledger_digest_unavailable"),
            (self.job_id, "subject_job_unavailable"),
        ):
            if digest is None and (reason not in self.reasons or self.valid_pairs):
                raise ValueError(
                    "unknown source digest requires an explicit reason and zero valid pairs"
                )
        if self.value is None:
            if not self.reasons:
                raise ValueError("unavailable quality requires explicit reasons")
        else:
            if (
                not self.planned_pairs
                or self.valid_pairs != self.planned_pairs
                or self.settled_calls != self.planned_calls
                or self.reasons
                or self.plan_sha256 is None
                or self.ledger_sha256 is None
                or self.job_id is None
            ):
                raise ValueError(
                    "available quality requires complete roles, pairs, calls and source digests"
                )
            # Values have already crossed the public float boundary. Allow only
            # float conversion noise; the trusted producer uses exact Fractions.
            expected = (
                fsum(value for value in self.pair_values.values() if value is not None)
                / self.planned_pairs
            )
            if not isclose(self.value, expected, rel_tol=1e-15, abs_tol=1e-15):
                raise ValueError(
                    "quality must equally average pair values, never weight by call count"
                )
        return self


class PairwiseQualitySnapshot(_PairwiseQualityFields):
    """Detached typed evidence with every identity/count/value bound by a digest.

    Roleless history retains the planned-pair denominator but cannot assert an
    available value. Nullable source digests are required fields with explicit
    unavailable reasons, not positive evidence. A valid content digest alone
    never grants trust or Gate eligibility.
    """

    content_sha256: _Digest

    def compute_content_sha256(self) -> str:
        return canonical_hash(self.model_dump(mode="json", exclude={"content_sha256"}))

    @model_validator(mode="after")
    def _content_is_bound(self) -> Self:
        if self.content_sha256 != self.compute_content_sha256():
            raise ValueError("pairwise quality content_sha256 does not match its complete content")
        return self

    @classmethod
    def seal(cls, payload: Any) -> Self:
        """Normalize/validate a pure producer payload, then seal its whole content."""
        raw = (
            payload.model_dump(mode="json")
            if isinstance(payload, _PairwiseQualityFields)
            else deepcopy(payload)
        )
        raw.pop("content_sha256", None)
        fields = _PairwiseQualityFields.model_validate(raw)
        data = fields.model_dump(mode="json")
        return cls.model_validate({**data, "content_sha256": canonical_hash(data)})
