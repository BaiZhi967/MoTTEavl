"""真实 Docker Sandbox。

统一适配器契约：prepare / start / send / events / interrupt / collect / cleanup。
安全不变量（在创建容器之前全部校验，fail closed）：
- 默认且仅支持 --network none；
- 永不 privileged，drop ALL capabilities + no-new-privileges，以非 root 运行；
- 只挂载受控 workspace 目录（绝不挂 Docker socket）；
- 只有显式声明的环境变量会传入容器；
- CPU / 内存 / PID / 磁盘(tmpfs) / 输出 / TTL 限制；
- cleanup 在成功、失败、超时、取消路径上都必须执行。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path
from typing import Any

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
        self._events: list[dict[str, Any]] = []
        self._prepared = False
        self._collected = False
        self._last_result: dict[str, Any] | None = None

    # ------------------------------------------------------------------ 契约

    def prepare(self, files: dict[str, str] | None = None) -> dict[str, Any]:
        """创建受控 host workspace 并物化输入文件；返回执行 spec。"""
        base = self._workspace_root or Path(os.environ.get("MOTTE_SANDBOX_ROOT", "var/sandboxes"))
        base.mkdir(parents=True, exist_ok=True)
        self._host_workspace = base / self.policy.workspace
        for name in files or {}:
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise PolicyViolationError(f"file path escape: {name}")
        if self._host_workspace.exists():
            shutil.rmtree(self._host_workspace)
        self._host_workspace.mkdir(parents=True)
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
        self._container = self._docker_client().containers.run(
            self.image,
            command=argv,
            detach=True,
            network_mode=self.policy.network,
            privileged=False,
            user="nobody",
            working_dir="/workspace",
            mem_limit=f"{self.policy.memory_mb}m",
            nano_cpus=int(self.policy.cpu_limit * 1_000_000_000),
            pids_limit=self.policy.pids_limit,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            tmpfs={"/tmp": f"size={self.policy.disk_mb}m"},
            environment=self._declared_environment(),
            volumes={str(self._host_workspace): {"bind": "/workspace", "mode": "rw"}},
        )
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
        if self._host_workspace is not None and self._host_workspace.exists():
            shutil.rmtree(self._host_workspace, ignore_errors=True)
            removed_workspace = True
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
        try:
            raw = self._container.logs()
        except Exception:
            return ""
        if len(raw) > self.policy.max_output_bytes:
            raw = raw[-self.policy.max_output_bytes :]
        return raw.decode("utf-8", errors="replace")

    def _collect_artifacts(self) -> list[dict[str, Any]]:
        artifacts = []
        if self._host_workspace is None:
            return artifacts
        for path in sorted(self._host_workspace.rglob("*")):
            if path.is_file():
                data = path.read_bytes()
                artifacts.append(
                    {
                        "name": str(path.relative_to(self._host_workspace)),
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
        return artifacts

    def _record(self, event_type: str, **payload: Any) -> None:
        self._events.append({"type": event_type, **payload})
