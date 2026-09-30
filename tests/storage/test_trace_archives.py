"""Archive file guarantees; the commit harness is not the retention DB implementation."""
from __future__ import annotations

import errno
import hashlib
import importlib
import json
import multiprocessing
import os
import sqlite3
import stat
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from motte_contracts.identity import canonical_json_bytes, canonical_sha256
from motte_storage.artifacts import ArtifactStore
from motte_storage.maintenance import begin_maintenance, end_maintenance
from motte_storage.operation_locks import MaintenanceConflict, artifact_maintenance_lock
from motte_storage.run_store import SQLiteRunStore
from motte_storage.trace_retention_models import (
    StoredTraceEvent, TraceArchiveInvalid, TraceArchiveReceipt, TracePrefix,
)

NOW = datetime(2026, 9, 30, tzinfo=UTC)
HASH = "a" * 64


def archives():
    return importlib.import_module("motte_storage.trace_archives")


def capability(store, root, owner):
    locks = importlib.import_module("motte_storage.operation_locks")
    return locks.trace_archive_write_capability(
        store, artifacts_root=root, maintenance_owner=owner,
    )


def evidence():
    rows = [StoredTraceEvent(
        run_id="run-1", seq=seq,
        payload={"seq": seq, "type": "custom", "timestamp": "forged-payload-time",
                 "nested": [{"artifact_id": "evidence/file.bin", "sha256": HASH},
                            {"artifacts": [{"sha256": "sha256:" + "b" * 64}]}],
                 "untouched": [None, False, {"你好": 3.25}]},
        stored_at=NOW - timedelta(days=10),
    ) for seq in (2, 3)]
    prefix = TracePrefix(
        run_id="run-1", run_revision=4, status="completed", first_seq=2, last_seq=3,
        keep_seq=5, event_count=2,
        events_sha256=canonical_sha256([row.model_dump(mode="json") for row in rows]),
    )
    return prefix, rows


def receipt_for(data, original_prefix, **updates):
    body = json.loads(data)
    digest = hashlib.sha256(data).hexdigest()
    return TraceArchiveReceipt(
        **{"archive_id": "archive-1", "plan_id": "c" * 64, "prefix": original_prefix,
           "artifact_id": "trace-archives/sha256/" + digest + ".json", "sha256": digest,
           "bytes": len(data), "cutoff": NOW,
           "artifact_refs": body["artifact_refs"], "artifact_hashes": body["artifact_hashes"],
           **updates},
    )


