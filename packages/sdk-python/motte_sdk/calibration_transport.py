"""Shared local/API calibration boundary; no execution or independent authority.

Operator names are declared audit provenance within deployment authorization.
Only the existing lifecycle can create reports/qualification sources. HTTP clients
use lightweight projections and never import this local execution-stack module.
"""
from __future__ import annotations

import re
from typing import Any, Generic, Literal, TypeVar

from pydantic import Field, ValidationError, field_validator

from motte_contracts.messages import Contract
from motte_eval.calibration import CalibrationSet, HumanReviewRequired
from motte_eval.calibration_records import (
    CalibrationAllowance, CalibrationImport, CalibrationQualificationSource, CalibrationRef,
    CalibrationReportRecord, CalibrationRunRequest, CalibrationVersion, Digest, HumanReviewInput,
    HumanReviewRecord, Text,
)
from motte_eval.judge import JudgeBudgetError, JudgeError, JudgeInputError, JudgeNotAuthorised, JudgeSpecError
from motte_eval.rubrics import CalibrationPolicy
from motte_storage.calibrations import CalibrationConflict, CalibrationCorrupt
from motte_storage.scoring_jobs import ScoringJobConflict, ScoringJobError

from .judge_calibrations import JudgeCalibrationService
from .resolve import ManifestResolutionError
from .scoring_jobs import JudgeEvidenceError, JudgeProviderSnapshotError


_SELECTOR_DESCRIPTION = (
    "Public selectors are literal, nonblank Unicode path segments. '/' and '\\', "
    "ASCII controls, percent-escape triplets (%HH), and exact '.' or '..' are unsupported. "
    "Values are never decoded, normalized, or rewritten."
)


def _public_selector(value: str) -> str:
    if (not value.strip() or value in {".", ".."}
            or re.search(r"[/\\\x00-\x1f\x7f]|%[0-9a-fA-F]{2}", value)):
        raise ValueError("calibration selector is not a supported literal URI segment")
    return value


class CalibrationImportRequest(CalibrationImport):
    """Public write boundary only; stored/legacy CalibrationSet models stay unchanged."""

    calibration: CalibrationSet = Field(description=_SELECTOR_DESCRIPTION)

    @field_validator("calibration")
    @classmethod
    def addressable_identity(cls, value: CalibrationSet) -> CalibrationSet:
        _public_selector(value.calibration_id)
        _public_selector(value.version)
        return value


class CalibrationReviewRequest(Contract):
    expected_parent_sha256: Digest
    new_version: Text = Field(description=_SELECTOR_DESCRIPTION)
    reviews: list[HumanReviewInput] = Field(min_length=1)

    _addressable_version = field_validator("new_version")(_public_selector)


class CalibrationExecuteRequest(Contract):
    content_sha256: Digest
    request: CalibrationRunRequest


class CalibrationPublishRequest(Contract):
    """Optional empty body only; report and qualification contents are server-owned."""


class CalibrationErrorDetail(Contract):
    code: str
    message: str


class CalibrationErrorResponse(Contract):
    error: CalibrationErrorDetail


def calibration_error(error: Exception) -> tuple[int, CalibrationErrorResponse]:
    """Never stringify untrusted input, validation context or provider messages."""
    if isinstance(error, KeyError):
        status, code, message = 404, "CALIBRATION_NOT_FOUND", "calibration source not found for this owner"
    elif isinstance(error, (CalibrationConflict, ScoringJobConflict)):
        status, code, message = 409, "CALIBRATION_CONFLICT", "immutable source or request fingerprint changed"
    elif isinstance(error, CalibrationCorrupt):
        status, code, message = 409, "CALIBRATION_SOURCE_INVALID", "stored calibration evidence is inconsistent"
    elif isinstance(error, JudgeNotAuthorised):
        status, code, message = 422, "JUDGE_NOT_AUTHORISED", "explicit aggregate authorisation is required"
    elif isinstance(error, JudgeBudgetError):
        status, code, message = 422, "JUDGE_BUDGET_NOT_EXECUTABLE", "aggregate frozen reservations exceed the allowance"
    elif isinstance(error, (ValidationError, HumanReviewRequired, JudgeInputError, JudgeSpecError,
                            JudgeEvidenceError, JudgeProviderSnapshotError, ManifestResolutionError,
                            ValueError)):
        status, code, message = 422, "CALIBRATION_CONTRACT_INVALID", "calibration request does not satisfy its contract"
    elif isinstance(error, (ScoringJobError, JudgeError)):
        status, code, message = 503, "CALIBRATION_UNAVAILABLE", "calibration submission is unavailable"
    else:
        raise error
    return status, CalibrationErrorResponse(error=CalibrationErrorDetail(code=code, message=message))


