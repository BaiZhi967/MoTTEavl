from dataclasses import dataclass, field


@dataclass(frozen=True)
class SkillManifest:
    name: str
    version: str
    entrypoint: str
    permissions: tuple[str, ...] = field(default_factory=tuple)
