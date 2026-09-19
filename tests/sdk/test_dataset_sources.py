from __future__ import annotations

import hashlib
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from motte_contracts.dataset_sources import MAX_ARTIFACT_BYTES, SourceOverrideEvidence, SourceSpec
from motte_sdk import dataset_sources as source_sdk
from motte_sdk.dataset_sources import (
    SafeFetcher,
    SourcePipelineError,
    UrllibTransport,
    artifact_cache_path,
    inspect_source,
    list_sources,
    load_registry,
    parse_artifact_bytes,
    parse_cached_artifact,
    parse_csv,
    parse_json,
    parse_jsonl,
    parse_parquet,
    read_cached_artifact,
    resolve_revision,
    source_registry_path,
    validate_revision,
)

SOURCE_DIR = Path(__file__).resolve().parents[2] / "datasets" / "direct-llm" / "sources"
REVISION = "a" * 40
URL = "https://example.test/dataset.jsonl"
OVERRIDE = SourceOverrideEvidence(
    actor="operator",
    purpose="isolated test",
    ticket="GOV-42",
    source_id="mmlu-pro",
    actions=["fetch", "import"],
    approved_by="license-reviewer",
    issued_at="2020-01-01T00:00:00Z",
    expires_at="2099-01-01T00:00:00Z",
)


class FakeResponse:
    def __init__(
        self,
        chunks: list[bytes | OSError],
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
        final_url: str = URL,
        close_error: Exception | None = None,
    ) -> None:
        self._chunks = list(chunks)
        self.status = status
        self.headers: Mapping[str, str] = dict(headers or {})
        self.final_url = final_url
        self.close_error = close_error
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        del size
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if isinstance(chunk, OSError):
            raise chunk
        return chunk

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FakeTransport:
    def __init__(self, responses: list[FakeResponse] | None = None, *, error: OSError | None = None):
        self.responses = list(responses or [])
        self.error = error
        self.calls: list[tuple[str, float]] = []

    def open(self, url: str, *, timeout: float) -> FakeResponse:
        self.calls.append((url, timeout))
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise AssertionError("unexpected fake transport call")
        return self.responses.pop(0)


def _payload(spec: SourceSpec) -> dict[str, Any]:
    return spec.model_dump(mode="json")


def _source(
    body: bytes,
    *,
    status: str = "approved",
    artifact_sha256: str | None = None,
    artifact_bytes: int | None = None,
    artifact_max_bytes: int | None = None,
    logical_name: str = "dataset.jsonl",
    artifact_format: str = "jsonl",
    url: str = URL,
) -> SourceSpec:
    data = _payload(inspect_source("mmlu-pro", SOURCE_DIR))
    governance = data["governance"]
    license_spec = data["license"]
    upstream = data["upstream"]
    conversion = data["conversion"]
    assert isinstance(governance, dict)
    assert isinstance(license_spec, dict)
    assert isinstance(upstream, dict)
    assert isinstance(conversion, dict)
    governance.update(
        {
            "status": status,
            "distribution_scope": {
                "approved": "public",
                "restricted": "restricted",
                "pending": "blocked",
            }[status],
            "stable_eligible": status == "approved",
            "reviewer": "reviewer" if status != "pending" else None,
            "reviewed_at": "2026-09-19" if status != "pending" else None,
        }
    )
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
    license_spec.update(
        {
            "commercial_use": "allowed-reviewed",
            "redistribution": "allowed-reviewed",
            "attribution": "required-reviewed",
            "share_alike": "not-required-reviewed",
        }
    )
    revision = upstream["revision"]
    artifacts = upstream["artifacts"]
    assert isinstance(revision, dict)
    assert isinstance(artifacts, list)
    revision["value"] = REVISION
    artifacts[:] = [
        {
            "logical_name": logical_name,
            "url": url,
            "format": artifact_format,
            "sha256": artifact_sha256 or hashlib.sha256(body).hexdigest(),
            "bytes": len(body) if artifact_bytes is None else artifact_bytes,
            "max_bytes": max(len(body), 1024)
            if artifact_max_bytes is None
            else artifact_max_bytes,
            "required": True,
        }
    ]
    converter = conversion["converter"]
    scorer = conversion["scorer"]
    assert isinstance(converter, dict)
    assert isinstance(scorer, dict)
    converter["version"] = "1"
    scorer["version"] = "1"
    conversion.update(
        {
            "splits": ["test"],
            "default_split": "test",
            "prompt_version": "prompt-v1",
            "profiles": ["full"],
        }
    )
    data["blockers"] = []
    return SourceSpec.model_validate(data)