T = TypeVar("T")


class CalibrationItems(Contract, Generic[T]):
    items: list[T]
    total: int = Field(ge=0)


class CalibrationCatalogEntry(Contract):
    calibration_id: str
    versions: list[CalibrationRef]


class CalibrationChildView(Contract):
    job_id: str
    status: str


class CalibrationJobView(Contract):
    execution_id: str
    version: CalibrationRef
    judge_spec_sha256: str
    provider_snapshot_sha256: str
    plan_sha256: str
    allowance: CalibrationAllowance | None
    child_job_ids: list[str]
    children: list[CalibrationChildView]
    recorded_at: str
    worker_command: Literal["uv run python -m apps.worker.motte_worker --once"] = "uv run python -m apps.worker.motte_worker --once"
    operator_identity: Literal["declared_unverified"] = "declared_unverified"


class CalibrationPreflightView(Contract):
    schema_version: Literal[1]
    execution_id: str
    version: CalibrationRef
    judge_spec_sha256: str
    provider_snapshot_sha256: str
    policy: CalibrationPolicy
    plan_sha256: str
    sample_count: int
    max_calls: int
    child_call_counts: list[int]
    allowance: CalibrationAllowance
    authorisation: dict[str, Any]
    mode: str
    authorised: Literal[True]
    budget_executable: Literal[True]
    executed: Literal[False]
    qualification_status: Literal["not_run"]
    preflight_sha256: str


class CalibrationReportView(Contract):
    report: CalibrationReportRecord
    qualification: CalibrationQualificationSource | None


