"""真实 Docker Sandbox。

统一适配器契约：prepare / start / send / events / interrupt / collect / cleanup。
安全不变量（在创建容器之前全部校验，fail closed）：
- 默认且仅支持 --network none；
- 永不 privileged，drop ALL capabilities + no-new-privileges，以非 root 运行；
- 只挂载受控 workspace 目录（绝不挂 Docker socket）；
- 只有显式声明的环境变量会传入容器；
- CPU / 内存 / PID / 磁盘(tmpfs for /tmp and /workspace) / 输出 / TTL 限制；
- cleanup 在成功、失败、超时、取消路径上都必须执行。
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import tarfile
import time
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from .policy import PolicyViolationError, SandboxPolicy


class UnsupportedOperation(NotImplementedError):
    """该适配器不支持的通道（例如 sandbox 没有交互 stdin）。"""


class DockerSandbox:
    def __init__(
        self,
        policy: SandboxPolicy | None = None,
        *,
        image: str = "alpine:3.20",
        client: Any = None,
        workspace_root: str | Path | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self.policy = policy or SandboxPolicy()
        self.image = image
        self._client = client
        self._workspace_root = Path(workspace_root) if workspace_root else None
        self._clock = clock
        self._container: Any = None
        self._host_workspace: Path | None = None
        self._workspace_anchor_real: Path | None = None
        self._archive_workspace = False
        self._events: list[dict[str, Any]] = []
        self._prepared = False
        self._collected = False
        self._last_result: dict[str, Any] | None = None

    # ------------------------------------------------------------------ 契约

    def prepare(self, files: dict[str, str] | None = None) -> dict[str, Any]:
        """创建受控 host workspace 并物化输入文件；返回执行 spec。"""
        if self._prepared or self._host_workspace is not None:
            raise PolicyViolationError("sandbox already prepared")
        base = self._workspace_root or Path(os.environ.get("MOTTE_SANDBOX_ROOT", "var/sandboxes"))
        base.mkdir(parents=True, exist_ok=True)
        # Never reuse the policy workspace name: concurrent instances and repeated
        # runs must not be able to remove or observe one another's staging tree.
        workspace_parent = base / self.policy.workspace
        workspace_parent.mkdir(parents=True, exist_ok=True)
        self._workspace_anchor_real = workspace_parent.resolve()
        for name in files or {}:
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise PolicyViolationError(f"file path escape: {name}")
        for _ in range(3):
            candidate = workspace_parent / f"run-{uuid4().hex}"
            try:
                candidate.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                continue
            self._host_workspace = candidate
            break
        else:
            raise PolicyViolationError("could not allocate a unique workspace")
        for name, content in (files or {}).items():
            target = self._host_workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self._prepared = True
        self._record("prepared", workspace=str(self._host_workspace))
        return {"workspace": str(self._host_workspace), "image": self.image}

    def start(self, command: list[str]) -> dict[str, Any]:
        """校验策略后创建并启动 hardened 容器。"""
        if not self._prepared:
            raise PolicyViolationError("prepare() must run before start()")
        argv = self.policy.commands.validate_command(command)
        if self._container is not None:
            raise PolicyViolationError("container already started")
        kwargs = {
            "command": argv,
            "network_mode": self.policy.network,
            "privileged": False,
            "user": "nobody",
            "working_dir": "/workspace",
            "mem_limit": f"{self.policy.memory_mb}m",
            "nano_cpus": int(self.policy.cpu_limit * 1_000_000_000),
            "pids_limit": self.policy.pids_limit,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges"],
            # /workspace is a tmpfs as well as /tmp; a writable host bind cannot
            # be bounded by Docker's tmpfs limit and is therefore not used here.
            "tmpfs": {
                "/tmp": f"size={self.policy.disk_mb}m",
                "/workspace": f"size={self.policy.disk_mb}m",
            },
            "environment": self._declared_environment(),
        }
        containers = self._docker_client().containers
        if hasattr(containers, "create"):
            self._container = containers.create(self.image, **kwargs)
            self._put_input_archive()
            self._container.start()
            self._archive_workspace = True
        else:
            # Minimal fake clients used by offline tests predate create/start.
            # Real Docker clients always take the bounded tmpfs path above.
            kwargs["detach"] = True
            kwargs["volumes"] = {
                str(self._host_workspace): {"bind": "/workspace", "mode": "rw"}
            }
            self._container = containers.run(self.image, **kwargs)
            self._archive_workspace = False
        self._record("started", program=argv[0], container=self._container.id)
        return {"container_id": self._container.id}

    def send(self, data) -> dict[str, Any]:
        raise UnsupportedOperation("sandbox has no interactive channel")

    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def interrupt(self) -> dict[str, Any]:
        """取消：杀死正在运行的容器（幂等）。"""
        if self._container is None:
            return {"interrupted": False}
        try:
            self._container.kill()
        except Exception:  # 已退出的容器 kill 会报错，忽略
            pass
        self._record("interrupted", container=self._container.id)
        return {"interrupted": True}

    def collect(self) -> dict[str, Any]:
        """等待退出（受 TTL 约束），采集输出（受字节上限）与 workspace artifact。"""
        if self._container is None:
            raise PolicyViolationError("start() must run before collect()")
        if self._collected:
            return self._last_result
        started = self._clock()
        status = "exited"
        exit_code: int | None = None
        try:
            outcome = self._container.wait(timeout=self.policy.timeout_seconds)
            exit_code = outcome.get("StatusCode")
        except Exception:
            self.interrupt()
            status = "timeout"
            self._record("timed_out", ttl_seconds=self.policy.timeout_seconds)
        result = {
            "status": status,
            "exit_code": exit_code,
            "output": self._capped_output(),
            "duration_ms": int((self._clock() - started) * 1000),
            "artifacts": self._collect_artifacts(),
        }
        if status == "exited":
            self._record("exited", exit_code=exit_code)
        self._last_result = result
        self._collected = True
        return result

    def cleanup(self) -> dict[str, Any]:
        """成功 / 失败 / 超时 / 取消路径都必须调用；幂等。"""
        removed_container = False
        if self._container is not None:
            try:
                self._container.remove(force=True)
                removed_container = True
            except Exception:
                pass
            self._container = None
        removed_workspace = False
        workspace = self._host_workspace
        if workspace is not None:
            # Never follow a replaced workspace symlink or remove another run's
            # directory. The path is unique and must still be a real directory.
            try:
                resolved = workspace.resolve(strict=False)
            except OSError:
                resolved = None
            owned = (
                resolved is not None
                and self._workspace_anchor_real is not None
                and (resolved == self._workspace_anchor_real or self._workspace_anchor_real in resolved.parents)
            )
            if workspace.is_symlink() or not owned:
                self._record("workspace_cleanup_refused", workspace=str(workspace))
            elif workspace.exists() and workspace.is_dir():
                try:
                    shutil.rmtree(workspace, ignore_errors=False)
                except OSError:
                    self._record("workspace_cleanup_failed", workspace=str(workspace))
                else:
                    removed_workspace = True
        self._host_workspace = None
        self._workspace_anchor_real = None
        self._archive_workspace = False
        self._prepared = False
        self._collected = False
        self._record("cleaned_up", container_removed=removed_container, workspace_removed=removed_workspace)
        return {"container_removed": removed_container, "workspace_removed": removed_workspace}

    def run(self, command: list[str], files: dict[str, str] | None = None) -> dict[str, Any]:
        """便捷编排：prepare → start → collect，任何路径都执行 cleanup。"""
        try:
            self.prepare(files)
            self.start(command)
            return self.collect()
        finally:
            self.cleanup()

    # ------------------------------------------------------------------ 内部

    def _put_input_archive(self) -> None:
        """Copy prepared inputs into the bounded container tmpfs."""
        if self._host_workspace is None or not hasattr(self._container, "put_archive"):
            return
        entries: list[tuple[Path, str, int]] = []
        total = 0
        for source in sorted(self._host_workspace.rglob("*")):
            relative = source.relative_to(self._host_workspace).as_posix()
            if source.is_symlink():
                raise PolicyViolationError(f"input symlink is not allowed: {relative}")
            if not source.is_file():
                continue
            info = source.stat()
            if not stat.S_ISREG(info.st_mode):
                raise PolicyViolationError(f"input is not a regular file: {relative}")
            total += info.st_size
            self._validate_artifact_limits(len(entries) + 1, total, info.st_size, relative)
            entries.append((source, relative, info.st_size))
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            for source, relative, size in entries:
                info = tarfile.TarInfo(relative)
                info.size = size
                info.mode = 0o644
                info.uid = 65534
                info.gid = 65534
                with source.open("rb") as handle:
                    archive.addfile(info, handle)
        self._container.put_archive("/workspace", payload.getvalue())

    def _docker_client(self):
        if self._client is None:
            import docker

            self._client = docker.from_env()
        return self._client

    def _declared_environment(self) -> dict[str, str]:
        environment = {}
        for name in self.policy.environment:
            value = os.environ.get(name)
            if value is not None:
                environment[name] = value
        return environment

    def _capped_output(self) -> str:
        """Consume Docker logs incrementally and retain only the tail."""
        try:
            try:
                stream = self._container.logs(stream=True, stdout=True, stderr=True)
            except TypeError:
                # Compatibility with the tiny offline fake container.
                stream = self._container.logs()
        except Exception:
            return ""
        cap = self.policy.max_output_bytes
        tail = bytearray()
        if isinstance(stream, (bytes, bytearray, str)):
            chunks = (stream,)
        else:
            chunks = stream
        try:
            for chunk in chunks:
                if isinstance(chunk, str):
                    chunk = chunk.encode()
                if not chunk:
                    continue
                tail.extend(chunk[-cap:])
                if len(tail) > cap:
                    del tail[:-cap]
        except Exception:
            # A log stream can close while a container is being removed.
            pass
        return bytes(tail).decode("utf-8", errors="replace")

    def _collect_artifacts(self) -> list[dict[str, Any]]:
        if self._archive_workspace:
            return self._collect_archive_artifacts()
        return self._collect_host_artifacts()

    def _validate_artifact_limits(self, count: int, total: int, size: int, name: str) -> None:
        if count > self.policy.max_artifacts:
            raise PolicyViolationError(
                f"artifact count exceeds limit {self.policy.max_artifacts}"
            )
        if size > self.policy.max_artifact_bytes:
            raise PolicyViolationError(
                f"artifact {name!r} is {size} bytes > limit {self.policy.max_artifact_bytes}"
            )
        if total > self.policy.max_total_artifact_bytes:
            raise PolicyViolationError(
                f"artifact total exceeds limit {self.policy.max_total_artifact_bytes}"
            )

    @staticmethod
    def _hash_file(path: Path, expected_size: int, limit: int) -> str:
        digest = hashlib.sha256()
        seen = 0
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise PolicyViolationError(f"artifact is not a regular file: {path.name}")
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                while chunk := handle.read(min(1 << 16, limit - seen + 1)):
                    seen += len(chunk)
                    if seen > limit:
                        raise PolicyViolationError(f"artifact grew beyond limit {limit}")
                    digest.update(chunk)
        except OSError as error:
            raise PolicyViolationError(f"cannot read artifact {path.name!r}: {error}") from error
        finally:
            if fd >= 0:
                os.close(fd)
        if seen != expected_size:
            raise PolicyViolationError("artifact changed while it was collected")
        return digest.hexdigest()

    def _collect_host_artifacts(self) -> list[dict[str, Any]]:
        if self._host_workspace is None:
            return []
        entries: list[tuple[Path, str, int]] = []
        total = 0
        for path in sorted(self._host_workspace.rglob("*")):
            relative = path.relative_to(self._host_workspace).as_posix()
            try:
                link_info = path.lstat()
            except OSError as error:
                raise PolicyViolationError(f"cannot inspect artifact {relative!r}: {error}") from error
            if stat.S_ISLNK(link_info.st_mode):
                raise PolicyViolationError(f"artifact symlink rejected: {relative}")
            if stat.S_ISDIR(link_info.st_mode):
                continue
            try:
                info = path.stat()
            except OSError as error:
                raise PolicyViolationError(f"cannot stat artifact {relative!r}: {error}") from error
            if not path.is_file():
                raise PolicyViolationError(f"artifact is not a regular file: {relative}")
            total += info.st_size
            self._validate_artifact_limits(len(entries) + 1, total, info.st_size, relative)
            entries.append((path, relative, info.st_size))
        artifacts = []
        for path, relative, size in entries:
            artifacts.append({
                "name": relative,
                "size": size,
                "sha256": self._hash_file(path, size, self.policy.max_artifact_bytes),
            })
        return artifacts

    def _collect_archive_artifacts(self) -> list[dict[str, Any]]:
        try:
            stream, _ = self._container.get_archive("/workspace")
        except Exception as error:
            raise PolicyViolationError(f"cannot collect workspace archive: {error}") from error
        if isinstance(stream, (bytes, bytearray)):
            stream = io.BytesIO(stream)
        artifacts: list[dict[str, Any]] = []
        total = 0
        try:
            with tarfile.open(fileobj=stream, mode="r|*") as archive:
                for member in archive:
                    name = PurePosixPath(member.name).as_posix()
                    if name in (".", "") or name.endswith("/"):
                        continue
                    if member.issym() or member.islnk():
                        raise PolicyViolationError(f"artifact symlink rejected: {name}")
                    if member.isdev() or not member.isfile():
                        raise PolicyViolationError(f"artifact is not a regular file: {name}")
                    if name.startswith("/") or ".." in PurePosixPath(name).parts:
                        raise PolicyViolationError(f"artifact path escape: {name}")
                    total += member.size
                    self._validate_artifact_limits(len(artifacts) + 1, total, member.size, name)
                    source = archive.extractfile(member)
                    if source is None:
                        raise PolicyViolationError(f"cannot read artifact: {name}")
                    digest = hashlib.sha256()
                    seen = 0
                    while chunk := source.read(min(1 << 16, self.policy.max_artifact_bytes - seen + 1)):
                        seen += len(chunk)
                        if seen > self.policy.max_artifact_bytes:
                            raise PolicyViolationError(f"artifact grew beyond limit {self.policy.max_artifact_bytes}")
                        digest.update(chunk)
                    if seen != member.size:
                        raise PolicyViolationError(f"artifact changed while it was collected: {name}")
                    artifacts.append({"name": name, "size": member.size, "sha256": digest.hexdigest()})
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        return sorted(artifacts, key=lambda item: item["name"])

    def _record(self, event_type: str, **payload: Any) -> None:
        self._events.append({"type": event_type, **payload})
