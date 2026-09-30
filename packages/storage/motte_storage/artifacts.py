import hashlib
import os
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path
from motte_contracts.evidence import Artifact

from .operation_locks import artifact_mutation, trace_archive_mutation
from .trace_archives import (
    _decode_trace_archive, is_trace_archive_id, trace_archive_artifact_id,
)
from .trace_retention_models import TraceArchiveInvalid


class ArtifactMutationUnsupported(ValueError):
    """This platform cannot safely bind ordinary mutation to validated handles."""


class _PinnedArtifact:
    """One safely opened ordinary file and its pinned directory ancestry.

    A name is only used relative to the parent descriptor that was validated.
    Namespace changes never redirect a mutation into a replacement directory.
    The caller keeps the existing artifact mutation/maintenance lock throughout.
    """

    def __init__(self, path, parent_fd, name, fd, links):
        self.path, self.parent_fd, self.name, self.fd = path, parent_fd, name, fd
        self.links = links

    def validate_parent(self):
        for parent, name, fd in self.links:
            linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
            opened = os.fstat(fd)
            if ((linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino)
                    or stat.S_ISLNK(linked.st_mode)):
                raise ValueError("artifact namespace changed during mutation")

    def validate(self):
        self.validate_parent()
        linked = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
        opened = os.fstat(self.fd)
        if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(linked.st_mode)
                or (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino)):
            raise ValueError("artifact target changed during mutation")

    def write_bytes(self, data):
        self.validate()
        if os.fstat(self.fd).st_nlink != 1:
            raise ValueError("hard-linked evidence may alias an immutable Trace archive")
        # Never truncate by pathname: this descriptor was opened without following
        # links and validated before any bytes are removed or replaced.
        os.ftruncate(self.fd, 0)
        view = memoryview(data)
        while view:
            count = os.write(self.fd, view)
            if count <= 0:
                raise OSError("short artifact write")
            view = view[count:]
        self.validate()

    def read_bytes(self):
        self.validate()
        os.lseek(self.fd, 0, os.SEEK_SET)
        chunks = []
        while chunk := os.read(self.fd, 1024 * 1024):
            chunks.append(chunk)
        self.validate()
        return b"".join(chunks)

    def unlink(self):
        self.validate()
        os.unlink(self.name, dir_fd=self.parent_fd)
        self.validate_parent()

    def exists(self):
        self.validate_parent()
        try:
            os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True


class ArtifactStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self._archive_root_preexisting = self.root.is_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        info = self.root.stat()
        self._root_identity = (info.st_dev, info.st_ino)
        if os.name == "nt":
            from ._windows_artifact_io import root_identity
            self._root_identity = root_identity(self.root)

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

    def _ordinary_mutation_path(self, artifact_id: str) -> Path:
        if is_trace_archive_id(artifact_id):
            raise ValueError("trace-archives is a reserved immutable namespace")
        path = self._resolve_artifact_path(artifact_id)
        if path == self.root:
            raise ValueError("artifact path must name a file")
        if is_trace_archive_id(path.relative_to(self.root).as_posix()):
            raise ValueError("trace-archives is a reserved immutable namespace")
        return path

    @contextmanager
    def _pinned_mutation_target(
        self, artifact_id: str, *, write: bool = False, resolve_aliases: bool = True,
        read: bool = False,
    ):
        """Internal target for ordinary put/delete and GC under their existing locks.

        GC only plans physical files, so it disables alias resolution: a newly
        introduced link must not redirect deletion into a path-only reference.
        """
        if os.name == "nt":
            from ._windows_artifact_io import pinned_target
            with pinned_target(self.root, artifact_id, self._root_identity,
                               write=write, read=read) as target:
                yield target
            return
        path = self._ordinary_mutation_path(artifact_id)
        if not resolve_aliases:
            path = self.root / artifact_id
        required = ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
        if (os.name != "posix" or any(not hasattr(os, name) for name in required)
                or any(function not in os.supports_dir_fd
                       for function in (os.open, os.mkdir, os.stat, os.unlink))):
            raise ArtifactMutationUnsupported("safe artifact mutation primitives are unsupported")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        with ExitStack() as handles:
            def open_fd(name, flags, *, dir_fd=None):
                fd = os.open(name, flags, 0o666, dir_fd=dir_fd)
                handles.callback(os.close, fd)
                return fd

            links = []
            parent = open_fd(self.root.anchor, flags)
            for part in self.root.parts[1:]:
                child = open_fd(part, flags, dir_fd=parent)
                links.append((parent, part, child))
                parent = child
            info = os.fstat(parent)
            if (info.st_dev, info.st_ino) != self._root_identity:
                raise ValueError("artifact root changed since store construction")
            for part in path.relative_to(self.root).parts[:-1]:
                if write:
                    try:
                        os.mkdir(part, dir_fd=parent)
                    except FileExistsError:
                        pass
                child = open_fd(part, flags, dir_fd=parent)
                links.append((parent, part, child))
                parent = child
            file_flags = os.O_WRONLY | os.O_CREAT if write else os.O_RDONLY
            fd = open_fd(path.name, file_flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            target = _PinnedArtifact(path, parent, path.name, fd, links)
            target.validate()
            yield target

    def put_bytes(
        self, artifact_id: str, data: bytes, *, kind: str = "file", media_type: str | None = None
    ) -> Artifact:
        with artifact_mutation(self.root), self._pinned_mutation_target(artifact_id, write=True) as target:
            target.write_bytes(data)
            path = target.path
        return Artifact(
            id=artifact_id, kind=kind, uri=str(path), sha256=hashlib.sha256(data).hexdigest()
        )

    def read_bytes(self, artifact_id: str) -> bytes:
        return self._resolve_artifact_path(artifact_id).read_bytes()

    def delete(self, artifact_id: str, *, maintenance_owner: str | None = None) -> None:
        with artifact_mutation(self.root, maintenance_owner=maintenance_owner):
            try:
                with self._pinned_mutation_target(artifact_id) as target:
                    target.unlink()
            except FileNotFoundError:
                pass  # Existing missing_ok behavior, including an absent parent.

    def put_trace_archive(self, data: bytes, *, maintenance_owner: str) -> Artifact:
        """Exclusively create, persist, then read back one immutable Trace archive.

        The root must have been provisioned durably before constructing this
        ArtifactStore. This API creates only the reserved descendants; it cannot
        establish durability of an arbitrary, newly created root's ancestors.
        Existing files/directories are not evidence of a successful earlier
        fsync, so every successful retry syncs the entire chain again.
        """
        with trace_archive_mutation(self.root, maintenance_owner=maintenance_owner):
            if not self._archive_root_preexisting:
                raise TraceArchiveInvalid("archive writes require an existing durable provisioned root")
            required = ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
            if (os.name != "posix" or any(not hasattr(os, name) for name in required)
                    or os.open not in os.supports_dir_fd or os.mkdir not in os.supports_dir_fd
                    or os.stat not in os.supports_dir_fd):
                raise TraceArchiveInvalid("safe archive durability primitives are unsupported")
            _decode_trace_archive(data)
            artifact_id = trace_archive_artifact_id(data)
            filename = Path(artifact_id).name
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            with ExitStack() as handles:
                def open_fd(path, flags, *, dir_fd=None, mode=0o600):
                    fd = os.open(path, flags, mode, dir_fd=dir_fd)
                    handles.callback(os.close, fd)
                    return fd

                # Traverse every root component via pinned no-follow directory
                # descriptors. Check their identities again before returning.
                links = []
                parent = open_fd(self.root.anchor, directory_flags)
                for part in self.root.parts[1:]:
                    child = open_fd(part, directory_flags, dir_fd=parent)
                    links.append((parent, part, child))
                    parent = child
                root_fd = parent
                info = os.fstat(root_fd)
                if (info.st_dev, info.st_ino) != self._root_identity:
                    raise TraceArchiveInvalid("artifact root changed since store construction")
                archive_dirs = []
                for part in ("trace-archives", "sha256"):
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                    child = open_fd(part, directory_flags, dir_fd=parent)
                    links.append((parent, part, child))
                    archive_dirs.append(child)
                    parent = child
                try:
                    fd = open_fd(filename, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                                 | os.O_NONBLOCK, dir_fd=parent)
                except FileExistsError:
                    fd = open_fd(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=parent)
                else:
                    written = os.write(fd, data)
                    if written != len(data):
                        raise TraceArchiveInvalid("short write while creating immutable Trace archive")
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise TraceArchiveInvalid("Trace archive must be a single-link regular file")
                links.append((parent, filename, fd))
                # Raw fd writes have no userspace buffer to flush. File data and
                # every namespace edge must be durable before readback/return.
                os.fsync(fd)
                for directory in [*reversed(archive_dirs), root_fd]:
                    os.fsync(directory)
                os.lseek(fd, 0, os.SEEK_SET)
                chunks = []
                remaining = len(data) + 1
                while remaining:
                    chunk = os.read(fd, min(remaining, 1024 * 1024))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                readback = b"".join(chunks)
                if readback != data:
                    raise TraceArchiveInvalid("existing Trace archive bytes differ from content identity")
                _decode_trace_archive(readback)
                for directory, name, opened in links:
                    linked = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    pinned = os.fstat(opened)
                    if ((linked.st_dev, linked.st_ino) != (pinned.st_dev, pinned.st_ino)
                            or stat.S_ISLNK(linked.st_mode)):
                        raise TraceArchiveInvalid("Trace archive namespace changed during write")
            return Artifact(id=artifact_id, kind="file", uri=str(self.root / artifact_id),
                            sha256=hashlib.sha256(readback).hexdigest())
