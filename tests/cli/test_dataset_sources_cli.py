from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from motte_cli.main import main
from motte_contracts.dataset_sources import SourceSpec
from motte_sdk import dataset_sources as source_sdk
from motte_sdk import managed_sources as managed_sdk
from motte_sdk.dataset_sources import (
    ArtifactManifestEntry,
    FetchReceipt,
    SafeFetcher,
    artifact_cache_path,
)
from motte_sdk.managed_sources import ConversionResult, ConverterRegistry
from motte_storage.resource_store import SQLiteResourceStore

SOURCE_DIR = Path(__file__).resolve().parents[2] / "datasets" / "direct-llm" / "sources"
REVISION = "a" * 40
URL = "https://example.test/source.jsonl"
BODY = b'{"id": 1}\n'


def _source(
    status: str = "approved", *, converter_id: str | None = None
) -> SourceSpec:
    data = source_sdk.inspect_source("mmlu-pro", SOURCE_DIR).model_dump(mode="json")
    governance = data["governance"]
    license_spec = data["license"]
    upstream = data["upstream"]
    conversion = data["conversion"]
    assert isinstance(governance, dict)
    assert isinstance(license_spec, dict)
    assert isinstance(upstream, dict)
    assert isinstance(conversion, dict)
    governance.update({
        "status": status,
        "distribution_scope": {
            "approved": "public",
            "restricted": "restricted",
            "pending": "blocked",
        }[status],
        "stable_eligible": status == "approved",
        "reviewer": "reviewer" if status != "pending" else None,
        "reviewed_at": "2026-09-19" if status != "pending" else None,
    })
    data_license = license_spec["data"]
    code_license = license_spec["code"]
    assert isinstance(data_license, dict)
    assert isinstance(code_license, dict)
    data_license["verified_spdx"] = "MIT"
    if not data_license["evidence_urls"]:
        data_license["evidence_urls"] = ["https://example.test/data-license"]
    if status == "approved":
        code_license.update({
            "declared_ids": ["Apache-2.0"],
            "verified_spdx": "Apache-2.0",
            "evidence_urls": ["https://example.test/code-license"],
        })
    license_spec.update({
        "commercial_use": "allowed-reviewed",
        "redistribution": "allowed-reviewed",
        "attribution": "required-reviewed",
        "share_alike": "not-required-reviewed",
    })
    revision = upstream["revision"]
    artifacts = upstream["artifacts"]
    assert isinstance(revision, dict)
    assert isinstance(artifacts, list)
    revision["value"] = REVISION
    artifacts[:] = [{
        "logical_name": "source.jsonl",
        "url": URL,
        "format": "jsonl",
        "sha256": hashlib.sha256(BODY).hexdigest(),
        "bytes": len(BODY),
        "max_bytes": 4096,
        "required": True,
    }]
    converter = conversion["converter"]
    scorer = conversion["scorer"]
    assert isinstance(converter, dict)
    assert isinstance(scorer, dict)
    if converter_id is not None:
        converter["id"] = converter_id
    converter["version"] = "1"
    scorer["version"] = "1"
    conversion.update({
        "splits": ["test"],
        "default_split": "test",
        "prompt_version": "prompt-v1",
        "profiles": ["full"],
    })
    data["blockers"] = []
    return SourceSpec.model_validate(data)


def _args(command: str, tmp_path: Path, *extra: str) -> list[str]:
    return [
        "direct-llm",
        "sources",
        command,
        "--registry-dir",
        str(SOURCE_DIR),
        "--cache-dir",
        str(tmp_path / "cache"),
        *extra,
    ]


