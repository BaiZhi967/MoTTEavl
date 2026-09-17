from typing import Any
from pydantic import Field, field_validator, model_validator
from .messages import Contract


class ReasoningProfile(Contract):
    supported: bool = Field(default=False, strict=True)
    levels: list[str] = Field(default_factory=list, max_length=32)
    control: str | None = Field(default=None, max_length=4096)
    default_level: str | None = None

    @model_validator(mode="after")
    def validate_mapping(self) -> "ReasoningProfile":
        from .reasoning import LEGACY_CONTROLS, evaluate_control

        if any(not level.strip() or len(level) > 64 for level in self.levels):
            raise ValueError("reasoning.levels entries must be nonempty and at most 64 characters")
        if len(set(self.levels)) != len(self.levels):
            raise ValueError("reasoning.levels must be unique")
        if self.default_level is not None and (
            not self.supported or self.default_level not in self.levels
        ):
            raise ValueError("reasoning.default_level must belong to supported reasoning.levels")
        if self.control in LEGACY_CONTROLS:
            if self.default_level is not None:
                raise ValueError("replace legacy reasoning.control metadata with a CEL expression")
        elif self.control is not None:
            for level in self.levels or ["__validation__"]:
                evaluate_control(self.control, level)
        elif self.default_level is not None:
            raise ValueError("reasoning.default_level requires reasoning.control CEL")
        return self


class ParameterProfile(Contract):
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = Field(default=None, gt=0, strict=True)
    seed: int | None = None

    @field_validator("temperature", "top_p")
    @classmethod
    def probability_range(cls, v: Any) -> Any:
        if v is not None and not 0 <= v <= 1:
            raise ValueError("must be between 0 and 1")
        return v


class ModelProfile(Contract):
    id: str
    provider: str
    model: str | None = None
    enabled: bool = True
    capabilities: dict[str, Any]
    input_modalities: list[str] = Field(default_factory=lambda: ["text"])
    output_modalities: list[str] = Field(default_factory=lambda: ["text"])
    supports_tools: bool = False
    tool_features: list[str] = Field(default_factory=list)
    context_window: int | None = Field(default=None, gt=0, strict=True)
    max_output_tokens: int | None = Field(default=None, gt=0, strict=True)
    reasoning: ReasoningProfile = Field(default_factory=ReasoningProfile)
    parameters: ParameterProfile = Field(default_factory=ParameterProfile)
    provenance: dict[str, Any] = Field(default_factory=dict)
    profile_hash: str | None = None

    @field_validator("input_modalities")
    @classmethod
    def text_required(cls, v: list[str]) -> list[str]:
        if "text" not in v or any(not item.strip() for item in v):
            raise ValueError("input_modalities must include text and contain nonempty strings")
        return list(dict.fromkeys(v))

    @field_validator("capabilities")
    @classmethod
    def require_modalities(cls, v: dict[str, Any]) -> dict[str, Any]:
        if "input_modalities" in v and not v["input_modalities"]:
            raise ValueError("input_modalities cannot be empty")
        result = dict(v)
        for key in ("structured_output", "native_search", "system_messages"):
            if key in result and type(result[key]) is not bool:
                raise ValueError(f"capabilities.{key} must be a boolean")
            result.setdefault(key, False)
        return result
