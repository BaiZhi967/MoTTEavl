import hashlib
from pathlib import Path
from motte_contracts.evidence import Artifact


class ArtifactStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve_artifact_path(self, artifact_id: str) -> Path:
        """Resolve an artifact id and enforce the store root boundary.

        Path.resolve is intentionally applied before the containment check so
        existing symlink components cannot redirect reads or deletes outside the
        configured root.
        """
        relative = Path(artifact_id)
        if relative.is_absolute() or relative.drive or ".." in relative.parts:
            raise ValueError("artifact path escapes root")
        resolved = (self.root / relative).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise ValueError("artifact path escapes root") from error
        return resolved

    def put_bytes(
        self, artifact_id: str, data: bytes, *, kind: str = "file", media_type: str | None = None
    ) -> Artifact:
        path = self._resolve_artifact_path(artifact_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return Artifact(
            id=artifact_id, kind=kind, uri=str(path), sha256=hashlib.sha256(data).hexdigest()
        )

    def read_bytes(self, artifact_id: str) -> bytes:
        return self._resolve_artifact_path(artifact_id).read_bytes()

    def delete(self, artifact_id: str) -> None:
        self._resolve_artifact_path(artifact_id).unlink(missing_ok=True)
