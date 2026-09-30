"""Local and HTTP calibration lifecycle; no private registry or model execution."""
import json
from pathlib import Path

from . import remote


def add_parsers(sub):
    root = sub.add_parser("judge-calibration", help="Persistent calibration lifecycle (declared operator provenance)")
    commands = root.add_subparsers(dest="calibration_command", required=True)
    for command in ("import", "review", "preflight", "submit", "get", "report", "qualification"):
        parser = commands.add_parser(command)
        parser.add_argument("--db", help="Local SQLite database")
        remote.add_mode_arguments(parser)
        if command in {"import", "review", "preflight", "submit"}:
            parser.add_argument("--file", required=True, help="Typed JSON input; submission includes request.request_key")
        if command != "import":
            parser.add_argument("--id", required=command != "get", help="Calibration ID")
        if command in {"review", "preflight", "submit"}:
            parser.add_argument("--version", required=True)
        if command == "get":
            selector = parser.add_mutually_exclusive_group()
            selector.add_argument("--version")
            selector.add_argument("--job")
        if command == "report":
            selector = parser.add_mutually_exclusive_group(required=True)
            selector.add_argument("--job")
            selector.add_argument("--report")
            parser.add_argument("--publish", action="store_true", help="Explicitly publish from a stored Job; otherwise read only")
        if command == "qualification":
            parser.add_argument("--qualification", required=True)


def _operation(args, body):
    command = args.calibration_command
    if command == "import":
        return "import_version", [body]
    if command in {"review", "preflight", "submit"}:
        return command, [args.id, args.version, body]
    if command == "get":
        if (args.job or args.version) and not args.id:
            raise ValueError("selectors require --id")
        if args.job:
            return "job", [args.id, args.job]
        if args.version:
            return "version", [args.id, args.version]
        return ("versions", [args.id]) if args.id else ("catalog", [])
    if command == "report":
        if args.publish and not args.job:
            raise ValueError("publication requires --job")
        if args.publish:
            return "publish", [args.id, args.job]
        return ("reports", [args.id, args.job]) if args.job else ("report", [args.id, args.report])
    return "qualification", [args.id, args.qualification]


def run(args):
    # Check mode before opening local files/stores; server never opens a local database.
    if remote.is_server(args):
        error = remote.server_precondition_error(args)
        if error is not None:
            return error
    try:
        body = json.loads(Path(args.file).read_text(encoding="utf-8")) if hasattr(args, "file") else None
        if hasattr(args, "file"):
            if not isinstance(body, dict):
                raise ValueError("calibration input must be an object")
            # Reject non-JSON numeric values before httpx serialization in server mode,
            # with the same safe error as local model validation and the HTTP boundary.
            json.dumps(body, allow_nan=False)
        operation, values = _operation(args, body)
    except (ValueError, OSError):
        return remote.cli_error("CALIBRATION_CONTRACT_INVALID", "invalid calibration file or selectors")
    if remote.is_server(args):
        methods = {
            "import_version": "import_judge_calibration", "catalog": "list_judge_calibrations",
            "versions": "list_judge_calibration_versions", "version": "get_judge_calibration_version",
            "review": "review_judge_calibration", "preflight": "preflight_judge_calibration",
            "submit": "submit_judge_calibration", "job": "get_judge_calibration_job",
            "publish": "publish_judge_calibration_report", "reports": "list_judge_calibration_reports",
            "report": "get_judge_calibration_report", "qualification": "get_judge_qualification",
        }
        outcome = remote.call_remote(args, lambda client: getattr(client, methods[operation])(*values).raw)
        if not isinstance(outcome, remote.RemoteOk):
            return outcome
        result = outcome.payload
    else:
        from motte_sdk.calibration_transport import (
            CalibrationExecuteRequest, CalibrationImportRequest, CalibrationLifecycle, CalibrationReviewRequest, calibration_error,
        )
        from motte_sdk.judge_calibrations import JudgeCalibrationService
        from motte_sdk.scoring_jobs import FrozenProviderFactory, ScoringJobService
        from .main import _resources, _service
        try:
            if operation in {"import_version", "review", "preflight", "submit"}:
                model = {"import_version": CalibrationImportRequest, "review": CalibrationReviewRequest,
                         "preflight": CalibrationExecuteRequest, "submit": CalibrationExecuteRequest}[operation]
                values[-1] = model.model_validate(values[-1])
            store = _service(args).store
            lifecycle = CalibrationLifecycle(JudgeCalibrationService(
                store, _resources(args), scoring_jobs=ScoringJobService(store, provider_factory=FrozenProviderFactory()),
            ))
            result = getattr(lifecycle, operation)(*values).model_dump(mode="json")
        except Exception as error:
            status, envelope = calibration_error(error)
            return remote.cli_error(envelope.error.code, envelope.error.message,
                                    exit_code=3 if status >= 500 else 2)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0
