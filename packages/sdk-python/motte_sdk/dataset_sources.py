"""Offline source registry and safe, explicitly invoked dataset artifact fetching."""
from __future__ import annotations

import csv
import hashlib
import http.client
import importlib
import ipaddress
import json
import math
import os
import re
import socket
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from io import StringIO
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from motte_contracts.dataset_sources import (
    MAX_ARTIFACT_BYTES,
    ArtifactFormat,
    SourceAction,
    SourceActionBlockedError,
    SourceArtifact,
    SourceOverrideEvidence,
    SourceSpec,
    load_source_registry as load_contract_registry,
    require_source_action,
)

SOURCE_REGISTRY_ENV = "MOTTE_DIRECT_LLM_SOURCE_REGISTRY"
SOURCE_CACHE_ENV = "MOTTE_DIRECT_LLM_CACHE_DIR"
DEFAULT_CACHE_ROOT = Path("var") / "datasets" / "direct-llm"
DEFAULT_REGISTRY_SUBDIR = Path("datasets") / "direct-llm" / "sources"
USER_AGENT = "motteavl-direct-llm-source/1"
MAX_JSON_DEPTH = 128
MAX_JSON_NODES = 1_000_000
MAX_CSV_ROWS = 1_000_000
MAX_CSV_COLUMNS = 2048
MAX_PARQUET_ROWS = 1_000_000
MAX_PARQUET_COLUMNS = 2048
MAX_PARQUET_UNCOMPRESSED_BYTES = MAX_ARTIFACT_BYTES
_SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FORBIDDEN_REVISIONS = frozenset({"head", "latest", "main", "master", "tip", "trunk"})
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)
_FETCH_CHUNK_SIZE = 64 * 1024