def _response(
    body: bytes,
    *,
    chunks: list[bytes | OSError] | None = None,
    content_length: int | None = None,
    status: int = 200,
    final_url: str = URL,
    close_error: Exception | None = None,
) -> FakeResponse:
    length = len(body) if content_length is None else content_length
    return FakeResponse(
        chunks if chunks is not None else [body],
        status=status,
        headers={"Content-Length": str(length)},
        final_url=final_url,
        close_error=close_error,
    )


def _fetch(
    tmp_path: Path,
    source: SourceSpec,
    transport: FakeTransport,
    *,
    max_bytes: int = 1024,
    url: str | None = None,
    override: SourceOverrideEvidence | Mapping[str, object] | None = None,
    approval_verifier=None,
):
    return SafeFetcher(
        cache_root=tmp_path,
        transport=transport,
        approval_verifier=approval_verifier,
    ).fetch(
        source,
        REVISION,
        source.upstream.artifacts[0].logical_name,
        entrypoint="cli",
        timeout=2.5,
        max_bytes=max_bytes,
        url=url,
        override=override,
    )


def _assert_no_partial_cache(root: Path) -> None:
    assert not [path for path in root.rglob("*") if path.is_file()]


def test_registry_path_load_list_and_inspect_are_static():
    assert source_registry_path(SOURCE_DIR) == SOURCE_DIR
    registry = load_registry(SOURCE_DIR)
    assert len(registry) == 7
    assert [source.id for source in list_sources(SOURCE_DIR)] == sorted(registry)
    assert inspect_source("truthfulqa", SOURCE_DIR).id == "truthfulqa"
    with pytest.raises(SourcePipelineError) as error:
        inspect_source("missing", SOURCE_DIR)
    assert error.value.code == "SOURCE_NOT_FOUND"


def test_registry_wraps_duplicate_json_keys_as_structured_error(tmp_path: Path):
    raw = (SOURCE_DIR / "mmlu-pro.json").read_text(encoding="utf-8")
    raw = raw[:-2] + ',\n  "id": "shadow"\n}\n'
    (tmp_path / "mmlu-pro.json").write_text(raw, encoding="utf-8")
    with pytest.raises(SourcePipelineError) as error:
        load_registry(tmp_path)
    assert error.value.code == "SOURCE_MANIFEST_INVALID"
    assert "duplicate JSON key" in error.value.message


def test_revision_validation_is_pinned_and_floating_names_are_forbidden():
    source = _source(b'{}\n')
    assert validate_revision(source, REVISION.upper()) == REVISION
    assert resolve_revision(source) == REVISION
    assert resolve_revision(source, REVISION) == REVISION

    for value in ("main", "latest", "HEAD", "a" * 39, "z" * 40, "../escape"):
        with pytest.raises(SourcePipelineError) as error:
            validate_revision(source, value)
        assert error.value.code == "SOURCE_REVISION_INVALID"

    with pytest.raises(SourcePipelineError, match="manifest-pinned"):
        resolve_revision(source, "b" * 40)
    assert validate_revision(source, "release-2026", immutable_pattern=r"release-\d{4}") == (
        "release-2026"
    )


def test_manifest_defined_internal_revision_is_accepted():
    internal = inspect_source("motte-core-zh", SOURCE_DIR)
    assert resolve_revision(internal) == "motte-core-zh-generator-v1"
    with pytest.raises(SourcePipelineError):
        validate_revision(internal, "other-version")


def test_fetch_success_cache_reuse_revalidation_and_parsing(tmp_path: Path):
    body = b'{"id": 1}\n{"id": 2}\n'
    source = _source(body)
    response = _response(body)
    transport = FakeTransport([response])

    receipt = _fetch(tmp_path, source, transport)
    target = artifact_cache_path(source, REVISION, "dataset.jsonl", cache_root=tmp_path)
    assert target.read_bytes() == body
    assert hashlib.sha256(body).hexdigest() in target.parts
    assert receipt.cache_path == str(target)
    assert receipt.cached is False
    assert receipt.artifact.sha256 == hashlib.sha256(body).hexdigest()
    assert response.closed is True
    assert transport.calls == [(URL, 2.5)]

    cached = _fetch(tmp_path, source, transport)
    assert cached.cached is True
    assert transport.calls == [(URL, 2.5)]
    assert read_cached_artifact(source, REVISION, "dataset.jsonl", cache_root=tmp_path) == body
    assert parse_cached_artifact(
        source, REVISION, "dataset.jsonl", cache_root=tmp_path
    ) == [{"id": 1}, {"id": 2}]


