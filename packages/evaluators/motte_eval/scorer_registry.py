"""Explicit registry for deterministic Direct LLM v2 scorers.

The registry is deliberately closed: callers can resolve built-in ``id`` +
``version`` pairs, but cannot register or dynamically import evaluator code.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from pydantic import ValidationError

from motte_contracts.direct_llm_v2 import ScorerSpec, scorer_config_sha256


class ScorerSpecError(ValueError):
    """A scorer id, version, or configuration is invalid."""


class ScorerImplementation(Protocol):
    scorer_id: str
    version: str

    def normalize_config(self, config: Mapping[str, Any]) -> dict[str, Any]: ...

    def validate_expected(self, expected: Any, config: Mapping[str, Any]) -> Any: ...

    def parse_output(self, content: Any, config: Mapping[str, Any]) -> Any: ...

    def compare(self, parsed: Any, expected: Any, config: Mapping[str, Any]) -> bool: ...

    def serialize_parsed(self, parsed: Any) -> Any: ...


@dataclass(frozen=True)
class ResolvedScorer:
    implementation: ScorerImplementation
    config: Mapping[str, Any]
    config_sha256: str

    @property
    def scorer_id(self) -> str:
        return self.implementation.scorer_id

    @property
    def version(self) -> str:
        return self.implementation.version

    def as_spec(self) -> dict[str, Any]:
        return {
            "id": self.scorer_id,
            "version": self.version,
            "config": dict(self.config),
            "config_sha256": self.config_sha256,
        }


class ScorerRegistry:
    """Immutable lookup table of audited built-in scorer implementations."""

    def __init__(self, implementations: tuple[ScorerImplementation, ...]) -> None:
        scorers: dict[tuple[str, str], ScorerImplementation] = {}
        for implementation in implementations:
            key = (implementation.scorer_id, implementation.version)
            if key in scorers:
                raise RuntimeError(f"duplicate built-in scorer: {key[0]}@{key[1]}")
            scorers[key] = implementation
        self._scorers = MappingProxyType(scorers)

    def keys(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._scorers))

    def get(self, scorer_id: str, version: str) -> ScorerImplementation:
        if not isinstance(scorer_id, str) or not scorer_id:
            raise ScorerSpecError("scorer id must be a non-empty string")
        if not isinstance(version, str) or not version:
            raise ScorerSpecError("scorer version must be a non-empty string")
        try:
            return self._scorers[(scorer_id, version)]
        except KeyError as error:
            raise ScorerSpecError(f"unsupported scorer: {scorer_id}@{version}") from error

    def resolve(self, spec: Mapping[str, Any]) -> ResolvedScorer:
        if not isinstance(spec, Mapping):
            raise ScorerSpecError("scorer spec must be an object")
        required = {"id", "version", "config", "config_sha256"}
        unknown = sorted(set(spec) - required)
        missing = sorted(required - set(spec))
        if unknown:
            raise ScorerSpecError(f"unknown scorer spec fields: {', '.join(unknown)}")
        if missing:
            raise ScorerSpecError(f"missing scorer spec fields: {', '.join(missing)}")
        implementation = self.get(spec.get("id"), spec.get("version"))
        raw_config = spec.get("config")
        if not isinstance(raw_config, Mapping):
            raise ScorerSpecError("scorer config must be an object")
        try:
            normalized = implementation.normalize_config(raw_config)
        except ScorerSpecError:
            raise
        except ValueError as error:
            raise ScorerSpecError(str(error)) from error
        config_sha256 = scorer_config_sha256(normalized)
        if spec.get("config_sha256") != config_sha256:
            raise ScorerSpecError("scorer config_sha256 mismatch after normalization")
        payload = {
            "id": implementation.scorer_id,
            "version": implementation.version,
            "config": normalized,
            "config_sha256": config_sha256,
        }
        try:
            ScorerSpec.model_validate(payload)
        except ValidationError as error:
            raise ScorerSpecError(str(error)) from error
        return ResolvedScorer(implementation, MappingProxyType(normalized), config_sha256)


from .direct_llm_v2 import ChoiceScorer, ExactScorer, JsonEqualScorer, NumericScorer  # noqa: E402

SCORER_REGISTRY = ScorerRegistry((
    ChoiceScorer(), ExactScorer(), NumericScorer(), JsonEqualScorer(),
))


def get_scorer(scorer_id: str, version: str) -> ScorerImplementation:
    return SCORER_REGISTRY.get(scorer_id, version)


def resolve_scorer(spec: Mapping[str, Any]) -> ResolvedScorer:
    return SCORER_REGISTRY.resolve(spec)


def available_scorers() -> tuple[tuple[str, str], ...]:
    return SCORER_REGISTRY.keys()