@pytest.fixture
def archive_store(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    # This fixture begins with a provisioned, durable root, never a new archive tree.
    for path in (root, root.parent):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return SQLiteRunStore(tmp_path / "runs.db"), ArtifactStore(root)


@contextmanager
def owner_for(store, artifacts):
    lease = begin_maintenance(store, reason="trace_retention", artifacts_root=artifacts.root)
    try:
        with capability(store, artifacts.root, lease["owner"]):
            yield lease["owner"]
    finally:
        end_maintenance(store, owner=lease["owner"])


def test_archive_is_canonical_complete_and_immutable(archive_store):
    store, artifacts = archive_store
    prefix, rows = evidence()
    before = [row.model_dump(mode="json") for row in rows]
    data = archives().build_trace_archive(prefix, rows)
    assert data == archives().build_trace_archive(prefix, rows)
    assert data == canonical_json_bytes(json.loads(data))
    decoded = json.loads(data)
    assert decoded["schema_version"] == 1
    assert decoded["events"] == before
    for field, value in prefix.model_dump(mode="json").items():
        assert decoded[field] == value
    assert decoded["artifact_refs"] == {"evidence/file.bin": "sha256:" + HASH}
    assert decoded["artifact_hashes"] == ["sha256:" + HASH, "sha256:" + "b" * 64]
    with owner_for(store, artifacts) as owner:
        artifact = artifacts.put_trace_archive(data, maintenance_owner=owner)
        again = artifacts.put_trace_archive(data, maintenance_owner=owner)
    assert artifact == again
    assert artifact.id == "trace-archives/sha256/" + hashlib.sha256(data).hexdigest() + ".json"
    assert artifact.sha256 == hashlib.sha256(data).hexdigest()
    assert artifacts.read_bytes(artifact.id) == data
    archives().verify_trace_archive(data, receipt_for(data, prefix))
    assert [row.model_dump(mode="json") for row in rows] == before


@pytest.mark.parametrize("change", ["run", "seq", "missing", "order", "payload", "timestamp"])
def test_archive_rejects_prefix_evidence_mismatch(change):
    prefix, rows = evidence()
    if change == "run":
        rows[0] = rows[0].model_copy(update={"run_id": "other"})
    elif change == "seq":
        rows[0] = rows[0].model_copy(update={"seq": 1})
    elif change == "missing":
        rows.pop()
    elif change == "order":
        rows.reverse()
    elif change == "payload":
        rows[0] = rows[0].model_copy(update={"payload": {"lost": "original evidence"}})
    else:
        rows[0] = rows[0].model_copy(update={"stored_at": None})
    with pytest.raises(TraceArchiveInvalid):
        archives().build_trace_archive(prefix, rows)


@pytest.mark.parametrize("change", [
    "bytes", "sha256", "artifact_id", "artifact_refs", "artifact_hashes", "prefix", "cutoff",
])
def test_verification_binds_receipt_to_complete_archive(change):
    prefix, rows = evidence()
    data = archives().build_trace_archive(prefix, rows)
    updates = {
        "bytes": len(data) + 1, "sha256": "d" * 64, "artifact_id": "other.json",
        "artifact_refs": {}, "artifact_hashes": [],
        "prefix": prefix.model_copy(update={"run_revision": prefix.run_revision + 1}),
        "cutoff": rows[0].stored_at,
    }
    with pytest.raises(TraceArchiveInvalid):
        archives().verify_trace_archive(data, receipt_for(data, prefix, **{change: updates[change]}))


@pytest.mark.parametrize("change", ["noncanonical", "unknown_field", "schema_bool", "schema_v2",
                                    "duplicate_key", "truncated", "invalid_model"])
def test_archive_rejects_malformed_or_unchecked_inputs(archive_store, change):
    store, artifacts = archive_store
    prefix, rows = evidence()
    data = archives().build_trace_archive(prefix, rows)
    body = json.loads(data)
    if change == "noncanonical":
        data = json.dumps(body, indent=2).encode()
    elif change == "unknown_field":
        data = canonical_json_bytes({**body, "extra": "lost if ignored"})
    elif change == "schema_bool":
        data = canonical_json_bytes({**body, "schema_version": True})
    elif change == "schema_v2":
        data = canonical_json_bytes({**body, "schema_version": 2})
    elif change == "duplicate_key":
        data = b'{"schema_version":1,' + data[1:]
    elif change == "truncated":
        data = data[:-1]
    else:
        rows[0] = rows[0].model_copy(update={"seq": True})
        with pytest.raises(TraceArchiveInvalid):
            archives().build_trace_archive(prefix, rows)
        return
    with owner_for(store, artifacts) as owner:
        with pytest.raises(TraceArchiveInvalid):
            artifacts.put_trace_archive(data, maintenance_owner=owner)
    assert not (artifacts.root / "trace-archives").exists()


def test_archive_requires_live_owner(archive_store, tmp_path):
    store, artifacts = archive_store
    prefix, rows = evidence()
    data = archives().build_trace_archive(prefix, rows)
    with pytest.raises(MaintenanceConflict):
        artifacts.put_trace_archive(data, maintenance_owner="invented")
    # Holding an artifact-only lock, even with the right reason, is not a DB capability.
    with artifact_maintenance_lock(artifacts.root, "reason-only", "trace_retention"):
        with pytest.raises(MaintenanceConflict):
            artifacts.put_trace_archive(data, maintenance_owner="reason-only")
        with pytest.raises(MaintenanceConflict):
            with capability(store, artifacts.root, "reason-only"):
                pass
    lease = begin_maintenance(store, reason="trace_retention", artifacts_root=artifacts.root)
    try:
        with pytest.raises(MaintenanceConflict):
            artifacts.put_trace_archive(data, maintenance_owner=lease["owner"])
        other = SQLiteRunStore(tmp_path / "other.db")
        with pytest.raises(MaintenanceConflict):
            with capability(other, artifacts.root, lease["owner"]):
                pass
        with pytest.raises(MaintenanceConflict):
            with capability(store, tmp_path / "other-root", lease["owner"]):
                pass
        with capability(store, artifacts.root, lease["owner"]):
            with pytest.raises(MaintenanceConflict):
                artifacts.delete("ordinary.bin", maintenance_owner=lease["owner"])
    finally:
        end_maintenance(store, owner=lease["owner"])
    with pytest.raises(MaintenanceConflict):
        with capability(store, artifacts.root, lease["owner"]):
            pass
    with pytest.raises(MaintenanceConflict):
        artifacts.put_trace_archive(data, maintenance_owner=lease["owner"])


def _fork_write(artifacts, data, owner, connection):
    try:
        artifacts.put_trace_archive(data, maintenance_owner=owner)
    except MaintenanceConflict:
        connection.send("denied")
    else:
        connection.send("authorized")
    finally:
        connection.close()


def test_archive_rejects_fork_inherited_capability(archive_store):
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("fork is not supported")
    store, artifacts = archive_store
    data = archives().build_trace_archive(*evidence())
    context = multiprocessing.get_context("fork")
    with owner_for(store, artifacts) as owner:
        receiver, sender = context.Pipe(duplex=False)
        child = context.Process(target=_fork_write, args=(artifacts, data, owner, sender))
        child.start()
        sender.close()
        child.join(10)
        assert child.exitcode == 0
        assert receiver.poll(1) and receiver.recv() == "denied"
        receiver.close()
    assert not (artifacts.root / "trace-archives").exists()


@pytest.mark.parametrize("alias", ["trace-archives", "trace-archives/orphan.bin",
                                    "./trace-archives/sha256/orphan.json", "alias/orphan.bin"])
def test_generic_mutations_cannot_touch_reserved_namespace(archive_store, alias):
    _, artifacts = archive_store
    reserved = artifacts.root / "trace-archives"
    reserved.mkdir()
    (reserved / "orphan.bin").write_bytes(b"orphan")
    (artifacts.root / "alias").symlink_to(reserved, target_is_directory=True)
    with pytest.raises(ValueError, match="reserved|immutable"):
        artifacts.put_bytes(alias, b"overwrite")
    with pytest.raises(ValueError, match="reserved|immutable"):
        artifacts.delete(alias)
    assert (reserved / "orphan.bin").read_bytes() == b"orphan"


@pytest.mark.parametrize("component", ["trace-archives", "sha256", "file"])
def test_archive_rejects_symlink_components(archive_store, tmp_path, component):
    store, artifacts = archive_store
    data = archives().build_trace_archive(*evidence())
    digest = hashlib.sha256(data).hexdigest()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"never change")
    if component == "trace-archives":
        (artifacts.root / "trace-archives").symlink_to(outside, target_is_directory=True)
    else:
        (artifacts.root / "trace-archives").mkdir()
        if component == "sha256":
            (artifacts.root / "trace-archives/sha256").symlink_to(outside, target_is_directory=True)
        else:
            (artifacts.root / "trace-archives/sha256").mkdir()
            (artifacts.root / f"trace-archives/sha256/{digest}.json").symlink_to(sentinel)
    with owner_for(store, artifacts) as owner:
        with pytest.raises((OSError, TraceArchiveInvalid)):
            artifacts.put_trace_archive(data, maintenance_owner=owner)
    assert sentinel.read_bytes() == b"never change"
    assert list(outside.iterdir()) == [sentinel]