def test_sha_namespace_prevents_changed_manifest_cache_collision(tmp_path: Path):
    first_body = b'{"version": 1}\n'
    second_body = b'{"version": 2}\n'
    first_source = _source(first_body)
    second_source = _source(second_body)
    first = _fetch(tmp_path, first_source, FakeTransport([_response(first_body)]))
    second = _fetch(tmp_path, second_source, FakeTransport([_response(second_body)]))

    assert first.cache_path != second.cache_path
    assert Path(first.cache_path).read_bytes() == first_body
    assert Path(second.cache_path).read_bytes() == second_body


def test_same_artifact_has_identical_manifest_hash_in_two_cache_roots(tmp_path: Path):
    body = b'{"stable": true}\n'
    source = _source(body)
    first = _fetch(tmp_path / "one", source, FakeTransport([_response(body)]))
    second = _fetch(tmp_path / "two", source, FakeTransport([_response(body)]))

    assert first.artifact_manifest_sha256 == second.artifact_manifest_sha256
    assert first.artifact.sha256 == second.artifact.sha256
    assert first.cache_path != second.cache_path


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (_response(b"x", status=302), "SOURCE_REDIRECT_FORBIDDEN"),
        (_response(b"x", final_url="https://redirect.test/data"), "SOURCE_REDIRECT_FORBIDDEN"),
        (_response(b"x", status=503), "SOURCE_UNAVAILABLE"),
    ],
)
def test_redirect_and_http_failures_leave_no_artifact(
    tmp_path: Path, response: FakeResponse, code: str
):
    source = _source(b"x")
    with pytest.raises(SourcePipelineError) as error:
        _fetch(tmp_path, source, FakeTransport([response]))
    assert error.value.code == code
    assert response.closed is True
    _assert_no_partial_cache(tmp_path)


def test_timeout_and_interrupted_stream_leave_no_partial_artifact(tmp_path: Path):
    body = b"abcdef"
    source = _source(body)
    with pytest.raises(SourcePipelineError) as timeout_error:
        _fetch(tmp_path / "timeout", source, FakeTransport(error=TimeoutError("timed out")))
    assert timeout_error.value.code == "SOURCE_UNAVAILABLE"
    _assert_no_partial_cache(tmp_path / "timeout")

    response = _response(body, chunks=[body[:3], OSError("connection reset")])
    with pytest.raises(SourcePipelineError) as stream_error:
        _fetch(tmp_path / "stream", source, FakeTransport([response]))
    assert stream_error.value.code == "SOURCE_UNAVAILABLE"
    assert response.closed is True
    _assert_no_partial_cache(tmp_path / "stream")


def test_close_error_never_overrides_primary_integrity_error(tmp_path: Path):
    expected = b"expected"
    source = _source(expected)
    response = _response(
        b"tampered",
        close_error=OSError("close failed"),
    )
    with pytest.raises(SourcePipelineError) as error:
        _fetch(tmp_path, source, FakeTransport([response]))
    assert error.value.code == "SOURCE_HASH_MISMATCH"
    assert response.closed is True


def test_short_read_is_rejected_and_temp_file_removed(tmp_path: Path):
    expected = b"expected"
    source = _source(expected)
    response = _response(expected, chunks=[expected[:-1]], content_length=len(expected))

    with pytest.raises(SourcePipelineError) as error:
        _fetch(tmp_path, source, FakeTransport([response]))
    assert error.value.code == "SOURCE_SHORT_READ"
    _assert_no_partial_cache(tmp_path)


