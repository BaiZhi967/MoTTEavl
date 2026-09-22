from motte_storage.artifacts import ArtifactStore


def test_artifact_store_writes_and_reads_bytes(tmp_path):
    store = ArtifactStore(tmp_path)
    artifact = store.put_bytes("run-1/out.txt", b"hello")
    assert artifact.sha256
    assert store.read_bytes(artifact.id) == b"hello"


def test_artifact_store_rejects_path_escape(tmp_path):
    import pytest

    store = ArtifactStore(tmp_path / "root")
    for artifact_id in ("../escape", str(tmp_path / "absolute")):
        with pytest.raises(ValueError):
            store.put_bytes(artifact_id, b"x")
        with pytest.raises(ValueError):
            store.read_bytes(artifact_id)
        with pytest.raises(ValueError):
            store.delete(artifact_id)


def test_artifact_store_rejects_symlink_escape_on_read_and_delete(tmp_path):
    import pytest

    root = tmp_path / "root"
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    store = ArtifactStore(root)
    link = root / "escape.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(ValueError):
        store.read_bytes("escape.txt")
    with pytest.raises(ValueError):
        store.delete("escape.txt")
    assert outside.read_bytes() == b"secret"
    assert link.is_symlink()