def test_archive_same_name_different_bytes_is_never_overwritten(archive_store):
    store, artifacts = archive_store
    data = archives().build_trace_archive(*evidence())
    path = artifacts.root / f"trace-archives/sha256/{hashlib.sha256(data).hexdigest()}.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"different original bytes")
    with owner_for(store, artifacts) as owner:
        with pytest.raises(TraceArchiveInvalid):
            artifacts.put_trace_archive(data, maintenance_owner=owner)
    assert path.read_bytes() == b"different original bytes"


class DurableNamespace:
    """Power-loss model: entries survive only their parent's fsync; bytes need file fsync."""

    def __init__(self, root, monkeypatch, fail=None):
        self.root = root
        self.fail = fail
        self.operations = []
        self.entries = {".": set()}
        self.contents = {}
        self.real_open, self.real_mkdir, self.real_fsync = os.open, os.mkdir, os.fsync
        monkeypatch.setattr(os, "mkdir", self.mkdir)
        monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, self.mkdir})
        monkeypatch.setattr(os, "fsync", self.fsync)

    def label(self, fd):
        return Path(os.readlink(f"/proc/self/fd/{fd}")).relative_to(self.root).as_posix()

    def boundary(self, operation):
        self.operations.append(operation)
        if self.fail == operation:
            raise OSError(errno.EIO, "injected " + operation)

    def mkdir(self, path, *args, **kwargs):
        # Archive traversal uses dir_fd, so labels describe actual opened directories.
        if kwargs.get("dir_fd") is not None:
            parent = Path(os.readlink(f"/proc/self/fd/{kwargs['dir_fd']}"))
            relative = (parent / path).relative_to(self.root).as_posix()
            self.boundary("mkdir_" + relative)
        return self.real_mkdir(path, *args, **kwargs)

    def fsync(self, fd):
        try:
            relative = self.label(fd)
        except ValueError:
            return self.real_fsync(fd)
        is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
        label = {".": "artifact_root", "trace-archives": "trace_archives_dir",
                 "trace-archives/sha256": "sha256_dir"}.get(relative, "file")
        self.boundary("fsync_" + label)
        self.real_fsync(fd)
        if is_dir:
            self.entries[relative] = set(os.listdir(fd))
        else:
            self.contents[relative] = (self.root / relative).read_bytes()

    def after_power_loss(self, relative):
        parent = "."
        for part in Path(relative).parts:
            if part not in self.entries.get(parent, set()):
                return None
            parent = part if parent == "." else parent + "/" + part
        return self.contents.get(relative)


