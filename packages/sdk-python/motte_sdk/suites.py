"""Compatibility facade over the benchmark plugin registry.

Callers keep the historical helpers while suite-specific dispatch lives in
``benchmark_plugins``. Adding a benchmark does not add branches to RunService.
"""
from __future__ import annotations

from typing import Any

from motte_contracts import suites as contract_suites

from .benchmark_plugins import (
    aggregate_with_plugin,
    plugin_for_scenario,
    prepare_with_plugin,
    score_with_plugin,
)

PROVENANCE_KEY = contract_suites.PROVENANCE_KEY
SNAPSHOT_KEY = contract_suites.SNAPSHOT_KEY
RESERVED_KEYS = contract_suites.RESERVED_KEYS
CASE_SELECTION_KEY = contract_suites.CASE_SELECTION_KEY


def resolve_managed_manifest(
    scenario: dict[str, Any], manifest: dict[str, Any], resources: Any
) -> dict[str, Any]:
    """Resolve a managed scenario through its registered benchmark plugin."""
    identity = plugin_for_scenario(scenario)
    if identity is None:
        raise ValueError(f"scenario is not a managed eval suite: {scenario.get('name')!r}")
    suite, version = identity
    return prepare_with_plugin(
        suite, scenario, manifest, resources, contract_version=version
    )


def managed_scores(run: dict[str, Any], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return score_with_plugin(run, results)


def managed_aggregate(run: dict[str, Any], scores: list[dict[str, Any]]) -> dict[str, Any]:
    return aggregate_with_plugin(run, scores)