def _managed_dataset(source: SourceSpec) -> dict[str, Any]:
    return {
        "name": "cli-managed-direct",
        "version": "1",
        "contract_version": 2,
        "eval": {
            "suite": "direct-llm",
            "id": "direct-llm-prompts",
            "version": 2,
            "selected_count": 1,
            "selection": "all-rows-in-file-order",
            "scorer": {"id": "exact", "version": "1", "config": {}},
            "prompt_version": "prompt-v1",
            "max_output_tokens": 16,
            "max_retries": 0,
        },
        "provenance": {
            "source_id": source.id,
            "source_kind": source.tier,
            "homepage": source.links.homepage,
            "upstream_revision": REVISION,
            "artifacts": [{
                "logical_name": "source.jsonl",
                "url": URL,
                "sha256": hashlib.sha256(BODY).hexdigest(),
                "bytes": len(BODY),
            }],
            "license": {
                "id": "MIT",
                "status": source.governance.status,
                "evidence_urls": list(source.license.data.evidence_urls),
                "commercial_use": source.license.commercial_use,
                "redistribution": source.license.redistribution,
                "reviewed_at": source.governance.reviewed_at,
            },
            "converter": {
                "id": source.conversion.converter.id,
                "version": "1",
                "config": {},
            },
            "synthetic": False,
        },
        "cases": [{
            "case_id": "cli-managed-case-1",
            "input": "Question",
            "expected": "ok",
            "metadata": {
                "source_line": 1,
                "source_id": "row-1",
                "language": "en",
                "subject": "fixture",
                "category": "fixture",
                "difficulty": None,
                "split": "test",
                "tags": ["fixture"],
                "template_family": "prompt-v1",
            },
        }],
        "profiles": [{
            "name": "full",
            "strategy": "all-fixed-ids",
            "case_ids": ["cli-managed-case-1"],
            "dimensions": ["category"],
            "seed": None,
        }],
    }


def _install_managed_source(monkeypatch, source: SourceSpec, *, transport=None) -> None:
    registry = ConverterRegistry({
        (source.conversion.converter.id, "1"): lambda **kwargs: ConversionResult(
            _managed_dataset(source), input_count=1
        )
    })
    monkeypatch.setattr(source_sdk, "inspect_source", lambda source_id, directory=None: source)
    monkeypatch.setattr(source_sdk, "load_registry", lambda directory=None: {source.id: source})
    original_convert = managed_sdk.convert_cached_source
    original_import = managed_sdk.import_cached_source
    original_prepare = managed_sdk.prepare_source

    def convert(*args, **kwargs):
        kwargs["converter_registry"] = registry
        return original_convert(*args, **kwargs)

    def import_source(*args, **kwargs):
        kwargs["converter_registry"] = registry
        return original_import(*args, **kwargs)

    def prepare(*args, **kwargs):
        kwargs["converter_registry"] = registry
        if transport is not None:
            kwargs["transport"] = transport
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(managed_sdk, "convert_cached_source", convert)
    monkeypatch.setattr(managed_sdk, "import_cached_source", import_source)
    monkeypatch.setattr(managed_sdk, "prepare_source", prepare)


def _write_managed_cache(source: SourceSpec, tmp_path: Path) -> Path:
    target = artifact_cache_path(
        source, REVISION, "source.jsonl", cache_root=tmp_path / "cache"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(BODY)
    return target


class ManagedResponse:
    status = 200
    headers = {"Content-Length": str(len(BODY))}
    final_url = URL

    def __init__(self):
        self.remaining = BODY
        self.closed = False

    def read(self, size=-1):
        del size
        result, self.remaining = self.remaining, b""
        return result

    def close(self):
        self.closed = True


class ManagedTransport:
    def __init__(self):
        self.calls: list[tuple[str, float]] = []

    def open(self, url: str, *, timeout: float):
        self.calls.append((url, timeout))
        return ManagedResponse()


def test_sources_help_lists_only_current_commands(capsys):
    with pytest.raises(SystemExit) as raised:
        main(["direct-llm", "sources", "--help"])
    assert raised.value.code == 0
    output = capsys.readouterr().out
    assert "list" in output
    assert "inspect" in output
    assert "fetch" in output
    assert "verify" in output
    assert "convert" in output
    assert "import" in output
    assert "prepare" in output
    assert "adapter 集成后提供" not in output


def test_sources_list_and_inspect_emit_json_without_network(capsys, tmp_path):
    assert main(_args("list", tmp_path)) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["total"] == 7
    mmlu = next(item for item in payload["items"] if item["id"] == "mmlu-pro")
    assert set(("status", "tier", "blockers", "revision", "artifacts", "conversion", "safety")) <= set(mmlu)
    assert mmlu["status"] == "pending"

    assert main(_args("inspect", tmp_path, "--source", "truthfulqa")) == 0
    captured = capsys.readouterr()
    inspected = json.loads(captured.out)
    assert captured.err == ""
    assert inspected["id"] == "truthfulqa"
    assert inspected["status"] == "pending"
    assert inspected["revision"]["value"] is None


def test_sources_inspect_unknown_is_structured_stderr(capsys, tmp_path):
    assert main(_args("inspect", tmp_path, "--source", "missing")) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)["error"]
    assert error["code"] == "SOURCE_NOT_FOUND"
    assert isinstance(error["details"], dict)


