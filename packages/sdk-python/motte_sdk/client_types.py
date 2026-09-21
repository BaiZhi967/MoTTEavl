"""M7 SDK 轻量 typed 响应视图（协议 §1.2 前向/后向兼容）。

每个视图是 dict 的 typed 投影：已知核心字段提升为属性，未知字段保留在
``extra`` 中不丢弃（后向兼容）；未来新增的服务端字段不会破坏旧 SDK。
视图不复制大对象语义——``raw`` 保留原始 payload 的浅引用，供调用方透传。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

#: Run 终态集合（与 motte_sdk.service.RunService.TERMINAL 同值；此处独立定义，
#: 避免 SDK HTTP 客户端导入执行栈——协议 §1.5）。
TERMINAL_RUN_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "cancelled",
        "unsupported",
        "profile_stale",
        "needs_review",
    }
)

__all__ = [
    "TERMINAL_RUN_STATUSES",
    "Baseline",
    "Capabilities",
    "Comparison",
    "EventsSnapshot",
    "Experiment",
    "Gate",
    "Report",
    "RunList",
    "RunView",
    "ScoringPassList",
    "is_terminal_status",
]


def is_terminal_status(status: str | None) -> bool:
    return status in TERMINAL_RUN_STATUSES


def _split(payload: Mapping[str, Any], known: tuple[str, ...]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in known}


@dataclass(frozen=True)
class RunView:
    """GET/POST /runs* 返回的 Run 视图（含 idempotent_replay 等附加字段在 extra）。"""

    id: str
    status: str
    scenario_version: str | None = None
    revision: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    _KNOWN = (
        "id", "status", "scenario_version", "revision", "created_at", "updated_at",
    )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> RunView:
        known = cls._KNOWN
        return cls(
            id=str(payload.get("id", "")),
            status=str(payload.get("status", "")),
            scenario_version=payload.get("scenario_version"),
            revision=payload.get("revision"),
            created_at=payload.get("created_at"),
            updated_at=payload.get("updated_at"),
            extra=_split(payload, known),
            raw=dict(payload),
        )

    @property
    def is_terminal(self) -> bool:
        return is_terminal_status(self.status)

    @property
    def idempotent_replay(self) -> bool | None:
        value = self.extra.get("idempotent_replay")
        return value if isinstance(value, bool) else None


@dataclass(frozen=True)
class RunList:
    items: list[RunView]
    total: int
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> RunList:
        items = [
            RunView.from_payload(item)
            for item in payload.get("items", [])
            if isinstance(item, Mapping)
        ]
        return cls(
            items=items,
            total=int(payload.get("total", len(items))),
            extra=_split(payload, ("items", "total")),
        )


@dataclass(frozen=True)
class EventsSnapshot:
    """GET /runs/{id}/events/snapshot（协议 §2 持久查询）。"""

    events: list[dict[str, Any]]
    last_seq: int | None
    run_status: str
    partial: bool
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> EventsSnapshot:
        last_seq = payload.get("last_seq")
        return cls(
            events=[dict(event) for event in payload.get("events", [])],
            last_seq=int(last_seq) if isinstance(last_seq, int) else None,
            run_status=str(payload.get("run_status", "")),
            partial=bool(payload.get("partial", False)),
            extra=_split(payload, ("events", "last_seq", "run_status", "partial")),
        )


@dataclass(frozen=True)
class Capabilities:
    """GET /api/v1/capabilities（协议 §1.2 能力握手）。"""

    name: str
    api_version: str
    app_version: str
    features: dict[str, bool]
    limits: dict[str, int]
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Capabilities:
        features = {
            str(key): bool(value)
            for key, value in (payload.get("features") or {}).items()
            if isinstance(value, bool)
        }
        limits = {
            str(key): int(value)
            for key, value in (payload.get("limits") or {}).items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
        return cls(
            name=str(payload.get("name", "")),
            api_version=str(payload.get("api_version", "")),
            app_version=str(payload.get("app_version", "")),
            features=features,
            limits=limits,
            extra=_split(payload, ("name", "api_version", "app_version", "features", "limits")),
        )

    def supports(self, feature: str) -> bool:
        """未知 feature 键按缺失处理；已知键必须显式为 True。"""
        return self.features.get(feature) is True


@dataclass(frozen=True)
class ScoringPassList:
    items: list[dict[str, Any]]
    total: int
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ScoringPassList:
        items = [dict(item) for item in payload.get("items", [])]
        return cls(
            items=items,
            total=int(payload.get("total", len(items))),
            extra=_split(payload, ("items", "total")),
        )


@dataclass(frozen=True)
class Report:
    """GET /runs/{id}/report：原始 dict 透传 + typed 核心字段。"""

    run_id: str
    status: str
    schema_version: int | None
    scoring_pass_id: str | None
    summary: dict[str, Any]
    extra: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    _KNOWN = ("run_id", "status", "schema_version", "scoring_pass_id", "summary")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Report:
        known = cls._KNOWN
        summary = payload.get("summary")
        return cls(
            run_id=str(payload.get("run_id", "")),
            status=str(payload.get("status", "")),
            schema_version=payload.get("schema_version"),
            scoring_pass_id=payload.get("scoring_pass_id"),
            summary=dict(summary) if isinstance(summary, Mapping) else {},
            extra=_split(payload, known),
            raw=dict(payload),
        )

    @property
    def is_terminal(self) -> bool:
        return is_terminal_status(self.status)


@dataclass(frozen=True)
class Comparison:
    """GET /comparisons 的比较资格视图。"""

    eligible: bool | None
    level: str | None
    reasons: list[str]
    structural_reasons: list[str]
    metric_reasons: list[str]
    allowed_differences: list[str]
    extra: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    _KNOWN = (
        "eligible", "level", "reasons", "structural_reasons", "metric_reasons",
        "allowed_differences",
    )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Comparison:
        def str_list(key: str) -> list[str]:
            value = payload.get(key)
            return [str(item) for item in value] if isinstance(value, list) else []

        known = cls._KNOWN
        return cls(
            eligible=payload.get("eligible"),
            level=payload.get("level"),
            reasons=str_list("reasons"),
            structural_reasons=str_list("structural_reasons"),
            metric_reasons=str_list("metric_reasons"),
            allowed_differences=str_list("allowed_differences"),
            extra=_split(payload, known),
            raw=dict(payload),
        )


@dataclass(frozen=True)
class Gate:
    """Gate 求值/结果视图（POST /gates*、GET /gates/results/*）。"""

    decision: str | None
    status: str | None
    policy_id: str | None
    policy_version: str | None
    run_id: str | None
    extra: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    _KNOWN = ("decision", "status", "policy_id", "policy_version", "run_id")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Gate:
        known = cls._KNOWN

        def text(key: str) -> str | None:
            value = payload.get(key)
            return value if isinstance(value, str) else None

        return cls(
            decision=text("decision"),
            status=text("status"),
            policy_id=text("policy_id"),
            policy_version=text("policy_version"),
            run_id=text("run_id"),
            extra=_split(payload, known),
            raw=dict(payload),
        )


@dataclass(frozen=True)
class Baseline:
    """Baseline 快照视图。"""

    baseline_id: str
    entries: list[dict[str, Any]]
    policy: dict[str, Any]
    created_by: str | None
    created_at: str | None
    extra: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    _KNOWN = ("baseline_id", "entries", "policy", "created_by", "created_at")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Baseline:
        known = cls._KNOWN
        entries = payload.get("entries")
        policy = payload.get("policy")
        return cls(
            baseline_id=str(payload.get("baseline_id", payload.get("id", ""))),
            entries=[dict(item) for item in entries] if isinstance(entries, list) else [],
            policy=dict(policy) if isinstance(policy, Mapping) else {},
            created_by=payload.get("created_by"),
            created_at=payload.get("created_at"),
            extra=_split(payload, known),
            raw=dict(payload),
        )


@dataclass(frozen=True)
class Experiment:
    """Experiment 状态视图。"""

    experiment_id: str
    version: str | None
    status: str | None
    cell_count: int | None
    extra: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    _KNOWN = ("experiment_id", "version", "status", "cell_count")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Experiment:
        known = cls._KNOWN
        cell_count = payload.get("cell_count")
        return cls(
            experiment_id=str(payload.get("experiment_id", "")),
            version=payload.get("version"),
            status=payload.get("status"),
            cell_count=cell_count if isinstance(cell_count, int) else None,
            extra=_split(payload, known),
            raw=dict(payload),
        )
