"""Pure, immutable contracts for explicit archive-before-trim Trace retention.

These records grant no maintenance ownership and perform no storage operations.
JSON timestamps are UTC; content identities use canonical ``sha256:`` digests.
Mutable collection fields are snapshots with detached readbacks. Consumers must
revalidate untrusted records, including records made with Pydantic's unchecked
``model_construct`` or ``model_copy(update=...)`` APIs, before using them.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterator
from copy import deepcopy
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel, BeforeValidator, ConfigDict, Field, field_serializer, field_validator, model_validator,
)

from motte_contracts.identity import canonical_sha256


class TraceRetentionDisabled(ValueError):
    """Retention was requested without explicit policy enablement."""


class TraceRetentionPlanChanged(ValueError):
    """A planned prefix or its protection inputs no longer match storage."""


class TraceArchiveInvalid(ValueError):
    """An archive or receipt cannot prove the planned immutable evidence."""


def utc_now() -> datetime:
    """Return an aware UTC server time, never a public payload timestamp."""
    return datetime.now(timezone.utc)


def _digest(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("sha256 identity must be a string")
    if re.fullmatch(r"[0-9a-f]{64}", value):
        value = "sha256:" + value
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("sha256 identity must be a lowercase SHA-256 digest")
    return value


def _utc_datetime(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("timestamp must be an aware ISO datetime") from error
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be an aware datetime")
    try:
        return value.astimezone(timezone.utc)
    except OverflowError as error:
        raise ValueError("timestamp must be representable in UTC") from error


def _nonempty(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("identity must be a nonempty string")
    return value


Sha256 = Annotated[str, BeforeValidator(_digest)]
NonEmptyStr = Annotated[str, BeforeValidator(_nonempty)]
PositiveInt = Annotated[int, Field(gt=0, strict=True)]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
UtcDateTime = Annotated[datetime, BeforeValidator(_utc_datetime)]


def _json_snapshot(value: Any) -> Any:
    """Keep only finite JSON values, without coercing keys or evidence types."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON evidence requires string keys")
        return {key: _json_snapshot(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_json_snapshot(item) for item in value]
    raise ValueError("Trace evidence requires finite JSON values")


class _RetentionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, revalidate_instances="always")

    @model_validator(mode="before")
    @classmethod
    def snapshot_input(cls, value: Any) -> Any:
        return deepcopy(value) if isinstance(value, dict) else value

    def __getattribute__(self, name: str) -> Any:
        value = super().__getattribute__(name)
        if isinstance(value, (dict, list)) and name in type(self).model_fields:
            return deepcopy(value)
        return value

    def __iter__(self) -> Iterator[tuple[str, Any]]:
        """Detach public iteration, including the standard ``dict(model)`` readback."""
        for name, value in super().__iter__():
            yield name, deepcopy(value)


class TraceRetentionConfig(_RetentionModel):
    enabled: bool = False
    retention_days: PositiveInt | None = None

    @model_validator(mode="after")
    def explicit_days_when_enabled(self) -> Self:
        if self.enabled and self.retention_days is None:
            raise ValueError("enabled retention requires explicit positive retention_days")
        return self


class StoredTraceEvent(_RetentionModel):
    run_id: NonEmptyStr
    seq: PositiveInt
    payload: dict[str, Any]
    stored_at: UtcDateTime | None

    @field_validator("payload")
    @classmethod
    def finite_json_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _json_snapshot(value)


class TracePrefix(_RetentionModel):
    run_id: NonEmptyStr
    run_revision: NonNegativeInt
    status: NonEmptyStr
    first_seq: PositiveInt
    last_seq: PositiveInt
    keep_seq: PositiveInt
    event_count: PositiveInt
    events_sha256: Sha256

    @model_validator(mode="after")
    def continuous_prefix_with_retained_highest_seq(self) -> Self:
        if self.last_seq < self.first_seq or self.keep_seq <= self.last_seq:
            raise ValueError("prefix must be ordered and retain a later highest seq")
        if self.event_count != self.last_seq - self.first_seq + 1:
            raise ValueError("event_count must equal the continuous prefix length")
        return self


