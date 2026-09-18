"""Public error payloads for execution and API responses."""
from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, Field, model_validator

from .messages import Contract


class ContractError(Contract):
    code: str
    message: str
    pointer: str = ""


class ExecutionError(Contract):
    code: str | None = None
    message: str | None = None
    type: str | None = None
    error_class: str | None = Field(default=None, validation_alias=AliasChoices("error_class", "class"))
    evidence: dict[str, Any] | None = None
    field: str | None = None
    pointer: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def identified_error(self) -> ExecutionError:
        if not (self.code or self.type or self.error_class):
            raise ValueError("execution error requires code, type or error_class")
        return self


class ErrorEnvelope(Contract):
    error: ExecutionError
    request_id: str | None = None