class CalibrationLifecycle:
    """Thin owner-aware projection shared by local CLI and HTTP routes."""

    def __init__(self, service: JudgeCalibrationService):
        self.service = service
        self.repository = service.repository

    def import_version(self, body: CalibrationImport) -> CalibrationVersion:
        # Unlike the trusted internal service, public import cannot accept reviewed records.
        body = CalibrationImportRequest.model_validate(body.model_dump(mode="json"))
        return self.service.import_version(body)

    def catalog(self) -> CalibrationItems[CalibrationCatalogEntry]:
        groups: dict[str, list[CalibrationRef]] = {}
        for row in self.repository.iter_records():
            if "calibration" in row:
                version = CalibrationVersion.model_validate(row)
                groups.setdefault(version.reference.calibration_id, []).append(version.reference)
        items = [CalibrationCatalogEntry(calibration_id=key, versions=sorted(groups[key], key=lambda ref: ref.version))
                 for key in sorted(groups)]
        return CalibrationItems(items=items, total=len(items))

    def versions(self, calibration_id: str) -> CalibrationItems[CalibrationVersion]:
        items = self.service.list_versions(calibration_id)
        if not items:
            raise KeyError(calibration_id)
        return CalibrationItems(items=items, total=len(items))

    def version(self, calibration_id: str, version: str, digest: str | None = None) -> CalibrationVersion:
        item = next((item for item in self.versions(calibration_id).items
                     if item.calibration.version == version), None)
        if item is None:
            raise KeyError(version)
        if digest is not None and item.content_sha256 != digest:
            raise CalibrationConflict("selected version digest changed")
        return item

    def review(self, calibration_id: str, version: str, body: CalibrationReviewRequest) -> CalibrationVersion:
        _public_selector(calibration_id)
        body = CalibrationReviewRequest.model_validate(body.model_dump(mode="json"))
        parent = self.version(calibration_id, version, body.expected_parent_sha256)
        return self.service.review(parent.reference, new_version=body.new_version, reviews=body.reviews)

    def reviews(self, calibration_id: str, version: str) -> CalibrationItems[HumanReviewRecord]:
        parent = self.version(calibration_id, version)
        items = [HumanReviewRecord.model_validate(row) for row in self.repository.iter_records()
                 if "review" in row and row["parent_ref"] == parent.reference.model_dump(mode="json")]
        return CalibrationItems(items=items, total=len(items))

    def preflight(self, calibration_id: str, version: str, body: CalibrationExecuteRequest) -> CalibrationPreflightView:
        selected = self.version(calibration_id, version, body.content_sha256)
        return CalibrationPreflightView.model_validate(self.service.preflight(selected.reference, body.request))

    def submit(self, calibration_id: str, version: str, body: CalibrationExecuteRequest) -> CalibrationJobView:
        selected = self.version(calibration_id, version, body.content_sha256)
        execution = self.service.submit(selected.reference, body.request)
        return self._job_view(execution)

    def _execution(self, calibration_id: str, execution_id: str):
        execution = self.service.get_execution(execution_id)
        if execution.version.reference.calibration_id != calibration_id:
            raise KeyError(execution_id)
        return execution

    def _job_view(self, execution) -> CalibrationJobView:
        children = []
        for job_id in execution.child_job_ids:
            child = self.service.scoring_jobs.jobs.get(job_id)
            if child is None or child.get("owner", {}).get("calibration_job_id") != execution.execution_id:
                raise CalibrationCorrupt("calibration child is missing or has a different owner")
            children.append(CalibrationChildView(job_id=job_id, status=child["status"]))
        return CalibrationJobView(
            execution_id=execution.execution_id, version=execution.version.reference,
            judge_spec_sha256=execution.spec.spec_sha256,
            provider_snapshot_sha256=execution.provider_snapshot["snapshot_sha256"],
            plan_sha256=execution.plan_sha256, allowance=execution.allowance,
            child_job_ids=execution.child_job_ids, children=children, recorded_at=execution.recorded_at,
        )

    def job(self, calibration_id: str, execution_id: str) -> CalibrationJobView:
        return self._job_view(self._execution(calibration_id, execution_id))

    def jobs(self, calibration_id: str, version: str) -> CalibrationItems[CalibrationJobView]:
        selected = self.version(calibration_id, version)
        items = [self._job_view(item) for item in self.service.list_executions(calibration_id)
                 if item.version.reference == selected.reference]
        return CalibrationItems(items=items, total=len(items))

    def _report_view(self, report: CalibrationReportRecord) -> CalibrationReportView:
        from .calibration_ledger import qualification_source
        expected = qualification_source(report, recorded_at=report.recorded_at)
        source = None if expected is None else self.repository.get_qualification(expected.binding.qualification_id)
        # A GET reads only already-published sources, never republishes or executes.
        return CalibrationReportView(report=report, qualification=source)

    def publish(self, calibration_id: str, execution_id: str) -> CalibrationReportView:
        self._execution(calibration_id, execution_id)
        return self._report_view(self.service.publish_report(execution_id))

    def reports(self, calibration_id: str, execution_id: str) -> CalibrationItems[CalibrationReportView]:
        self._execution(calibration_id, execution_id)
        items = [self._report_view(item) for item in self.service.list_reports(execution_id)]
        return CalibrationItems(items=items, total=len(items))

    def report(self, calibration_id: str, report_id: str) -> CalibrationReportView:
        report = self.service.get_report(report_id)
        if report.source.calibration_id != calibration_id:
            raise KeyError(report_id)
        return self._report_view(report)

    def qualification(self, calibration_id: str, qualification_id: str) -> CalibrationQualificationSource:
        source = self.repository.get_qualification(qualification_id)
        if source is None or source.binding.calibration_id != calibration_id:
            raise KeyError(qualification_id)
        return source