class SourcePipelineError(RuntimeError):
    """Structured source-platform failure suitable for CLI error mapping."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.details = dict(details or {})
        super().__init__(f"{code}: {message}")

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ApprovalVerifier(Protocol):
    """Trusted capability that authenticates externally issued approval evidence."""

    def __call__(
        self,
        source: SourceSpec,
        action: SourceAction,
        evidence: SourceOverrideEvidence,
    ) -> bool: ...


@dataclass(frozen=True)
class ArtifactManifestEntry:
    logical_name: str
    url: str
    format: ArtifactFormat | None
    sha256: str
    bytes: int
    max_bytes: int


@dataclass(frozen=True)
class ArtifactManifest:
    source_id: str
    revision: str
    artifacts: tuple[ArtifactManifestEntry, ...]

    def canonical_payload(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "revision": self.revision,
            "artifacts": [asdict(artifact) for artifact in self.artifacts],
        }

    @property
    def sha256(self) -> str:
        payload = json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class FetchReceipt:
    source_id: str
    revision: str
    artifact: ArtifactManifestEntry
    artifact_manifest_sha256: str
    cache_path: str
    cached: bool
    approval_evidence: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "revision": self.revision,
            "artifact": asdict(self.artifact),
            "artifact_manifest_sha256": self.artifact_manifest_sha256,
            "cache_path": self.cache_path,
            "cached": self.cached,
            "approval_evidence": self.approval_evidence,
        }


class RevisionResolver(Protocol):
    """Resolver interface; implementations must return an immutable revision."""

    def resolve(self, source: SourceSpec, requested: str | None) -> str: ...


class ManifestRevisionResolver:
    """Offline resolver using an explicit request or the manifest-pinned value."""

    def resolve(self, source: SourceSpec, requested: str | None) -> str:
        candidate = requested if requested is not None else source.upstream.revision.value
        if candidate is None:
            raise SourcePipelineError(
                "SOURCE_REVISION_INVALID",
                f"source {source.id!r} has no pinned revision",
            )
        return candidate


class TransportResponse(Protocol):
    status: int
    headers: Mapping[str, str]
    final_url: str

    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


class SourceTransport(Protocol):
    def open(self, url: str, *, timeout: float) -> TransportResponse: ...


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class _UrllibResponse:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.status = int(response.status)
        self.headers: Mapping[str, str] = {
            str(key).lower(): str(value) for key, value in response.headers.items()
        }
        self.final_url = str(response.geturl())

    def read(self, size: int = -1) -> bytes:
        return cast(bytes, self._response.read(size))

    def close(self) -> None:
        self._response.close()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._pinned_address = address

    def connect(self) -> None:
        connection = cast(Any, self)
        connection.sock = connection._create_connection(
            (self._pinned_address, self.port),
            self.timeout,
            connection.source_address,
        )
        if connection._tunnel_host:
            raise OSError("HTTP CONNECT tunnels are forbidden for source fetching")
        connection.sock = connection._context.wrap_socket(
            connection.sock,
            server_hostname=self.host,
        )


class _PinnedResponse:
    def __init__(self, response: Any, connection: Any, url: str) -> None:
        self._response = response
        self._connection = connection
        self.status = int(response.status)
        self.headers: Mapping[str, str] = {
            str(key).lower(): str(value) for key, value in response.getheaders()
        }
        self.final_url = url

    def read(self, size: int = -1) -> bytes:
        return cast(bytes, self._response.read(size))

    def close(self) -> None:
        response_error: Exception | None = None
        try:
            self._response.close()
        except Exception as error:
            response_error = error
        try:
            self._connection.close()
        except Exception:
            if response_error is None:
                raise
        if response_error is not None:
            raise response_error


def _pinned_connection(host: str, port: int, address: str, timeout: float) -> Any:
    return _PinnedHTTPSConnection(host, port, address, timeout)


class UrllibTransport:
    """HTTPS transport that disables proxies and connects only to DNS-validated addresses."""

    def __init__(
        self,
        *,
        opener: Any | None = None,
        resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
        connection_factory: Callable[[str, int, str, float], Any] = _pinned_connection,
    ) -> None:
        self._opener = opener
        self._resolver = resolver
        self._connection_factory = connection_factory

    def _validate_dns(self, url: str) -> tuple[str, ...]:
        parsed = urlsplit(url)
        host = parsed.hostname
        if host is None:
            raise SourcePipelineError("SOURCE_URL_INVALID", "source URL has no hostname")
        try:
            addresses = self._resolver(host, parsed.port or 443, type=socket.SOCK_STREAM)
        except OSError as error:
            raise SourcePipelineError(
                "SOURCE_UNAVAILABLE", f"source DNS resolution failed: {error}", details={"host": host}
            ) from error
        if not addresses:
            raise SourcePipelineError(
                "SOURCE_UNAVAILABLE", "source DNS resolution returned no addresses", details={"host": host}
            )
        resolved: list[str] = []
        for address in addresses:
            socket_address = cast(tuple[Any, ...], address[4])
            ip_text = str(socket_address[0]).split("%", 1)[0]
            try:
                ip = ipaddress.ip_address(ip_text)
            except ValueError as error:
                raise SourcePipelineError(
                    "SOURCE_DNS_UNSAFE", f"source DNS returned an invalid address: {ip_text!r}"
                ) from error
            if not ip.is_global:
                raise SourcePipelineError(
                    "SOURCE_DNS_UNSAFE",
                    "all source DNS addresses must be globally routable",
                    details={"host": host, "address": str(ip)},
                )
            canonical = str(ip)
            if canonical not in resolved:
                resolved.append(canonical)
        if not resolved:
            raise SourcePipelineError("SOURCE_UNAVAILABLE", "source DNS produced no usable addresses")
        return tuple(resolved)

    def _open_injected(self, url: str, timeout: float) -> TransportResponse:
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"},
        )
        opener = self._opener
        assert opener is not None
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            try:
                if 300 <= error.code < 400:
                    raise SourcePipelineError(
                        "SOURCE_REDIRECT_FORBIDDEN",
                        f"source redirect rejected with HTTP {error.code}",
                        details={"url": url, "location": error.headers.get("Location")},
                    ) from error
                raise SourcePipelineError(
                    "SOURCE_UNAVAILABLE",
                    f"source returned HTTP {error.code}",
                    details={"url": url, "status": error.code},
                ) from error
            finally:
                try:
                    error.close()
                except Exception:
                    pass
        except (OSError, urllib.error.URLError) as error:
            raise SourcePipelineError(
                "SOURCE_UNAVAILABLE", f"source request failed: {error}", details={"url": url}
            ) from error
        return _UrllibResponse(response)

    def open(self, url: str, *, timeout: float) -> TransportResponse:
        _validate_https_url(url)
        addresses = self._validate_dns(url)
        if self._opener is not None:
            return self._open_injected(url, timeout)

        parsed = urlsplit(url)
        assert parsed.hostname is not None
        port = parsed.port or 443
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        failures: list[str] = []
        for address in addresses:
            connection = self._connection_factory(parsed.hostname, port, address, timeout)
            try:
                connection.request(
                    "GET",
                    target,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"},
                )
                return _PinnedResponse(connection.getresponse(), connection, url)
            except (OSError, http.client.HTTPException) as error:
                failures.append(f"{address}: {error}")
                try:
                    connection.close()
                except Exception:
                    pass
        raise SourcePipelineError(
            "SOURCE_UNAVAILABLE",
            "source request failed for every validated address",
            details={"url": url, "failures": failures},
        )


def source_registry_path(directory: str | Path | None = None) -> Path:
    if directory is not None:
        return Path(directory)
    override = os.environ.get(SOURCE_REGISTRY_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / DEFAULT_REGISTRY_SUBDIR


def source_cache_root(directory: str | Path | None = None) -> Path:
    if directory is not None:
        return Path(directory)
    override = os.environ.get(SOURCE_CACHE_ENV)
    return Path(override) if override else DEFAULT_CACHE_ROOT


def load_registry(directory: str | Path | None = None) -> dict[str, SourceSpec]:
    try:
        return cast(
            dict[str, SourceSpec], load_contract_registry(source_registry_path(directory))
        )
    except OSError as error:
        raise SourcePipelineError(
            "SOURCE_REGISTRY_IO", f"cannot read source registry: {error}"
        ) from error
    except (ValueError, RecursionError) as error:
        raise SourcePipelineError(
            "SOURCE_MANIFEST_INVALID", f"invalid source registry: {error}"
        ) from error


def list_sources(directory: str | Path | None = None) -> list[SourceSpec]:
    return [item for _, item in sorted(load_registry(directory).items())]


def inspect_source(source_id: str, directory: str | Path | None = None) -> SourceSpec:
    source = load_registry(directory).get(source_id)
    if source is None:
        raise SourcePipelineError(
            "SOURCE_NOT_FOUND", f"source is not registered: {source_id}", details={"id": source_id}
        )
    return source


def _forbid_floating_revision(revision: str) -> None:
    if revision.strip().lower() in _FORBIDDEN_REVISIONS:
        raise SourcePipelineError(
            "SOURCE_REVISION_INVALID", f"floating revision is forbidden: {revision!r}"
        )


def validate_revision(
    source: SourceSpec,
    revision: str,
    *,
    immutable_pattern: str | None = None,
) -> str:
    """Validate a pinned source revision and return its canonical representation."""
    if not isinstance(revision, str) or not revision.strip():
        raise SourcePipelineError("SOURCE_REVISION_INVALID", "revision must be a non-empty string")
    candidate = revision.strip()
    _forbid_floating_revision(candidate)

    kind = source.upstream.revision.kind
    if kind in {"git-commit", "git-sha", "hf-commit"} and _SHA40.fullmatch(candidate):
        return candidate.lower()
    if immutable_pattern is not None:
        try:
            if re.fullmatch(immutable_pattern, candidate):
                return candidate
        except re.error as error:
            raise SourcePipelineError(
                "SOURCE_REVISION_INVALID", f"invalid immutable revision pattern: {error}"
            ) from error
    pinned = source.upstream.revision.value
    if kind not in {"git-commit", "git-sha", "hf-commit"} and pinned == candidate:
        return candidate
    expectation = "a 40-character hexadecimal commit"
    if immutable_pattern is not None:
        expectation += " or the configured immutable pattern"
    raise SourcePipelineError(
        "SOURCE_REVISION_INVALID",
        f"revision for {source.id!r} must be {expectation}",
        details={"kind": kind, "revision": candidate},
    )


def resolve_revision(
    source: SourceSpec,
    requested: str | None = None,
    *,
    resolver: RevisionResolver | None = None,
    immutable_pattern: str | None = None,
) -> str:
    if requested is not None:
        _forbid_floating_revision(requested)
    effective_resolver = resolver or ManifestRevisionResolver()
    resolved = validate_revision(
        source,
        effective_resolver.resolve(source, requested),
        immutable_pattern=immutable_pattern,
    )
    pinned = source.upstream.revision.value
    if pinned is not None:
        pinned_value = validate_revision(source, pinned, immutable_pattern=immutable_pattern)
        if resolved != pinned_value:
            raise SourcePipelineError(
                "SOURCE_REVISION_INVALID",
                "resolved revision does not match the manifest-pinned revision",
                details={"requested": resolved, "pinned": pinned_value},
            )
    return resolved


def _safe_segment(value: str, *, field: str) -> str:
    device_stem = value.split(".", 1)[0].casefold()
    if (
        value in {".", ".."}
        or value.endswith((".", " "))
        or not _SAFE_SEGMENT.fullmatch(value)
        or device_stem in _WINDOWS_DEVICE_NAMES
    ):
        raise SourcePipelineError(
            "SOURCE_PATH_INVALID", f"unsafe {field} path segment: {value!r}"
        )
    return value


def _validate_https_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise SourcePipelineError("SOURCE_URL_INVALID", f"invalid source URL: {error}") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise SourcePipelineError(
            "SOURCE_URL_INVALID", "source artifacts must use an absolute credential-free HTTPS URL"
        )
    if port is not None and not 1 <= port <= 65535:
        raise SourcePipelineError("SOURCE_URL_INVALID", "source URL port is invalid")
    try:
        literal = ipaddress.ip_address(parsed.hostname.split("%", 1)[0])
    except ValueError:
        return
    if not literal.is_global:
        raise SourcePipelineError(
            "SOURCE_URL_INVALID", "non-global IP literals are forbidden in source URLs"
        )


def _artifact(source: SourceSpec, logical_name: str) -> SourceArtifact:
    for artifact in source.upstream.artifacts:
        if artifact.logical_name == logical_name:
            return artifact
    raise SourcePipelineError(
        "SOURCE_ARTIFACT_NOT_FOUND",
        f"artifact is not registered for {source.id!r}: {logical_name}",
    )


def _manifest_entry(artifact: SourceArtifact) -> ArtifactManifestEntry:
    missing = [
        name
        for name, value in (
            ("url", artifact.url),
            ("format", artifact.format),
            ("sha256", artifact.sha256),
            ("bytes", artifact.bytes),
            ("max_bytes", artifact.max_bytes),
        )
        if value is None
    ]
    if missing:
        raise SourcePipelineError(
            "SOURCE_NOT_READY",
            f"artifact {artifact.logical_name!r} is missing immutable evidence: {', '.join(missing)}",
        )
    assert artifact.url is not None
    assert artifact.sha256 is not None
    assert artifact.bytes is not None
    assert artifact.max_bytes is not None
    return ArtifactManifestEntry(
        logical_name=artifact.logical_name,
        url=artifact.url,
        format=artifact.format,
        sha256=artifact.sha256,
        bytes=artifact.bytes,
        max_bytes=artifact.max_bytes,
    )


def build_artifact_manifest(source: SourceSpec, revision: str) -> ArtifactManifest:
    return ArtifactManifest(
        source_id=source.id,
        revision=revision,
        artifacts=tuple(_manifest_entry(item) for item in source.upstream.artifacts if item.required),
    )


def artifact_cache_path(
    source: SourceSpec,
    revision: str,
    logical_name: str,
    *,
    cache_root: str | Path | None = None,
) -> Path:
    artifact = _artifact(source, logical_name)
    if artifact.sha256 is None:
        raise SourcePipelineError(
            "SOURCE_NOT_READY", f"artifact {logical_name!r} has no registered SHA-256"
        )
    return (
        source_cache_root(cache_root)
        / _safe_segment(source.id, field="source id")
        / _safe_segment(revision, field="revision")
        / _safe_segment(artifact.sha256, field="artifact hash namespace")
        / _safe_segment(logical_name, field="artifact")
    )


def _approval_evidence(
    override: SourceOverrideEvidence | Mapping[str, object] | None,
) -> SourceOverrideEvidence | None:
    if override is None:
        return None
    try:
        payload = override.model_dump() if isinstance(override, SourceOverrideEvidence) else override
        return SourceOverrideEvidence.model_validate(payload)
    except (TypeError, ValueError):
        return None


def _gate(
    source: SourceSpec,
    action: SourceAction,
    override: SourceOverrideEvidence | Mapping[str, object] | None,
    approval_verifier: ApprovalVerifier | None,
) -> SourceOverrideEvidence | None:
    evidence = _approval_evidence(override)
    verified = False
    if (
        source.governance.status == "restricted"
        and evidence is not None
        and approval_verifier is not None
    ):
        try:
            verified = approval_verifier(source, action, evidence) is True
        except Exception:
            verified = False
    try:
        require_source_action(
            source,
            action,
            override=evidence,
            override_verified=verified,
        )
    except SourceActionBlockedError as error:
        decision = error.decision
        raise SourcePipelineError(
            decision.code or "SOURCE_LICENSE_BLOCKED",
            "; ".join(decision.reasons),
            details={"source_id": source.id, "action": action},
        ) from error
    if source.governance.status == "restricted" and verified and evidence is not None:
        return SourceOverrideEvidence.model_validate(evidence.model_dump())
    return None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    expected = name.lower()
    return next((str(value) for key, value in headers.items() if key.lower() == expected), None)


def _content_length(headers: Mapping[str, str]) -> int | None:
    value = _header(headers, "content-length")
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as error:
        raise SourcePipelineError(
            "SOURCE_SIZE_MISMATCH", f"invalid Content-Length header: {value!r}"
        ) from error
    if parsed < 0:
        raise SourcePipelineError("SOURCE_SIZE_MISMATCH", "Content-Length cannot be negative")
    return parsed


def _is_link_or_junction(path: Path) -> bool:
    try:
        return path.is_symlink() or path.is_junction()
    except OSError as error:
        raise SourcePipelineError(
            "SOURCE_CACHE_IO", f"cannot inspect cache path: {error}"
        ) from error


def validate_cache_path_safety(path: Path, root: Path) -> None:
    absolute_root = root.absolute()
    absolute_path = path.absolute()
    if not absolute_path.is_relative_to(absolute_root):
        raise SourcePipelineError("SOURCE_PATH_INVALID", "cache path escapes the configured root")
    for ancestor in (*reversed(absolute_root.parents), absolute_root):
        if _is_link_or_junction(ancestor):
            raise SourcePipelineError(
                "SOURCE_CACHE_UNSAFE", f"cache root has an unsafe ancestor: {ancestor}"
            )
    current = absolute_root
    for part in absolute_path.relative_to(absolute_root).parts:
        current = current / part
        if _is_link_or_junction(current):
            raise SourcePipelineError(
                "SOURCE_CACHE_UNSAFE", f"cache path contains a symlink or junction: {current}"
            )


def read_verified_cache_bytes(
    path: Path,
    entry: ArtifactManifestEntry,
    *,
    cache_root: Path | None = None,
) -> bytes:
    if cache_root is not None:
        validate_cache_path_safety(path, cache_root)
    try:
        if _is_link_or_junction(path):
            raise SourcePipelineError(
                "SOURCE_CACHE_UNSAFE", f"cached artifact is a symlink or junction: {path}"
            )
        if not path.is_file():
            raise SourcePipelineError(
                "SOURCE_CACHE_MISS", f"cached artifact does not exist: {path}"
            )
    except SourcePipelineError:
        raise
    except OSError as error:
        raise SourcePipelineError(
            "SOURCE_CACHE_IO", f"cannot inspect cached artifact: {error}"
        ) from error
    if entry.bytes > MAX_ARTIFACT_BYTES or entry.max_bytes > MAX_ARTIFACT_BYTES:
        raise SourcePipelineError("SOURCE_TOO_LARGE", "artifact exceeds the global 512 MiB limit")
    digest = hashlib.sha256()
    payload = bytearray()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(_FETCH_CHUNK_SIZE):
                payload.extend(chunk)
                digest.update(chunk)
                if len(payload) > min(entry.max_bytes, MAX_ARTIFACT_BYTES):
                    raise SourcePipelineError(
                        "SOURCE_CACHE_CORRUPT", "cached artifact exceeds max_bytes"
                    )
    except SourcePipelineError:
        raise
    except OSError as error:
        raise SourcePipelineError(
            "SOURCE_CACHE_IO", f"cannot read cached artifact: {error}"
        ) from error
    if len(payload) != entry.bytes or digest.hexdigest() != entry.sha256:
        raise SourcePipelineError(
            "SOURCE_CACHE_CORRUPT",
            "cached artifact size or SHA-256 does not match the manifest",
            details={
                "expected_bytes": entry.bytes,
                "actual_bytes": len(payload),
                "expected_sha256": entry.sha256,
                "actual_sha256": digest.hexdigest(),
            },
        )
    return bytes(payload)


def read_cached_artifact(
    source: SourceSpec,
    revision: str,
    logical_name: str,
    *,
    cache_root: str | Path | None = None,
    override: SourceOverrideEvidence | Mapping[str, object] | None = None,
    approval_verifier: ApprovalVerifier | None = None,
) -> bytes:
    _gate(source, "import", override, approval_verifier)
    resolved = resolve_revision(source, revision)
    entry = _manifest_entry(_artifact(source, logical_name))
    path = artifact_cache_path(source, resolved, logical_name, cache_root=cache_root)
    root = source_cache_root(cache_root)
    validate_cache_path_safety(path, root)
    return read_verified_cache_bytes(path, entry, cache_root=root)


class SafeFetcher:
    """Fetch registered artifacts through a bounded, atomic, revalidating cache."""

    def __init__(
        self,
        *,
        cache_root: str | Path | None = None,
        transport: SourceTransport | None = None,
        approval_verifier: ApprovalVerifier | None = None,
    ) -> None:
        self.cache_root = source_cache_root(cache_root)
        self.transport = transport or UrllibTransport()
        self.approval_verifier = approval_verifier

    def fetch(
        self,
        source: SourceSpec,
        revision: str,
        logical_name: str,
        *,
        entrypoint: str,
        timeout: float,
        max_bytes: int,
        url: str | None = None,
        override: SourceOverrideEvidence | Mapping[str, object] | None = None,
    ) -> FetchReceipt:
        if entrypoint != "cli" or source.safety.network_entrypoint != "cli-only":
            raise SourcePipelineError(
                "SOURCE_NETWORK_DISABLED", "source fetching is available only to an explicit CLI entrypoint"
            )
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            raise SourcePipelineError("SOURCE_LIMIT_INVALID", "timeout must be a finite positive number")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
            or max_bytes > MAX_ARTIFACT_BYTES
        ):
            raise SourcePipelineError(
                "SOURCE_LIMIT_INVALID", "max_bytes must be between 1 and 512 MiB"
            )

        approval = _gate(source, "fetch", override, self.approval_verifier)
        approval_payload = approval.model_dump(mode="json") if approval is not None else None
        resolved = resolve_revision(source, revision)
        artifact = _artifact(source, logical_name)
        entry = _manifest_entry(artifact)
        requested_url = url or entry.url
        _validate_https_url(requested_url)
        if requested_url != entry.url:
            raise SourcePipelineError(
                "SOURCE_URL_NOT_REGISTERED", "requested URL does not exactly match the source manifest"
            )
        if max_bytes > entry.max_bytes:
            raise SourcePipelineError(
                "SOURCE_LIMIT_INVALID", "requested max_bytes exceeds the registered artifact limit"
            )
        if entry.bytes > max_bytes or entry.bytes > MAX_ARTIFACT_BYTES:
            raise SourcePipelineError(
                "SOURCE_TOO_LARGE", "registered artifact size exceeds max_bytes"
            )

        manifest = build_artifact_manifest(source, resolved)
        target = artifact_cache_path(source, resolved, logical_name, cache_root=self.cache_root)
        validate_cache_path_safety(target, self.cache_root)
        if target.exists() or _is_link_or_junction(target):
            read_verified_cache_bytes(target, entry, cache_root=self.cache_root)
            return FetchReceipt(
                source.id,
                resolved,
                entry,
                manifest.sha256,
                str(target),
                cached=True,
                approval_evidence=approval_payload,
            )

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise SourcePipelineError(
                "SOURCE_CACHE_IO", f"cannot create cache directory: {error}"
            ) from error
        validate_cache_path_safety(target, self.cache_root)
        target = target.absolute()
        response: TransportResponse | None = None
        temporary_path: Path | None = None
        primary_error = False
        try:
            try:
                response = self.transport.open(requested_url, timeout=float(timeout))
            except SourcePipelineError:
                raise
            except OSError as error:
                raise SourcePipelineError(
                    "SOURCE_UNAVAILABLE", f"source request failed: {error}"
                ) from error
            if 300 <= response.status < 400 or response.final_url != requested_url:
                raise SourcePipelineError(
                    "SOURCE_REDIRECT_FORBIDDEN", "source redirects are forbidden"
                )
            if response.status != 200:
                raise SourcePipelineError(
                    "SOURCE_UNAVAILABLE", f"source returned HTTP {response.status}"
                )
            declared_length = _content_length(response.headers)
            if declared_length is not None:
                if declared_length > min(max_bytes, MAX_ARTIFACT_BYTES):
                    raise SourcePipelineError(
                        "SOURCE_TOO_LARGE", "Content-Length exceeds max_bytes"
                    )
                if declared_length != entry.bytes:
                    raise SourcePipelineError(
                        "SOURCE_SIZE_MISMATCH",
                        "Content-Length does not match the registered byte count",
                    )

            digest = hashlib.sha256()
            actual_bytes = 0
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=target.parent,
                    prefix=f".{target.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    while True:
                        try:
                            chunk = response.read(_FETCH_CHUNK_SIZE)
                        except OSError as error:
                            raise SourcePipelineError(
                                "SOURCE_UNAVAILABLE", f"source stream failed: {error}"
                            ) from error
                        if not chunk:
                            break
                        if not isinstance(chunk, bytes):
                            raise SourcePipelineError(
                                "SOURCE_UNAVAILABLE", "source transport returned a non-bytes chunk"
                            )
                        actual_bytes += len(chunk)
                        if actual_bytes > min(max_bytes, MAX_ARTIFACT_BYTES):
                            raise SourcePipelineError(
                                "SOURCE_TOO_LARGE", "source stream exceeds max_bytes"
                            )
                        if actual_bytes > entry.bytes:
                            raise SourcePipelineError(
                                "SOURCE_SIZE_MISMATCH",
                                "source stream exceeds the registered byte count",
                            )
                        digest.update(chunk)
                        temporary.write(chunk)
                    temporary.flush()
                    os.fsync(temporary.fileno())
            except SourcePipelineError:
                raise
            except OSError as error:
                raise SourcePipelineError(
                    "SOURCE_CACHE_IO", f"cannot write cache artifact: {error}"
                ) from error

            if declared_length is not None and actual_bytes < declared_length:
                raise SourcePipelineError(
                    "SOURCE_SHORT_READ", "source stream ended before Content-Length"
                )
            if actual_bytes < entry.bytes:
                raise SourcePipelineError(
                    "SOURCE_SHORT_READ", "source stream ended before the registered byte count"
                )
            actual_sha256 = digest.hexdigest()
            if actual_sha256 != entry.sha256:
                raise SourcePipelineError(
                    "SOURCE_HASH_MISMATCH",
                    "source SHA-256 does not match the manifest",
                    details={"expected": entry.sha256, "actual": actual_sha256},
                )
            validate_cache_path_safety(target, self.cache_root)
            try:
                os.replace(temporary_path, target)
            except OSError as error:
                raise SourcePipelineError(
                    "SOURCE_CACHE_IO", f"cannot atomically publish cache artifact: {error}"
                ) from error
            temporary_path = None
            return FetchReceipt(
                source.id,
                resolved,
                entry,
                manifest.sha256,
                str(target),
                cached=False,
                approval_evidence=approval_payload,
            )
        except BaseException:
            primary_error = True
            raise
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception as close_error:
                        if not primary_error:
                            raise SourcePipelineError(
                                "SOURCE_UNAVAILABLE", f"source response close failed: {close_error}"
                            ) from close_error
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass


def _ensure_raw_size(raw: bytes) -> None:
    if not isinstance(raw, bytes):
        raise SourcePipelineError("SOURCE_PARSE_FAILED", "artifact parser requires bytes")
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise SourcePipelineError("SOURCE_TOO_LARGE", "artifact exceeds the global 512 MiB limit")


def _utf8(raw: bytes, *, format_name: str) -> str:
    _ensure_raw_size(raw)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SourcePipelineError(
            "SOURCE_PARSE_FAILED", f"{format_name} artifact must be UTF-8"
        ) from error


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _strict_json(text: str, *, context: str) -> object:
    try:
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as error:
        raise SourcePipelineError(
            "SOURCE_PARSE_FAILED", f"invalid {context}: {error}"
        ) from error


def _validate_json_shape(value: object, *, node_budget: int) -> int:
    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > node_budget:
            raise SourcePipelineError(
                "SOURCE_PARSE_LIMIT", f"JSON exceeds the {MAX_JSON_NODES} node limit"
            )
        if depth > MAX_JSON_DEPTH:
            raise SourcePipelineError(
                "SOURCE_PARSE_LIMIT", f"JSON exceeds the {MAX_JSON_DEPTH} depth limit"
            )
        if isinstance(current, float) and not math.isfinite(current):
            raise SourcePipelineError(
                "SOURCE_PARSE_FAILED", "JSON numbers must be finite"
            )
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return nodes


def parse_json(raw: bytes) -> object:
    value = _strict_json(_utf8(raw, format_name="JSON"), context="JSON")
    _validate_json_shape(value, node_budget=MAX_JSON_NODES)
    return value


def parse_jsonl(raw: bytes) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    remaining_nodes = MAX_JSON_NODES
    for line_no, line in enumerate(_utf8(raw, format_name="JSONL").splitlines(), 1):
        if not line.strip():
            continue
        value = _strict_json(line, context=f"JSONL at line {line_no}")
        if not isinstance(value, dict):
            raise SourcePipelineError(
                "SOURCE_PARSE_FAILED", f"JSONL line {line_no} must be an object"
            )
        used = _validate_json_shape(value, node_budget=remaining_nodes)
        remaining_nodes -= used
        rows.append(cast(dict[str, object], value))
    if not rows:
        raise SourcePipelineError("SOURCE_PARSE_FAILED", "JSONL artifact contains no rows")
    return rows


def parse_csv(raw: bytes) -> list[dict[str, str]]:
    try:
        reader = csv.DictReader(StringIO(_utf8(raw, format_name="CSV"), newline=""))
        fields = reader.fieldnames
        if fields is None or not fields or any(not field for field in fields):
            raise SourcePipelineError("SOURCE_PARSE_FAILED", "CSV requires a non-empty header")
        if len(fields) > MAX_CSV_COLUMNS:
            raise SourcePipelineError(
                "SOURCE_PARSE_LIMIT", f"CSV exceeds the {MAX_CSV_COLUMNS} column limit"
            )
        if len(set(fields)) != len(fields):
            raise SourcePipelineError("SOURCE_PARSE_FAILED", "CSV header fields must be unique")
        rows: list[dict[str, str]] = []
        for line_no, row in enumerate(reader, 2):
            if len(rows) >= MAX_CSV_ROWS:
                raise SourcePipelineError(
                    "SOURCE_PARSE_LIMIT", f"CSV exceeds the {MAX_CSV_ROWS} row limit"
                )
            if None in row or any(value is None for value in row.values()):
                raise SourcePipelineError(
                    "SOURCE_PARSE_FAILED", f"CSV row {line_no} does not match the header"
                )
            rows.append({str(key): cast(str, value) for key, value in row.items()})
        return rows
    except SourcePipelineError:
        raise
    except (csv.Error, ValueError, RecursionError) as error:
        raise SourcePipelineError("SOURCE_PARSE_FAILED", f"invalid CSV: {error}") from error


def _parquet_uncompressed_bytes(metadata: Any) -> int:
    direct = getattr(metadata, "total_uncompressed_size", None)
    if isinstance(direct, int):
        return direct
    total = 0
    try:
        for row_group_index in range(int(metadata.num_row_groups)):
            row_group = metadata.row_group(row_group_index)
            for column_index in range(int(row_group.num_columns)):
                size = row_group.column(column_index).total_uncompressed_size
                if not isinstance(size, int) or size < 0:
                    raise ValueError("invalid Parquet column size")
                total += size
    except (AttributeError, TypeError, ValueError) as error:
        raise SourcePipelineError(
            "SOURCE_PARSE_FAILED", "Parquet metadata lacks uncompressed size evidence"
        ) from error
    return total


def parse_parquet(
    raw: bytes,
    *,
    module_loader: Callable[[str], object] = importlib.import_module,
) -> list[dict[str, object]]:
    _ensure_raw_size(raw)
    try:
        pyarrow = cast(Any, module_loader("pyarrow"))
        parquet = cast(Any, module_loader("pyarrow.parquet"))
    except (ImportError, ModuleNotFoundError) as error:
        raise SourcePipelineError(
            "SOURCE_DEPENDENCY_MISSING",
            "Parquet parsing requires the optional pyarrow dependency",
            details={"dependency": "pyarrow"},
        ) from error
    try:
        parquet_file = parquet.ParquetFile(pyarrow.BufferReader(raw))
        metadata = parquet_file.metadata
        rows = int(metadata.num_rows)
        columns = int(metadata.num_columns)
        uncompressed_bytes = _parquet_uncompressed_bytes(metadata)
        if rows < 0 or columns < 0 or uncompressed_bytes < 0:
            raise SourcePipelineError("SOURCE_PARSE_FAILED", "Parquet metadata is invalid")
        if rows > MAX_PARQUET_ROWS:
            raise SourcePipelineError(
                "SOURCE_PARSE_LIMIT", f"Parquet exceeds the {MAX_PARQUET_ROWS} row limit"
            )
        if columns > MAX_PARQUET_COLUMNS:
            raise SourcePipelineError(
                "SOURCE_PARSE_LIMIT", f"Parquet exceeds the {MAX_PARQUET_COLUMNS} column limit"
            )
        if uncompressed_bytes > MAX_PARQUET_UNCOMPRESSED_BYTES:
            raise SourcePipelineError(
                "SOURCE_PARSE_LIMIT", "Parquet uncompressed data exceeds the 512 MiB limit"
            )
        table = parquet_file.read()
        result = cast(list[dict[str, object]], table.to_pylist())
        if len(result) > rows:
            raise SourcePipelineError(
                "SOURCE_PARSE_FAILED", "Parquet materialized more rows than metadata declared"
            )
        return result
    except SourcePipelineError:
        raise
    except (ValueError, RecursionError) as error:
        raise SourcePipelineError(
            "SOURCE_PARSE_FAILED", f"invalid Parquet artifact: {error}"
        ) from error
    except Exception as error:
        raise SourcePipelineError(
            "SOURCE_PARSE_FAILED", f"invalid Parquet artifact: {error}"
        ) from error


def parse_artifact_bytes(
    raw: bytes,
    artifact_format: str,
    *,
    parquet_module_loader: Callable[[str], object] = importlib.import_module,
) -> object:
    normalized = artifact_format.strip().lower()
    if normalized == "json":
        return parse_json(raw)
    if normalized == "jsonl":
        return parse_jsonl(raw)
    if normalized == "csv":
        return parse_csv(raw)
    if normalized == "parquet":
        return parse_parquet(raw, module_loader=parquet_module_loader)
    raise SourcePipelineError(
        "SOURCE_FORMAT_UNSUPPORTED",
        f"unsupported source artifact format: {artifact_format!r}",
    )


def parse_cached_artifact(
    source: SourceSpec,
    revision: str,
    logical_name: str,
    *,
    cache_root: str | Path | None = None,
    override: SourceOverrideEvidence | Mapping[str, object] | None = None,
    approval_verifier: ApprovalVerifier | None = None,
    parquet_module_loader: Callable[[str], object] = importlib.import_module,
) -> object:
    artifact = _artifact(source, logical_name)
    if artifact.format is None:
        raise SourcePipelineError(
            "SOURCE_FORMAT_UNSUPPORTED", f"artifact {logical_name!r} has no registered format"
        )
    raw = read_cached_artifact(
        source,
        revision,
        logical_name,
        cache_root=cache_root,
        override=override,
        approval_verifier=approval_verifier,
    )
    return parse_artifact_bytes(
        raw, artifact.format, parquet_module_loader=parquet_module_loader
    )


__all__ = [
    "ApprovalVerifier",
    "ArtifactManifest",
    "ArtifactManifestEntry",
    "FetchReceipt",
    "MAX_CSV_COLUMNS",
    "MAX_CSV_ROWS",
    "MAX_JSON_DEPTH",
    "MAX_JSON_NODES",
    "MAX_PARQUET_COLUMNS",
    "MAX_PARQUET_ROWS",
    "MAX_PARQUET_UNCOMPRESSED_BYTES",
    "ManifestRevisionResolver",
    "RevisionResolver",
    "SafeFetcher",
    "SourcePipelineError",
    "SourceTransport",
    "TransportResponse",
    "UrllibTransport",
    "artifact_cache_path",
    "build_artifact_manifest",
    "inspect_source",
    "list_sources",
    "load_registry",
    "parse_artifact_bytes",
    "parse_cached_artifact",
    "parse_csv",
    "parse_json",
    "parse_jsonl",
    "parse_parquet",
    "read_cached_artifact",
    "read_verified_cache_bytes",
    "resolve_revision",
    "source_cache_root",
    "source_registry_path",
    "validate_cache_path_safety",
    "validate_revision",
]
