from motte_storage.artifacts import ArtifactStore

def test_artifact_store_writes_and_reads_bytes(tmp_path):
    store = ArtifactStore(tmp_path)
    artifact = store.put_bytes("run-1/out.txt", b"hello")
    assert artifact.sha256
    assert store.read_bytes(artifact.id) == b"hello"

def test_artifact_store_rejects_path_escape(tmp_path):
    import pytest
    with pytest.raises(ValueError): ArtifactStore(tmp_path).put_bytes("../escape", b"x")