def commit_harness(artifacts, data, owner, db):
    """Synthetic caller boundary, deliberately not the future product trim transaction."""
    artifact = artifacts.put_trace_archive(data, maintenance_owner=owner)
    with db:
        db.execute("INSERT INTO receipts VALUES (?)", (artifact.id,))
        db.execute("DELETE FROM live_events WHERE seq < 3")
    return artifact


def harness_db():
    db = sqlite3.connect(":memory:")
    db.executescript("CREATE TABLE receipts(artifact_id TEXT); CREATE TABLE live_events(seq INT);"
                     "INSERT INTO live_events VALUES (1), (2), (3);")
    return db


@pytest.mark.parametrize("failure", [
    "mkdir_trace-archives", "mkdir_trace-archives/sha256", "fsync_file", "fsync_sha256_dir",
    "fsync_trace_archives_dir", "fsync_artifact_root", None,
])
def test_first_archive_persists_every_directory_entry(archive_store, monkeypatch, failure):
    store, artifacts = archive_store
    data = archives().build_trace_archive(*evidence())
    with owner_for(store, artifacts) as owner:
        namespace = DurableNamespace(artifacts.root, monkeypatch, fail=failure)
        artifact_module = importlib.import_module("motte_storage.artifacts")
        original_decode = artifact_module._decode_trace_archive
        def verified(value):
            result = original_decode(value)
            namespace.operations.append(
                "verified" if "fsync_artifact_root" in namespace.operations else "input_validated"
            )
            return result
        monkeypatch.setattr(artifact_module, "_decode_trace_archive", verified)
        with harness_db() as db:
            if failure:
                with pytest.raises(OSError, match="injected"):
                    commit_harness(artifacts, data, owner, db)
                path = "trace-archives/sha256/" + hashlib.sha256(data).hexdigest() + ".json"
                assert namespace.after_power_loss(path) is None
                assert "verified" not in namespace.operations
                assert db.execute("SELECT * FROM receipts").fetchall() == []
                assert db.execute("SELECT * FROM live_events").fetchall() == [(1,), (2,), (3,)]
            else:
                artifact = commit_harness(artifacts, data, owner, db)
                assert namespace.after_power_loss(artifact.id) == data
                assert db.execute("SELECT * FROM receipts").fetchall() == [(artifact.id,)]
                assert db.execute("SELECT * FROM live_events").fetchall() == [(3,)]
                expected = ["fsync_file", "fsync_sha256_dir", "fsync_trace_archives_dir",
                            "fsync_artifact_root"]
                assert [op for op in namespace.operations if op.startswith("fsync_")] == expected
                assert namespace.operations.index("fsync_artifact_root") < namespace.operations.index("verified")
            # Existing entries after an interrupted call do not prove durable ancestry.
            namespace.fail = None
            namespace.operations.clear()
            artifact = artifacts.put_trace_archive(data, maintenance_owner=owner)
            assert namespace.after_power_loss(artifact.id) == data
            assert namespace.operations.index("fsync_artifact_root") < namespace.operations.index("verified")
            assert [op for op in namespace.operations if op.startswith("fsync_")] == [
                "fsync_file", "fsync_sha256_dir", "fsync_trace_archives_dir", "fsync_artifact_root",
            ]