def test_fetch_forwards_cli_limits_directories_and_json_receipt(monkeypatch, capsys, tmp_path):
    source = _source()
    calls: list[dict[str, Any]] = []
    roots: list[Path] = []

    monkeypatch.setattr(source_sdk, "inspect_source", lambda source_id, directory=None: source)

    class CapturingFetcher:
        def __init__(self, *, cache_root=None):
            roots.append(Path(cache_root))

        def fetch(self, source_arg, revision, logical_name, **kwargs):
            calls.append({
                "source": source_arg,
                "revision": revision,
                "logical_name": logical_name,
                **kwargs,
            })
            entry = ArtifactManifestEntry(
                logical_name="source.jsonl",
                url=URL,
                format="jsonl",
                sha256=hashlib.sha256(BODY).hexdigest(),
                bytes=len(BODY),
                max_bytes=4096,
            )
            return FetchReceipt(
                source_id=source.id,
                revision=REVISION,
                artifact=entry,
                artifact_manifest_sha256="f" * 64,
                cache_path=str(tmp_path / "cache" / source.id / REVISION / "source.jsonl"),
                cached=False,
            )

    monkeypatch.setattr(source_sdk, "SafeFetcher", CapturingFetcher)
    args = _args(
        "fetch",
        tmp_path,
        "--source",
        source.id,
        "--revision",
        REVISION,
        "--timeout",
        "4.5",
        "--max-bytes",
        "2048",
    )
    assert main(args) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert roots == [tmp_path / "cache"]
    assert len(calls) == 1
    assert calls[0]["entrypoint"] == "cli"
    assert calls[0]["timeout"] == 4.5
    assert calls[0]["max_bytes"] == 2048
    assert calls[0]["override"] is None
    assert payload["source_id"] == source.id
    assert payload["revision"] == REVISION
    assert payload["total"] == 1
    assert payload["artifacts"][0]["cached"] is False


def test_fetch_override_fields_must_be_complete_before_fetcher(monkeypatch, capsys, tmp_path):
    source = _source("restricted")
    constructed: list[bool] = []
    monkeypatch.setattr(source_sdk, "inspect_source", lambda source_id, directory=None: source)

    class NeverConstructed:
        def __init__(self, **kwargs):
            constructed.append(True)

    monkeypatch.setattr(source_sdk, "SafeFetcher", NeverConstructed)
    args = _args(
        "fetch",
        tmp_path,
        "--source",
        source.id,
        "--override-actor",
        "reviewer",
        "--override-purpose",
        "test",
    )
    assert main(args) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == "CONTRACT_INVALID"
    assert constructed == []


