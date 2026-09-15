"""Sandbox 安全策略。

command policy 与 network policy 相互独立；默认全部收窄：
- 网络：只有 "none" 合法（默认拒绝网络）。
- 命令：argv 列表（无 shell），可配置程序白名单/黑名单。
- 环境变量：只有显式声明的名字会传入容器。
"""
from dataclasses import dataclass, field
from pathlib import Path


class PolicyViolationError(ValueError):
    """策略违规：在创建容器之前抛出，fail closed。"""


@dataclass(frozen=True)
class CommandPolicy:
    """命令策略：只接受 argv 列表，绝不经过 shell。"""

    allowed_programs: tuple[str, ...] | None = None
    forbidden_programs: tuple[str, ...] = ("docker", "sudo", "su")
    max_args: int = 128

    def validate_command(self, command) -> list[str]:
        if not isinstance(command, (list, tuple)) or not command:
            raise PolicyViolationError("command must be a non-empty argv list")
        argv = list(command)
        if len(argv) > self.max_args:
            raise PolicyViolationError(f"too many arguments: {len(argv)} > {self.max_args}")
        if any(not isinstance(part, str) or part == "" for part in argv):
            raise PolicyViolationError("command parts must be non-empty strings")
        program = Path(argv[0]).name
        if program in self.forbidden_programs:
            raise PolicyViolationError(f"forbidden program: {program}")
        if self.allowed_programs is not None and program not in self.allowed_programs:
            raise PolicyViolationError(f"program not allowed: {program}")
        return argv


@dataclass(frozen=True)
class SandboxPolicy:
    network: str = "none"
    commands: CommandPolicy = field(default_factory=CommandPolicy)
    workspace: str = "workspace"
    memory_mb: int = 512
    cpu_limit: float = 1.0
    pids_limit: int = 64
    disk_mb: int = 256
    max_output_bytes: int = 1_000_000
    timeout_seconds: int = 60
    environment: tuple[str, ...] = ()
    privileged: bool = False

    def __post_init__(self):
        if self.network != "none":
            raise PolicyViolationError("network policy only supports 'none' (deny by default)")
        if self.privileged:
            raise PolicyViolationError("privileged mode is forbidden")
        if Path(self.workspace).is_absolute() or ".." in Path(self.workspace).parts:
            raise PolicyViolationError("workspace escape")
        if self.memory_mb <= 0 or self.pids_limit <= 0 or self.disk_mb <= 0:
            raise PolicyViolationError("resource limits must be positive")
        if self.timeout_seconds <= 0:
            raise PolicyViolationError("timeout must be positive")