@pytest.mark.parametrize("failure", ["disk_full", "short_write", "unsupported_fsync"])
def test_archive_io_failures_delete_nothing(archive_store, monkeypatch, failure):
    store, artifacts = archive_store
    data = archives().build_trace_archive(*evidence())
    with owner_for(store, artifacts) as owner:
        if failure == "short_write":
            original = os.write
            monkeypatch.setattr(os, "write", lambda fd, value: original(fd, value[:7]))
        elif failure == "disk_full":
            def full(*args):
                raise OSError(errno.ENOSPC, "disk full")
            monkeypatch.setattr(os, "write", full)
        else:
            original_fsync = os.fsync
            def unsupported(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError(errno.EINVAL, "directory fsync unsupported")
                return original_fsync(fd)
            monkeypatch.setattr(os, "fsync", unsupported)
        with harness_db() as db:
            with pytest.raises((OSError, TraceArchiveInvalid)):
                commit_harness(artifacts, data, owner, db)
            assert db.execute("SELECT * FROM receipts").fetchall() == []
            assert db.execute("SELECT * FROM live_events").fetchall() == [(1,), (2,), (3,)]


def test_existing_identical_archive_is_reverified_after_fsync(archive_store, monkeypatch):
    store, artifacts = archive_store
    prefix, rows = evidence()
    data = archives().build_trace_archive(prefix, rows)
    with owner_for(store, artifacts) as owner:
        artifact = artifacts.put_trace_archive(data, maintenance_owner=owner)
        real_fsync = os.fsync
        def corrupt_after_fsync(fd):
            real_fsync(fd)
            if Path(os.readlink(f"/proc/self/fd/{fd}")) == artifacts.root:
                (artifacts.root / artifact.id).write_bytes(b"corrupted during retry")
        monkeypatch.setattr(os, "fsync", corrupt_after_fsync)
        with pytest.raises(TraceArchiveInvalid):
            artifacts.put_trace_archive(data, maintenance_owner=owner)


def test_archive_requires_existing_root(archive_store, tmp_path):
    store, _ = archive_store
    artifacts = ArtifactStore(tmp_path / "new-root")
    data = archives().build_trace_archive(*evidence())
    with owner_for(store, artifacts) as owner:
        with pytest.raises(TraceArchiveInvalid, match="existing|provision"):
            artifacts.put_trace_archive(data, maintenance_owner=owner)


def test_generic_put_cannot_overwrite_archive_through_hardlink(archive_store):
    store, artifacts = archive_store
    data = archives().build_trace_archive(*evidence())
    with owner_for(store, artifacts) as owner:
        artifact = artifacts.put_trace_archive(data, maintenance_owner=owner)
    os.link(artifacts.root / artifact.id, artifacts.root / "hardlink.bin")
    with pytest.raises(ValueError, match="hard.link|immutable"):
        artifacts.put_bytes("hardlink.bin", b"overwrite via hardlink")
    assert artifacts.read_bytes(artifact.id) == data


def test_archive_rejects_replaced_directory_after_file_fsync(archive_store, tmp_path, monkeypatch):
    store, artifacts = archive_store
    data = archives().build_trace_archive(*evidence())
    outside = tmp_path / "replacement"
    outside.mkdir()
    original = os.fsync
    with owner_for(store, artifacts) as owner:
        def replace(fd):
            original(fd)
            if Path(os.readlink(f"/proc/self/fd/{fd}")) == artifacts.root:
                source = artifacts.root / "trace-archives/sha256"
                source.rename(source.with_name("detached"))
                source.symlink_to(outside, target_is_directory=True)
        monkeypatch.setattr(os, "fsync", replace)
        with harness_db() as db:
            with pytest.raises(TraceArchiveInvalid, match="namespace changed"):
                commit_harness(artifacts, data, owner, db)
            assert db.execute("SELECT * FROM receipts").fetchall() == []
            assert db.execute("SELECT * FROM live_events").fetchall() == [(1,), (2,), (3,)]
    assert list(outside.iterdir()) == []


def test_archive_rejects_replaced_root(archive_store):
    store, artifacts = archive_store
    data = archives().build_trace_archive(*evidence())
    artifacts.root.rename(artifacts.root.with_name("original-root"))
    artifacts.root.mkdir()
    with owner_for(store, artifacts) as owner:
        with pytest.raises(TraceArchiveInvalid, match="root changed"):
            artifacts.put_trace_archive(data, maintenance_owner=owner)
    assert list(artifacts.root.iterdir()) == []


def test_archive_unsupported_nofollow_fails_closed(archive_store, monkeypatch):
    store, artifacts = archive_store
    data = archives().build_trace_archive(*evidence())
    with owner_for(store, artifacts) as owner:
        monkeypatch.delattr(os, "O_NOFOLLOW")
        with pytest.raises(TraceArchiveInvalid, match="unsupported"):
            artifacts.put_trace_archive(data, maintenance_owner=owner)
    assert not (artifacts.root / "trace-archives").exists()


def test_reserved_delete_stays_denied_to_generic_gc_owner(archive_store):
    store, artifacts = archive_store
    path = artifacts.root / "trace-archives/orphan.bin"
    path.parent.mkdir()
    path.write_bytes(b"orphan")
    lease = begin_maintenance(store, reason="gc", artifacts_root=artifacts.root)
    try:
        with pytest.raises(ValueError, match="reserved|immutable"):
            artifacts.delete("trace-archives/orphan.bin", maintenance_owner=lease["owner"])
    finally:
        end_maintenance(store, owner=lease["owner"])
    assert path.read_bytes() == b"orphan"


def test_archive_capability_expires_before_maintenance_lease(archive_store):
    store, artifacts = archive_store
    data = archives().build_trace_archive(*evidence())
    lease = begin_maintenance(store, reason="trace_retention", artifacts_root=artifacts.root)
    try:
        with capability(store, artifacts.root, lease["owner"]):
            pass
        with pytest.raises(MaintenanceConflict):
            artifacts.put_trace_archive(data, maintenance_owner=lease["owner"])
    finally:
        end_maintenance(store, owner=lease["owner"])
    assert not (artifacts.root / "trace-archives").exists()


@pytest.mark.parametrize("replacement", ["symlink", "hardlink"])
def test_generic_put_mutation_boundary_cannot_follow_replaced_leaf(archive_store, monkeypatch, replacement):
    _, artifacts = archive_store
    archive = artifacts.root / "trace-archives/sha256/orphan.json"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"immutable evidence")
    target = artifacts.root / "ordinary.bin"
    target.write_bytes(b"ordinary evidence")
    original_write, original_open = Path.write_bytes, os.open
    attacked = False

    def swap():
        nonlocal attacked
        if not attacked:
            attacked = True
            target.unlink()
            if replacement == "symlink":
                target.symlink_to(archive)
            else:
                os.link(archive, target)

    def write(path, data):
        if path == target:
            swap()
        return original_write(path, data)

    def open_file(path, flags, *args, **kwargs):
        if Path(path).name == target.name and flags & (os.O_WRONLY | os.O_RDWR):
            swap()
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(Path, "write_bytes", write)
    monkeypatch.setattr(os, "open", open_file)
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, open_file})
    try:
        artifacts.put_bytes("ordinary.bin", b"overwrite through replaced leaf")
    except (ValueError, OSError):
        pass  # A safely detected substitution must fail before truncation.
    assert attacked
    assert archive.read_bytes() == b"immutable evidence"


