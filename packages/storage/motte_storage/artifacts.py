import hashlib
from pathlib import Path
from motte_contracts.evidence import Artifact

class ArtifactStore:
    def __init__(self, root: str | Path): self.root = Path(root).resolve(); self.root.mkdir(parents=True, exist_ok=True)
    def put_bytes(self, artifact_id: str, data: bytes, *, kind: str = "file", media_type: str | None = None) -> Artifact:
        rel = Path(artifact_id)
        if rel.is_absolute() or ".." in rel.parts: raise ValueError("artifact path escapes root")
        path = (self.root / rel).resolve()
        if self.root not in path.parents and path != self.root: raise ValueError("artifact path escapes root")
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
        return Artifact(id=artifact_id, kind=kind, uri=str(path), sha256=hashlib.sha256(data).hexdigest())
    def read_bytes(self, artifact_id: str) -> bytes:
        rel=Path(artifact_id)
        if rel.is_absolute() or ".." in rel.parts: raise ValueError("artifact path escapes root")
        return (self.root / rel).resolve().read_bytes()
    def delete(self, artifact_id: str) -> None: (self.root / artifact_id).resolve().unlink(missing_ok=True)