def test_header_stream_and_global_size_limits_are_enforced(tmp_path: Path):
    body = b"12345678"
    source = _source(body, artifact_max_bytes=8)
    with pytest.raises(SourcePipelineError) as header_error:
        _fetch(
            tmp_path / "header",
            source,
            FakeTransport([_response(body, content_length=9)]),
            max_bytes=8,
        )
    assert header_error.value.code == "SOURCE_TOO_LARGE"

    stream_response = FakeResponse([body, b"x"], headers={}, final_url=URL)
    with pytest.raises(SourcePipelineError) as stream_error:
        _fetch(tmp_path / "stream", source, FakeTransport([stream_response]), max_bytes=8)
    assert stream_error.value.code == "SOURCE_TOO_LARGE"

    with pytest.raises(SourcePipelineError) as global_error:
        _fetch(
            tmp_path / "global",
            source,
            FakeTransport(),
            max_bytes=MAX_ARTIFACT_BYTES + 1,
        )
    assert global_error.value.code == "SOURCE_LIMIT_INVALID"
    _assert_no_partial_cache(tmp_path / "header")
    _assert_no_partial_cache(tmp_path / "stream")


def test_content_length_and_hash_mismatch_are_rejected(tmp_path: Path):
    body = b"payload"
    source = _source(body)
    with pytest.raises(SourcePipelineError) as length_error:
        _fetch(
            tmp_path / "length",
            source,
            FakeTransport([_response(body, content_length=len(body) + 1)]),
        )
    assert length_error.value.code == "SOURCE_SIZE_MISMATCH"

    wrong_hash = _source(body, artifact_sha256="0" * 64)
    with pytest.raises(SourcePipelineError) as hash_error:
        _fetch(tmp_path / "hash", wrong_hash, FakeTransport([_response(body)]))
    assert hash_error.value.code == "SOURCE_HASH_MISMATCH"
    _assert_no_partial_cache(tmp_path / "length")
    _assert_no_partial_cache(tmp_path / "hash")