def test_generic_delete_mutation_boundary_cannot_follow_replaced_parent(archive_store, monkeypatch):
    _, artifacts = archive_store
    archive = artifacts.root / "trace-archives/sha256/orphan.json"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"immutable evidence")
    ordinary = artifacts.root / "ordinary"
    ordinary.mkdir()
    target = ordinary / archive.name
    target.write_bytes(b"ordinary evidence")
    original_path_unlink, original_unlink = Path.unlink, os.unlink
    attacked = False

    def swap():
        nonlocal attacked
        if not attacked:
            attacked = True
            ordinary.rename(artifacts.root / "detached-ordinary")
            ordinary.symlink_to(archive.parent, target_is_directory=True)

    def path_unlink(path, *args, **kwargs):
        if path == target:
            swap()
        return original_path_unlink(path, *args, **kwargs)

    def unlink(path, *args, **kwargs):
        if Path(path).name == target.name:
            swap()
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", path_unlink)
    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, unlink})
    try:
        artifacts.delete("ordinary/orphan.json")
    except (ValueError, OSError):
        pass
    assert attacked
    assert archive.read_bytes() == b"immutable evidence"


@pytest.mark.parametrize("action", ["put", "capability"])
def test_cross_thread_fork_rejects_owner_before_inherited_lock(archive_store, action):
    import signal
    import threading
    import warnings

    if not hasattr(os, "fork"):
        pytest.skip("fork is unavailable")
    store, artifacts = archive_store
    data = archives().build_trace_archive(*evidence())
    reader, writer = os.pipe()
    outcome = []

    def fork_in_thread(owner):
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=DeprecationWarning,
                                    message=".*multi-threaded.*")
            pid = os.fork()
        if pid == 0:
            os.close(reader)
            def timeout(*args):
                os.write(writer, b"blocked")
                os._exit(0)
            signal.signal(signal.SIGALRM, timeout)
            signal.alarm(2)
            try:
                if action == "put":
                    artifacts.put_trace_archive(data, maintenance_owner=owner)
                else:
                    with capability(store, artifacts.root, owner):
                        pass
            except MaintenanceConflict:
                os.write(writer, b"denied")
            except BaseException:
                os.write(writer, b"unexpected error")
            else:
                os.write(writer, b"authorized")
            os._exit(0)
        outcome.append(os.waitpid(pid, 0)[1])

    try:
        with owner_for(store, artifacts) as owner:
            thread = threading.Thread(target=fork_in_thread, args=(owner,))
            thread.start()
            thread.join(5)
            assert not thread.is_alive()
        os.close(writer)
        writer = None
        assert outcome == [0]
        assert os.read(reader, 100) == b"denied"
    finally:
        os.close(reader)
        if writer is not None:
            os.close(writer)
    assert not (artifacts.root / "trace-archives").exists()


