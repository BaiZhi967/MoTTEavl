"""Windows native tests run on NTFS; portable cases test contracts, not Windows parity."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("name", ["", ".", "..", "../x", "C:foo", "C:/foo", "/foo", "\\foo",
                                  "x:stream", "x\0y", "CON", "aux.txt", "LPT1", "x.", "x ",
                                  "trace-archives/a", "TRACE-ARCHIVES/a"])
def test_windows_lexical_mutation_rejects_unsafe_names(name):
    from motte_storage._windows_artifact_io import _components

    with pytest.raises(ValueError):
        _components(name)


def test_windows_lexical_normalization_is_io_free():
    from motte_storage._windows_artifact_io import _components

    assert _components("run\\./目录/😀.bin") == ("run", "目录", "😀.bin")


def test_windows_abi_contract_is_fixed_width():
    from motte_storage import _windows_artifact_io as win

    assert ctypes.sizeof(win.FILE_DISPOSITION_INFO) == 1
    assert ctypes.alignment(win.FILE_DISPOSITION_INFO) == 1
    assert win.FILE_DISPOSITION_INFO.DeleteFile.offset == 0
    assert ctypes.sizeof(win.FILE_STANDARD_INFO) == 24
    assert win.FILE_STANDARD_INFO.DeletePending.offset == 20
    assert win.FILE_STANDARD_INFO.Directory.offset == 21
    assert ctypes.sizeof(win.FILE_ID_INFO) == 24
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(win.UNICODE_STRING) == 16
        assert ctypes.sizeof(win.OBJECT_ATTRIBUTES) == 48
        assert ctypes.sizeof(win.IO_STATUS_BLOCK) == 16
        assert win.IO_STATUS_BLOCK.Information.offset == 8


def test_windows_utf16_uses_encoded_byte_count_and_rejects_overflow():
    from motte_storage._windows_artifact_io import _unicode_string

    value, buffer = _unicode_string("😀é")
    assert value.Length == 6 and value.MaximumLength == 8
    assert buffer.raw == "😀é".encode("utf-16-le") + b"\0\0"
    with pytest.raises(ValueError):
        _unicode_string("😀" * 16384)


def test_windows_dispatch_keeps_original_lexical_id(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import motte_storage.artifacts as module
    from motte_storage import _windows_artifact_io as win

    store = module.ArtifactStore(tmp_path)
    calls = []

    @contextmanager
    def target(root, artifact_id, root_identity, *, write, read):
        calls.append((artifact_id, write, read))
        yield SimpleNamespace(path=root / "run/out", write_bytes=lambda data: None)

    monkeypatch.setattr(module, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(win, "pinned_target", target)
    monkeypatch.setattr(store, "_ordinary_mutation_path", lambda name: pytest.fail("resolved first"))
    with store._pinned_mutation_target("run\\out", write=True):
        pass
    assert calls == [("run\\out", True, False)]


native = pytest.mark.skipif(os.name != "nt", reason="requires actual Windows native I/O")


@pytest.fixture
def ntfs_root(tmp_path):
    if os.environ.get("MOTTE_REQUIRE_WINDOWS_ARTIFACT_TESTS") and os.name != "nt":
        pytest.fail("required Windows job is not running on Windows")
    if os.name != "nt":
        pytest.skip("requires actual Windows")
    from motte_storage import _windows_artifact_io as win

    kernel = win._api().kernel
    fn = kernel.GetVolumeInformationW
    fn.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_wchar_p, ctypes.c_uint32]
    fn.restype = ctypes.c_int32
    fs = ctypes.create_unicode_buffer(64)
    assert fn(tmp_path.anchor, None, 0, None, None, None, fs, len(fs)), ctypes.get_last_error()
    assert fs.value == "NTFS", f"required native tests need NTFS, got {fs.value}"
    return tmp_path


@native
def test_native_windows_environment_and_ordinary_crud(ntfs_root, record_property):
    import platform
    from motte_storage.artifacts import ArtifactStore

    record_property("windows", platform.platform())
    record_property("python", platform.python_version())
    record_property("filesystem", "NTFS")
    assert ctypes.sizeof(ctypes.c_void_p) == 8, "native acceptance requires Python x64"
    print(f"Native acceptance: {platform.platform()}, Python {platform.python_version()}, NTFS, x64")
    store = ArtifactStore(ntfs_root / "root")
    for value in (b"long ordinary contents", b"short", b"", bytes(range(256))):
        artifact = store.put_bytes("run/目录/😀.bin", value)
        assert store.read_bytes(artifact.id) == value
    store.delete("run/目录/😀.bin")
    assert not (store.root / "run/目录/😀.bin").exists()
    store.delete("run/目录/😀.bin")
    store.delete("absent/child")


def _archive_fixture(root):
    from motte_storage.artifacts import ArtifactStore
    store = ArtifactStore(root / "artifacts")
    archive = store.root / "trace-archives/sha256/orphan.json"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"immutable evidence")
    return store, archive



def _verified_archive_fixture(root):
    """Provision portable canonical evidence; do not claim Windows archive durability.

    The typed receipt is test evidence, not a retention DB publication. Its full
    production verification must pass before and after each adversarial action.
    """
    import hashlib
    import json
    from datetime import UTC, datetime, timedelta
    from motte_contracts.identity import canonical_sha256
    from motte_storage.artifacts import ArtifactStore
    from motte_storage.trace_archives import build_trace_archive, trace_archive_artifact_id
    from motte_storage.trace_retention_models import StoredTraceEvent, TraceArchiveReceipt, TracePrefix

    cutoff = datetime(2026, 9, 30, tzinfo=UTC)
    row = StoredTraceEvent(run_id="fixture-run", seq=1, stored_at=cutoff - timedelta(days=2),
                           payload={"seq": 1, "type": "custom", "unchanged": [None, "证据"],
                                    "artifact": {"artifact_id": "evidence.bin", "sha256": "a" * 64}})
    prefix = TracePrefix(run_id=row.run_id, run_revision=3, status="completed", first_seq=1,
                         last_seq=1, keep_seq=2, event_count=1,
                         events_sha256=canonical_sha256([row.model_dump(mode="json")]))
    data = build_trace_archive(prefix, [row])
    body = json.loads(data)
    receipt = TraceArchiveReceipt(
        archive_id="provisioned-fixture", plan_id="c" * 64, prefix=prefix,
        artifact_id=trace_archive_artifact_id(data), sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data), cutoff=cutoff, artifact_refs=body["artifact_refs"],
        artifact_hashes=body["artifact_hashes"],
    )
    store = ArtifactStore(root / "artifacts")
    archive = store.root / receipt.artifact_id
    archive.parent.mkdir(parents=True)
    archive.write_bytes(data)
    _assert_verified_archive(store, archive, data, receipt)
    return store, archive, data, receipt


def _assert_verified_archive(store, archive, data, receipt):
    from motte_storage.trace_archives import verify_trace_archive
    from motte_storage.trace_retention_models import TraceArchiveReceipt

    assert isinstance(receipt, TraceArchiveReceipt)
    assert archive.read_bytes() == data
    actual = store.read_bytes(receipt.artifact_id)
    assert actual == data
    verify_trace_archive(actual, receipt)


def test_provisioned_archive_fixture_requires_exact_receipt_and_content(tmp_path):
    from motte_storage.trace_archives import verify_trace_archive
    from motte_storage.trace_retention_models import TraceArchiveInvalid

    store, archive, data, receipt = _verified_archive_fixture(tmp_path)
    _assert_verified_archive(store, archive, data, receipt)
    assert receipt.artifact_refs == {"evidence.bin": "sha256:" + "a" * 64}
    with pytest.raises(TraceArchiveInvalid):
        verify_trace_archive(data + b" ", receipt)
    with pytest.raises(TraceArchiveInvalid):
        verify_trace_archive(data, receipt.model_copy(update={"sha256": "b" * 64}))
    with pytest.raises(TraceArchiveInvalid):
        verify_trace_archive(data, receipt.model_copy(update={"artifact_refs": {}}))


def _required_symlink(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        pytest.fail(f"required symlink fixture unavailable without privilege changes: {error}")


def _required_junction(link, target):
    import subprocess
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, f"required junction fixture unavailable: {result.stderr}"


@native
@pytest.mark.parametrize("alias", ["symlink", "hardlink", "junction"])
@pytest.mark.parametrize("operation", ["put", "delete"])
def test_native_archive_aliases_preserve_orphan(ntfs_root, alias, operation):
    from motte_storage.artifacts import ArtifactStore
    store, archive = _archive_fixture(ntfs_root)
    original_identity = archive.stat().st_ino
    if alias == "junction":
        _required_junction(store.root / "alias", archive.parent)
        name = "alias/orphan.json"
    elif alias == "symlink":
        name = "alias.json"
        _required_symlink(store.root / name, archive)
    else:
        name = "alias.json"
        os.link(archive, store.root / name)
    if alias == "hardlink" and operation == "delete":
        store.delete(name)
        assert not (store.root / name).exists()
    else:
        with pytest.raises((ValueError, OSError)):
            if operation == "put":
                store.put_bytes(name, b"overwrite")
            else:
                store.delete(name)
    assert archive.read_bytes() == b"immutable evidence"
    assert archive.stat().st_ino == original_identity
    assert ArtifactStore(store.root).read_bytes("trace-archives/sha256/orphan.json")


@native
@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_native_put_leaf_replacement_before_open(ntfs_root, monkeypatch, alias):
    from motte_storage import _windows_artifact_io as win
    store, archive = _archive_fixture(ntfs_root)
    leaf = store.root / "ordinary.bin"
    leaf.write_bytes(b"ordinary")
    api, attacked = win._api(), []
    original = api.open

    def open_file(handles, parent, name, **kwargs):
        if name == leaf.name and kwargs.get("create") and not attacked:
            attacked.append(True)
            leaf.unlink()
            if alias == "symlink":
                _required_symlink(leaf, archive)
            else:
                os.link(archive, leaf)
        return original(handles, parent, name, **kwargs)

    monkeypatch.setattr(api, "open", open_file)
    with pytest.raises((ValueError, OSError)):
        store.put_bytes(leaf.name, b"must not overwrite")
    assert attacked
    assert archive.read_bytes() == b"immutable evidence"


@native
@pytest.mark.parametrize("boundary", ["before_parent_open", "after_parent_open", "truncate"])
def test_native_parent_junction_substitution_boundaries(ntfs_root, monkeypatch, boundary):
    from motte_storage import _windows_artifact_io as win
    store, archive = _archive_fixture(ntfs_root)
    parent = store.root / "ordinary"
    parent.mkdir()
    (parent / archive.name).write_bytes(b"ordinary")
    api, attempted = win._api(), []
    original_open, original_truncate = api.open, api.truncate

    def attack():
        attempted.append(True)
        try:
            parent.rename(store.root / "detached")
        except OSError as error:
            assert error.winerror in {5, 32}, error
            return
        _required_junction(parent, archive.parent)

    def open_file(handles, held, name, **kwargs):
        if boundary == "before_parent_open" and name == "ordinary" and not attempted:
            attack()
        result = original_open(handles, held, name, **kwargs)
        if boundary == "after_parent_open" and name == "ordinary" and not attempted:
            attack()
        return result

    def truncate(handle):
        if boundary == "truncate":
            attack()
        return original_truncate(handle)

    monkeypatch.setattr(api, "open", open_file)
    monkeypatch.setattr(api, "truncate", truncate)
    try:
        store.put_bytes("ordinary/orphan.json", b"ordinary replacement")
    except (ValueError, OSError):
        pass
    assert attempted
    assert archive.read_bytes() == b"immutable evidence"


def _attribute_handle(path):
    from motte_storage import _windows_artifact_io as win
    api = win._api()
    fn = api.kernel.CreateFileW
    fn.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                   ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    fn.restype = ctypes.c_void_p
    result = fn(str(path), 0x100, 7, None, 3, 0x02000000 | 0x00200000, None)
    assert result not in (None, ctypes.c_void_p(-1).value), ctypes.WinError(ctypes.get_last_error())
    return win._Handle(api, result)


def _set_junction(handle, destination):
    import struct
    from motte_storage import _windows_artifact_io as win
    substitute = ("\\??\\" + str(destination)).encode("utf-16-le")
    display = str(destination).encode("utf-16-le")
    paths = substitute + b"\0\0" + display + b"\0\0"
    payload = struct.pack("<IHHHHHH", 0xA0000003, 8 + len(paths), 0,
                          0, len(substitute), len(substitute) + 2, len(display)) + paths
    buffer = ctypes.create_string_buffer(payload)
    fn = win._api().kernel.DeviceIoControl
    fn.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                   ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                   ctypes.c_void_p]
    fn.restype = ctypes.c_int32
    returned = ctypes.c_uint32()
    success = fn(handle.value, 0x900A4, buffer, len(payload), None, 0,
                 ctypes.byref(returned), None)
    return bool(success), ctypes.get_last_error()


@native
@pytest.mark.parametrize("boundary", ["child_open", "truncate"])
def test_native_attribute_only_in_place_reparse(ntfs_root, monkeypatch, boundary):
    from motte_storage import _windows_artifact_io as win
    store, archive = _archive_fixture(ntfs_root)
    parent = store.root / "ordinary"
    parent.mkdir()
    if boundary == "truncate":
        (parent / archive.name).write_bytes(b"ordinary")
    attacker = _attribute_handle(parent)
    api, attempts = win._api(), []
    original_open, original_truncate = api.open, api.truncate

    def attack():
        success, error = _set_junction(attacker, archive.parent)
        attempts.append((success, error))
        # A nonempty directory can refuse in-place conversion. Other failures
        # are reported, never silently interpreted as a successful attack test.
        if not success:
            assert error in {5, 32, 145}, ctypes.WinError(error)

    def open_file(handles, held, name, **kwargs):
        if boundary == "child_open" and name == archive.name and not attempts:
            attack()
        return original_open(handles, held, name, **kwargs)

    def truncate(handle):
        if boundary == "truncate":
            attack()
        return original_truncate(handle)

    monkeypatch.setattr(api, "open", open_file)
    monkeypatch.setattr(api, "truncate", truncate)
    try:
        try:
            store.put_bytes("ordinary/orphan.json", b"ordinary replacement")
        except (ValueError, OSError):
            pass
    finally:
        attacker.close()
    assert attempts
    assert archive.read_bytes() == b"immutable evidence"


@native
@pytest.mark.parametrize("alias", ["symlink", "hardlink", "junction"])
@pytest.mark.parametrize("operation", ["put", "delete"])
def test_native_archive_aliases_preserve_verified_evidence(ntfs_root, alias, operation):
    store, archive, data, receipt = _verified_archive_fixture(ntfs_root)
    identity = archive.stat().st_ino
    if alias == "junction":
        _required_junction(store.root / "alias", archive.parent)
        name = "alias/" + archive.name
    else:
        name = "alias.json"
        if alias == "symlink":
            _required_symlink(store.root / name, archive)
        else:
            os.link(archive, store.root / name)
    _assert_verified_archive(store, archive, data, receipt)
    if alias == "hardlink" and operation == "delete":
        store.delete(name)
        assert not (store.root / name).exists()
    else:
        with pytest.raises((ValueError, OSError)):
            if operation == "put":
                store.put_bytes(name, b"must not replace verified evidence")
            else:
                store.delete(name)
    assert archive.stat().st_ino == identity
    _assert_verified_archive(store, archive, data, receipt)


@native
@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
@pytest.mark.parametrize("boundary", ["before_open", "truncate"])
def test_native_raced_alias_preserves_verified_evidence(ntfs_root, monkeypatch, alias, boundary):
    from motte_storage import _windows_artifact_io as win

    store, archive, data, receipt = _verified_archive_fixture(ntfs_root)
    identity = archive.stat().st_ino
    leaf, replacement = store.root / "ordinary.bin", store.root / "attacker-alias.bin"
    leaf.write_bytes(b"ordinary original")
    api, outcomes = win._api(), []
    original_open, original_truncate = api.open, api.truncate

    def attack():
        if alias == "symlink":
            _required_symlink(replacement, archive)
        else:
            os.link(archive, replacement)
        assert replacement.read_bytes() == data  # The actual canonical target, not an orphan.
        _assert_verified_archive(store, archive, data, receipt)
        try:
            os.replace(replacement, leaf)
        except OSError as error:
            assert boundary == "truncate" and error.winerror in {5, 32}, error
            outcomes.append(("blocked", error.winerror))
        else:
            outcomes.append(("replaced", None))

    def open_file(handles, parent, name, **kwargs):
        if boundary == "before_open" and name == leaf.name and kwargs.get("create") and not outcomes:
            attack()
        return original_open(handles, parent, name, **kwargs)

    def truncate(handle):
        if boundary == "truncate":
            attack()
        return original_truncate(handle)

    monkeypatch.setattr(api, "open", open_file)
    monkeypatch.setattr(api, "truncate", truncate)
    if boundary == "before_open":
        with pytest.raises((ValueError, OSError)):
            store.put_bytes(leaf.name, b"changed ordinary")
        assert outcomes == [("replaced", None)]
    else:
        store.put_bytes(leaf.name, b"changed ordinary")
        assert len(outcomes) == 1 and outcomes[0][0] == "blocked"
        assert leaf.read_bytes() == b"changed ordinary"
    assert archive.stat().st_ino == identity
    _assert_verified_archive(store, archive, data, receipt)


@native
def test_native_late_unreceipted_hardlink_is_an_explicit_limit(ntfs_root, monkeypatch, record_property):
    """Share=0 does not freeze link topology; an error cannot undo written bytes."""
    from motte_storage import _windows_artifact_io as win
    from motte_storage.trace_archives import verify_trace_archive
    from motte_storage.trace_retention_models import TraceArchiveInvalid

    store, archive, data, receipt = _verified_archive_fixture(ntfs_root)
    identity = archive.stat().st_ino
    ordinary = store.root / "ordinary.bin"
    ordinary.write_bytes(b"ordinary original")
    late_alias = archive.parent / "late-unreceipted-alias.bin"
    api, attempts = win._api(), []
    truncate = api.truncate

    def attack(handle):
        # This required native attack must actually succeed, not skip on failure.
        os.link(ordinary, late_alias)
        attempts.append(True)
        assert late_alias.stat().st_ino != identity
        _assert_verified_archive(store, archive, data, receipt)
        return truncate(handle)

    monkeypatch.setattr(api, "truncate", attack)
    with pytest.raises(ValueError, match="hard-linked") as outcome:
        store.put_bytes(ordinary.name, b"changed")
    assert attempts == [True]
    assert ordinary.read_bytes() == late_alias.read_bytes() == b"changed"
    assert archive.stat().st_ino == identity
    _assert_verified_archive(store, archive, data, receipt)
    with pytest.raises(TraceArchiveInvalid):
        verify_trace_archive(late_alias.read_bytes(), receipt)
    record_property("late_alias_created", True)
    record_property("ordinary_put_outcome", str(outcome.value))
    record_property("late_unreceipted_alias_bytes", "changed")
    print(f"Explicit limit: late alias created; put raised {outcome.value!s}; alias bytes changed; "
          "canonical archive and typed receipt still verify")


def _gc_fixture(root):
    from datetime import UTC, datetime, timedelta
    from motte_storage.artifacts import ArtifactStore
    from motte_storage.gc import plan_gc
    from motte_storage.run_store import SQLiteRunStore
    db = SQLiteRunStore(root / "gc.db")
    store = ArtifactStore(root / "artifacts")
    artifact = store.put_bytes("ordinary/candidate.bin", b"expired ordinary")
    old = (datetime.now(UTC) - timedelta(days=200)).timestamp()
    os.utime(artifact.uri, (old, old))
    return db, store, plan_gc(db, store.root)


@native
def test_native_gc_closes_before_audited_completion(ntfs_root, monkeypatch):
    from motte_storage import _windows_artifact_io as win
    from motte_storage.gc import apply_gc
    from motte_storage.platform import platform_for
    db, store, plan = _gc_fixture(ntfs_root)
    api, events = win._api(), []
    original_disposition, original_close = api.disposition, api.close
    tombstones = platform_for(db).tombstones
    original_append = type(tombstones).append
    disposed = []

    def disposition(handle):
        events.append("disposition")
        disposed.append(handle.value)
        return original_disposition(handle)

    def close(handle):
        result = original_close(handle)
        if handle in disposed:
            events.append("close")
        return result

    def append(repository, rows):
        rows = list(rows)
        events.extend(row["deletion_status"] for row in rows)
        if any(row["deletion_status"] == "deleted" for row in rows):
            assert not (store.root / "ordinary/candidate.bin").exists()
        return original_append(repository, rows)

    monkeypatch.setattr(api, "disposition", disposition)
    monkeypatch.setattr(api, "close", close)
    monkeypatch.setattr(type(tombstones), "append", append)
    result = apply_gc(db, store.root, plan, confirm=True)
    assert result["deleted"] == 1
    assert events.index("deleting") < events.index("disposition") < events.index("close")
    assert events.index("close") < events.index("deleted")
    assert platform_for(db).meta.get("maintenance") is None


@native
@pytest.mark.parametrize("failure", ["disposition", "close", "probe"])
def test_native_gc_no_completion_after_leaf_failure(ntfs_root, monkeypatch, failure):
    from motte_storage import _windows_artifact_io as win
    from motte_storage.gc import apply_gc
    from motte_storage.platform import platform_for
    db, store, plan = _gc_fixture(ntfs_root)
    api, disposed = win._api(), []
    original_disposition, original_close, original_open = api.disposition, api.close, api.open

    def disposition(handle):
        if failure == "disposition":
            raise OSError("injected disposition failure")
        original_disposition(handle)
        disposed.append(handle.value)

    def close(handle):
        original_close(handle)  # Actual close; report uncertainty afterwards.
        if failure == "close" and handle in disposed:
            raise OSError("injected close uncertainty")

    def open_file(handles, parent, name, **kwargs):
        if failure == "probe" and disposed and name == "candidate.bin":
            raise OSError("injected probe error")
        return original_open(handles, parent, name, **kwargs)

    monkeypatch.setattr(api, "disposition", disposition)
    monkeypatch.setattr(api, "close", close)
    monkeypatch.setattr(api, "open", open_file)
    with pytest.raises(OSError):
        apply_gc(db, store.root, plan, confirm=True)
    rows = platform_for(db).tombstones.list()
    assert rows and all(row["deletion_status"] == "failed" for row in rows)
    assert all("deleted_at" not in row for row in rows)
    assert platform_for(db).meta.get("maintenance") is None


@native
def test_native_attribute_handle_delays_delete_without_false_completion(ntfs_root):
    from motte_storage.gc import apply_gc
    from motte_storage.platform import platform_for
    db, store, plan = _gc_fixture(ntfs_root)
    attacker = _attribute_handle(store.root / "ordinary/candidate.bin")
    try:
        with pytest.raises(OSError):
            apply_gc(db, store.root, plan, confirm=True)
        assert all(row["deletion_status"] == "failed"
                   for row in platform_for(db).tombstones.list())
    finally:
        attacker.close()
    assert platform_for(db).meta.get("maintenance") is None


def test_required_windows_runner_is_not_silently_skipped():
    if os.environ.get("MOTTE_REQUIRE_WINDOWS_ARTIFACT_TESTS"):
        assert os.name == "nt", "native acceptance requires an actual Windows runner"


def _short_basename(path):
    from motte_storage import _windows_artifact_io as win
    function = win._api().kernel.GetShortPathNameW
    function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32768)
    count = function(str(path), buffer, len(buffer))
    assert 0 < count < len(buffer), ctypes.WinError(ctypes.get_last_error())
    return Path(buffer.value).name


@native
@pytest.mark.parametrize("real_short_alias", [False, True])
def test_native_guard_follows_first_directory_pin(ntfs_root, monkeypatch, real_short_alias,
                                                 record_property):
    from motte_storage import _windows_artifact_io as win
    from motte_storage.artifacts import ArtifactStore
    store = ArtifactStore(ntfs_root / "artifacts")
    reserved = store.root / "trace-archives"
    if real_short_alias:
        reserved.mkdir()
        name = _short_basename(reserved)
        reserved.rmdir()
        if name.casefold() == "trace-archives":
            record_property("short_names", "unavailable; real alias branch not executed")
            pytest.skip("volume does not supply a reserved-directory short alias")
    else:
        name = "injected-alias"
    assert not reserved.exists()  # Earlier absence cannot authorize later traversal.
    original, events = win._api().open, []

    def open_file(handles, parent, component, **kwargs):
        if component == name and not events:
            reserved.mkdir()
            (reserved / "orphan.bin").write_bytes(b"newly introduced archive")
            events.append("introduced")
            if real_short_alias:
                assert _short_basename(reserved).casefold() == name.casefold()
            else:
                component = "trace-archives"  # Inject only alias lookup, not identity.
            handle = original(handles, parent, component, **kwargs)
            events.append("pinned")
            return handle
        if component == "trace-archives" and events:
            events.append("guard")
        return original(handles, parent, component, **kwargs)

    monkeypatch.setattr(win._api(), "open", open_file)
    with pytest.raises(ValueError, match="reserved"):
        store.put_bytes(name + "/orphan.bin", b"must not change archive")
    assert events == ["introduced", "pinned", "guard"]
    assert (reserved / "orphan.bin").read_bytes() == b"newly introduced archive"


@native
def test_native_reserved_case_variants_and_gc_orphans(ntfs_root):
    from datetime import UTC, datetime, timedelta
    from motte_storage.gc import apply_gc, plan_gc
    from motte_storage.run_store import SQLiteRunStore
    store, archive = _archive_fixture(ntfs_root)
    for name in ("TRACE-ARCHIVES/sha256/orphan.json", "Trace-Archives/sha256/orphan.json"):
        with pytest.raises(ValueError, match="reserved"):
            store.put_bytes(name, b"overwrite")
        with pytest.raises(ValueError, match="reserved"):
            store.delete(name)
    old = (datetime.now(UTC) - timedelta(days=200)).timestamp()
    os.utime(archive, (old, old))
    db = SQLiteRunStore(ntfs_root / "case.db")
    plan = plan_gc(db, store.root)
    assert not plan.deletable
    assert [row["reason"] for row in plan.protected] == ["trace_archive"]
    assert apply_gc(db, store.root, plan, confirm=True)["deleted"] == 0
    assert archive.read_bytes() == b"immutable evidence"


@native
def test_native_case_sensitive_reserved_normalized_basename(ntfs_root, monkeypatch, record_property):
    from motte_storage import _windows_artifact_io as win
    from motte_storage.artifacts import ArtifactStore
    store = ArtifactStore(ntfs_root / "artifacts")
    handle = _attribute_handle(store.root)
    try:
        enabled = ctypes.c_uint32(1)
        success = win._api().kernel.SetFileInformationByHandle(
            handle.value, 23, ctypes.byref(enabled), ctypes.sizeof(enabled))
        if not success:
            record_property("case_sensitive_fixture", f"unavailable: {ctypes.get_last_error()}")
            pytest.skip("temporary case-sensitive directory is unavailable; no privilege changes")
    finally:
        handle.close()
    for variant in ("trace-archives", "TRACE-ARCHIVES"):
        directory = store.root / variant
        directory.mkdir()
        (directory / "orphan.bin").write_bytes(variant.encode())
    original = win._api().open

    def alias(handles, parent, name, **kwargs):
        # Native open deliberately binds a second differently-cased directory;
        # only alias resolution is injected, never the handle's normalized name.
        if name == "other-alias":
            # Open the exact case, since case-insensitive lookup is ambiguous.
            with monkeypatch.context() as patch:
                original_create = win._api().nt.NtCreateFile

                def exact_case(output, access, attrs, *args):
                    attrs._obj.Attributes = 0
                    return original_create(output, access, attrs, *args)

                patch.setattr(win._api().nt, "NtCreateFile", exact_case)
                return original(handles, parent, "TRACE-ARCHIVES", **kwargs)
        return original(handles, parent, name, **kwargs)

    monkeypatch.setattr(win._api(), "open", alias)
    with pytest.raises(ValueError, match="reserved"):
        store.put_bytes("other-alias/orphan.bin", b"overwrite")
    for variant in ("trace-archives", "TRACE-ARCHIVES"):
        assert (store.root / variant / "orphan.bin").read_bytes() == variant.encode()


@native
def test_native_gc_post_plan_symlink_cannot_delete_path_reference(ntfs_root):
    from datetime import UTC, datetime, timedelta
    from motte_storage.gc import apply_gc, plan_gc
    from motte_storage.platform import platform_for
    db, store, _ = _gc_fixture(ntfs_root)
    data = b"expired ordinary"
    protected = store.put_bytes("protected.bin", data)
    old = (datetime.now(UTC) - timedelta(days=200)).timestamp()
    os.utime(protected.uri, (old, old))
    db.runs.create({"id": "run-reference", "status": "completed", "revision": 1,
                    "scenario_version": "replay@1", "case_ids": [],
                    "manifest": {"artifacts": [protected.id]}},
                   event={"run_id": "run-reference", "type": "completed"})
    plan = plan_gc(db, store.root)
    assert any(row["artifact_id"] == "ordinary/candidate.bin" for row in plan.deletable)
    assert any(row["artifact_id"] == protected.id for row in plan.protected)
    candidate = store.root / "ordinary/candidate.bin"
    candidate.unlink()
    _required_symlink(candidate, Path(protected.uri))
    result = apply_gc(db, store.root, plan, confirm=True)
    assert result["deleted"] == 0
    assert Path(protected.uri).read_bytes() == data
    assert platform_for(db).tombstones.list() == []


@native
def test_native_gc_parent_swap_before_disposition_preserves_archive(ntfs_root, monkeypatch):
    from motte_storage import _windows_artifact_io as win
    from motte_storage.gc import apply_gc
    db, store, plan = _gc_fixture(ntfs_root)
    archive = store.root / "trace-archives/sha256/candidate.bin"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"reserved orphan")
    parent, original, attempts = store.root / "ordinary", win._api().disposition, []

    def disposition(handle):
        attempts.append(True)
        try:
            parent.rename(store.root / "detached")
        except OSError as error:
            assert error.winerror in {5, 32}, error
        else:
            _required_junction(parent, archive.parent)
        return original(handle)

    monkeypatch.setattr(win._api(), "disposition", disposition)
    try:
        apply_gc(db, store.root, plan, confirm=True)
    except (ValueError, OSError):
        pass
    assert attempts
    assert archive.read_bytes() == b"reserved orphan"


def _hold_native_file(path, ready, release):
    from motte_storage import _windows_artifact_io as win
    api = win._api()
    with win._Handles(api) as handles:
        api.open(handles, None, "\\??\\" + str(path), directory=False,
                 access=win.GENERIC_READ, share=7)
        ready.put("held")
        if not release.wait(15):
            raise RuntimeError("parent failed to release native fixture")


@native
def test_native_other_process_sharing_conflict_and_release(ntfs_root):
    import multiprocessing
    from motte_storage.artifacts import ArtifactStore
    store = ArtifactStore(ntfs_root / "artifacts")
    store.put_bytes("ordinary.bin", b"preserved")
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Queue(), ctx.Event()
    child = ctx.Process(target=_hold_native_file, args=(store.root / "ordinary.bin", ready, release))
    child.start()
    try:
        assert ready.get(timeout=15) == "held"
        with pytest.raises(OSError) as failure:
            store.put_bytes("ordinary.bin", b"must not write")
        assert failure.value.winerror == 32
        assert (store.root / "ordinary.bin").read_bytes() == b"preserved"
    finally:
        release.set()
        child.join(20)
        if child.is_alive():
            child.terminate()
            child.join(5)
            pytest.fail("native sharing fixture hung")
        ready.close()
    assert child.exitcode == 0
    store.put_bytes("ordinary.bin", b"released")
    assert store.read_bytes("ordinary.bin") == b"released"


@native
def test_native_readonly_denial_and_maintenance_owner(ntfs_root):
    import stat
    from motte_storage.operation_locks import MaintenanceConflict, artifact_maintenance_lock
    from motte_storage.artifacts import ArtifactStore
    store = ArtifactStore(ntfs_root / "artifacts")
    store.put_bytes("ordinary.bin", b"preserved")
    leaf = store.root / "ordinary.bin"
    leaf.chmod(stat.S_IREAD)
    try:
        with pytest.raises(OSError):
            store.put_bytes(leaf.name, b"must not write")
        with pytest.raises(OSError):
            store.delete(leaf.name)
    finally:
        leaf.chmod(stat.S_IWRITE | stat.S_IREAD)
    assert leaf.read_bytes() == b"preserved"
    with artifact_maintenance_lock(store.root, "owner", "backup"):
        with pytest.raises(MaintenanceConflict):
            store.put_bytes("blocked.bin", b"x")
        with pytest.raises(MaintenanceConflict):
            store.delete(leaf.name, maintenance_owner="owner")
    with artifact_maintenance_lock(store.root, "owner", "gc"):
        store.delete(leaf.name, maintenance_owner="owner")
    assert not leaf.exists()


@native
def test_native_archive_durability_still_unsupported(ntfs_root):
    from motte_storage.artifacts import ArtifactStore
    from motte_storage.maintenance import begin_maintenance, end_maintenance
    from motte_storage.operation_locks import trace_archive_write_capability
    from motte_storage.run_store import SQLiteRunStore
    from motte_storage.trace_retention_models import TraceArchiveInvalid
    store = ArtifactStore(ntfs_root)
    db = SQLiteRunStore(ntfs_root.parent / (ntfs_root.name + ".db"))
    owner = begin_maintenance(db, reason="trace_retention", artifacts_root=store.root)["owner"]
    try:
        with trace_archive_write_capability(db, artifacts_root=store.root, maintenance_owner=owner):
            with pytest.raises(TraceArchiveInvalid, match="durability primitives are unsupported"):
                store.put_trace_archive(b"not decoded before unsupported check", maintenance_owner=owner)
    finally:
        end_maintenance(db, owner=owner)
    store.put_bytes("ordinary.bin", b"still writable")
    assert store.read_bytes("ordinary.bin") == b"still writable"


@native
def test_native_short_write_and_zero_progress(ntfs_root, monkeypatch):
    from motte_storage import _windows_artifact_io as win
    from motte_storage.artifacts import ArtifactStore
    store = ArtifactStore(ntfs_root / "artifacts")
    api, calls = win._api(), []
    original = api.kernel.WriteFile

    def short(handle, buffer, length, transferred, overlapped):
        calls.append(length)
        return original(handle, buffer, min(length, 2), transferred, overlapped)

    monkeypatch.setattr(api.kernel, "WriteFile", short)
    store.put_bytes("ordinary.bin", b"abcdefghi")
    assert store.read_bytes("ordinary.bin") == b"abcdefghi"
    assert len(calls) == 5

    def no_progress(handle, buffer, length, transferred, overlapped):
        transferred._obj.value = 0
        return 1

    monkeypatch.setattr(api.kernel, "WriteFile", no_progress)
    with pytest.raises(OSError, match="progress"):
        store.put_bytes("ordinary.bin", b"x")
    # Failed ordinary writes may be partial, but no exclusive handle may leak.
    store.delete("ordinary.bin")
    assert not (store.root / "ordinary.bin").exists()


@native
def test_native_handles_release_on_repeat_and_validation_failure(ntfs_root, monkeypatch):
    from motte_storage import _windows_artifact_io as win
    from motte_storage.artifacts import ArtifactStore
    store = ArtifactStore(ntfs_root / "artifacts")
    api = win._api()
    count_handles = api.kernel.GetProcessHandleCount
    count_handles.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    count_handles.restype = ctypes.c_int32

    def count():
        value = ctypes.c_uint32()
        assert count_handles(ctypes.c_void_p(-1), ctypes.byref(value))
        return value.value

    before = count()
    for _ in range(25):
        store.put_bytes("nested/ordinary.bin", b"bytes")
        store.delete("nested/ordinary.bin")
        store.delete("nested/ordinary.bin")
    assert count() <= before + 1
    original_info = api.info
    attempts = []

    def fail_once(handle):
        if not attempts:
            attempts.append(True)
            raise OSError("injected info failure")
        return original_info(handle)

    monkeypatch.setattr(api, "info", fail_once)
    with pytest.raises(OSError, match="injected info"):
        store.put_bytes("nested/ordinary.bin", b"x")
    assert count() <= before + 1
    store.put_bytes("nested/ordinary.bin", b"released")
    assert store.read_bytes("nested/ordinary.bin") == b"released"


@native
def test_native_unexpected_completed_status_closes_returned_handle(ntfs_root, monkeypatch):
    from motte_storage import _windows_artifact_io as win
    from motte_storage.artifacts import ArtifactMutationUnsupported, ArtifactStore
    store = ArtifactStore(ntfs_root / "artifacts")
    store.put_bytes("ordinary.bin", b"preserved")
    api, intercepted = win._api(), []
    original = api.nt.NtCreateFile

    def unexpected(output, access, attrs, *args):
        result = original(output, access, attrs, *args)
        name = attrs._obj.ObjectName.contents
        text = ctypes.wstring_at(name.Buffer, name.Length // 2)
        if result == 0 and text == "ordinary.bin":
            intercepted.append(output._obj.value)
            return 0x40000000  # Completed informational result is not approved success.
        return result

    monkeypatch.setattr(api.nt, "NtCreateFile", unexpected)
    with pytest.raises(ArtifactMutationUnsupported, match="status"):
        store.put_bytes("ordinary.bin", b"must not write")
    assert intercepted
    assert store.read_bytes("ordinary.bin") == b"preserved"
    leaf = store.root / "ordinary.bin"
    leaf.rename(store.root / "renamed.bin")  # No stale exclusive handle remains.


def test_portable_owned_cleanup_preserves_primary_exception():
    from motte_storage._windows_artifact_io import _Handles
    closed = []

    def close(value):
        closed.append(value)
        raise OSError("cleanup")

    with pytest.raises(ValueError, match="primary") as caught:
        with _Handles(SimpleNamespace(close=close)) as handles:
            handles.own(10)
            handles.own(11)
            raise ValueError("primary")
    assert closed == [11, 10]
    assert len(caught.value.__notes__) == 2


def test_portable_handle_is_never_closed_twice_after_failure():
    from motte_storage._windows_artifact_io import _Handle
    closed = []

    def close(value):
        closed.append(value)
        raise OSError("uncertain close")

    handle = _Handle(SimpleNamespace(close=close), 10)
    with pytest.raises(OSError):
        handle.close()
    handle.close()
    assert closed == [10]


def test_windows_reserved_classification_does_not_change_posix(monkeypatch):
    import motte_storage.trace_archives as module
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    assert module.is_trace_archive_id("TRACE-ARCHIVES/a")
    monkeypatch.setattr(module, "os", SimpleNamespace(name="posix"))
    assert not module.is_trace_archive_id("TRACE-ARCHIVES/a")


def test_portable_guard_identity_lookup_occurs_after_first_pin(tmp_path, monkeypatch):
    from motte_storage import _windows_artifact_io as win
    events = []
    identifiers = {1: (1, b"root"), 2: (1, b"reserved"), 3: (1, b"reserved")}

    def info(handle):
        return (identifiers[handle.value], SimpleNamespace(FileAttributes=0, ReparseTag=0),
                SimpleNamespace(Directory=1, DeletePending=0, NumberOfLinks=1))

    api = SimpleNamespace(info=info, close=lambda handle: None,
                          basename=lambda handle: "short-alias")

    def root(native_api, handles, path):
        return [(handles.own(1), identifiers[1])]

    def directory(native_api, handles, parent, name, *, create=False):
        events.append(name)
        value = 2 if name == "alias" else 3
        return handles.own(value), identifiers[value]

    monkeypatch.setattr(win, "_api", lambda: api)
    monkeypatch.setattr(win, "_root", root)
    monkeypatch.setattr(win, "_directory", directory)
    with pytest.raises(ValueError, match="reserved"):
        with win.pinned_target(tmp_path, "alias/file", identifiers[1], write=True):
            pytest.fail("reserved identity escaped the guard")
    assert events == ["alias", "trace-archives"]
