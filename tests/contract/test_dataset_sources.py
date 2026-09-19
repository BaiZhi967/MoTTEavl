from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from motte_contracts.dataset_sources import (
    MAX_ARTIFACT_BYTES,
    SourceActionBlockedError,
    SourceOverrideEvidence,
    SourceSpec,
    evaluate_source_action,
    load_source_registry,
    load_source_spec,
    require_registry_action,
    require_source_action,
    validate_source_spec,
)

SOURCE_DIR = Path(__file__).resolve().parents[2] / "datasets" / "direct-llm" / "sources"
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
EXPECTED_STATUSES = {
    "ceval": "restricted",
    "cmmlu": "restricted",
    "ifeval": "pending",
    "longbench-v2": "pending",
    "mmlu-pro": "pending",
    "motte-core-zh": "pending",
    "truthfulqa": "pending",
}
OVERRIDE = SourceOverrideEvidence(
    actor="operator",
    purpose="isolated noncommercial evaluation",
    ticket="GOV-42",
    source_id="ceval",
    actions=["fetch", "import", "publish"],
    approved_by="license-reviewer",
    issued_at="2026-09-20T11:00:00Z",
    expires_at="2026-09-20T13:00:00Z",
)


def _payload(spec: SourceSpec) -> dict[str, object]:
    return spec.model_dump(mode="json")


def _complete_external(spec: SourceSpec, *, restricted: bool = False) -> SourceSpec:
    data = _payload(spec)
    governance = data["governance"]
    license_spec = data["license"]
    upstream = data["upstream"]
    conversion = data["conversion"]
    assert isinstance(governance, dict)
    assert isinstance(license_spec, dict)
    assert isinstance(upstream, dict)
    assert isinstance(conversion, dict)
    governance.update(
        {
            "status": "restricted" if restricted else "approved",
            "distribution_scope": "restricted" if restricted else "public",
            "stable_eligible": not restricted,
            "reviewer": "license-reviewer",
            "reviewed_at": "2026-09-19",
        }
    )
    data_license = license_spec["data"]
    code_license = license_spec["code"]
    assert isinstance(data_license, dict)
    assert isinstance(code_license, dict)
    data_license.update({
        "declared_ids": ["MIT"],
        "verified_spdx": "MIT",
        "evidence_urls": ["https://example.invalid/data-license"],
    })
    code_license.update({
        "declared_ids": ["Apache-2.0"],
        "verified_spdx": "Apache-2.0",
        "evidence_urls": ["https://example.invalid/code-license"],
    })
    license_spec.update(
        {
            "commercial_use": "allowed-reviewed",
            "redistribution": "allowed-reviewed",
            "attribution": "required-reviewed",
            "share_alike": "not-required-reviewed",
        }
    )
    revision = upstream["revision"]
    artifacts = upstream["artifacts"]
    assert isinstance(revision, dict) and isinstance(artifacts, list)
    revision["value"] = "a" * 40
    for artifact in artifacts:
        assert isinstance(artifact, dict)
        artifact.update(
            {
                "url": "https://example.invalid/source.jsonl",
                "format": "jsonl",
                "sha256": "b" * 64,
                "bytes": 1024,
                "max_bytes": 2048,
            }
        )
    converter = conversion["converter"]
    scorer = conversion["scorer"]
    assert isinstance(converter, dict) and isinstance(scorer, dict)
    converter["version"] = "1"
    scorer["version"] = "1"
    conversion.update(
        {
            "splits": ["test"],
            "default_split": "test",
            "prompt_version": "prompt-v1",
            "profiles": ["full"],
        }
    )
    data["blockers"] = []
    return validate_source_spec(data)


def _complete_internal(spec: SourceSpec) -> SourceSpec:
    data = _payload(spec)
    governance = data["governance"]
    upstream = data["upstream"]
    conversion = data["conversion"]
    assert isinstance(governance, dict)
    assert isinstance(upstream, dict)
    assert isinstance(conversion, dict)
    governance.update(
        {
            "status": "approved-internal",
            "distribution_scope": "internal-only",
            "stable_eligible": True,
            "reviewer": "internal-reviewer",
            "reviewed_at": "2026-09-19",
        }
    )
    artifacts = upstream["artifacts"]
    assert isinstance(artifacts, list)
    for artifact in artifacts:
        assert isinstance(artifact, dict)
        artifact["format"] = "jsonl"
    converter = conversion["converter"]
    scorer = conversion["scorer"]
    assert isinstance(converter, dict) and isinstance(scorer, dict)
    converter["version"] = "1"
    scorer["version"] = "1"
    conversion.update(
        {
            "splits": ["generated"],
            "default_split": "generated",
            "prompt_version": "internal-prompt-v1",
            "profiles": ["full"],
        }
    )
    data["blockers"] = []
    return validate_source_spec(data)