def test_complete_cli_override_is_not_treated_as_verified_approval(
    monkeypatch, capsys, tmp_path
):
    source = _source("restricted")
    transport = NoNetworkTransport()
    monkeypatch.setattr(source_sdk, "inspect_source", lambda source_id, directory=None: source)

    class OfflineFetcher(SafeFetcher):
        def __init__(self, *, cache_root=None):
            super().__init__(cache_root=cache_root, transport=transport)

    monkeypatch.setattr(source_sdk, "SafeFetcher", OfflineFetcher)
    args = _args(
        "fetch",
        tmp_path,
        "--source",
        source.id,
        "--override-actor",
        "reviewer",
        "--override-purpose",
        "noncommercial evaluation",
        "--override-ticket",
        "GOV-42",
    )
    assert main(args) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == "SOURCE_APPROVAL_REQUIRED"
    assert transport.calls == 0


class NoNetworkTransport:
    def __init__(self):
        self.calls = 0

    def open(self, url: str, *, timeout: float):
        del url, timeout
        self.calls += 1
        raise AssertionError("network must not be called")


def test_pending_and_restricted_fetch_fail_before_fake_transport(
    monkeypatch, capsys, tmp_path
):
    original_fetcher = SafeFetcher
    transport = NoNetworkTransport()

    class OfflineFetcher(original_fetcher):
        def __init__(self, *, cache_root=None):
            super().__init__(cache_root=cache_root, transport=transport)

    monkeypatch.setattr(source_sdk, "SafeFetcher", OfflineFetcher)
    for status in ("pending", "restricted"):
        source = _source(status)
        monkeypatch.setattr(
            source_sdk, "inspect_source", lambda source_id, directory=None, value=source: value
        )
        assert main(_args("fetch", tmp_path, "--source", source.id)) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        expected = "SOURCE_LICENSE_BLOCKED" if status == "pending" else "SOURCE_APPROVAL_REQUIRED"
        assert json.loads(captured.err)["error"]["code"] == expected
    assert transport.calls == 0


def test_verify_revalidates_cache_without_constructing_fetcher(monkeypatch, capsys, tmp_path):
    source = _source()
    monkeypatch.setattr(source_sdk, "inspect_source", lambda source_id, directory=None: source)

    class ForbiddenFetcher:
        def __init__(self, **kwargs):
            raise AssertionError("verify must never construct a fetcher")

    monkeypatch.setattr(source_sdk, "SafeFetcher", ForbiddenFetcher)
    target = artifact_cache_path(
        source, REVISION, "source.jsonl", cache_root=tmp_path / "cache"
    )
    target.parent.mkdir(parents=True)
    target.write_bytes(BODY)

    assert main(_args("verify", tmp_path, "--source", source.id)) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload == {
        "source_id": source.id,
        "revision": REVISION,
        "artifacts": [{
            "logical_name": "source.jsonl",
            "path": str(target),
            "sha256": hashlib.sha256(BODY).hexdigest(),
            "bytes": len(BODY),
            "verified": True,
        }],
        "total": 1,
    }


def test_verify_cache_corruption_is_structured_and_never_networks(
    monkeypatch, capsys, tmp_path
):
    source = _source()
    monkeypatch.setattr(source_sdk, "inspect_source", lambda source_id, directory=None: source)
    target = artifact_cache_path(
        source, REVISION, "source.jsonl", cache_root=tmp_path / "cache"
    )
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupt")

    assert main(_args("verify", tmp_path, "--source", source.id)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)["error"]
    assert error["code"] == "SOURCE_CACHE_CORRUPT"
    assert error["details"]["expected_sha256"] == hashlib.sha256(BODY).hexdigest()


def test_convert_reads_verified_cache_writes_dataset_receipt_and_never_fetches(
    monkeypatch, capsys, tmp_path
):
    source = _source(converter_id="cli-fixture-converter")
    _install_managed_source(monkeypatch, source)
    _write_managed_cache(source, tmp_path)

    class ForbiddenFetcher:
        def __init__(self, **kwargs):
            raise AssertionError("convert must never construct a fetcher")

    monkeypatch.setattr(source_sdk, "SafeFetcher", ForbiddenFetcher)
    output = tmp_path / "converted.json"
    args = _args(
        "convert",
        tmp_path,
        "--source",
        source.id,
        "--revision",
        REVISION,
        "--config",
        json.dumps({"input_count": 1}),
        "--output",
        str(output),
    )
    assert main(args) == 0
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    document = json.loads(output.read_text(encoding="utf-8"))
    assert captured.err == ""
    assert set(document) == {"dataset", "receipt"}
    assert document["receipt"]["governance"]["publishable"] is True
    assert summary["dataset_fingerprint"] == document["dataset"]["dataset_fingerprint"]
    assert summary["profile_hashes"] == document["receipt"]["profile_hashes"]
    assert "dataset" not in summary