@pytest.mark.parametrize("missing", ["nofollow", "dir_fd", "native_unavailable"])
@pytest.mark.parametrize("operation", ["put", "delete"])
def test_ordinary_mutation_fails_closed_without_safe_primitives(archive_store, monkeypatch, missing, operation):
    _, artifacts = archive_store
    target = artifacts.root / "ordinary.bin"
    target.write_bytes(b"preserved ordinary bytes")
    if missing == "nofollow":
        monkeypatch.delattr(os, "O_NOFOLLOW")
    elif missing == "dir_fd":
        monkeypatch.setattr(os, "supports_dir_fd", set())
    else:
        from types import SimpleNamespace
        import motte_storage.artifacts as artifact_module
        simulated_os = SimpleNamespace(**vars(os))
        simulated_os.name = "nt"
        monkeypatch.setattr(artifact_module, "os", simulated_os)
        import motte_storage._windows_artifact_io as native
        def unavailable():
            raise artifact_module.ArtifactMutationUnsupported(
                "safe artifact mutation primitives are unsupported")
        monkeypatch.setattr(native, "_api", unavailable)
    with pytest.raises(ValueError, match="safe artifact mutation primitives are unsupported"):
        if operation == "put":
            artifacts.put_bytes("ordinary.bin", b"must not overwrite")
        else:
            artifacts.delete("ordinary.bin")
    assert target.read_bytes() == b"preserved ordinary bytes"


@pytest.mark.parametrize("operation", ["put", "delete"])
def test_ordinary_mutation_never_treats_store_root_as_file(archive_store, operation):
    _, artifacts = archive_store
    before = sorted(artifacts.root.iterdir())
    with pytest.raises(ValueError, match="must name a file"):
        if operation == "put":
            artifacts.put_bytes(".", b"not a file identity")
        else:
            artifacts.delete(".")
    assert sorted(artifacts.root.iterdir()) == before
