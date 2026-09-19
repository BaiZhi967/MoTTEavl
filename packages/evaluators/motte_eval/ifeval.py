"""Independent preview evaluator for instruction-following rule reports.

This module intentionally does not register an evaluation plugin or import upstream
IFEval code/data. The closed rule registry contains only locally implemented preview
rules. Official readiness remains fail-closed without an injected trusted verifier.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import InitVar, dataclass
from types import MappingProxyType
from typing import Any, Protocol

IFEVAL_FULL_CASE_COUNT = 541
IFEVAL_SOURCE_STATUS = "pending"
IFEVAL_EVALUATOR_NOT_APPROVED = "IFEVAL_EVALUATOR_NOT_APPROVED"

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")


def _canonical_sha256(value: Any) -> str:
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError("value must contain canonical JSON data") from error
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def rule_config_sha256(config: Mapping[str, Any]) -> str:
    if not isinstance(config, Mapping):
        raise ValueError("rule config must be an object")
    return _canonical_sha256(dict(config))


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{label} schema mismatch "
            f"(missing={sorted(expected - actual)}, extra={sorted(actual - expected)})"
        )


def _nonempty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


@dataclass(frozen=True, slots=True)
class RuleSpec:
    id: str
    version: str
    config: Mapping[str, Any]
    config_sha256: str

    def __post_init__(self) -> None:
        _nonempty_text(self.id, "rule id")
        _nonempty_text(self.version, "rule version")
        if not isinstance(self.config, Mapping):
            raise ValueError("rule config must be an object")
        config = dict(self.config)
        expected = rule_config_sha256(config)
        if self.config_sha256 != expected:
            raise ValueError("rule config_sha256 mismatch")
        object.__setattr__(self, "config", MappingProxyType(config))

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "config": dict(self.config),
            "config_sha256": self.config_sha256,
        }


@dataclass(frozen=True, slots=True)
class IFEvalCase:
    case_id: str
    prompt: str
    rules: tuple[RuleSpec, ...]

    def __post_init__(self) -> None:
        _nonempty_text(self.case_id, "case_id")
        _nonempty_text(self.prompt, "prompt")
        rules = tuple(_coerce_rule_spec(rule) for rule in self.rules)
        if not rules:
            raise ValueError("IFEval case requires at least one rule")
        for rule in rules:
            resolve_rule(rule)
        identities = [(rule.id, rule.version) for rule in rules]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate rule in IFEval case")
        object.__setattr__(self, "rules", rules)

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "prompt": self.prompt,
            "rules": [rule.as_dict() for rule in self.rules],
        }


class RuleImplementation(Protocol):
    rule_id: str
    version: str

    def normalize_config(self, config: Mapping[str, Any]) -> dict[str, Any]: ...

    def evaluate(self, output: str, config: Mapping[str, Any]) -> tuple[bool, bool]: ...


def _unknown_config(config: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(config) - allowed)
    if unknown:
        raise ValueError(f"unknown rule config fields: {', '.join(unknown)}")


def _text_config(config: Mapping[str, Any]) -> dict[str, Any]:
    _unknown_config(config, {"value", "case_sensitive"})
    value = _nonempty_text(config.get("value"), "rule config value")
    case_sensitive = config.get("case_sensitive", True)
    if type(case_sensitive) is not bool:
        raise ValueError("case_sensitive must be a boolean")
    return {"value": value, "case_sensitive": case_sensitive}


def _loose_text(value: str) -> str:
    return " ".join(value.split())


def _comparable(value: str, *, case_sensitive: bool) -> str:
    return value if case_sensitive else value.casefold()


@dataclass(frozen=True, slots=True)
class _TextRule:
    rule_id: str
    operation: str
    version: str = "1"

    def normalize_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        return _text_config(config)

    def evaluate(self, output: str, config: Mapping[str, Any]) -> tuple[bool, bool]:
        case_sensitive = config["case_sensitive"]
        strict_output = _comparable(output, case_sensitive=case_sensitive)
        strict_value = _comparable(config["value"], case_sensitive=case_sensitive)
        loose_output = _comparable(_loose_text(output), case_sensitive=case_sensitive)
        loose_value = _comparable(_loose_text(config["value"]), case_sensitive=case_sensitive)
        if self.operation == "contains":
            return strict_value in strict_output, loose_value in loose_output
        if self.operation == "starts_with":
            return strict_output.startswith(strict_value), loose_output.startswith(loose_value)
        return strict_output.endswith(strict_value), loose_output.endswith(loose_value)


class _DuplicateKey(ValueError):
    pass


class _JsonConstant(ValueError):
    pass


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise _JsonConstant(value)


def _parse_json_object(value: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_json_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError, _DuplicateKey, _JsonConstant):
        return None
    return parsed if isinstance(parsed, dict) else None


def _unwrap_json_fence(value: str) -> str:
    stripped = value.strip()
    lines = stripped.splitlines()
    if (
        len(lines) >= 3
        and lines[0].strip().casefold() in {"```", "```json"}
        and lines[-1].strip() == "```"
    ):
        return "\n".join(lines[1:-1]).strip()
    return stripped


@dataclass(frozen=True, slots=True)
class _JsonObjectRule:
    rule_id: str = "json_object"
    version: str = "1"

    def normalize_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        _unknown_config(config, {"required_keys", "allow_extra_keys"})
        required_keys = config.get("required_keys", [])
        if not isinstance(required_keys, (list, tuple)):
            raise ValueError("required_keys must be a list")
        if any(not isinstance(key, str) or not key.strip() for key in required_keys):
            raise ValueError("required_keys must contain non-empty strings")
        if len(set(required_keys)) != len(required_keys):
            raise ValueError("required_keys must not contain duplicates")
        allow_extra = config.get("allow_extra_keys", True)
        if type(allow_extra) is not bool:
            raise ValueError("allow_extra_keys must be a boolean")
        return {
            "required_keys": sorted(required_keys),
            "allow_extra_keys": allow_extra,
        }

    def _passes(self, parsed: dict[str, Any] | None, config: Mapping[str, Any]) -> bool:
        if parsed is None:
            return False
        required = set(config["required_keys"])
        actual = set(parsed)
        return required.issubset(actual) and (config["allow_extra_keys"] or actual == required)

    def evaluate(self, output: str, config: Mapping[str, Any]) -> tuple[bool, bool]:
        strict = None if output != output.strip() else _parse_json_object(output)
        loose = _parse_json_object(_unwrap_json_fence(output))
        return self._passes(strict, config), self._passes(loose, config)


@dataclass(frozen=True, slots=True)
class _MaxWordsRule:
    rule_id: str = "max_words"
    version: str = "1"

    def normalize_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        _unknown_config(config, {"max_words"})
        maximum = config.get("max_words")
        if type(maximum) is not int or maximum <= 0:
            raise ValueError("max_words must be a positive integer")
        return {"max_words": maximum}

    def evaluate(self, output: str, config: Mapping[str, Any]) -> tuple[bool, bool]:
        strict_tokens = output.split()
        loose_tokens = [
            token for token in strict_tokens if any(character.isalnum() for character in token)
        ]
        maximum = config["max_words"]
        return len(strict_tokens) <= maximum, len(loose_tokens) <= maximum


_RULES: tuple[RuleImplementation, ...] = (
    _TextRule("contains", "contains"),
    _TextRule("starts_with", "starts_with"),
    _TextRule("ends_with", "ends_with"),
    _JsonObjectRule(),
    _MaxWordsRule(),
)
RULE_REGISTRY: Mapping[tuple[str, str], RuleImplementation] = MappingProxyType(
    {(rule.rule_id, rule.version): rule for rule in _RULES}
)


@dataclass(frozen=True, slots=True)
class ResolvedRule:
    implementation: RuleImplementation
    config: Mapping[str, Any]
    config_sha256: str


def available_rules() -> tuple[tuple[str, str], ...]:
    return tuple(sorted(RULE_REGISTRY))


def _coerce_rule_spec(value: RuleSpec | Mapping[str, Any]) -> RuleSpec:
    if isinstance(value, RuleSpec):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("rule spec must be an object")
    _exact_keys(value, {"id", "version", "config", "config_sha256"}, "rule spec")
    return RuleSpec(
        id=value["id"],
        version=value["version"],
        config=value["config"],
        config_sha256=value["config_sha256"],
    )


def resolve_rule(value: RuleSpec | Mapping[str, Any]) -> ResolvedRule:
    spec = _coerce_rule_spec(value)
    implementation = RULE_REGISTRY.get((spec.id, spec.version))
    if implementation is None:
        raise ValueError(f"unsupported IFEval preview rule: {spec.id}@{spec.version}")
    normalized = implementation.normalize_config(spec.config)
    expected_hash = rule_config_sha256(normalized)
    if spec.config_sha256 != expected_hash:
        raise ValueError("rule config_sha256 mismatch after normalization")
    return ResolvedRule(
        implementation=implementation,
        config=MappingProxyType(normalized),
        config_sha256=expected_hash,
    )


def build_rule_spec(rule_id: str, config: Mapping[str, Any], *, version: str = "1") -> RuleSpec:
    implementation = RULE_REGISTRY.get((rule_id, version))
    if implementation is None:
        raise ValueError(f"unsupported IFEval preview rule: {rule_id}@{version}")
    if not isinstance(config, Mapping):
        raise ValueError("rule config must be an object")
    normalized = implementation.normalize_config(config)
    return RuleSpec(
        id=rule_id,
        version=version,
        config=normalized,
        config_sha256=rule_config_sha256(normalized),
    )


def _coerce_case(value: IFEvalCase | Mapping[str, Any]) -> IFEvalCase:
    if isinstance(value, IFEvalCase):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("IFEval case must be an object")
    _exact_keys(value, {"case_id", "prompt", "rules"}, "IFEval case")
    rules = value["rules"]
    if isinstance(rules, (str, bytes)) or not isinstance(rules, Sequence):
        raise ValueError("IFEval case rules must be a list")
    return IFEvalCase(
        case_id=value["case_id"],
        prompt=value["prompt"],
        rules=tuple(_coerce_rule_spec(rule) for rule in rules),
    )


def _normalized_output_summary(output: str) -> dict[str, Any]:
    normalized_lines = [
        line.rstrip() for line in output.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    normalized = "\n".join(normalized_lines).strip()
    return {
        "sha256": "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "utf8_bytes": len(normalized.encode("utf-8")),
        "characters": len(normalized),
        "lines": 0 if not normalized else normalized.count("\n") + 1,
        "words": len(re.findall(r"\S+", normalized)),
        "empty": not normalized,
    }


def evaluate_rule(case_id: str, rule: RuleSpec | Mapping[str, Any], output: str) -> dict[str, Any]:
    _nonempty_text(case_id, "case_id")
    if not isinstance(output, str):
        raise ValueError("IFEval output must be text")
    spec = _coerce_rule_spec(rule)
    resolved = resolve_rule(spec)
    strict_passed, loose_passed = resolved.implementation.evaluate(output, resolved.config)
    return {
        "case_id": case_id,
        "rule_id": spec.id,
        "rule_version": spec.version,
        "config_sha256": resolved.config_sha256,
        "outcome": "judged",
        "strict_passed": strict_passed,
        "loose_passed": loose_passed,
        "normalized_output": _normalized_output_summary(output),
    }


_EVIDENCE_KEYS = {
    "case_id",
    "rule_id",
    "rule_version",
    "config_sha256",
    "outcome",
    "strict_passed",
    "loose_passed",
    "normalized_output",
}
_SUMMARY_KEYS = {"sha256", "utf8_bytes", "characters", "lines", "words", "empty"}


def _not_attempted(case_id: str, rule: RuleSpec) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "rule_id": rule.id,
        "rule_version": rule.version,
        "config_sha256": rule.config_sha256,
        "outcome": "not_attempted",
        "strict_passed": None,
        "loose_passed": None,
        "normalized_output": None,
    }


def _validate_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    _exact_keys(value, _EVIDENCE_KEYS, "rule evidence")
    case_id = _nonempty_text(value["case_id"], "evidence case_id")
    rule_id = _nonempty_text(value["rule_id"], "evidence rule_id")
    rule_version = _nonempty_text(value["rule_version"], "evidence rule_version")
    config_sha256 = value["config_sha256"]
    if not isinstance(config_sha256, str) or _SHA256.fullmatch(config_sha256) is None:
        raise ValueError("evidence config_sha256 must be canonical")
    outcome = value["outcome"]
    if outcome not in {"judged", "not_attempted"}:
        raise ValueError("evidence outcome must be judged or not_attempted")
    strict_passed = value["strict_passed"]
    loose_passed = value["loose_passed"]
    summary = value["normalized_output"]
    if outcome == "not_attempted":
        if strict_passed is not None or loose_passed is not None or summary is not None:
            raise ValueError("not_attempted evidence cannot contain judgments or output summary")
    else:
        if type(strict_passed) is not bool or type(loose_passed) is not bool:
            raise ValueError("judged evidence requires strict and loose booleans")
        if not isinstance(summary, Mapping):
            raise ValueError("judged evidence requires normalized output summary")
        _exact_keys(summary, _SUMMARY_KEYS, "normalized output summary")
        if not isinstance(summary["sha256"], str) or _SHA256.fullmatch(summary["sha256"]) is None:
            raise ValueError("normalized output sha256 must be canonical")
        for field in ("utf8_bytes", "characters", "lines", "words"):
            if type(summary[field]) is not int or summary[field] < 0:
                raise ValueError(f"normalized output {field} must be non-negative")
        if type(summary["empty"]) is not bool:
            raise ValueError("normalized output empty must be a boolean")
        summary = dict(summary)
    return {
        "case_id": case_id,
        "rule_id": rule_id,
        "rule_version": rule_version,
        "config_sha256": config_sha256,
        "outcome": outcome,
        "strict_passed": strict_passed,
        "loose_passed": loose_passed,
        "normalized_output": summary,
    }


def _metric(selected: int, judged: int, passed: int) -> dict[str, Any]:
    return {
        "selected": selected,
        "judged": judged,
        "passed": passed,
        "rate": passed / judged if judged else None,
        "coverage": judged / selected if selected else None,
    }


def aggregate_ifeval(
    cases: Sequence[IFEvalCase | Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    normalized_cases = [_coerce_case(case) for case in cases]
    case_ids = [case.case_id for case in normalized_cases]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("duplicate IFEval case_id")

    required: dict[tuple[str, str, str], RuleSpec] = {}
    for case in normalized_cases:
        for rule in case.rules:
            required[(case.case_id, rule.id, rule.version)] = rule

    supplied: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in evidence:
        if not isinstance(raw, Mapping):
            raise ValueError("rule evidence must be an object")
        row = _validate_evidence(raw)
        key = (row["case_id"], row["rule_id"], row["rule_version"])
        if key not in required:
            raise ValueError(f"rule evidence is not selected: {key!r}")
        if key in supplied:
            raise ValueError(f"duplicate rule evidence: {key!r}")
        if row["config_sha256"] != required[key].config_sha256:
            raise ValueError("rule evidence config_sha256 does not match selected rule")
        supplied[key] = row

    case_reports: list[dict[str, Any]] = []
    all_rule_rows: list[dict[str, Any]] = []
    for case in normalized_cases:
        rule_rows = [
            supplied.get(
                (case.case_id, rule.id, rule.version),
                _not_attempted(case.case_id, rule),
            )
            for rule in case.rules
        ]
        all_rule_rows.extend(rule_rows)
        judged_rows = [row for row in rule_rows if row["outcome"] == "judged"]
        if not judged_rows:
            outcome = "not_attempted"
            prompt_strict = None
            prompt_loose = None
        elif len(judged_rows) != len(rule_rows):
            outcome = "incomplete"
            prompt_strict = None
            prompt_loose = None
        else:
            outcome = "judged"
            prompt_strict = all(row["strict_passed"] for row in judged_rows)
            prompt_loose = all(row["loose_passed"] for row in judged_rows)
        case_reports.append(
            {
                "case_id": case.case_id,
                "outcome": outcome,
                "prompt_strict": prompt_strict,
                "prompt_loose": prompt_loose,
                "rules": rule_rows,
            }
        )

    prompt_judged = [case for case in case_reports if case["outcome"] == "judged"]
    instruction_judged = [row for row in all_rule_rows if row["outcome"] == "judged"]
    aggregate = {
        "prompt_level_strict": _metric(
            len(case_reports),
            len(prompt_judged),
            sum(case["prompt_strict"] is True for case in prompt_judged),
        ),
        "prompt_level_loose": _metric(
            len(case_reports),
            len(prompt_judged),
            sum(case["prompt_loose"] is True for case in prompt_judged),
        ),
        "instruction_level_strict": _metric(
            len(all_rule_rows),
            len(instruction_judged),
            sum(row["strict_passed"] is True for row in instruction_judged),
        ),
        "instruction_level_loose": _metric(
            len(all_rule_rows),
            len(instruction_judged),
            sum(row["loose_passed"] is True for row in instruction_judged),
        ),
    }
    return {
        "schema_version": 1,
        "suite": "ifeval-preview",
        "official": False,
        "source_status": IFEVAL_SOURCE_STATUS,
        "full_case_count": IFEVAL_FULL_CASE_COUNT,
        "selected_case_ids": case_ids,
        "cases": case_reports,
        "aggregate": aggregate,
    }


def evaluate_cases(
    cases: Sequence[IFEvalCase | Mapping[str, Any]],
    outputs: Mapping[str, str],
) -> dict[str, Any]:
    if not isinstance(outputs, Mapping):
        raise ValueError("outputs must be a case_id to text mapping")
    normalized_cases = [_coerce_case(case) for case in cases]
    case_ids = [case.case_id for case in normalized_cases]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("duplicate IFEval case_id")
    unknown = sorted(set(outputs) - set(case_ids))
    if unknown:
        raise ValueError(f"output case_id is not selected: {unknown[0]}")
    evidence = []
    for case in normalized_cases:
        if case.case_id not in outputs:
            continue
        output = outputs[case.case_id]
        if not isinstance(output, str):
            raise ValueError("IFEval output must be text")
        evidence.extend(evaluate_rule(case.case_id, rule, output) for rule in case.rules)
    return aggregate_ifeval(normalized_cases, evidence)


class IFEvalOfficialGateError(RuntimeError):
    code = IFEVAL_EVALUATOR_NOT_APPROVED

    def __init__(self) -> None:
        super().__init__(IFEVAL_EVALUATOR_NOT_APPROVED)


_APPROVAL_KEYS = {
    "source_id",
    "source_revision",
    "license_approval_sha256",
    "evaluator_revision",
    "parity_evidence_sha256",
}


def _approval_evidence(value: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise IFEvalOfficialGateError()
    try:
        _exact_keys(value, _APPROVAL_KEYS, "IFEval approval evidence")
        source_id = _nonempty_text(value["source_id"], "approval source_id")
        source_revision = value["source_revision"]
        evaluator_revision = value["evaluator_revision"]
        license_hash = value["license_approval_sha256"]
        parity_hash = value["parity_evidence_sha256"]
        if source_id != "ifeval":
            raise ValueError("approval source_id must be ifeval")
        if not isinstance(source_revision, str) or _REVISION.fullmatch(source_revision) is None:
            raise ValueError("approval source_revision must be a 40-character commit")
        if (
            not isinstance(evaluator_revision, str)
            or _REVISION.fullmatch(evaluator_revision) is None
        ):
            raise ValueError("approval evaluator_revision must be a 40-character commit")
        for label, evidence_hash in (
            ("license_approval_sha256", license_hash),
            ("parity_evidence_sha256", parity_hash),
        ):
            if not isinstance(evidence_hash, str) or _SHA256.fullmatch(evidence_hash) is None:
                raise ValueError(f"approval {label} must be a canonical sha256")
    except (KeyError, TypeError, ValueError) as error:
        raise IFEvalOfficialGateError() from error
    return {
        "source_id": source_id,
        "source_revision": source_revision,
        "license_approval_sha256": license_hash,
        "evaluator_revision": evaluator_revision,
        "parity_evidence_sha256": parity_hash,
    }


TrustedApprovalVerifier = Callable[[Mapping[str, str]], bool]


def require_official_ifeval_ready(
    approval_evidence: Mapping[str, Any] | None = None,
    *,
    verifier: TrustedApprovalVerifier | None = None,
) -> Any:
    """Return a payload-bound descriptor only after injected trusted verification."""
    if approval_evidence is None or verifier is None or not callable(verifier):
        raise IFEvalOfficialGateError()
    evidence = _approval_evidence(approval_evidence)
    try:
        approved = verifier(MappingProxyType(evidence))
    except Exception as error:
        raise IFEvalOfficialGateError() from error
    if approved is not True:
        raise IFEvalOfficialGateError()

    payload: dict[str, Any] = {
        **evidence,
        "full_case_count": IFEVAL_FULL_CASE_COUNT,
        "status": "ready",
    }
    approved_payload_sha256 = _canonical_sha256(payload)
    gate_attestation = object()

    @dataclass(frozen=True, slots=True)
    class ReadyDescriptor:
        source_id: str
        source_revision: str
        license_approval_sha256: str
        evaluator_revision: str
        parity_evidence_sha256: str
        full_case_count: int
        status: str
        descriptor_sha256: str
        _gate_attestation: InitVar[object]

        def __post_init__(self, _gate_attestation: object) -> None:
            descriptor_payload = {
                "source_id": self.source_id,
                "source_revision": self.source_revision,
                "license_approval_sha256": self.license_approval_sha256,
                "evaluator_revision": self.evaluator_revision,
                "parity_evidence_sha256": self.parity_evidence_sha256,
                "full_case_count": self.full_case_count,
                "status": self.status,
            }
            if _gate_attestation is not gate_attestation:
                raise IFEvalOfficialGateError()
            if _canonical_sha256(descriptor_payload) != approved_payload_sha256:
                raise IFEvalOfficialGateError()
            _approval_evidence({key: descriptor_payload[key] for key in _APPROVAL_KEYS})
            if self.full_case_count != IFEVAL_FULL_CASE_COUNT or self.status != "ready":
                raise ValueError("invalid IFEval ready descriptor")
            if self.descriptor_sha256 != approved_payload_sha256:
                raise ValueError("IFEval ready descriptor hash mismatch")

        def as_dict(self) -> dict[str, Any]:
            return {
                "source_id": self.source_id,
                "source_revision": self.source_revision,
                "license_approval_sha256": self.license_approval_sha256,
                "evaluator_revision": self.evaluator_revision,
                "parity_evidence_sha256": self.parity_evidence_sha256,
                "full_case_count": self.full_case_count,
                "status": self.status,
                "descriptor_sha256": self.descriptor_sha256,
            }

    return ReadyDescriptor(
        **payload,
        descriptor_sha256=approved_payload_sha256,
        _gate_attestation=gate_attestation,
    )


aggregate_report = aggregate_ifeval


__all__ = [
    "IFEVAL_EVALUATOR_NOT_APPROVED",
    "IFEVAL_FULL_CASE_COUNT",
    "IFEVAL_SOURCE_STATUS",
    "IFEvalCase",
    "IFEvalOfficialGateError",
    "RULE_REGISTRY",
    "RuleSpec",
    "TrustedApprovalVerifier",
    "aggregate_ifeval",
    "aggregate_report",
    "available_rules",
    "build_rule_spec",
    "evaluate_cases",
    "evaluate_rule",
    "require_official_ifeval_ready",
    "resolve_rule",
    "rule_config_sha256",
]
