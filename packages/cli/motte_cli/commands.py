from motte_sdk.service import RunService, build_run_service

COMMANDS = ("doctor", "run", "replay", "live-smoke", "benchmark", "backup", "restore", "cleanup-artifacts")


def get_service() -> RunService:
    return build_run_service()


def create_run(scenario_version: str, manifest: dict, case_ids=()):
    return get_service().create_run(scenario_version, manifest, case_ids)


def import_benchmark_dataset(raw: bytes, *, name: str, version: str, revision: str,
                             license_id: str, synthetic: bool = False):
    """Validate official-format GSM8K JSONL and store it as an immutable version."""
    from motte_contracts.gsm8k import import_official_jsonl

    record = import_official_jsonl(raw, name=name, version=version, revision=revision,
                                   license_id=license_id, synthetic=synthetic)
    from motte_storage.factory import create_resource_store

    create_resource_store().datasets.put(record)
    return record
