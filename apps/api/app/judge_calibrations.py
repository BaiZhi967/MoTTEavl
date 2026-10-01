"""Thin persistent calibration routes under the existing deployment security boundary."""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from motte_eval.calibration_records import (
    CalibrationQualificationSource, CalibrationVersion, HumanReviewRecord,
)
from apps.api.app.schemas import (
    CalibrationCatalogEntry, CalibrationErrorResponse, CalibrationExecuteRequest, CalibrationImportRequest, CalibrationItems,
    CalibrationJobView, CalibrationPreflightView, CalibrationPublishRequest,
    CalibrationReportView, CalibrationReviewRequest,
)

from motte_sdk.calibration_transport import CalibrationLifecycle, calibration_error

ROOT = "/api/v1/judge-calibrations"


def register_calibration_routes(application, lifecycle_factory):
    router = APIRouter(prefix=ROOT, tags=["judge-calibrations"],
                       responses={code: {"model": CalibrationErrorResponse} for code in (404, 409, 422, 503)})

    def invoke(operation, *args):
        try:
            lifecycle: CalibrationLifecycle = lifecycle_factory()
            return getattr(lifecycle, operation)(*args)
        except Exception as error:
            status, body = calibration_error(error)
            return JSONResponse(status_code=status, content=body.model_dump(mode="json"))

    @router.post("", status_code=201, response_model=CalibrationVersion)
    def import_version(body: CalibrationImportRequest):
        """Import unreviewed inputs only; declared names are not authentication."""
        return invoke("import_version", body)

    @router.get("", response_model=CalibrationItems[CalibrationCatalogEntry])
    def catalog():
        return invoke("catalog")

    @router.get("/{calibration_id}/versions", response_model=CalibrationItems[CalibrationVersion])
    def versions(calibration_id: str):
        return invoke("versions", calibration_id)

    @router.get("/{calibration_id}/versions/{version}", response_model=CalibrationVersion)
    def version_get(calibration_id: str, version: str):
        return invoke("version", calibration_id, version)

    @router.post("/{calibration_id}/versions/{version}/reviews", status_code=201, response_model=CalibrationVersion)
    def review(calibration_id: str, version: str, body: CalibrationReviewRequest):
        return invoke("review", calibration_id, version, body)

    @router.get("/{calibration_id}/versions/{version}/reviews", response_model=CalibrationItems[HumanReviewRecord])
    def reviews(calibration_id: str, version: str):
        return invoke("reviews", calibration_id, version)

    @router.post("/{calibration_id}/versions/{version}/preflight", response_model=CalibrationPreflightView)
    def preflight(calibration_id: str, version: str, body: CalibrationExecuteRequest):
        return invoke("preflight", calibration_id, version, body)

    @router.post("/{calibration_id}/versions/{version}/jobs", status_code=202, response_model=CalibrationJobView)
    def submit(calibration_id: str, version: str, body: CalibrationExecuteRequest):
        return invoke("submit", calibration_id, version, body)

    @router.get("/{calibration_id}/versions/{version}/jobs", response_model=CalibrationItems[CalibrationJobView])
    def jobs(calibration_id: str, version: str):
        return invoke("jobs", calibration_id, version)

    @router.get("/{calibration_id}/jobs/{execution_id}", response_model=CalibrationJobView)
    def job(calibration_id: str, execution_id: str):
        return invoke("job", calibration_id, execution_id)

    @router.post("/{calibration_id}/jobs/{execution_id}/reports", status_code=201, response_model=CalibrationReportView)
    def publish(calibration_id: str, execution_id: str, body: CalibrationPublishRequest | None = None):
        return invoke("publish", calibration_id, execution_id)

    @router.get("/{calibration_id}/jobs/{execution_id}/reports", response_model=CalibrationItems[CalibrationReportView])
    def reports(calibration_id: str, execution_id: str):
        return invoke("reports", calibration_id, execution_id)

    @router.get("/{calibration_id}/reports/{report_id}", response_model=CalibrationReportView)
    def report(calibration_id: str, report_id: str):
        return invoke("report", calibration_id, report_id)

    @router.get("/{calibration_id}/qualifications/{qualification_id}", response_model=CalibrationQualificationSource)
    def qualification(calibration_id: str, qualification_id: str):
        return invoke("qualification", calibration_id, qualification_id)

    application.include_router(router)
