"""Provider response model identity and explicit, versioned alias policy."""
from __future__ import annotations

from typing import Any

IDENTITY_POLICIES = frozenset({"report_only", "require_reported", "require_match"})


def validate_identity_config(
    policy: Any,
    aliases: Any,
    alias_version: Any,
) -> tuple[str, dict[str, str], str | None]:
    if not isinstance(policy, str) or policy not in IDENTITY_POLICIES:
        raise ValueError("identity_policy must be report_only, require_reported or require_match")
    if aliases is None:
        aliases = {}
    if not isinstance(aliases, dict) or any(
        not isinstance(alias, str) or not alias.strip()
        or not isinstance(canonical, str) or not canonical.strip()
        for alias, canonical in aliases.items()
    ):
        raise ValueError("identity_aliases must map non-empty reported model names to canonical model names")
    if alias_version is not None and (not isinstance(alias_version, str) or not alias_version.strip()):
        raise ValueError("identity_alias_version must be a non-empty string")
    if aliases and alias_version is None:
        raise ValueError("identity_alias_version is required when identity_aliases are configured")
    return policy, dict(aliases), alias_version


def assess_identity(
    requested: str,
    reported: str | None,
    *,
    policy: str,
    aliases: dict[str, str],
    alias_version: str | None,
) -> tuple[str | None, str, dict[str, Any], bool]:
    """Only a provider-reported name can establish identity; no inferred prefixes."""
    reported = reported if isinstance(reported, str) and reported.strip() else None
    resolved = None
    if reported is None:
        result = "unreported"
    elif reported == requested:
        result = "exact_match"
        resolved = reported
    elif aliases.get(reported) == requested:
        result = "alias_match"
        resolved = requested
    else:
        result = "mismatch"
    evidence = {
        "source": "provider_response" if reported is not None else None,
        "reported_value": reported,
        "path": "$.model" if reported is not None else None,
        "alias_map_version": alias_version,
        "matched_alias": reported if result == "alias_match" else None,
        "policy": policy,
        "details": {},
    }
    allowed = (policy == "report_only" or (policy == "require_reported" and reported is not None)
               or (policy == "require_match" and result in {"exact_match", "alias_match"}))
    return resolved, result, evidence, allowed