def test_cache_corruption_is_detected_before_network_and_on_read(tmp_path: Path):
    body = b"payload"
    source = _source(body)
    target = artifact_cache_path(source, REVISION, "dataset.jsonl", cache_root=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupt")
    transport = FakeTransport()

    with pytest.raises(SourcePipelineError) as fetch_error:
        _fetch(tmp_path, source, transport)
    assert fetch_error.value.code == "SOURCE_CACHE_CORRUPT"
    assert transport.calls == []
    with pytest.raises(SourcePipelineError) as read_error:
        read_cached_artifact(source, REVISION, "dataset.jsonl", cache_root=tmp_path)
    assert read_error.value.code == "SOURCE_CACHE_CORRUPT"


def test_cache_symlink_is_unsafe_not_a_miss(monkeypatch, tmp_path: Path):
    body = b"payload"
    source = _source(body)
    target = artifact_cache_path(source, REVISION, "dataset.jsonl", cache_root=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(body)
    original_is_symlink = Path.is_symlink

    def marked_symlink(path: Path) -> bool:
        return path == target or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", marked_symlink)
    with pytest.raises(SourcePipelineError) as raised:
        read_cached_artifact(source, REVISION, "dataset.jsonl", cache_root=tmp_path)
    assert raised.value.code == "SOURCE_CACHE_UNSAFE"


def test_cache_root_ancestor_junction_is_unsafe(monkeypatch, tmp_path: Path):
    source = _source(b"payload")
    target = artifact_cache_path(source, REVISION, "dataset.jsonl", cache_root=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"payload")
    original_is_junction = Path.is_junction
    unsafe_ancestor = tmp_path.parent

    def marked_junction(path: Path) -> bool:
        return path == unsafe_ancestor or original_is_junction(path)

    monkeypatch.setattr(Path, "is_junction", marked_junction)
    with pytest.raises(SourcePipelineError) as raised:
        read_cached_artifact(source, REVISION, "dataset.jsonl", cache_root=tmp_path)
    assert raised.value.code == "SOURCE_CACHE_UNSAFE"


def test_local_cache_io_has_distinct_error_code(monkeypatch, tmp_path: Path):
    body = b"payload"
    source = _source(body)
    target = artifact_cache_path(source, REVISION, "dataset.jsonl", cache_root=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(body)
    original_open = Path.open

    def failing_open(path: Path, *args, **kwargs):
        if path == target:
            raise OSError("disk read failed")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(SourcePipelineError) as raised:
        read_cached_artifact(source, REVISION, "dataset.jsonl", cache_root=tmp_path)
    assert raised.value.code == "SOURCE_CACHE_IO"


def test_cache_stat_io_has_distinct_error_code(monkeypatch, tmp_path: Path):
    body = b"payload"
    source = _source(body)
    target = artifact_cache_path(source, REVISION, "dataset.jsonl", cache_root=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(body)
    original_is_file = Path.is_file

    def failing_stat(path: Path) -> bool:
        if path == target:
            raise OSError("stat failed")
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", failing_stat)
    with pytest.raises(SourcePipelineError) as raised:
        read_cached_artifact(source, REVISION, "dataset.jsonl", cache_root=tmp_path)
    assert raised.value.code == "SOURCE_CACHE_IO"


def test_protocol_exact_url_entrypoint_and_limits_fail_before_network(tmp_path: Path):
    body = b"payload"
    source = _source(body)
    transport = FakeTransport()
    fetcher = SafeFetcher(cache_root=tmp_path, transport=transport)

    cases = [
        ({"url": "http://example.test/data"}, "SOURCE_URL_INVALID"),
        ({"url": "https://127.0.0.1/data"}, "SOURCE_URL_INVALID"),
        ({"url": "https://other.test/data"}, "SOURCE_URL_NOT_REGISTERED"),
        ({"entrypoint": "api"}, "SOURCE_NETWORK_DISABLED"),
        ({"timeout": 0}, "SOURCE_LIMIT_INVALID"),
        ({"timeout": float("nan")}, "SOURCE_LIMIT_INVALID"),
        ({"timeout": float("inf")}, "SOURCE_LIMIT_INVALID"),
        ({"max_bytes": 2048}, "SOURCE_LIMIT_INVALID"),
    ]
    for overrides, code in cases:
        arguments: dict[str, Any] = {
            "entrypoint": "cli",
            "timeout": 1.0,
            "max_bytes": 1024,
        }
        arguments.update(overrides)
        with pytest.raises(SourcePipelineError) as error:
            fetcher.fetch(source, REVISION, "dataset.jsonl", **arguments)
        assert error.value.code == code
    assert transport.calls == []


def test_urllib_ignores_environment_proxy_pins_dns_and_rejects_any_non_global_address(
    monkeypatch,
):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    connections: list[tuple[str, int, str, float]] = []
    resolver_calls: list[str] = []

    class RawResponse:
        status = 200

        def getheaders(self):
            return [("Content-Length", "0")]

        def read(self, size=-1):
            del size
            return b""

        def close(self):
            return None

    class Connection:
        def request(self, method, target, *, headers):
            del method, target, headers

        def getresponse(self):
            return RawResponse()

        def close(self):
            return None

    def public_resolver(host, port, **kwargs):
        del port, kwargs
        resolver_calls.append(host)
        return [(2, None, None, None, ("8.8.8.8", 443))]

    def connection_factory(host, port, address, timeout):
        connections.append((host, port, address, timeout))
        return Connection()

    transport = UrllibTransport(
        resolver=public_resolver,
        connection_factory=connection_factory,
    )
    response = transport.open(URL, timeout=1.0)
    response.close()
    assert resolver_calls == ["example.test"]
    assert connections == [("example.test", 443, "8.8.8.8", 1.0)]

    def mixed_resolver(*args, **kwargs):
        del args, kwargs
        return [
            (socket_family, None, None, None, (address, 443))
            for socket_family, address in ((2, "8.8.8.8"), (2, "127.0.0.1"))
        ]

    transport = UrllibTransport(
        resolver=mixed_resolver,
        connection_factory=lambda *args: pytest.fail(f"unexpected connection: {args}"),
    )
    with pytest.raises(SourcePipelineError) as error:
        transport.open(URL, timeout=1.0)
    assert error.value.code == "SOURCE_DNS_UNSAFE"


def test_urllib_http_error_close_failure_does_not_replace_primary_error():
    class ClosingBody:
        def close(self):
            raise OSError("close failed")

    class ErrorOpener:
        def open(self, request, *, timeout):
            del request, timeout
            raise source_sdk.urllib.error.HTTPError(
                URL, 503, "unavailable", {}, ClosingBody()
            )

    def public_dns(*args, **kwargs):
        del args, kwargs
        return [(2, None, None, None, ("8.8.8.8", 443))]

    transport = UrllibTransport(opener=ErrorOpener(), resolver=public_dns)
    with pytest.raises(SourcePipelineError) as raised:
        transport.open(URL, timeout=1.0)
    assert raised.value.code == "SOURCE_UNAVAILABLE"


def test_unsafe_artifact_paths_include_windows_devices_and_trailing_suffix(tmp_path: Path):
    for logical_name in ("../escape", "CON.jsonl", "report.", "report "):
        source = _source(b"payload", logical_name=logical_name)
        with pytest.raises(SourcePipelineError) as error:
            artifact_cache_path(source, REVISION, logical_name, cache_root=tmp_path)
        assert error.value.code == "SOURCE_PATH_INVALID"


def test_restricted_requires_injected_verifier_and_records_evidence_on_all_receipts(
    tmp_path: Path,
):
    body = b"payload"
    pending = _source(body, status="pending")
    restricted_payload = _source(body, status="restricted").model_dump(mode="json")
    restricted_payload["license"]["code"].update({
        "declared_ids": ["Apache-2.0"],
        "verified_spdx": "Apache-2.0",
        "evidence_urls": ["https://example.test/code-license"],
    })
    restricted = SourceSpec.model_validate(restricted_payload)
    transport = FakeTransport([_response(body)])

    with pytest.raises(SourcePipelineError) as pending_error:
        _fetch(tmp_path / "pending", pending, transport, override=OVERRIDE)
    assert pending_error.value.code == "SOURCE_LICENSE_BLOCKED"
    with pytest.raises(SourcePipelineError) as restricted_error:
        _fetch(tmp_path / "restricted", restricted, transport, override=OVERRIDE)
    assert restricted_error.value.code == "SOURCE_APPROVAL_REQUIRED"
    assert transport.calls == []

    verifier_calls = []

    def verifier(source, action, evidence):
        verifier_calls.append((source.id, action, evidence.ticket))
        return True

    receipt = _fetch(
        tmp_path / "accepted",
        restricted,
        transport,
        override=OVERRIDE,
        approval_verifier=verifier,
    )
    expected_evidence = OVERRIDE.model_dump(mode="json")
    assert receipt.cached is False
    assert receipt.approval_evidence == expected_evidence
    assert receipt.to_dict()["approval_evidence"] == expected_evidence
    assert transport.calls == [(URL, 2.5)]

    cached = _fetch(
        tmp_path / "accepted",
        restricted,
        transport,
        override=OVERRIDE,
        approval_verifier=verifier,
    )
    assert cached.cached is True
    assert cached.approval_evidence == expected_evidence
    assert transport.calls == [(URL, 2.5)]
    assert verifier_calls == [
        (restricted.id, "fetch", "GOV-42"),
        (restricted.id, "fetch", "GOV-42"),
    ]

    with pytest.raises(SourcePipelineError) as unverified_read:
        read_cached_artifact(
            restricted,
            REVISION,
            "dataset.jsonl",
            cache_root=tmp_path / "accepted",
            override=OVERRIDE,
        )
    assert unverified_read.value.code == "SOURCE_APPROVAL_REQUIRED"
    assert read_cached_artifact(
        restricted,
        REVISION,
        "dataset.jsonl",
        cache_root=tmp_path / "accepted",
        override=OVERRIDE,
        approval_verifier=verifier,
    ) == body


@pytest.mark.parametrize("verifier_result", [False, None, 0, 1, "true", object()])
def test_restricted_verifier_requires_literal_true_and_never_calls_transport(
    tmp_path: Path, verifier_result: object
):
    source = _source(b"payload", status="restricted")
    transport = FakeTransport()

    def verifier(source_arg, action, evidence):
        del source_arg, action, evidence
        return verifier_result

    with pytest.raises(SourcePipelineError) as raised:
        _fetch(
            tmp_path,
            source,
            transport,
            override=OVERRIDE,
            approval_verifier=verifier,
        )
    assert raised.value.code == "SOURCE_APPROVAL_REQUIRED"
    assert transport.calls == []


def test_restricted_verifier_exception_invalid_data_and_binding_fail_closed(tmp_path: Path):
    source = _source(b"payload", status="restricted")
    transport = FakeTransport()
    verifier_calls: list[bool] = []

    def exploding(source_arg, action, evidence):
        del source_arg, action, evidence
        raise RuntimeError("verifier unavailable")

    with pytest.raises(SourcePipelineError) as raised:
        _fetch(
            tmp_path / "exception",
            source,
            transport,
            override=OVERRIDE,
            approval_verifier=exploding,
        )
    assert raised.value.code == "SOURCE_APPROVAL_REQUIRED"

    def approving(source_arg, action, evidence):
        del source_arg, action, evidence
        verifier_calls.append(True)
        return True

    invalid_data = {**OVERRIDE.model_dump(), "override_verified": True}
    with pytest.raises(SourcePipelineError) as invalid:
        _fetch(
            tmp_path / "invalid",
            source,
            transport,
            override=invalid_data,
            approval_verifier=approving,
        )
    assert invalid.value.code == "SOURCE_APPROVAL_REQUIRED"
    assert verifier_calls == []

    for evidence in (
        OVERRIDE.model_copy(update={"source_id": "cmmlu"}),
        OVERRIDE.model_copy(update={"actions": ["import"]}),
        OVERRIDE.model_copy(
            update={
                "issued_at": "2020-01-01T00:00:00Z",
                "expires_at": "2021-01-01T00:00:00Z",
            }
        ),
    ):
        with pytest.raises(SourcePipelineError) as binding:
            _fetch(
                tmp_path / "binding",
                source,
                transport,
                override=evidence,
                approval_verifier=approving,
            )
        assert binding.value.code == "SOURCE_APPROVAL_REQUIRED"
    assert transport.calls == []


def test_public_sdk_has_no_verified_boolean_escape_hatch(tmp_path: Path):
    source = _source(b"payload", status="restricted")
    transport = FakeTransport()
    fetcher = SafeFetcher(cache_root=tmp_path, transport=transport)
    fetch_kwargs: dict[str, object] = {
        "entrypoint": "cli",
        "timeout": 1.0,
        "max_bytes": 1024,
        "override": OVERRIDE,
        "override_verified": True,
    }
    with pytest.raises(TypeError, match="override_verified"):
        fetcher.fetch(source, REVISION, "dataset.jsonl", **fetch_kwargs)

    read_kwargs: dict[str, object] = {
        "cache_root": tmp_path,
        "override": OVERRIDE,
        "override_verified": True,
    }
    with pytest.raises(TypeError, match="override_verified"):
        read_cached_artifact(source, REVISION, "dataset.jsonl", **read_kwargs)
    assert transport.calls == []


def test_json_and_jsonl_reject_duplicate_keys_nonfinite_numbers_and_recursion(
    monkeypatch,
):
    for raw in (
        b'{"id": 1, "id": 2}',
        b'{"value": NaN}',
        b'{"value": Infinity}',
        b'{"value": 1e999}',
    ):
        with pytest.raises(SourcePipelineError) as raised:
            parse_json(raw)
        assert raised.value.code == "SOURCE_PARSE_FAILED"
    with pytest.raises(SourcePipelineError) as jsonl_error:
        parse_jsonl(b'{"id": 1, "id": 2}\n')
    assert jsonl_error.value.code == "SOURCE_PARSE_FAILED"

    def recursive(*args, **kwargs):
        del args, kwargs
        raise RecursionError("too deep")

    monkeypatch.setattr(source_sdk.json, "loads", recursive)
    with pytest.raises(SourcePipelineError) as recursion:
        parse_json(b"{}")
    assert recursion.value.code == "SOURCE_PARSE_FAILED"


def test_json_depth_node_and_raw_size_limits(monkeypatch):
    monkeypatch.setattr(source_sdk, "MAX_JSON_DEPTH", 3)
    with pytest.raises(SourcePipelineError) as depth:
        parse_json(b'[[[[]]]]')
    assert depth.value.code == "SOURCE_PARSE_LIMIT"

    monkeypatch.setattr(source_sdk, "MAX_JSON_NODES", 3)
    with pytest.raises(SourcePipelineError) as nodes:
        parse_json(b'[1,2,3]')
    assert nodes.value.code == "SOURCE_PARSE_LIMIT"

    monkeypatch.setattr(source_sdk, "MAX_ARTIFACT_BYTES", 3)
    with pytest.raises(SourcePipelineError) as size:
        parse_json(b"null")
    assert size.value.code == "SOURCE_TOO_LARGE"


def test_csv_row_and_column_limits_and_strict_shape(monkeypatch):
    assert parse_csv(b"id,name\n1,alpha\n") == [{"id": "1", "name": "alpha"}]
    with pytest.raises(SourcePipelineError) as duplicate:
        parse_csv(b"id,id\n1,2\n")
    assert duplicate.value.code == "SOURCE_PARSE_FAILED"

    monkeypatch.setattr(source_sdk, "MAX_CSV_COLUMNS", 2)
    with pytest.raises(SourcePipelineError) as columns:
        parse_csv(b"a,b,c\n1,2,3\n")
    assert columns.value.code == "SOURCE_PARSE_LIMIT"

    monkeypatch.setattr(source_sdk, "MAX_CSV_ROWS", 1)
    with pytest.raises(SourcePipelineError) as rows:
        parse_csv(b"id\n1\n2\n")
    assert rows.value.code == "SOURCE_PARSE_LIMIT"


def test_artifact_dispatch_rejects_executable_or_archive_formats():
    assert parse_json(b'{"ok": true}') == {"ok": True}
    assert parse_jsonl(b'{"id": 1}\n\n{"id": 2}\n') == [{"id": 1}, {"id": 2}]
    assert parse_artifact_bytes(b'{"ok": true}', "json") == {"ok": True}
    with pytest.raises(SourcePipelineError) as scalar:
        parse_jsonl(b"1\n")
    assert scalar.value.code == "SOURCE_PARSE_FAILED"
    for unsafe_format in ("pickle", "zip", "tar"):
        with pytest.raises(SourcePipelineError) as unsupported:
            parse_artifact_bytes(b"irrelevant", unsafe_format)
        assert unsupported.value.code == "SOURCE_FORMAT_UNSUPPORTED"


def test_parquet_metadata_limits_run_before_materialization(monkeypatch):
    read_calls: list[bool] = []

    class Metadata:
        num_rows = 2
        num_columns = 1
        num_row_groups = 1
        total_uncompressed_size = 16

    class Table:
        def to_pylist(self):
            return [{"id": 1}, {"id": 2}]

    class ParquetFile:
        def __init__(self, buffer):
            del buffer
            self.metadata = Metadata()

        def read(self):
            read_calls.append(True)
            return Table()

    class Arrow:
        @staticmethod
        def BufferReader(raw):
            return raw

    class Parquet:
        pass

    Parquet.ParquetFile = ParquetFile
    modules = {"pyarrow": Arrow, "pyarrow.parquet": Parquet}

    def loader(name: str):
        return modules[name]

    assert parse_parquet(b"parquet", module_loader=loader) == [{"id": 1}, {"id": 2}]
    assert read_calls == [True]

    read_calls.clear()
    monkeypatch.setattr(source_sdk, "MAX_PARQUET_ROWS", 1)
    with pytest.raises(SourcePipelineError) as rows:
        parse_parquet(b"parquet", module_loader=loader)
    assert rows.value.code == "SOURCE_PARSE_LIMIT"
    assert read_calls == []


def test_parquet_uncompressed_size_limit_runs_before_materialization(monkeypatch):
    read_calls: list[bool] = []

    class Metadata:
        num_rows = 1
        num_columns = 1
        num_row_groups = 1
        total_uncompressed_size = 9

    class ParquetFile:
        def __init__(self, buffer):
            del buffer
            self.metadata = Metadata()

        def read(self):
            read_calls.append(True)
            raise AssertionError("must not materialize")

    class Arrow:
        BufferReader = staticmethod(lambda raw: raw)

    class Parquet:
        pass

    Parquet.ParquetFile = ParquetFile
    modules = {"pyarrow": Arrow, "pyarrow.parquet": Parquet}
    monkeypatch.setattr(source_sdk, "MAX_PARQUET_UNCOMPRESSED_BYTES", 8)
    with pytest.raises(SourcePipelineError) as size:
        parse_parquet(b"parquet", module_loader=lambda name: modules[name])
    assert size.value.code == "SOURCE_PARSE_LIMIT"
    assert read_calls == []


def test_parquet_missing_dependency_fails_closed_without_fallback():
    calls: list[str] = []

    def missing(name: str) -> object:
        calls.append(name)
        raise ModuleNotFoundError(name)

    with pytest.raises(SourcePipelineError) as error:
        parse_parquet(b"not parquet", module_loader=missing)
    assert error.value.code == "SOURCE_DEPENDENCY_MISSING"
    assert error.value.details == {"dependency": "pyarrow"}
    assert calls == ["pyarrow"]
