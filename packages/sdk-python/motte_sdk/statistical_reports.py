"""Publish one fixed-Pass calculation; retrieve its immutable stored evidence."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from motte_contracts.statistical_reports import (
    StatisticalReportPublishRequest,
    statistical_report_id,
)
from motte_storage.statistical_reports import statistical_publication_guard

from .comparisons import ComparisonService


class StatisticalReportService:
    def __init__(self, store: Any) -> None:
        self.store = store

    def publish(
        self, baseline_run_id: str, candidate_run_id: str, *,
        allowed_factors: list[str] | tuple[str, ...],
        baseline_pass_id: str | None = None,
        candidate_pass_id: str | None = None,
        k: int = 1,
    ) -> dict[str, Any]:
        request = StatisticalReportPublishRequest(
            baseline_run_id=baseline_run_id, candidate_run_id=candidate_run_id,
            allowed_factors=allowed_factors, baseline_pass_id=baseline_pass_id,
            candidate_pass_id=candidate_pass_id, k=k,
        )
        # Capture and insertion share one maintenance exclusion window: no GC
        # or rollback may delete evidence after capture but before it is pinned.
        with statistical_publication_guard(self.store):
            from motte_eval.statistics import STATISTICAL_POLICY_V1

            policy = deepcopy(STATISTICAL_POLICY_V1)
            result = ComparisonService(self.store).paired_statistics(**request.model_dump())
            body = {"schema_version": 1, "policy": policy, "result": deepcopy(result)}
            # The repository validates the complete body and its policy/input
            # bindings before insertion, then returns the first server timestamp.
            return self.store.statistical_reports.put(statistical_report_id(body), body)

    def get(self, report_id: str) -> dict[str, Any]:
        report = self.store.statistical_reports.get(report_id)
        if report is None:
            raise KeyError(report_id)
        return report
