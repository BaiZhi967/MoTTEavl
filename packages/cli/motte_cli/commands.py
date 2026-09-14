from motte_sdk.service import RunService, build_run_service

COMMANDS = ("run", "doctor", "provider", "model")


def get_service() -> RunService:
    return build_run_service()


def create_run(scenario_version: str, manifest: dict, case_ids=()):
    return get_service().create_run(scenario_version, manifest, case_ids)