class TraceProtection(_RetentionModel):
    run_ids: frozenset[NonEmptyStr] = Field(strict=False)
    event_seqs: dict[NonEmptyStr, frozenset[PositiveInt]]
    sha256: Sha256

    @field_validator("event_seqs", mode="before")
    @classmethod
    def sequence_sets(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for seqs in value.values():
                if isinstance(seqs, (list, set, frozenset, tuple)) and any(
                    type(seq) is not int or seq <= 0 for seq in seqs
                ):
                    raise ValueError("protected sequences must be positive strict integers")
            return {
                key: frozenset(seqs) if isinstance(seqs, (list, set, frozenset, tuple)) else seqs
                for key, seqs in value.items()
            }
        return value

    @field_validator("event_seqs")
    @classmethod
    def canonical_mapping(cls, value: dict[str, frozenset[int]]) -> dict[str, frozenset[int]]:
        return dict(sorted(value.items()))

    @field_serializer("run_ids", when_used="json")
    def serialize_run_ids(self, value: frozenset[str]) -> list[str]:
        return sorted(value)

    @field_serializer("event_seqs", when_used="json")
    def serialize_event_seqs(self, value: dict[str, frozenset[int]]) -> dict[str, list[int]]:
        return {run_id: sorted(seqs) for run_id, seqs in sorted(value.items())}

    @model_validator(mode="after")
    def digest_binds_protection(self) -> Self:
        if self.sha256 != canonical_sha256(self.model_dump(mode="json", exclude={"sha256"})):
            raise ValueError("protection sha256 does not match canonical protection")
        return self


class TraceRetentionPlan(_RetentionModel):
    schema_version: Literal[1]
    plan_id: Sha256
    store_identity_sha256: Sha256
    config: TraceRetentionConfig
    cutoff: UtcDateTime | None
    protection_sha256: Sha256
    prefixes: list[TracePrefix]

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("schema_version must be the strict integer 1")
        return value

    @field_validator("prefixes")
    @classmethod
    def canonical_prefixes(cls, value: list[TracePrefix]) -> list[TracePrefix]:
        if len({prefix.run_id for prefix in value}) != len(value):
            raise ValueError("plan requires at most one prefix per run")
        return sorted(value, key=lambda prefix: prefix.run_id)

    @model_validator(mode="after")
    def policy_and_identity_are_bound(self) -> Self:
        if self.config.enabled:
            if self.cutoff is None:
                raise ValueError("enabled plans require an explicit cutoff")
        elif self.cutoff is not None or self.prefixes:
            raise ValueError("disabled plans cannot have a cutoff or trim candidates")
        if self.plan_id != canonical_sha256(self.model_dump(mode="json", exclude={"plan_id"})):
            raise ValueError("plan_id does not match canonical plan body")
        return self


class TracePassReference(_RetentionModel):
    pass_id: NonEmptyStr
    owning_run_id: NonEmptyStr | None = None
    expected_run_id: NonEmptyStr | None = None


class TraceReferenceDocument(_RetentionModel):
    """Syntactic references copied from canonical evidence, not a trust assertion."""
    schema_version: Literal[1]
    run_ids: tuple[NonEmptyStr, ...] = Field(strict=False)
    event_seqs: dict[NonEmptyStr, tuple[PositiveInt, ...]]
    pass_references: tuple[TracePassReference, ...] = Field(strict=False)

    @field_validator('schema_version', mode='before')
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int:
            raise ValueError('reference schema_version must be integer 1')
        return value

    @field_validator('run_ids')
    @classmethod
    def ordered_runs(cls, value):
        return tuple(sorted(set(value)))

    @field_validator('event_seqs', mode='before')
    @classmethod
    def ordered_events(cls, value):
        if not isinstance(value, dict):
            raise ValueError('reference event sequences must be a mapping')
        result = {}
        for key, seqs in sorted(value.items()):
            if not isinstance(seqs, (list, tuple)) or any(type(seq) is not int or seq <= 0 for seq in seqs):
                raise ValueError('reference event sequences must be positive strict integers')
            result[key] = tuple(sorted(set(seqs)))
        return result

    @field_validator('pass_references')
    @classmethod
    def ordered_passes(cls, value):
        keys = {(ref.pass_id, ref.owning_run_id or '', ref.expected_run_id or '') for ref in value}
        return tuple(TracePassReference(pass_id=key[0], owning_run_id=key[1] or None,
                                        expected_run_id=key[2] or None) for key in sorted(keys))


class TraceArchiveReceipt(_RetentionModel):
    archive_id: NonEmptyStr
    plan_id: Sha256
    prefix: TracePrefix
    artifact_id: NonEmptyStr
    sha256: Sha256
    bytes: PositiveInt
    cutoff: UtcDateTime
    artifact_refs: dict[NonEmptyStr, Sha256 | None]
    artifact_hashes: list[Sha256]
    committed_at: UtcDateTime | None = None
    reference_document: TraceReferenceDocument | None = None

    @field_validator("artifact_refs")
    @classmethod
    def canonical_refs(cls, value: dict[str, str | None]) -> dict[str, str | None]:
        return dict(sorted(value.items()))

    @field_validator("artifact_hashes")
    @classmethod
    def canonical_hashes(cls, value: list[str]) -> list[str]:
        return sorted(set(value))


class TraceEventWindow(_RetentionModel):
    events: list[dict[str, Any]]
    trimmed_through: NonNegativeInt

    @field_validator("events")
    @classmethod
    def finite_json_events(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _json_snapshot(value)

    @model_validator(mode="after")
    def visible_sequences_follow_trim_boundary(self) -> Self:
        previous = self.trimmed_through
        for event in self.events:
            seq = event.get("seq")
            if type(seq) is not int or seq <= previous:
                raise ValueError("visible event seqs must be strictly increasing after trim boundary")
            previous = seq
        return self


class TraceRetentionResult(_RetentionModel):
    """Operation result, not a proof authorizing mutation.

    Zero with receipts represents exact replay. The storage operation must verify
    the complete saved plan and archive bytes before constructing that result;
    this pure model only checks committed identities and count consistency.
    """
    plan_id: Sha256
    trimmed_events: NonNegativeInt
    receipts: list[TraceArchiveReceipt]

    @field_validator("receipts")
    @classmethod
    def canonical_receipts(cls, value: list[TraceArchiveReceipt]) -> list[TraceArchiveReceipt]:
        if len({receipt.prefix.run_id for receipt in value}) != len(value):
            raise ValueError("result requires at most one receipt per run")
        return sorted(value, key=lambda receipt: receipt.prefix.run_id)

    @model_validator(mode="after")
    def committed_receipts_match_result(self) -> Self:
        if any(receipt.plan_id != self.plan_id or receipt.committed_at is None
               for receipt in self.receipts):
            raise ValueError("result requires committed receipts from its plan")
        if self.trimmed_events not in (0, sum(receipt.prefix.event_count for receipt in self.receipts)):
            raise ValueError("trimmed_events must be zero for verified replay or equal the complete receipt event count")
        return self
