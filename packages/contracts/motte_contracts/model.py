from typing import Any
from pydantic import Field, field_validator
from .messages import Contract

class ReasoningProfile(Contract):
    supported: bool = False
    levels: list[str] = Field(default_factory=list)
    control: str | None = None

class ParameterProfile(Contract):
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    seed: int | None = None
    @field_validator("temperature", "top_p")
    @classmethod
    def probability_range(cls, v):
        if v is not None and not 0 <= v <= 1: raise ValueError("must be between 0 and 1")
        return v

class ModelProfile(Contract):
    id: str
    provider: str
    capabilities: dict[str, Any]
    input_modalities: list[str] = Field(default_factory=lambda: ["text"])
    output_modalities: list[str] = Field(default_factory=lambda: ["text"])
    supports_tools: bool = False
    tool_features: list[str] = Field(default_factory=list)
    context_window: int | None = None
    max_output_tokens: int | None = None
    reasoning: ReasoningProfile = Field(default_factory=ReasoningProfile)
    parameters: ParameterProfile = Field(default_factory=ParameterProfile)
    provenance: dict[str, Any] = Field(default_factory=dict)
    profile_hash: str | None = None

    @field_validator("capabilities")
    @classmethod
    def require_modalities(cls, v):
        if "input_modalities" in v and not v["input_modalities"]: raise ValueError("input_modalities cannot be empty")
        return v
