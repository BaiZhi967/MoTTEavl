"""M7 迁移测试：合成 legacy 导出包构建器（确定性生成，JSON + 文本工件）。

fixture 落在 ``tests/fixtures/m7-accept/legacy-export-v1/``（.gitignore 已排
除，仓库不提交任何 blob），内容由固定时间戳与固定工件字节决定，可重复生成。
样本内容对应验收 A08–A13：一条已完成 legacy run（2 题、聚合 summary=0.5、
**无**逐题分、reported_model=null）、一条进行中 run（必须拒绝）、一个
artifact、一条 baseline、dataset/scenario 缺 license（restricted）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

import pytest

from motte_sdk.migration.sources import load_source_package, package_content_sha256

FIXTURE_ROOT = (
    Path(__file__).resolve().parent.parent / "fixtures" / "m7-accept" / "legacy-export-v1"
)

#: 固定工件内容（文本；不向仓库提交二进制 blob）。
ARTIFACT_PAYLOAD = b"legacy artifact payload v1\n"
EXPORTED_AT = "2026-08-15T10:00:00+00:00"
CREATED_AT = "2026-08-15T09:00:00+00:00"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def build_legacy_export(
    root: str | Path,
    *,
    import_id: str = "imp-accept-0001",
    source_system: str = "legacy-eval",
    run_id: str = "run-9001",
    summary_id: str = "sum-9001",
    baseline_id: str = "bl-9001",
    with_in_flight: bool = True,
    in_flight_run_id: str = "run-9002",
    with_scores: bool = False,
    artifacts: Sequence[tuple[str, bytes]] | None = None,
    missing_artifact_shas: Sequence[str] = (),
) -> Path:
    """在 ``root`` 下确定性地生成一个 legacy 导出包，返回包根路径。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    artifact_items = list(artifacts) if artifacts is not None else [("art-out", ARTIFACT_PAYLOAD)]

    artifact_records: list[dict[str, Any]] = []
    for record_id, content in artifact_items:
        sha = sha256_hex(content)
        artifact_dir = root / "artifacts" / sha[:2]
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / sha).write_bytes(content)
        artifact_records.append({
            "id": record_id, "sha256": sha, "kind": "file", "media_type": "text/plain",
            "filename": f"{record_id}.txt", "bytes": len(content),
            "run_id": run_id, "created_at": CREATED_AT,
        })
    run_artifacts: list[dict[str, Any]] = [
        {"sha256": record["sha256"], "record_id": record["id"]} for record in artifact_records
    ]
    run_artifacts.extend({"sha256": sha, "record_id": None} for sha in missing_artifact_shas)

    records: dict[str, list[dict[str, Any]]] = {
        "provider_connection": [{
            "id": "conn-openai", "name": "legacy-openai-conn", "provider": "openai",
            "api_key_env": "LEGACY_OPENAI_API_KEY", "created_at": CREATED_AT,
        }],
        "model_profile": [{
            "id": "profile-leg", "name": "legacy-model-x", "provider": "openai",
            "license": "unknown", "created_at": CREATED_AT,
        }],
        "dataset": [{
            "id": "ds-leg", "name": "legacy-dataset-a", "format": "jsonl", "case_count": 2,
            "created_at": CREATED_AT,
        }],
        "case": [
            {"id": "case-101", "dataset_id": "ds-leg",
             "input": {"prompt": "legacy prompt 101"}, "expected": None},
            {"id": "case-102", "dataset_id": "ds-leg",
             "input": {"prompt": "legacy prompt 102"}, "expected": None},
        ],
        "scenario": [{"id": "scen-leg", "name": "legacy-scenario", "dataset_id": "ds-leg"}],
        "run": [{
            "id": run_id, "status": "completed", "created_at": CREATED_AT,
            "completed_at": EXPORTED_AT, "reported_model": None,
            "model_profile_id": "profile-leg", "provider_connection_id": "conn-openai",
            "scenario_id": "scen-leg", "case_ids": ["case-101", "case-102"],
            "artifacts": run_artifacts, "legacy_note": "hand-tuned legacy run",
        }],
        "run_summary": [{
            "id": summary_id, "run_id": run_id, "score": 0.5, "metric": "accuracy",
            "aggregation": "mean", "per_case_scores_available": False,
        }],
        "score": [],
        "artifact": artifact_records,
        "baseline": [{
            "id": baseline_id, "run_id": run_id, "name": "legacy-baseline-q3",
            "metrics": {"accuracy": 0.5}, "created_at": CREATED_AT,
        }],
    }
    if with_in_flight:
        records["run"].append({
            "id": in_flight_run_id, "status": "running", "created_at": CREATED_AT,
            "reported_model": None, "case_ids": ["case-101"], "artifacts": [],
        })
    if with_scores:
        records["score"] = [
            {"id": "score-101", "run_id": run_id, "case_id": "case-101", "value": 1.0,
             "passed": True, "metric": "accuracy"},
            {"id": "score-102", "run_id": run_id, "case_id": "case-102", "value": 0.0,
             "passed": False, "metric": "accuracy"},
        ]

    for record_type, items in records.items():
        for item in items:
            write_json(root / "records" / record_type / f"{item['id']}.json", item)

    artifact_listing = sorted(
        (path.relative_to(root).as_posix(), sha256_hex(path.read_bytes()))
        for path in (root / "artifacts").rglob("*") if path.is_file()
    )
    manifest: dict[str, Any] = {
        "import_id": import_id,
        "importer_version": "motte-importer@1",
        "transform_version": "legacy-transform@1",
        "source_system": source_system,
        "source_repository": "legacy-repo",
        "source_revision": "rev-0001",
        "source_schema_version": "1",
        "exported_at": EXPORTED_AT,
        "record_counts": {t: len(items) for t, items in records.items() if items},
        "artifact_manifest_sha256": "sha256:" + hashlib.sha256(json.dumps(
            artifact_listing, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest(),
        "scope": sorted(records),
    }
    write_json(root / "manifest.json", manifest)
    # content_sha256 的定义排除该字段自身（见 sources.py docstring），
    # 先写不含该字段的 manifest 再补算，保证声明值 == 重算值。
    manifest["content_sha256"] = package_content_sha256(root)
    write_json(root / "manifest.json", manifest)
    return root


@pytest.fixture(scope="session")
def legacy_export_root() -> Path:
    return build_legacy_export(FIXTURE_ROOT)


@pytest.fixture()
def legacy_source(legacy_export_root: Path):
    from motte_sdk.migration import load_source_package as _load

    return _load(legacy_export_root)


@pytest.fixture()
def export_builder():
    return build_legacy_export


@pytest.fixture()
def load_package():
    return load_source_package