def test_loads_all_seven_auditable_drafts_with_expected_statuses():
    registry = load_source_registry(SOURCE_DIR)

    assert set(registry) == set(EXPECTED_STATUSES)
    assert {source_id: spec.governance.status for source_id, spec in registry.items()} == (
        EXPECTED_STATUSES
    )
    assert all(spec.safety.trust_remote_code is False for spec in registry.values())
    assert all(spec.blockers for spec in registry.values())

    mmlu = load_source_spec(SOURCE_DIR / "mmlu-pro.json")
    assert validate_source_spec(mmlu.model_dump_json()) == mmlu


def test_strict_schema_rejects_unknown_keys_and_type_coercion():
    base = _payload(load_source_spec(SOURCE_DIR / "mmlu-pro.json"))

    top_level = deepcopy(base)
    top_level["unknown"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        validate_source_spec(top_level)

    nested = deepcopy(base)
    governance = nested["governance"]
    assert isinstance(governance, dict)
    governance["unknown"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        validate_source_spec(nested)

    coerced = deepcopy(base)
    coerced["schema_version"] = "1"
    with pytest.raises(ValidationError):
        validate_source_spec(coerced)

    coerced_bool = deepcopy(base)
    coerced_governance = coerced_bool["governance"]
    assert isinstance(coerced_governance, dict)
    coerced_governance["stable_eligible"] = 0
    with pytest.raises(ValidationError):
        validate_source_spec(coerced_bool)


def test_manifest_json_rejects_duplicate_keys_and_nonfinite_numbers():
    spec = load_source_spec(SOURCE_DIR / "mmlu-pro.json")
    raw = spec.model_dump_json()
    duplicate = raw[:-1] + ',"id":"shadow"}'
    with pytest.raises(ValueError, match="duplicate JSON key"):
        validate_source_spec(duplicate)
    nonfinite = raw[:-1] + ',"unknown":NaN}'
    with pytest.raises(ValueError, match="non-finite"):
        validate_source_spec(nonfinite)


def test_schema_rejects_unknown_status_unsafe_remote_code_and_unsafe_artifact_shape():
    base = _payload(load_source_spec(SOURCE_DIR / "mmlu-pro.json"))

    unknown_status = deepcopy(base)
    governance = unknown_status["governance"]
    assert isinstance(governance, dict)
    governance["status"] = "unknown"
    with pytest.raises(ValidationError):
        validate_source_spec(unknown_status)

    remote_code = deepcopy(base)
    safety = remote_code["safety"]
    assert isinstance(safety, dict)
    safety["trust_remote_code"] = True
    with pytest.raises(ValidationError):
        validate_source_spec(remote_code)

    invalid_review_date = deepcopy(base)
    review = invalid_review_date["governance"]
    assert isinstance(review, dict)
    review["reviewer"] = "reviewer"
    review["reviewed_at"] = "2026-99-99"
    with pytest.raises(ValidationError, match="real ISO calendar date"):
        validate_source_spec(invalid_review_date)

    unsafe_format = deepcopy(base)
    upstream = unsafe_format["upstream"]
    assert isinstance(upstream, dict) and isinstance(upstream["artifacts"], list)
    upstream["artifacts"][0]["format"] = "pickle"
    with pytest.raises(ValidationError):
        validate_source_spec(unsafe_format)

    too_large = deepcopy(base)
    too_large_upstream = too_large["upstream"]
    assert isinstance(too_large_upstream, dict)
    too_large_upstream["artifacts"][0]["max_bytes"] = MAX_ARTIFACT_BYTES + 1
    with pytest.raises(ValidationError):
        validate_source_spec(too_large)


def test_schema_rejects_casefold_duplicate_artifact_names():
    base = _payload(load_source_spec(SOURCE_DIR / "mmlu-pro.json"))
    upstream = base["upstream"]
    assert isinstance(upstream, dict) and isinstance(upstream["artifacts"], list)
    duplicate = deepcopy(upstream["artifacts"][0])
    duplicate["logical_name"] = str(duplicate["logical_name"]).upper()
    duplicate["required"] = False
    upstream["artifacts"].append(duplicate)
    with pytest.raises(ValidationError, match="case-insensitively unique"):
        validate_source_spec(base)


def test_registry_rejects_filename_identity_drift_and_empty_directory(tmp_path: Path):
    with pytest.raises(ValueError, match="empty"):
        load_source_registry(tmp_path)

    spec = load_source_spec(SOURCE_DIR / "mmlu-pro.json")
    (tmp_path / "wrong-name.json").write_text(spec.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="filename must match id"):
        load_source_registry(tmp_path)


def test_every_current_draft_validates_but_action_gate_rejects():
    registry = load_source_registry(SOURCE_DIR)

    for source_id, spec in registry.items():
        for action in ("fetch", "import", "publish"):
            decision = evaluate_source_action(spec, action)
            assert decision.allowed is False, (source_id, action)
            assert decision.code in {"SOURCE_LICENSE_BLOCKED", "SOURCE_APPROVAL_REQUIRED"}
        assert evaluate_source_action(
            spec, "generate", local=True, internal_scope=True
        ).allowed is False


def test_pending_and_unknown_sources_fail_closed_even_with_override():
    registry = load_source_registry(SOURCE_DIR)
    pending = registry["mmlu-pro"]

    decision = evaluate_source_action(
        pending, "fetch", override=OVERRIDE, override_verified=True, now=NOW
    )
    assert decision.allowed is False
    assert decision.code == "SOURCE_LICENSE_BLOCKED"

    missing = evaluate_source_action(None, "fetch", source_id="not-registered")
    assert missing.allowed is False
    assert missing.code == "SOURCE_NOT_FOUND"
    with pytest.raises(SourceActionBlockedError, match="SOURCE_NOT_FOUND"):
        require_registry_action(registry, "not-registered", "fetch")


def test_restricted_requires_trusted_bound_unexpired_approval():
    current = _complete_external(load_source_spec(SOURCE_DIR / "ceval.json"), restricted=True)

    assert evaluate_source_action(current, "fetch").code == "SOURCE_APPROVAL_REQUIRED"
    assert evaluate_source_action(
        current, "fetch", override=OVERRIDE, now=NOW
    ).code == "SOURCE_APPROVAL_REQUIRED"
    assert evaluate_source_action(
        current,
        "fetch",
        override=OVERRIDE,
        override_verified=True,
        now=NOW,
    ).allowed is True

    wrong_source = OVERRIDE.model_copy(update={"source_id": "cmmlu"})
    wrong_action = OVERRIDE.model_copy(update={"actions": ["publish"]})
    expired = OVERRIDE.model_copy(
        update={
            "issued_at": (NOW - timedelta(hours=2)).isoformat(),
            "expires_at": (NOW - timedelta(hours=1)).isoformat(),
        }
    )
    invalid_copy = OVERRIDE.model_copy(update={"approved_by": ""})
    for evidence in (wrong_source, wrong_action, expired, invalid_copy):
        decision = evaluate_source_action(
            current,
            "fetch",
            override=evidence,
            override_verified=True,
            now=NOW,
        )
        assert decision.allowed is False
        assert decision.code == "SOURCE_APPROVAL_REQUIRED"

    for action in ("fetch", "import", "publish"):
        assert require_source_action(
            current,
            action,
            override=OVERRIDE,
            override_verified=True,
            now=NOW,
        ) is current


@pytest.mark.parametrize(
    ("field", "expected_reason"),
    [
        ("review", "reviewer"),
        ("data_license", "data SPDX"),
        ("code_license", "code SPDX"),
        ("data_evidence", "data license evidence URL"),
        ("code_evidence", "code license evidence URL"),
        ("license_conclusion", "commercial_use"),
        ("revision", "revision"),
        ("url", "URL"),
        ("format", "format"),
        ("sha256", "SHA-256"),
        ("bytes", "byte count"),
        ("max_bytes", "max_bytes"),
        ("converter", "converter version"),
        ("scorer", "scorer version"),
        ("prompt", "prompt version"),
        ("splits", "splits"),
        ("profiles", "profiles"),
        ("blockers", "blockers"),
    ],
)
def test_approved_source_rejects_missing_governance_license_or_release_evidence(
    field: str, expected_reason: str
):
    complete = _complete_external(load_source_spec(SOURCE_DIR / "mmlu-pro.json"))
    data = _payload(complete)
    governance = data["governance"]
    license_spec = data["license"]
    upstream = data["upstream"]
    conversion = data["conversion"]
    assert isinstance(governance, dict)
    assert isinstance(license_spec, dict)
    assert isinstance(upstream, dict)
    assert isinstance(conversion, dict)
    artifacts = upstream["artifacts"]
    assert isinstance(artifacts, list) and isinstance(artifacts[0], dict)

    if field == "review":
        governance["reviewer"] = None
        governance["reviewed_at"] = None
    elif field in {"data_license", "code_license"}:
        kind = field.removesuffix("_license")
        assert isinstance(license_spec[kind], dict)
        license_spec[kind]["verified_spdx"] = None
    elif field in {"data_evidence", "code_evidence"}:
        kind = field.removesuffix("_evidence")
        assert isinstance(license_spec[kind], dict)
        license_spec[kind]["evidence_urls"] = []
    elif field == "license_conclusion":
        license_spec["commercial_use"] = "not reviewed"
    elif field == "revision":
        assert isinstance(upstream["revision"], dict)
        upstream["revision"]["value"] = None
    elif field in {"url", "format", "sha256", "bytes", "max_bytes"}:
        artifacts[0][field] = None
    elif field in {"converter", "scorer"}:
        assert isinstance(conversion[field], dict)
        conversion[field]["version"] = None
    elif field == "prompt":
        conversion["prompt_version"] = None
    elif field == "splits":
        conversion["splits"] = []
        conversion["default_split"] = None
    elif field == "profiles":
        conversion["profiles"] = []
    else:
        data["blockers"] = ["review not complete"]

    incomplete = validate_source_spec(data)
    decision = evaluate_source_action(incomplete, "publish")
    assert decision.allowed is False
    assert decision.code == "SOURCE_NOT_READY"
    assert any(expected_reason in reason for reason in decision.reasons)


def test_missing_code_verification_reproduces_and_closes_publish_bypass():
    complete = _complete_external(load_source_spec(SOURCE_DIR / "mmlu-pro.json"))
    data = _payload(complete)
    license_spec = data["license"]
    assert isinstance(license_spec, dict)
    code_license = license_spec["code"]
    assert isinstance(code_license, dict)
    code_license["verified_spdx"] = None

    decision = evaluate_source_action(validate_source_spec(data), "publish")
    assert decision.allowed is False
    assert decision.code == "SOURCE_NOT_READY"
    assert "verified code SPDX license is missing" in "; ".join(decision.reasons)
    assert not any("verified data SPDX" in reason for reason in decision.reasons)


@pytest.mark.parametrize(
    ("missing_kind", "intact_kind"),
    [("data", "code"), ("code", "data")],
)
def test_data_and_code_license_evidence_are_independently_required(
    missing_kind: str, intact_kind: str
):
    complete = _complete_external(load_source_spec(SOURCE_DIR / "mmlu-pro.json"))
    data = _payload(complete)
    license_spec = data["license"]
    assert isinstance(license_spec, dict)
    missing = license_spec[missing_kind]
    assert isinstance(missing, dict)
    missing["verified_spdx"] = None

    decision = evaluate_source_action(validate_source_spec(data), "fetch")
    assert decision.allowed is False
    assert any(f"verified {missing_kind} SPDX" in reason for reason in decision.reasons)
    assert not any(f"verified {intact_kind} SPDX" in reason for reason in decision.reasons)


@pytest.mark.parametrize(
    ("kind", "verified_spdx"),
    [("data", "Apache-2.0"), ("code", "MIT")],
)
def test_data_and_code_verified_spdx_must_match_their_own_declarations(
    kind: str, verified_spdx: str
):
    complete = _complete_external(load_source_spec(SOURCE_DIR / "mmlu-pro.json"))
    data = _payload(complete)
    license_spec = data["license"]
    assert isinstance(license_spec, dict)
    evidence = license_spec[kind]
    assert isinstance(evidence, dict)
    evidence["verified_spdx"] = verified_spdx

    decision = evaluate_source_action(validate_source_spec(data), "import")
    assert decision.allowed is False
    assert decision.code == "SOURCE_NOT_READY"
    assert any(
        f"verified {kind} SPDX license is missing or inconsistent" in reason
        for reason in decision.reasons
    )


def test_evidence_complete_approved_source_allows_external_actions_only():
    complete = _complete_external(load_source_spec(SOURCE_DIR / "mmlu-pro.json"))

    for action in ("fetch", "import", "publish"):
        decision = evaluate_source_action(complete, action)
        assert decision.allowed is True
        assert decision.code is None
        assert decision.reasons == ()

    denied = evaluate_source_action(complete, "generate", local=True, internal_scope=True)
    assert denied.allowed is False
    assert denied.code == "SOURCE_LICENSE_BLOCKED"


def test_approved_internal_requires_review_and_complete_local_generation_evidence():
    complete = _complete_internal(load_source_spec(SOURCE_DIR / "motte-core-zh.json"))
    assert require_source_action(
        complete, "generate", local=True, internal_scope=True
    ) is complete

    data = _payload(complete)
    governance = data["governance"]
    assert isinstance(governance, dict)
    governance["reviewer"] = None
    governance["reviewed_at"] = None
    unreviewed = validate_source_spec(data)
    decision = evaluate_source_action(
        unreviewed, "generate", local=True, internal_scope=True
    )
    assert decision.allowed is False
    assert decision.code == "SOURCE_NOT_READY"

    for kwargs in (
        {"local": False, "internal_scope": True},
        {"local": True, "internal_scope": False},
    ):
        denied = evaluate_source_action(complete, "generate", **kwargs)
        assert denied.allowed is False
        assert denied.code == "SOURCE_LICENSE_BLOCKED"
    for action in ("fetch", "import", "publish"):
        assert evaluate_source_action(complete, action).allowed is False
