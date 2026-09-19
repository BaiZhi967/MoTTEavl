"""数据集资源通用工具（套件无关）。"""
from __future__ import annotations

from typing import Any

from motte_contracts.identity import dataset_fingerprint


def next_dataset_version(record: dict[str, Any], resources: Any) -> str:
    """Reuse identical content, otherwise allocate a free numeric dataset version.

    Records carrying ``dataset_fingerprint`` use their complete semantic identity. Legacy callers
    such as GSM8K keep the historical cases-hash behavior until their own contract is versioned.
    Same-name scenario versions are reserved so an orphan scenario cannot trap automatic imports.
    """
    known = [dataset for dataset in resources.datasets.list() if dataset.get("name") == record["name"]]
    fingerprint = record.get("dataset_fingerprint")
    for dataset in known:
        if fingerprint is not None:
            existing = dataset.get("dataset_fingerprint") or dataset_fingerprint(dataset)
            if existing == fingerprint:
                return str(dataset["version"])
        elif dataset.get("cases_sha256") == record.get("cases_sha256"):
            return str(dataset["version"])
    reserved = [*known, *(
        scenario for scenario in resources.scenarios.list()
        if scenario.get("name") == record["name"]
    )]
    numeric = [int(item["version"]) for item in reserved
               if isinstance(item.get("version"), str) and item["version"].isdigit()]
    return str(max(numeric, default=0) + 1)
