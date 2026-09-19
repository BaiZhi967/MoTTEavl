from __future__ import annotations

import os
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from motte_contracts.dataset_sources import load_source_registry
from motte_contracts.identity import canonical_sha256
from motte_sdk.core_zh import build_dataset
from motte_sdk.dataset_sources import SourcePipelineError, validate_cache_path_safety
from motte_sdk.managed_sources import (
    ConverterRegistry,
    ManagedSourceError,
    convert_cached_source,
    validate_registered_dataset_provenance,
)

from tests.sdk.test_managed_sources import (
    ARTIFACT_SHA,
    BODY,
    REVISION,
    _dataset,
    _source,
    _write_cache,
)


def _convert_with_dataset(source, root: Path, dataset: dict) -> dict:
    _write_cache(source, root)

    def converter(**kwargs):
        del kwargs
        return deepcopy(dataset)

    return convert_cached_source(
        source.id,
        REVISION,
        {"input_count": 1},
        True,
        registry={source.id: source},
        cache_root=root,
        converter_registry=ConverterRegistry({
            (source.conversion.converter.id, source.conversion.converter.version): converter,
        }),
    )


def test_reconcile_replaces_all_source_and_artifact_claims(tmp_path: Path):
    source = _source("approved")
    dataset = _dataset()
    forged_artifacts = [
        {
            "logical_name": "attacker",
            "url": "https://attacker.invalid/artifact",
            "sha256": "0" * 64,
            "bytes": 1,
        }
    ]
    dataset["provenance"].update(
        {
            "source_id": "attacker-source",
            "source_kind": "generated-internal",
            "homepage": "https://attacker.invalid",
            "upstream_revision": "attacker-revision",
            "artifacts": forged_artifacts,
            "artifact_manifest_sha256": canonical_sha256(forged_artifacts),
            "synthetic": True,
        }
    )
    receipt = _convert_with_dataset(source, tmp_path, dataset)
    provenance = receipt["dataset"]["provenance"]
    assert provenance["source_id"] == source.id
    assert provenance["source_kind"] == "managed-public"
    assert provenance["homepage"] == source.links.homepage
    assert provenance["upstream_revision"] == REVISION
    assert provenance["synthetic"] is False
    assert provenance["artifacts"] == [
        {
            "logical_name": "fixture.jsonl",
            "url": "https://example.test/fixture.jsonl",
            "sha256": ARTIFACT_SHA,
            "bytes": len(BODY),
        }
    ]
    assert provenance["artifact_manifest_sha256"] != "0" * 64
    assert provenance["converter"]["id"] == source.conversion.converter.id
    assert provenance["converter"]["version"] == source.conversion.converter.version


def test_unknown_adapter_converter_identity_fails_closed(tmp_path: Path):
    source = _source("approved")
    dataset = _dataset()
    dataset["provenance"]["converter"]["id"] = "attacker-converter"
    with pytest.raises(ManagedSourceError) as error:
        _convert_with_dataset(source, tmp_path, dataset)
    assert error.value.code == "SOURCE_LICENSE_INVALID"


def test_mmlu_adapter_alias_is_recorded_but_published_identity_is_canonical(tmp_path: Path):
    source_payload = _source("approved").model_dump(mode="json")
    source_payload["id"] = "mmlu-fixture"
    source_payload["conversion"]["converter"] = {
        "id": "mmlu-pro-to-direct",
        "version": "1",
    }
    source = type(_source("approved")).model_validate(source_payload)
    dataset = _dataset()
    dataset["provenance"]["converter"]["id"] = "mmlu-pro-5shot-cot-to-direct"
    dataset["provenance"]["converter"]["config"] = {"fixture": True}
    receipt = _convert_with_dataset(source, tmp_path, dataset)
    converter = receipt["dataset"]["provenance"]["converter"]
    assert converter["id"] == "mmlu-pro-to-direct"
    assert converter["version"] == "1"
    assert converter["config"]["adapter_converter_id"] == "mmlu-pro-5shot-cot-to-direct"
    assert converter["config"]["adapter_converter_version"] == "1"
    assert converter["config"]["converter_mapping"] == "builtin-canonical-to-adapter"


def test_core_zh_internal_artifact_binding_matches_generator_contract():
    registry = load_source_registry(Path("datasets/direct-llm/sources"))
    validate_registered_dataset_provenance(build_dataset(), registry["motte-core-zh"])


def _symlink_or_skip(link: Path, target: Path, *, directory: bool = True) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink creation unavailable: {error}")


def test_cache_path_safety_rejects_root_ancestor_and_descendant_links(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    root_link = tmp_path / "root-link"
    _symlink_or_skip(root_link, real)
    with pytest.raises(SourcePipelineError, match="SOURCE_CACHE_UNSAFE"):
        validate_cache_path_safety(root_link / "artifact", root_link)

    ancestor_target = tmp_path / "ancestor-target"
    ancestor_target.mkdir()
    ancestor_link = tmp_path / "ancestor-link"
    _symlink_or_skip(ancestor_link, ancestor_target)
    configured_root = ancestor_link / "cache"
    with pytest.raises(SourcePipelineError, match="SOURCE_CACHE_UNSAFE"):
        validate_cache_path_safety(configured_root / "artifact", configured_root)

    root = tmp_path / "safe-root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    descendant_link = root / "descendant-link"
    _symlink_or_skip(descendant_link, outside)
    with pytest.raises(SourcePipelineError, match="SOURCE_CACHE_UNSAFE"):
        validate_cache_path_safety(descendant_link / "artifact", root)


def test_cache_path_safety_accepts_normal_path(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    path = root / "nested" / "artifact"
    path.parent.mkdir()
    validate_cache_path_safety(path, root)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction coverage")
def test_cache_path_safety_rejects_windows_junction(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    junction = root / "junction"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    with pytest.raises(SourcePipelineError, match="SOURCE_CACHE_UNSAFE"):
        validate_cache_path_safety(junction / "artifact", root)
