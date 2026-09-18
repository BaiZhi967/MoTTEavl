"""数据集资源通用工具（套件无关）。"""
from __future__ import annotations

from typing import Any


def next_dataset_version(record: dict[str, Any], resources: Any) -> str:
    """自动版本号：同内容已存在则复用其版本（重复导入保持幂等），否则取下一个数字空号。"""
    known = [dataset for dataset in resources.datasets.list() if dataset.get("name") == record["name"]]
    for dataset in known:
        if dataset.get("cases_sha256") == record["cases_sha256"]:
            return str(dataset["version"])
    numeric = [int(dataset["version"]) for dataset in known
               if isinstance(dataset.get("version"), str) and dataset["version"].isdigit()]
    return str(max(numeric, default=0) + 1)
