from motte_sdk.service import RunService
from motte_storage.repositories import InMemoryRepository

COMMANDS = ("run", "doctor", "provider", "model")
service = RunService(InMemoryRepository())


def create_run(scenario_version: str, manifest: dict):
    return service.create_run(scenario_version, manifest)