def test_pending_convert_requires_flag_and_remains_unpublishable(
    monkeypatch, capsys, tmp_path
):
    source = _source("pending", converter_id="cli-fixture-converter")
    _install_managed_source(monkeypatch, source)
    _write_managed_cache(source, tmp_path)
    output = tmp_path / "pending.json"
    base = _args(
        "convert",
        tmp_path,
        "--source",
        source.id,
        "--revision",
        REVISION,
        "--config",
        json.dumps({"input_count": 1}),
        "--output",
        str(output),
    )

    assert main(base) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.err)["error"]["code"] == "SOURCE_LICENSE_BLOCKED"
    assert not output.exists()

    assert main([*base, "--allow-experimental"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["publishable"] is False
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["receipt"]["governance"]["publishable"] is False
    assert "publication_id" not in document["receipt"]


def test_convert_output_conflict_never_overwrites_existing_content(
    monkeypatch, capsys, tmp_path
):
    source = _source(converter_id="cli-fixture-converter")
    _install_managed_source(monkeypatch, source)
    _write_managed_cache(source, tmp_path)
    output = tmp_path / "converted.json"
    args = _args(
        "convert",
        tmp_path,
        "--source",
        source.id,
        "--revision",
        REVISION,
        "--config",
        json.dumps({"input_count": 1}),
        "--output",
        str(output),
    )
    assert main(args) == 0
    capsys.readouterr()
    output.write_text("do-not-overwrite", encoding="utf-8")

    assert main(args) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.err)["error"]["code"] == "SOURCE_OUTPUT_CONFLICT"
    assert output.read_text(encoding="utf-8") == "do-not-overwrite"


def test_import_reconverts_verified_cache_and_publishes_three_records_idempotently(
    monkeypatch, capsys, tmp_path
):
    source = _source(converter_id="cli-fixture-converter")
    _install_managed_source(monkeypatch, source)
    _write_managed_cache(source, tmp_path)
    db_path = tmp_path / "resources.db"
    args = _args(
        "import",
        tmp_path,
        "--source",
        source.id,
        "--revision",
        REVISION,
        "--config",
        json.dumps({"input_count": 1, "version": "1"}),
        "--actor",
        "cli-test",
        "--db",
        str(db_path),
    )

    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert first["publication_id"] == second["publication_id"]
    assert first["dataset_fingerprint"] == second["dataset_fingerprint"]
    resources = SQLiteResourceStore(str(db_path))
    assert len(resources.datasets.list()) == 1
    assert len(resources.scenarios.list()) == 1
    assert len(resources.publications.list()) == 1


def test_import_and_prepare_unapproved_sources_stop_before_orchestration_or_database(
    monkeypatch, capsys, tmp_path
):
    calls: list[str] = []

    def forbidden(*args, **kwargs):
        del args, kwargs
        calls.append("managed")
        raise AssertionError("governance must block before managed SDK orchestration")

    monkeypatch.setattr(managed_sdk, "import_cached_source", forbidden)
    monkeypatch.setattr(managed_sdk, "prepare_source", forbidden)
    for command in ("import", "prepare"):
        for status, expected in (
            ("pending", "SOURCE_LICENSE_BLOCKED"),
            ("restricted", "SOURCE_APPROVAL_REQUIRED"),
        ):
            source = _source(status, converter_id="cli-fixture-converter")
            monkeypatch.setattr(
                source_sdk, "inspect_source", lambda source_id, directory=None, value=source: value
            )
            monkeypatch.setattr(
                source_sdk, "load_registry", lambda directory=None, value=source: {value.id: value}
            )
            db_path = tmp_path / f"{command}-{status}.db"
            args = _args(
                command,
                tmp_path,
                "--source",
                source.id,
                "--revision",
                REVISION,
                "--config",
                json.dumps({"input_count": 1}),
                "--actor",
                "cli-test",
                "--db",
                str(db_path),
            )
            assert main(args) == 2
            captured = capsys.readouterr()
            assert json.loads(captured.err)["error"]["code"] == expected
            assert not db_path.exists()
    assert calls == []


def test_all_current_registry_sources_block_prepare_before_orchestration(
    monkeypatch, capsys, tmp_path
):
    calls: list[bool] = []

    def forbidden(*args, **kwargs):
        del args, kwargs
        calls.append(True)
        raise AssertionError("current registry source must fail governance preflight")

    monkeypatch.setattr(managed_sdk, "prepare_source", forbidden)
    registry = source_sdk.load_registry(SOURCE_DIR)
    for source in registry.values():
        db_path = tmp_path / f"{source.id}.db"
        args = _args(
            "prepare",
            tmp_path,
            "--source",
            source.id,
            "--revision",
            source.upstream.revision.value or REVISION,
            "--config",
            "{}",
            "--actor",
            "cli-test",
            "--db",
            str(db_path),
        )
        assert main(args) == 2
        captured = capsys.readouterr()
        assert json.loads(captured.err)["error"]["code"] in {
            "SOURCE_LICENSE_BLOCKED",
            "SOURCE_APPROVAL_REQUIRED",
        }
        assert not db_path.exists()
    assert calls == []


def test_prepare_approved_fetches_with_fake_transport_and_publishes(
    monkeypatch, capsys, tmp_path
):
    source = _source(converter_id="cli-fixture-converter")
    transport = ManagedTransport()
    _install_managed_source(monkeypatch, source, transport=transport)
    db_path = tmp_path / "prepared.db"
    args = _args(
        "prepare",
        tmp_path,
        "--source",
        source.id,
        "--revision",
        REVISION,
        "--config",
        json.dumps({"input_count": 1, "version": "1"}),
        "--actor",
        "cli-test",
        "--db",
        str(db_path),
    )

    assert main(args) == 0
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert captured.err == ""
    assert summary["publication_id"]
    assert summary["dataset_fingerprint"]
    assert transport.calls == [(URL, 30.0)]
    resources = SQLiteResourceStore(str(db_path))
    assert len(resources.datasets.list()) == 1
    assert len(resources.scenarios.list()) == 1
    assert len(resources.publications.list()) == 1


@pytest.mark.parametrize(
    ("config", "code"),
    [
        ({"unexpected": True}, "SOURCE_CONFIG_INVALID"),
        ({"api_key": "secret"}, "CREDENTIALS_REJECTED"),
        ({"nested": {"token": "secret"}}, "CREDENTIALS_REJECTED"),
    ],
)
def test_managed_commands_reject_extra_or_secret_config_before_sdk_call(
    monkeypatch, capsys, tmp_path, config, code
):
    source = _source(converter_id="cli-fixture-converter")
    calls: list[bool] = []
    monkeypatch.setattr(source_sdk, "inspect_source", lambda source_id, directory=None: source)
    monkeypatch.setattr(source_sdk, "load_registry", lambda directory=None: {source.id: source})

    def forbidden(*args, **kwargs):
        del args, kwargs
        calls.append(True)
        raise AssertionError("invalid config must be rejected before managed SDK")

    monkeypatch.setattr(managed_sdk, "convert_cached_source", forbidden)
    output = tmp_path / "invalid.json"
    args = _args(
        "convert",
        tmp_path,
        "--source",
        source.id,
        "--revision",
        REVISION,
        "--config",
        json.dumps(config),
        "--output",
        str(output),
    )
    assert main(args) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.err)["error"]["code"] == code
    assert calls == []
    assert not output.exists()
