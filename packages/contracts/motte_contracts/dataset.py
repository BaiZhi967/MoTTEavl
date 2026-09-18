import hashlib, json
from typing import Any

from pydantic import Field

from .messages import Contract
from .scenario import Case


class DatasetVersion(Contract):
    cases: list[Case]
    line_count: int
    sha256: str
    encoding: str = "utf-8"
    schema_version: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)


def validate_jsonl(text: str, *, encoding: str = "utf-8") -> DatasetVersion:
    cases: list[Case] = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON at line {n}") from e
        try:
            case = Case.model_validate(obj)
        except Exception as e:
            raise ValueError(f"invalid case at line {n}") from e
        if any(c.case_id == case.case_id for c in cases):
            raise ValueError(f"duplicate case_id: {case.case_id}")
        cases.append(case)
    return DatasetVersion(
        cases=cases,
        line_count=len(cases),
        sha256=hashlib.sha256(text.encode(encoding)).hexdigest(),
        encoding=encoding,
    )
