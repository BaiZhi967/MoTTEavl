from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SandboxPolicy:
    network: str = "none"
    privileged: bool = False
    workspace: str = "workspace"
    memory_mb: int = 512
    timeout_seconds: int = 60

    def __post_init__(self):
        if self.privileged:
            raise ValueError("privileged mode is forbidden")
        if Path(self.workspace).is_absolute() or ".." in Path(self.workspace).parts:
            raise ValueError("workspace escape")
