"""Agent file-task dataset contracts (suite ``agent-tasks``).

Cases pair a natural-language task with a fixture workspace. ``expected`` and
``forbidden_paths`` are platform-side assertions and are never projected into
the model prompt or the tool-visible workspace.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from .identity import canonical_sha256
from .messages import Contract

SUITE = "agent-tasks"
PLUGIN_VERSION = "1"
DATASET_VERSION = "1"
SCORER_ID = "agent-deterministic"
SCORER_VERSION = "1"
SCENARIO_MODE = "agent-tasks"

AGENT_BACKEND_ID = "builtin-agent"
AGENT_BACKEND_VERSION = "1"
AGENT_MODES = ("native-tool", "legacy-json")
AGENT_TOOLS = ("list_files", "read_file", "write_file")


def case_ids_sha256(case_ids: list[str]) -> str:
    return canonical_sha256({"case_ids": case_ids})


class AgentTaskCase(Contract):
    case_id: str = Field(min_length=1)
    input: str = Field(min_length=1)
    fixture: dict[str, str] = Field(default_factory=dict)
    expected: dict[str, Any] | None = None
    forbidden_paths: list[str] = Field(default_factory=list)
    limits: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def safe_paths(self) -> AgentTaskCase:
        from .evaluation import validate_safe_relative_path

        for path in self.fixture:
            validate_safe_relative_path(path)
        for path in self.forbidden_paths:
            validate_safe_relative_path(path)
        return self


class AgentTasksDataset(Contract):
    schema_version: Literal[1] = 1
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    cases: list[AgentTaskCase] = Field(min_length=1)
    cases_sha256: str | None = None
    dataset_fingerprint: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_cases(self) -> AgentTasksDataset:
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("agent task case_id values must be unique")
        return self


def normalize_agent_tasks_dataset(record: dict[str, Any]) -> dict[str, Any]:
    """校验并填充派生 hash；显式 supplied hash 不一致时拒绝。"""
    dataset = AgentTasksDataset.model_validate(record).model_dump(mode="json")
    cases_hash = canonical_sha256(dataset["cases"])
    if dataset.get("cases_sha256") not in (None, cases_hash):
        raise ValueError("cases_sha256 mismatch")
    dataset["cases_sha256"] = cases_hash
    fingerprint = canonical_sha256({
        key: value for key, value in dataset.items()
        if key not in {"name", "version", "dataset_fingerprint"}
    })
    if dataset.get("dataset_fingerprint") not in (None, fingerprint):
        raise ValueError("dataset_fingerprint mismatch")
    dataset["dataset_fingerprint"] = fingerprint
    return dataset


def scenario_for_agent_tasks(dataset: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": dataset["name"],
        "version": dataset["version"],
        "mode": SCENARIO_MODE,
        "dataset": f"{dataset['name']}@{dataset['version']}",
        "suite": SUITE,
        "plugin_version": PLUGIN_VERSION,
        "case_count": len(dataset["cases"]),
    }


def agent_task_metrics(case: dict[str, Any]) -> list[dict[str, Any]]:
    """从隐藏 expected 派生确定性指标配置（评分侧专用，不进入模型输入）。"""
    expected = case.get("expected") or {}
    metrics: list[dict[str, Any]] = []
    for path, rule in (expected.get("files") or {}).items():
        if not isinstance(rule, dict):
            raise ValueError(f"expected file rule must be an object: {path}")
        metrics.append({
            "metric_id": f"file-content:{path}",
            "kind": "file-content",
            "path": path,
            **rule,
        })
    final = expected.get("final")
    if isinstance(final, dict) and final.get("kind"):
        metrics.append({"metric_id": "final-answer", **final})
    forbidden = list(case.get("forbidden_paths") or [])
    if forbidden:
        metrics.append({
            "metric_id": "no-forbidden-write",
            "kind": "no-forbidden-write",
            "forbidden": forbidden,
        })
    if not metrics:
        # 无可判定期望：显式 no_expectation 指标，不进入分母（G12）。
        metrics.append({"metric_id": "completion", "kind": "exact"})
    return metrics
