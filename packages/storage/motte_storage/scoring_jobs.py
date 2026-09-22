"""M5-T09b/T09c：durable ScoringJob 存储（SQLite / memory / PostgreSQL）。

一个 ScoringJob 是一次 Judge 评分请求的持久执行准备：request_key（幂等键，与
内容 fingerprint 分离）-> job -> 预分配 ScoringPass id 在**同一个事务**里建立；
终态 ScoreSet + ScoringPass + job 完成 receipt + current 指针也在同一个事务里
以 CAS 提交（见 publish）。

状态机（任何非终态都不能被静默当作成功或当作零费用）：

    queued     --claim--> prepared --begin_call--> dispatching
    dispatching --settle_call--> dispatching (还有调用) 或 settled (全部落地)
    settled    --publish--> completed
    settled    --cancel-->  cancelled        (cancel 赢得竞争，不发布 current pass)
    queued/prepared --cancel--> cancelled    (确认未 dispatch：零调用)
    dispatching --cancel--> cancellation_requested（已发出的请求只能中断）
    dispatching --崩溃恢复--> indeterminate  (结果未知，不自动重发、不重计费)

调用账本仍走 agent_invocations：purpose=judge，owner 是经过校验的
subject-or-calibration union；subject 的 run_id/case_id NOT NULL 约束保持，
calibration 样本用显式命名空间 calibration:<calibration_job_id>，不伪造 Run。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from copy import deepcopy
from threading import RLock
from typing import Any
from uuid import uuid4

from .integrity import RunConflictError
from .invocations import create_invocation_in_transaction, transition_invocation_in_transaction
from .run_store import _append_event, _connect
from .sqlite_schema import create_and_upgrade

JOB_SCHEMA_VERSION = 1
JOB_STATES = (
    "queued", "prepared", "dispatching", "settled",
    "completed", "failed", "cancelled", "indeterminate",
)
TERMINAL_JOB_STATES = ("completed", "failed", "cancelled", "indeterminate")
JOB_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"prepared", "cancelled", "failed"},
    "prepared": {"dispatching", "queued", "cancelled", "failed", "indeterminate"},
    # dispatching -> cancelled 只允许在全部调用都已 settle 后由 runner 兑现取消请求。
    "dispatching": {"settled", "failed", "indeterminate", "cancelled"},
    "settled": {"completed", "failed", "cancelled", "indeterminate"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
    "indeterminate": set(),
}
PUBLISH_POLICIES = ("all_scored", "allow_non_scored")
JOB_MODES = ("single", "pairwise")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scoring_jobs (
  job_id TEXT PRIMARY KEY,
  request_key TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  owner_kind TEXT NOT NULL,
  owner_ref TEXT NOT NULL,
  run_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  revision INTEGER NOT NULL,
  reserved_pass_id TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS scoring_jobs_request_key_idx ON scoring_jobs(request_key);
CREATE INDEX IF NOT EXISTS scoring_jobs_run_idx ON scoring_jobs(run_id);
CREATE INDEX IF NOT EXISTS scoring_jobs_status_idx ON scoring_jobs(status);
"""


class ScoringJobConflict(RunConflictError):
    """同一幂等键对应的请求内容不同：409 语义，不覆盖既有 job。"""


class ScoringJobError(RuntimeError):
    """ScoringJob 状态或参数不合法。"""


def new_job_id() -> str:
    return f"sjob-{uuid4().hex}"


def new_reserved_pass_id() -> str:
    return f"pass-{uuid4().hex}"


def _owner_ref(owner: dict[str, Any]) -> str:
    if owner.get("kind") == "subject":
        run_id = owner.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("subject scoring job requires run_id")
        return f"run:{run_id}"
    if owner.get("kind") == "calibration":
        job_id = owner.get("calibration_job_id")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("calibration scoring job requires calibration_job_id")
        return f"calibration:{job_id}"
    raise ValueError("scoring job owner kind must be subject or calibration")


def validate_scoring_job(record: dict[str, Any]) -> dict[str, Any]:
    """存储层校验：身份、owner union、状态与预留 pass id。"""
    stored = deepcopy(record)
    if stored.get("schema_version") != JOB_SCHEMA_VERSION:
        raise ValueError("scoring job schema_version must be 1")
    for key in ("job_id", "request_key", "reserved_pass_id"):
        if not isinstance(stored.get(key), str) or not stored[key]:
            raise ValueError(f"scoring job requires a nonempty {key}")
    fingerprint = stored.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.startswith("sha256:"):
        raise ValueError("scoring job fingerprint must be a sha256:<64 hex> digest")
    if stored.get("status") not in JOB_STATES:
        raise ValueError(f"unknown scoring job status: {stored.get('status')!r}")
    if type(stored.get("revision")) is not int or stored["revision"] < 1:
        raise ValueError("scoring job revision must be a positive integer")
    owner = stored.get("owner")
    if not isinstance(owner, dict):
        raise ValueError("scoring job requires an owner object")
    expected_ref = _owner_ref(owner)
    if stored.get("owner_ref") != expected_ref:
        raise ValueError("scoring job owner_ref does not match its owner")
    if stored.get("owner_kind") != owner["kind"]:
        raise ValueError("scoring job owner_kind does not match its owner")
    if owner["kind"] == "subject":
        if stored.get("run_id") != owner["run_id"]:
            raise ValueError("scoring job run_id must match the subject run")
    elif stored.get("run_id") != expected_ref:
        raise ValueError(
            "calibration scoring job must use the namespaced owner reference"
        )
    if stored.get("mode") not in JOB_MODES:
        raise ValueError(f"unknown scoring job mode: {stored.get('mode')!r}")
    if stored.get("publish_policy") not in PUBLISH_POLICIES:
        raise ValueError(
            f"unknown publish policy: {stored.get('publish_policy')!r}"
        )
    if not isinstance(stored.get("judge_spec"), dict) or not stored["judge_spec"]:
        raise ValueError("scoring job requires a frozen judge spec")
    if not isinstance(stored.get("budget"), dict):
        raise ValueError("scoring job requires a budget object")
    stored.setdefault("calls", [])
    stored.setdefault("result", None)
    stored.setdefault("receipt", None)
    stored.setdefault("cancellation", None)
    stored.setdefault("failure", None)
    stored.setdefault("input_digests", {})
    return stored


def _next_revision(record: dict[str, Any], expected_revision: int) -> dict[str, Any]:
    updated = deepcopy(record)
    updated["revision"] = expected_revision + 1
    return validate_scoring_job(updated)


def _reserve_call_allowance(current: dict[str, Any], call: dict[str, Any]) -> None:
    """dispatch 前的原子额度核对（在同一个事务里）。

    已发出但未结算的调用占用额度；同一个 call_id 绝不重发。job 声明了额度却
    没有预留信息、或预留超过冻结额度时，在发出任何动作之前拒绝。
    """
    call_id = call.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        raise ScoringJobError("a judge call requires a call_id")
    calls = current.get("calls") or []
    if any(item.get("call_id") == call_id for item in calls):
        raise ScoringJobError(
            f"judge call was already dispatched and is never resent: {call_id}"
        )
    allowance = current.get("allowance")
    if not isinstance(allowance, dict):
        return
    reservation = call.get("reservation")
    if not isinstance(reservation, dict):
        raise ScoringJobError(
            f"judge call {call_id} carries no reservation while the job "
            "declares a frozen allowance"
        )
    if len(calls) + 1 > int(allowance.get("max_calls") or 0):
        raise ScoringJobError(
            "judge call exceeds the frozen allowance: max_calls"
        )

    def total(key: str) -> int:
        return sum(
            int((item.get("reservation") or {}).get(key) or 0) for item in calls
        )

    prompt = total("prompt_tokens") + int(reservation.get("prompt_tokens") or 0)
    if prompt > int(allowance.get("max_prompt_tokens") or 0):
        raise ScoringJobError(
            "judge call exceeds the frozen allowance: max_prompt_tokens"
        )
    completion = total("completion_tokens") + int(
        reservation.get("completion_tokens") or 0
    )
    if completion > int(allowance.get("max_completion_tokens") or 0):
        raise ScoringJobError(
            "judge call exceeds the frozen allowance: max_completion_tokens"
        )
    max_cost = allowance.get("max_cost_usd")
    if max_cost is None:
        return
    costs = [*(item.get("reservation") or {} for item in calls), reservation]
    if any(item.get("cost_usd") is None for item in costs):
        raise ScoringJobError(
            "judge call cost is unknown: a job with a monetary allowance "
            "cannot reserve an unknown cost"
        )
    if sum(float(item["cost_usd"]) for item in costs) > float(max_cost) + 1e-12:
        raise ScoringJobError(
            "judge call exceeds the frozen allowance: max_cost_usd"
        )


def _assert_transition(current: str, target: str) -> None:
    if target not in JOB_TRANSITIONS.get(current, set()):
        raise ScoringJobError(f"invalid scoring job transition: {current} -> {target}")


def job_view(record: dict[str, Any]) -> dict[str, Any]:
    """对外视图：保留证据与状态，但显式标出预算是分开计的。"""
    view = deepcopy(record)
    calls = view.get("calls") or []
    view["billed_calls"] = sum(1 for call in calls if call.get("outcome") == "succeeded")
    view["attempted_calls"] = sum(
        1 for call in calls if call.get("outcome") is not None
    )
    view["cost_total_usd"] = view.get("cost_total_usd")
    view["terminal"] = view.get("status") in TERMINAL_JOB_STATES
    return view


class SQLiteScoringJobs:
    def __init__(self, path: str) -> None:
        self._path = path
        with closing(_connect(path)) as connection:
            # F-01：建缺失表并把旧库对齐到当前形状（主键不同则重建）。
            create_and_upgrade(connection, _SCHEMA)

    # ------------------------------------------------------------- 读取
    def get(self, job_id: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute(
                "SELECT payload FROM scoring_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def get_by_request_key(self, request_key: str) -> dict[str, Any] | None:
        with closing(_connect(self._path)) as connection:
            row = connection.execute(
                "SELECT payload FROM scoring_jobs WHERE request_key = ?", (request_key,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT payload FROM scoring_jobs WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list_by_status(self, *statuses: str) -> list[dict[str, Any]]:
        wanted = statuses or JOB_STATES
        placeholders = ", ".join("?" for _ in wanted)
        with closing(_connect(self._path)) as connection:
            rows = connection.execute(
                f"SELECT payload FROM scoring_jobs WHERE status IN ({placeholders}) "
                "ORDER BY rowid",
                tuple(wanted),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    # ------------------------------------------------------------- 提交
    def submit(self, record: dict[str, Any]) -> dict[str, Any]:
        """原子建立 request -> job -> 预留 pass id；同键异内容抛冲突。"""
        stored = validate_scoring_job(record)
        try:
            with closing(_connect(self._path)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT payload FROM scoring_jobs WHERE request_key = ?",
                    (stored["request_key"],),
                ).fetchone()
                if existing is not None:
                    current = json.loads(existing[0])
                    if current["fingerprint"] != stored["fingerprint"]:
                        raise ScoringJobConflict(
                            "request key already exists with different content: "
                            f"{stored['request_key']}"
                        )
                    return {"created": False, "job": current}
                connection.execute(
                    "INSERT INTO scoring_jobs(job_id, request_key, fingerprint, owner_kind, "
                    "owner_ref, run_id, status, revision, reserved_pass_id, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        stored["job_id"], stored["request_key"], stored["fingerprint"],
                        stored["owner_kind"], stored["owner_ref"], stored["run_id"],
                        stored["status"], stored["revision"], stored["reserved_pass_id"],
                        json.dumps(stored, sort_keys=True),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ScoringJobConflict(f"scoring job already exists: {stored['job_id']}") from error
        return {"created": True, "job": stored}

    # ------------------------------------------------------------- 状态机
    def claim(self, job_id: str) -> dict[str, Any] | None:
        """queued -> prepared CAS；settled 只登记恢复（零新调用）。"""
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM scoring_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            current = json.loads(row[0])
            status = current["status"]
            if status == "queued":
                updated = _next_revision({**current, "status": "prepared"}, current["revision"])
                updated["prepared_at"] = updated.get("prepared_at") or _now()
                updated["attempts"] = int(current.get("attempts") or 0) + 1
            elif status == "settled":
                updated = _next_revision(current, current["revision"])
                updated["resumed_at"] = _now()
            else:
                return None
            self._write(connection, updated, expected_revision=current["revision"],
                        expected_status=status)
        return deepcopy(updated)

    def claim_next(self) -> dict[str, Any] | None:
        for status in ("queued", "settled"):
            with closing(_connect(self._path)) as connection:
                row = connection.execute(
                    "SELECT job_id FROM scoring_jobs WHERE status = ? ORDER BY rowid LIMIT 1",
                    (status,),
                ).fetchone()
            if row is None:
                continue
            claimed = self.claim(row[0])
            if claimed is not None:
                return claimed
        return None

    def transition(
        self, job_id: str, *, expected_revision: int, expected_status: str, status: str,
        changes: dict[str, Any] | None = None, allow_same_status: bool = False,
    ) -> dict[str, Any]:
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._read_locked(connection, job_id)
            self._check(current, expected_revision, expected_status)
            if status != expected_status or not allow_same_status:
                _assert_transition(current["status"], status)
            updated = _next_revision({**current, **deepcopy(changes or {}), "status": status},
                                     expected_revision)
            self._write(connection, updated, expected_revision=expected_revision,
                        expected_status=expected_status)
        return deepcopy(updated)

    # ------------------------------------------------------------- 调用边界
    def begin_call(
        self, job_id: str, *, expected_revision: int, invocation: dict[str, Any],
        call: dict[str, Any],
    ) -> dict[str, Any]:
        """先落库 prepared 与 dispatching，再允许发出动作（同一事务）。"""
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._read_locked(connection, job_id)
            self._check(current, expected_revision, expected_status=None)
            if current["status"] not in {"prepared", "dispatching"}:
                raise ScoringJobError(
                    f"job cannot dispatch a call from status {current['status']!r}"
                )
            if (current.get("cancellation") or {}).get("requested"):
                raise ScoringJobError("job cancellation was requested before dispatch")
            _reserve_call_allowance(current, call)
            created = create_invocation_in_transaction(connection, invocation)
            dispatched = transition_invocation_in_transaction(
                connection, created["id"], expected_revision=created["revision"],
                expected_status="prepared", status="dispatching",
                changes={"dispatched_at": _now()},
            )
            record = deepcopy(current)
            record["status"] = "dispatching"
            record["calls"] = [
                *record.get("calls", []),
                {
                    **deepcopy(call),
                    "invocation_id": dispatched["id"],
                    "invocation_revision": dispatched["revision"],
                    "status": "dispatching",
                    "dispatched_at": _now(),
                },
            ]
            record["dispatch_started_at"] = record.get("dispatch_started_at") or _now()
            updated = _next_revision(record, expected_revision)
            self._write(connection, updated, expected_revision=expected_revision,
                        expected_status=current["status"])
        return deepcopy(updated)

    def settle_call(
        self, job_id: str, *, expected_revision: int, call_id: str, outcome: str,
        result_summary: dict[str, Any], job_status: str, job_changes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """settle 边界：调用结果与原始响应落库；job 状态由调用方显式给出。"""
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._read_locked(connection, job_id)
            self._check(current, expected_revision, expected_status="dispatching")
            calls = deepcopy(current.get("calls") or [])
            target = None
            for item in calls:
                if item.get("call_id") == call_id:
                    target = item
                    break
            if target is None:
                raise ScoringJobError(f"unknown judge call: {call_id}")
            transition_invocation_in_transaction(
                connection, target["invocation_id"],
                expected_revision=target["invocation_revision"],
                expected_status="dispatching", status="settled",
                changes={
                    "outcome": outcome,
                    "result_summary": deepcopy(result_summary),
                    "settled_at": _now(),
                },
            )
            target.update({
                "status": "settled",
                "outcome": outcome,
                "settled_at": _now(),
                "usage": deepcopy(result_summary.get("usage")),
                "cost_usd": result_summary.get("cost_usd"),
                "price_table_version": result_summary.get("price_table_version"),
                "raw_response": result_summary.get("raw_response"),
                "error_class": result_summary.get("error_class"),
            })
            record = deepcopy(current)
            record["calls"] = calls
            record["billed_calls"] = sum(
                1 for item in calls if item.get("outcome") == "succeeded"
            )
            if job_changes:
                record.update(deepcopy(job_changes))
            if job_status != record["status"]:
                _assert_transition(record["status"], job_status)
            record["status"] = job_status
            if job_status == "settled":
                record["settled_at"] = _now()
            updated = _next_revision(record, expected_revision)
            self._write(connection, updated, expected_revision=expected_revision,
                        expected_status="dispatching")
        return deepcopy(updated)

    # ------------------------------------------------------------- 取消
    def cancel(self, job_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        """幂等取消 + revision CAS；已发出的请求只能中断，不能宣称未计费。"""
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._read_locked(connection, job_id)
            status = current["status"]
            if status == "completed":
                return {"job": deepcopy(current), "outcome": "already_completed",
                        "receipt": current.get("receipt")}
            if status == "cancelled":
                return {"job": deepcopy(current), "outcome": "already_cancelled",
                        "receipt": None}
            if status == "indeterminate":
                return {"job": deepcopy(current), "outcome": "indeterminate",
                        "receipt": None,
                        "note": "unknown outcome stays indeterminate and is never resent"}
            if status in {"queued", "prepared"}:
                for item in current.get("calls") or []:
                    if item.get("status") == "dispatching":
                        raise ScoringJobError(
                            "cannot cancel with an unsettled dispatching call"
                        )
                self._settle_prepared_calls(connection, current, reason)
                record = deepcopy(current)
                record["status"] = "cancelled"
                record["cancellation"] = {
                    "requested": True, "actor": actor, "reason": reason,
                    "at": _now(), "phase": status, "billed_calls": 0,
                }
                record["cancelled_at"] = _now()
                record["result"] = record.get("result") or {"cancelled_before_dispatch": True}
                updated = _next_revision(record, current["revision"])
                self._write(connection, updated, expected_revision=current["revision"],
                            expected_status=status)
                return {"job": deepcopy(updated), "outcome": "cancelled",
                        "receipt": None, "billed_calls": 0}
            if status == "dispatching":
                if (current.get("cancellation") or {}).get("requested"):
                    return {"job": deepcopy(current), "outcome": "cancel_requested",
                            "receipt": None, "in_flight": True}
                record = deepcopy(current)
                record["cancellation"] = {
                    "requested": True, "actor": actor, "reason": reason,
                    "at": _now(), "phase": status,
                }
                updated = _next_revision(record, current["revision"])
                self._write(connection, updated, expected_revision=current["revision"],
                            expected_status=status)
                return {
                    "job": deepcopy(updated), "outcome": "cancel_requested",
                    "receipt": None, "in_flight": True,
                    "note": "a sent remote request can only be interrupted, "
                            "never declared unbilled",
                }
            # settled: 还没有 publish，cancel 可以赢下竞争。
            record = deepcopy(current)
            record["status"] = "cancelled"
            existing = deepcopy(current.get("cancellation") or {})
            record["cancellation"] = {
                **existing,
                "requested": True,
                "actor": existing.get("actor") or actor,
                "reason": existing.get("reason") or reason,
                "at": existing.get("at") or _now(),
                "phase": existing.get("phase") or status,
                "finalized_at": _now(),
                "billed_calls": record.get("billed_calls") or 0,
            }
            record["cancelled_at"] = _now()
            record["publish_outcome"] = "cancelled_before_publish"
            updated = _next_revision(record, current["revision"])
            self._write(connection, updated, expected_revision=current["revision"],
                        expected_status=status)
        return {
            "job": deepcopy(updated), "outcome": "cancelled_before_publish",
            "receipt": None, "billed_calls": updated.get("billed_calls") or 0,
        }

    # ------------------------------------------------------------- 发布
    def publish(
        self, job_id: str, *, expected_revision: int, pass_record: dict[str, Any],
        scores: list[dict[str, Any]], events: list[dict[str, Any]] | None = None,
        run_advance: dict[str, Any] | None = None,
        expected_run_revision: int | None = None,
        expected_run_status: str | None = None,
        receipt: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """终态 ScoreSet + pass + receipt + current 指针，同一事务 CAS。"""
        stored_pass = deepcopy(pass_record)
        if not isinstance(stored_pass.get("id"), str) or not stored_pass["id"]:
            raise ValueError("scoring pass requires a nonempty id")
        rows = [deepcopy(row) for row in scores]
        for row in rows:
            if row.get("scoring_pass_id") not in (None, stored_pass["id"]):
                raise ValueError("score belongs to a different scoring pass")
            row["scoring_pass_id"] = stored_pass["id"]
        if (expected_run_revision is None) != (expected_run_status is None):
            raise ValueError("provide both expected run revision and status")

        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._read_locked(connection, job_id)
            if current["status"] == "completed":
                return {
                    "job": deepcopy(current), "receipt": current.get("receipt"),
                    "published": False, "outcome": "already_completed",
                }
            if current["status"] == "cancelled":
                return {
                    "job": deepcopy(current), "receipt": None,
                    "published": False, "outcome": "already_cancelled",
                }
            if current["status"] not in {"settled", "prepared"}:
                raise ScoringJobError(
                    f"job cannot publish from status {current['status']!r}"
                )
            if current["status"] == "prepared" and any(
                item.get("status") == "dispatching" for item in current.get("calls") or []
            ):
                raise ScoringJobError("job has an unsettled dispatching call")
            self._check(current, expected_revision, expected_status=None)
            if stored_pass["id"] != current["reserved_pass_id"]:
                raise ScoringJobError("pass id differs from the reserved pass id")
            if stored_pass.get("run_id") != current["run_id"]:
                raise ScoringJobError("pass run_id differs from the job owner reference")

            updated_run_payload = None
            if current["owner_kind"] == "subject":
                row = connection.execute(
                    "SELECT payload, revision FROM runs WHERE id = ?",
                    (current["run_id"],),
                ).fetchone()
                if row is None:
                    raise RunConflictError(f"subject run is missing: {current['run_id']}")
                run = json.loads(row[0])
                if row[1] != expected_run_revision or run.get("status") != expected_run_status:
                    raise RunConflictError(
                        f"run revision or status changed: {current['run_id']}"
                    )
                updated_run_payload = {
                    **run,
                    **deepcopy(run_advance or {}),
                    "current_scoring_pass_id": stored_pass["id"],
                    "revision": row[1] + 1,
                }
            connection.execute(
                "INSERT INTO scoring_passes(id, run_id, payload) VALUES (?, ?, ?)",
                (stored_pass["id"], stored_pass["run_id"],
                 json.dumps(stored_pass, sort_keys=True)),
            )
            connection.executemany(
                "INSERT INTO score_sets(scoring_pass_id, case_id, trial_id, metric_id, "
                "evaluator_id, evaluator_version, ordinal, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(
                    stored_pass["id"], row["case_id"], row.get("trial_id") or "",
                    row.get("metric_id") or "", row.get("evaluator_id") or "",
                    row.get("evaluator_version") or "", index,
                    json.dumps(row, sort_keys=True),
                ) for index, row in enumerate(rows)],
            )
            if updated_run_payload is not None:
                if connection.execute(
                    "UPDATE runs SET payload = ?, revision = ? WHERE id = ? AND revision = ? "
                    "AND json_extract(payload, '$.status') = ?",
                    (json.dumps(updated_run_payload, sort_keys=True),
                     updated_run_payload["revision"], current["run_id"],
                     expected_run_revision, expected_run_status),
                ).rowcount != 1:
                    raise RunConflictError(
                        f"run revision or status changed: {current['run_id']}"
                    )
            record = deepcopy(current)
            record["status"] = "completed"
            record["completed_at"] = _now()
            record["receipt"] = deepcopy(receipt) if receipt is not None else {
                "job_id": current["job_id"],
                "scoring_pass_id": stored_pass["id"],
                "published_at": _now(),
            }
            if result is not None:
                record["result"] = deepcopy(result)
            updated = _next_revision(record, current["revision"])
            self._write(connection, updated, expected_revision=current["revision"],
                        expected_status=current["status"])
            for event in events or []:
                _append_event(connection, event)
        return {
            "job": deepcopy(updated), "receipt": deepcopy(updated["receipt"]),
            "published": True, "outcome": "published",
        }

    # ------------------------------------------------------------- 恢复
    def recover_interrupted(self) -> list[str]:
        """崩溃恢复：prepared（无发送证据）回队列；dispatching 一律待复核。"""
        touched: list[str] = []
        with closing(_connect(self._path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT payload FROM scoring_jobs WHERE status IN "
                "('prepared', 'dispatching') ORDER BY rowid"
            ).fetchall()
            for payload in rows:
                current = json.loads(payload[0])
                if current["status"] == "prepared":
                    if any(
                        item.get("status") == "dispatching"
                        for item in current.get("calls") or []
                    ):
                        continue
                    record = deepcopy(current)
                    record["status"] = "queued"
                    record["recovered_at"] = _now()
                    record["recovery"] = {
                        "reason": "prepared_without_dispatch_evidence",
                        "billed_calls": 0,
                    }
                    updated = _next_revision(record, current["revision"])
                else:
                    for item in current.get("calls") or []:
                        if item.get("status") == "dispatching":
                            transition_invocation_in_transaction(
                                connection, item["invocation_id"],
                                expected_revision=item["invocation_revision"],
                                expected_status="dispatching", status="settled",
                                changes={
                                    "outcome": "indeterminate",
                                    "result_summary": {
                                        "reason": "worker crashed after dispatch",
                                    },
                                    "settled_at": _now(),
                                },
                            )
                            item["status"] = "settled"
                            item["outcome"] = "indeterminate"
                            item["settled_at"] = _now()
                    record = deepcopy(current)
                    record["status"] = "indeterminate"
                    record["failure"] = {
                        "code": "CALL_OUTCOME_INDETERMINATE",
                        "message": "a dispatched judge call was interrupted before a "
                                   "durable result landed; it is never resent automatically",
                    }
                    record["indeterminate_at"] = _now()
                    updated = _next_revision(record, current["revision"])
                self._write(connection, updated, expected_revision=current["revision"],
                            expected_status=current["status"])
                touched.append(updated["job_id"])
        return touched

    # ------------------------------------------------------------- 内部
    def _read_locked(self, connection: sqlite3.Connection, job_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT payload FROM scoring_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise ScoringJobError(f"scoring job is missing: {job_id}")
        return json.loads(row[0])

    @staticmethod
    def _check(
        current: dict[str, Any], expected_revision: int, expected_status: str | None,
    ) -> None:
        if current["revision"] != expected_revision:
            raise ScoringJobConflict(
                f"scoring job revision changed: {current['job_id']}"
            )
        if expected_status is not None and current["status"] != expected_status:
            raise ScoringJobConflict(
                f"scoring job status changed: {current['job_id']}"
            )

    def _write(
        self, connection: sqlite3.Connection, record: dict[str, Any], *,
        expected_revision: int, expected_status: str,
    ) -> None:
        stored = validate_scoring_job(record)
        if connection.execute(
            "UPDATE scoring_jobs SET payload = ?, status = ?, revision = ?, "
            "owner_kind = ?, owner_ref = ?, run_id = ? "
            "WHERE job_id = ? AND status = ? AND revision = ?",
            (
                json.dumps(stored, sort_keys=True), stored["status"], stored["revision"],
                stored["owner_kind"], stored["owner_ref"], stored["run_id"],
                stored["job_id"], expected_status, expected_revision,
            ),
        ).rowcount != 1:
            raise ScoringJobConflict(
                f"scoring job revision or status changed: {stored['job_id']}"
            )

    def _settle_prepared_calls(
        self, connection: sqlite3.Connection, current: dict[str, Any], reason: str,
    ) -> None:
        for item in current.get("calls") or []:
            if item.get("status") != "prepared":
                continue
            transition_invocation_in_transaction(
                connection, item["invocation_id"],
                expected_revision=item["invocation_revision"],
                expected_status="prepared", status="settled",
                changes={
                    "outcome": "failed",
                    "result_summary": {
                        "reason": "cancelled_before_dispatch", "detail": reason,
                    },
                    "settled_at": _now(),
                },
            )
            item["status"] = "settled"
            item["outcome"] = "failed"


class MemoryScoringJobs:
    """内存实现：与 SQLite 同语义，借用 RunStore 的内存仓储与同一把锁。"""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._lock = getattr(getattr(store, "runs", None), "_lock", RLock())
        self._rows: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------- 读取
    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            return deepcopy(self._rows.get(job_id))

    def get_by_request_key(self, request_key: str) -> dict[str, Any] | None:
        with self._lock:
            for row in self._rows.values():
                if row["request_key"] == request_key:
                    return deepcopy(row)
        return None

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy([
                row for row in self._rows.values() if row["run_id"] == run_id
            ])

    def list_by_status(self, *statuses: str) -> list[dict[str, Any]]:
        wanted = set(statuses or JOB_STATES)
        with self._lock:
            return deepcopy([
                row for row in self._rows.values() if row["status"] in wanted
            ])

    def submit(self, record: dict[str, Any]) -> dict[str, Any]:
        stored = validate_scoring_job(record)
        with self._lock:
            for row in self._rows.values():
                if row["request_key"] == stored["request_key"]:
                    if row["fingerprint"] != stored["fingerprint"]:
                        raise ScoringJobConflict(
                            "request key already exists with different content: "
                            f"{stored['request_key']}"
                        )
                    return {"created": False, "job": deepcopy(row)}
            if stored["job_id"] in self._rows:
                raise ScoringJobConflict(f"scoring job already exists: {stored['job_id']}")
            self._rows[stored["job_id"]] = deepcopy(stored)
        return {"created": True, "job": deepcopy(stored)}

    # ------------------------------------------------------------- 状态机
    def claim(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            current = self._rows.get(job_id)
            if current is None:
                return None
            if current["status"] == "queued":
                updated = _next_revision({**current, "status": "prepared"}, current["revision"])
                updated["prepared_at"] = updated.get("prepared_at") or _now()
                updated["attempts"] = int(current.get("attempts") or 0) + 1
            elif current["status"] == "settled":
                updated = _next_revision(current, current["revision"])
                updated["resumed_at"] = _now()
            else:
                return None
            self._rows[job_id] = deepcopy(updated)
            return deepcopy(updated)

    def claim_next(self) -> dict[str, Any] | None:
        for status in ("queued", "settled"):
            with self._lock:
                candidate = next(
                    (row for row in self._rows.values() if row["status"] == status), None,
                )
            if candidate is None:
                continue
            claimed = self.claim(candidate["job_id"])
            if claimed is not None:
                return claimed
        return None

    def transition(
        self, job_id: str, *, expected_revision: int, expected_status: str, status: str,
        changes: dict[str, Any] | None = None, allow_same_status: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            current = self._require(job_id, expected_revision, expected_status)
            if status != expected_status or not allow_same_status:
                _assert_transition(current["status"], status)
            updated = _next_revision(
                {**current, **deepcopy(changes or {}), "status": status}, expected_revision,
            )
            self._rows[job_id] = deepcopy(updated)
            return deepcopy(updated)

    # ------------------------------------------------------------- 调用边界
    def begin_call(
        self, job_id: str, *, expected_revision: int, invocation: dict[str, Any],
        call: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            current = self._require(job_id, expected_revision, None)
            if current["status"] not in {"prepared", "dispatching"}:
                raise ScoringJobError(
                    f"job cannot dispatch a call from status {current['status']!r}"
                )
            if (current.get("cancellation") or {}).get("requested"):
                raise ScoringJobError("job cancellation was requested before dispatch")
            _reserve_call_allowance(current, call)
            invocations = self._store.invocations
            created = invocations.create(invocation)
            dispatched = invocations.transition(
                created["id"], expected_revision=created["revision"],
                expected_status="prepared", status="dispatching",
                changes={"dispatched_at": _now()},
            )
            record = deepcopy(current)
            record["status"] = "dispatching"
            record["calls"] = [
                *record.get("calls", []),
                {
                    **deepcopy(call),
                    "invocation_id": dispatched["id"],
                    "invocation_revision": dispatched["revision"],
                    "status": "dispatching",
                    "dispatched_at": _now(),
                },
            ]
            record["dispatch_started_at"] = record.get("dispatch_started_at") or _now()
            updated = _next_revision(record, expected_revision)
            self._rows[job_id] = deepcopy(updated)
            return deepcopy(updated)

    def settle_call(
        self, job_id: str, *, expected_revision: int, call_id: str, outcome: str,
        result_summary: dict[str, Any], job_status: str,
        job_changes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            current = self._require(job_id, expected_revision, "dispatching")
            calls = deepcopy(current.get("calls") or [])
            target = next((item for item in calls if item.get("call_id") == call_id), None)
            if target is None:
                raise ScoringJobError(f"unknown judge call: {call_id}")
            self._store.invocations.transition(
                target["invocation_id"],
                expected_revision=target["invocation_revision"],
                expected_status="dispatching", status="settled",
                changes={
                    "outcome": outcome,
                    "result_summary": deepcopy(result_summary),
                    "settled_at": _now(),
                },
            )
            target.update({
                "status": "settled",
                "outcome": outcome,
                "settled_at": _now(),
                "usage": deepcopy(result_summary.get("usage")),
                "cost_usd": result_summary.get("cost_usd"),
                "price_table_version": result_summary.get("price_table_version"),
                "raw_response": result_summary.get("raw_response"),
                "error_class": result_summary.get("error_class"),
            })
            record = deepcopy(current)
            record["calls"] = calls
            record["billed_calls"] = sum(
                1 for item in calls if item.get("outcome") == "succeeded"
            )
            if job_changes:
                record.update(deepcopy(job_changes))
            if job_status != record["status"]:
                _assert_transition(record["status"], job_status)
            record["status"] = job_status
            if job_status == "settled":
                record["settled_at"] = _now()
            updated = _next_revision(record, expected_revision)
            self._rows[job_id] = deepcopy(updated)
            return deepcopy(updated)

    # ------------------------------------------------------------- 取消
    def cancel(self, job_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        with self._lock:
            current = self._rows.get(job_id)
            if current is None:
                raise ScoringJobError(f"scoring job is missing: {job_id}")
            status = current["status"]
            if status == "completed":
                return {"job": deepcopy(current), "outcome": "already_completed",
                        "receipt": current.get("receipt")}
            if status == "cancelled":
                return {"job": deepcopy(current), "outcome": "already_cancelled",
                        "receipt": None}
            if status == "indeterminate":
                return {"job": deepcopy(current), "outcome": "indeterminate",
                        "receipt": None,
                        "note": "unknown outcome stays indeterminate and is never resent"}
            if status in {"queued", "prepared"}:
                for item in current.get("calls") or []:
                    if item.get("status") == "dispatching":
                        raise ScoringJobError(
                            "cannot cancel with an unsettled dispatching call"
                        )
                self._settle_prepared_calls(current, reason)
                record = deepcopy(current)
                record["status"] = "cancelled"
                record["cancellation"] = {
                    "requested": True, "actor": actor, "reason": reason,
                    "at": _now(), "phase": status, "billed_calls": 0,
                }
                record["cancelled_at"] = _now()
                record["result"] = record.get("result") or {
                    "cancelled_before_dispatch": True,
                }
                updated = _next_revision(record, current["revision"])
                self._rows[job_id] = deepcopy(updated)
                return {"job": deepcopy(updated), "outcome": "cancelled",
                        "receipt": None, "billed_calls": 0}
            if status == "dispatching":
                if (current.get("cancellation") or {}).get("requested"):
                    return {"job": deepcopy(current), "outcome": "cancel_requested",
                            "receipt": None, "in_flight": True}
                record = deepcopy(current)
                record["cancellation"] = {
                    "requested": True, "actor": actor, "reason": reason,
                    "at": _now(), "phase": status,
                }
                updated = _next_revision(record, current["revision"])
                self._rows[job_id] = deepcopy(updated)
                return {
                    "job": deepcopy(updated), "outcome": "cancel_requested",
                    "receipt": None, "in_flight": True,
                    "note": "a sent remote request can only be interrupted, "
                            "never declared unbilled",
                }
            record = deepcopy(current)
            record["status"] = "cancelled"
            existing = deepcopy(current.get("cancellation") or {})
            record["cancellation"] = {
                **existing,
                "requested": True,
                "actor": existing.get("actor") or actor,
                "reason": existing.get("reason") or reason,
                "at": existing.get("at") or _now(),
                "phase": existing.get("phase") or status,
                "finalized_at": _now(),
                "billed_calls": record.get("billed_calls") or 0,
            }
            record["cancelled_at"] = _now()
            record["publish_outcome"] = "cancelled_before_publish"
            updated = _next_revision(record, current["revision"])
            self._rows[job_id] = deepcopy(updated)
            return {
                "job": deepcopy(updated), "outcome": "cancelled_before_publish",
                "receipt": None, "billed_calls": updated.get("billed_calls") or 0,
            }

    # ------------------------------------------------------------- 发布
    def publish(
        self, job_id: str, *, expected_revision: int, pass_record: dict[str, Any],
        scores: list[dict[str, Any]], events: list[dict[str, Any]] | None = None,
        run_advance: dict[str, Any] | None = None,
        expected_run_revision: int | None = None,
        expected_run_status: str | None = None,
        receipt: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        stored_pass = deepcopy(pass_record)
        if not isinstance(stored_pass.get("id"), str) or not stored_pass["id"]:
            raise ValueError("scoring pass requires a nonempty id")
        rows = [deepcopy(row) for row in scores]
        for row in rows:
            row.setdefault("scoring_pass_id", stored_pass["id"])
            if row["scoring_pass_id"] != stored_pass["id"]:
                raise ValueError("score belongs to a different scoring pass")
        if (expected_run_revision is None) != (expected_run_status is None):
            raise ValueError("provide both expected run revision and status")
        with self._lock:
            current = self._rows.get(job_id)
            if current is None:
                raise ScoringJobError(f"scoring job is missing: {job_id}")
            if current["status"] == "completed":
                return {"job": deepcopy(current), "receipt": current.get("receipt"),
                        "published": False, "outcome": "already_completed"}
            if current["status"] == "cancelled":
                return {"job": deepcopy(current), "receipt": None,
                        "published": False, "outcome": "already_cancelled"}
            if current["status"] not in {"settled", "prepared"}:
                raise ScoringJobError(
                    f"job cannot publish from status {current['status']!r}"
                )
            self._check(current, expected_revision, None)
            if stored_pass["id"] != current["reserved_pass_id"]:
                raise ScoringJobError("pass id differs from the reserved pass id")
            if stored_pass.get("run_id") != current["run_id"]:
                raise ScoringJobError("pass run_id differs from the job owner reference")
            if current["owner_kind"] == "subject":
                run = self._store.runs.get(current["run_id"])
                if run is None:
                    raise RunConflictError(f"subject run is missing: {current['run_id']}")
                if run["revision"] != expected_run_revision or (
                    run.get("status") != expected_run_status
                ):
                    raise RunConflictError(
                        f"run revision or status changed: {current['run_id']}"
                    )
                updated_run = {
                    **run,
                    **deepcopy(run_advance or {}),
                    "current_scoring_pass_id": stored_pass["id"],
                    "revision": run["revision"] + 1,
                }
                self._store.runs._runs[current["run_id"]] = deepcopy(updated_run)
            if stored_pass["id"] in self._store.scoring_passes._passes:
                raise RunConflictError(
                    f"scoring pass already exists: {stored_pass['id']}"
                )
            self._store.scoring_passes._passes[stored_pass["id"]] = deepcopy(stored_pass)
            self._store.score_sets._sets[stored_pass["id"]] = deepcopy(rows)
            record = deepcopy(current)
            record["status"] = "completed"
            record["completed_at"] = _now()
            record["receipt"] = deepcopy(receipt) if receipt is not None else {
                "job_id": current["job_id"],
                "scoring_pass_id": stored_pass["id"],
                "published_at": _now(),
            }
            if result is not None:
                record["result"] = deepcopy(result)
            updated = _next_revision(record, current["revision"])
            self._rows[job_id] = deepcopy(updated)
            for event in events or []:
                self._store.events.append(event)
            return {"job": deepcopy(updated), "receipt": deepcopy(updated["receipt"]),
                    "published": True, "outcome": "published"}

    # ------------------------------------------------------------- 恢复
    def recover_interrupted(self) -> list[str]:
        with self._lock:
            touched: list[str] = []
            for job_id, current in list(self._rows.items()):
                if current["status"] == "prepared":
                    if any(
                        item.get("status") == "dispatching"
                        for item in current.get("calls") or []
                    ):
                        continue
                    record = deepcopy(current)
                    record["status"] = "queued"
                    record["recovered_at"] = _now()
                    record["recovery"] = {
                        "reason": "prepared_without_dispatch_evidence",
                        "billed_calls": 0,
                    }
                elif current["status"] == "dispatching":
                    for item in current.get("calls") or []:
                        if item.get("status") == "dispatching":
                            self._store.invocations.transition(
                                item["invocation_id"],
                                expected_revision=item["invocation_revision"],
                                expected_status="dispatching", status="settled",
                                changes={
                                    "outcome": "indeterminate",
                                    "result_summary": {
                                        "reason": "worker crashed after dispatch",
                                    },
                                    "settled_at": _now(),
                                },
                            )
                            item["status"] = "settled"
                            item["outcome"] = "indeterminate"
                            item["settled_at"] = _now()
                    record = deepcopy(current)
                    record["status"] = "indeterminate"
                    record["failure"] = {
                        "code": "CALL_OUTCOME_INDETERMINATE",
                        "message": "a dispatched judge call was interrupted before a "
                                   "durable result landed; it is never resent automatically",
                    }
                    record["indeterminate_at"] = _now()
                else:
                    continue
                updated = _next_revision(record, current["revision"])
                self._rows[job_id] = deepcopy(updated)
                touched.append(job_id)
            return touched

    # ------------------------------------------------------------- 内部
    def _settle_prepared_calls(self, current: dict[str, Any], reason: str) -> None:
        for item in current.get("calls") or []:
            if item.get("status") != "prepared":
                continue
            self._store.invocations.transition(
                item["invocation_id"],
                expected_revision=item["invocation_revision"],
                expected_status="prepared", status="settled",
                changes={
                    "outcome": "failed",
                    "result_summary": {
                        "reason": "cancelled_before_dispatch", "detail": reason,
                    },
                    "settled_at": _now(),
                },
            )
            item["status"] = "settled"
            item["outcome"] = "failed"

    def _require(
        self, job_id: str, expected_revision: int, expected_status: str | None,
    ) -> dict[str, Any]:
        current = self._rows.get(job_id)
        if current is None:
            raise ScoringJobError(f"scoring job is missing: {job_id}")
        self._check(current, expected_revision, expected_status)
        return current

    @staticmethod
    def _check(
        current: dict[str, Any], expected_revision: int, expected_status: str | None,
    ) -> None:
        if current["revision"] != expected_revision:
            raise ScoringJobConflict(
                f"scoring job revision changed: {current['job_id']}"
            )
        if expected_status is not None and current["status"] != expected_status:
            raise ScoringJobConflict(
                f"scoring job status changed: {current['job_id']}"
            )


class PostgresScoringJobs:
    """PostgreSQL 实现：与 SQLite/Memory 同状态机，SQL 使用 psycopg 方言。

    表结构由 Alembic 0012_scoring_jobs 建立。本地没有 PG 实例时该路径未被
    实际执行（M5-T09 报告中标 not_run），因此它与 SQLite 实现保持逐句对应，
    不做额外优化。
    """

    def __init__(self, dsn: str) -> None:
        from .postgres import normalize_dsn

        self._dsn = normalize_dsn(dsn)

    def _open(self):
        from .postgres import _connect

        return _connect(self._dsn)

    @staticmethod
    def _json(value: Any) -> Any:
        from psycopg.types.json import Json

        return Json(value)

    @staticmethod
    def _row_payload(row: Any) -> dict[str, Any]:
        return row[0] if isinstance(row[0], dict) else json.loads(row[0])

    # ------------------------------------------------------------- 读取
    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._open() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM scoring_jobs WHERE job_id = %s", (job_id,)
                )
                row = cursor.fetchone()
        return self._row_payload(row) if row else None

    def get_by_request_key(self, request_key: str) -> dict[str, Any] | None:
        with self._open() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM scoring_jobs WHERE request_key = %s",
                    (request_key,),
                )
                row = cursor.fetchone()
        return self._row_payload(row) if row else None

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._open() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM scoring_jobs WHERE run_id = %s ORDER BY position",
                    (run_id,),
                )
                rows = cursor.fetchall()
        return [self._row_payload(row) for row in rows]

    def list_by_status(self, *statuses: str) -> list[dict[str, Any]]:
        wanted = list(statuses or JOB_STATES)
        with self._open() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM scoring_jobs WHERE status = ANY(%s) "
                    "ORDER BY position",
                    (wanted,),
                )
                rows = cursor.fetchall()
        return [self._row_payload(row) for row in rows]

    # ------------------------------------------------------------- 提交
    def submit(self, record: dict[str, Any]) -> dict[str, Any]:
        stored = validate_scoring_job(record)
        with self._open() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM scoring_jobs WHERE request_key = %s FOR UPDATE",
                    (stored["request_key"],),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    current = self._row_payload(existing)
                    if current["fingerprint"] != stored["fingerprint"]:
                        raise ScoringJobConflict(
                            "request key already exists with different content: "
                            f"{stored['request_key']}"
                        )
                    return {"created": False, "job": current}
                cursor.execute(
                    "INSERT INTO scoring_jobs(job_id, request_key, fingerprint, "
                    "owner_kind, owner_ref, run_id, status, revision, "
                    "reserved_pass_id, payload) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING RETURNING payload",
                    (
                        stored["job_id"], stored["request_key"],
                        stored["fingerprint"], stored["owner_kind"],
                        stored["owner_ref"], stored["run_id"], stored["status"],
                        stored["revision"], stored["reserved_pass_id"],
                        self._json(stored),
                    ),
                )
                inserted = cursor.fetchone()
                if inserted is not None:
                    return {"created": True, "job": self._row_payload(inserted)}
                # A concurrent first insert may have won the request-key index.
                # READ COMMITTED gives this statement the committed winner.
                cursor.execute(
                    "SELECT payload FROM scoring_jobs WHERE request_key = %s FOR UPDATE",
                    (stored["request_key"],),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    current = self._row_payload(existing)
                    if current["fingerprint"] != stored["fingerprint"]:
                        raise ScoringJobConflict(
                            "request key already exists with different content: "
                            f"{stored['request_key']}"
                        )
                    return {"created": False, "job": current}
                # The only remaining conflict is a different job_id.
                cursor.execute(
                    "SELECT payload FROM scoring_jobs WHERE job_id = %s FOR UPDATE",
                    (stored["job_id"],),
                )
                if cursor.fetchone() is not None:
                    raise ScoringJobConflict(
                        f"scoring job already exists: {stored['job_id']}"
                    )
                raise ScoringJobConflict(
                    "scoring job insert was rejected without a conflicting row: "
                    + stored["job_id"]
                )

    # ------------------------------------------------------------- 状态机
    def claim(self, job_id: str) -> dict[str, Any] | None:
        with self._open() as connection:
            with connection.cursor() as cursor:
                current = self._locked(cursor, job_id)
                status = current["status"]
                if status == "queued":
                    updated = _next_revision(
                        {**current, "status": "prepared"}, current["revision"]
                    )
                    updated["prepared_at"] = updated.get("prepared_at") or _now()
                    updated["attempts"] = int(current.get("attempts") or 0) + 1
                elif status == "settled":
                    updated = _next_revision(current, current["revision"])
                    updated["resumed_at"] = _now()
                else:
                    return None
                self._write(cursor, updated, expected_revision=current["revision"],
                            expected_status=status)
        return deepcopy(updated)

    def claim_next(self) -> dict[str, Any] | None:
        for status in ("queued", "settled"):
            with self._open() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT job_id FROM scoring_jobs WHERE status = %s "
                        "ORDER BY position LIMIT 1",
                        (status,),
                    )
                    row = cursor.fetchone()
            if row is None:
                continue
            claimed = self.claim(row[0])
            if claimed is not None:
                return claimed
        return None

    def transition(
        self, job_id: str, *, expected_revision: int, expected_status: str, status: str,
        changes: dict[str, Any] | None = None, allow_same_status: bool = False,
    ) -> dict[str, Any]:
        with self._open() as connection:
            with connection.cursor() as cursor:
                current = self._locked(cursor, job_id)
                self._check(current, expected_revision, expected_status)
                if status != expected_status or not allow_same_status:
                    _assert_transition(current["status"], status)
                updated = _next_revision(
                    {**current, **deepcopy(changes or {}), "status": status},
                    expected_revision,
                )
                self._write(cursor, updated, expected_revision=expected_revision,
                            expected_status=expected_status)
        return deepcopy(updated)

    # ------------------------------------------------------------- 调用边界
    def begin_call(
        self, job_id: str, *, expected_revision: int, invocation: dict[str, Any],
        call: dict[str, Any],
    ) -> dict[str, Any]:
        from .invocations import create_invocation_pg, transition_invocation_pg

        with self._open() as connection:
            with connection.cursor() as cursor:
                current = self._locked(cursor, job_id)
                self._check(current, expected_revision, None)
                if current["status"] not in {"prepared", "dispatching"}:
                    raise ScoringJobError(
                        f"job cannot dispatch a call from status "
                        f"{current['status']!r}"
                    )
                if (current.get("cancellation") or {}).get("requested"):
                    raise ScoringJobError(
                        "job cancellation was requested before dispatch"
                    )
                _reserve_call_allowance(current, call)
                created = create_invocation_pg(cursor, invocation)
                dispatched = transition_invocation_pg(
                    cursor, created["id"], expected_revision=created["revision"],
                    expected_status="prepared", status="dispatching",
                    changes={"dispatched_at": _now()},
                )
                record = deepcopy(current)
                record["status"] = "dispatching"
                record["calls"] = [
                    *record.get("calls", []),
                    {
                        **deepcopy(call),
                        "invocation_id": dispatched["id"],
                        "invocation_revision": dispatched["revision"],
                        "status": "dispatching",
                        "dispatched_at": _now(),
                    },
                ]
                record["dispatch_started_at"] = (
                    record.get("dispatch_started_at") or _now()
                )
                updated = _next_revision(record, expected_revision)
                self._write(cursor, updated, expected_revision=expected_revision,
                            expected_status=current["status"])
        return deepcopy(updated)

    def settle_call(
        self, job_id: str, *, expected_revision: int, call_id: str, outcome: str,
        result_summary: dict[str, Any], job_status: str,
        job_changes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from .invocations import transition_invocation_pg

        with self._open() as connection:
            with connection.cursor() as cursor:
                current = self._locked(cursor, job_id)
                self._check(current, expected_revision, "dispatching")
                calls = deepcopy(current.get("calls") or [])
                target = next(
                    (item for item in calls if item.get("call_id") == call_id), None,
                )
                if target is None:
                    raise ScoringJobError(f"unknown judge call: {call_id}")
                transition_invocation_pg(
                    cursor, target["invocation_id"],
                    expected_revision=target["invocation_revision"],
                    expected_status="dispatching", status="settled",
                    changes={
                        "outcome": outcome,
                        "result_summary": deepcopy(result_summary),
                        "settled_at": _now(),
                    },
                )
                target.update({
                    "status": "settled",
                    "outcome": outcome,
                    "settled_at": _now(),
                    "usage": deepcopy(result_summary.get("usage")),
                    "cost_usd": result_summary.get("cost_usd"),
                    "price_table_version": result_summary.get("price_table_version"),
                    "raw_response": result_summary.get("raw_response"),
                    "error_class": result_summary.get("error_class"),
                })
                record = deepcopy(current)
                record["calls"] = calls
                record["billed_calls"] = sum(
                    1 for item in calls if item.get("outcome") == "succeeded"
                )
                if job_changes:
                    record.update(deepcopy(job_changes))
                if job_status != record["status"]:
                    _assert_transition(record["status"], job_status)
                record["status"] = job_status
                if job_status == "settled":
                    record["settled_at"] = _now()
                updated = _next_revision(record, expected_revision)
                self._write(cursor, updated, expected_revision=expected_revision,
                            expected_status="dispatching")
        return deepcopy(updated)

    # ------------------------------------------------------------- 取消
    def cancel(self, job_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        from .invocations import transition_invocation_pg

        with self._open() as connection:
            with connection.cursor() as cursor:
                current = self._locked(cursor, job_id)
                status = current["status"]
                if status == "completed":
                    return {"job": deepcopy(current), "outcome": "already_completed",
                            "receipt": current.get("receipt")}
                if status == "cancelled":
                    return {"job": deepcopy(current), "outcome": "already_cancelled",
                            "receipt": None}
                if status == "indeterminate":
                    return {"job": deepcopy(current), "outcome": "indeterminate",
                            "receipt": None,
                            "note": "unknown outcome stays indeterminate and is "
                                    "never resent"}
                if status in {"queued", "prepared"}:
                    for item in current.get("calls") or []:
                        if item.get("status") != "prepared":
                            continue
                        transition_invocation_pg(
                            cursor, item["invocation_id"],
                            expected_revision=item["invocation_revision"],
                            expected_status="prepared", status="settled",
                            changes={
                                "outcome": "failed",
                                "result_summary": {
                                    "reason": "cancelled_before_dispatch",
                                    "detail": reason,
                                },
                                "settled_at": _now(),
                            },
                        )
                        item["status"] = "settled"
                        item["outcome"] = "failed"
                    record = deepcopy(current)
                    record["status"] = "cancelled"
                    record["cancellation"] = {
                        "requested": True, "actor": actor, "reason": reason,
                        "at": _now(), "phase": status, "billed_calls": 0,
                    }
                    record["cancelled_at"] = _now()
                    record["result"] = record.get("result") or {
                        "cancelled_before_dispatch": True,
                    }
                    updated = _next_revision(record, current["revision"])
                    self._write(cursor, updated, expected_revision=current["revision"],
                                expected_status=status)
                    return {"job": deepcopy(updated), "outcome": "cancelled",
                            "receipt": None, "billed_calls": 0}
                if status == "dispatching":
                    if (current.get("cancellation") or {}).get("requested"):
                        return {"job": deepcopy(current),
                                "outcome": "cancel_requested", "receipt": None,
                                "in_flight": True}
                    record = deepcopy(current)
                    record["cancellation"] = {
                        "requested": True, "actor": actor, "reason": reason,
                        "at": _now(), "phase": status,
                    }
                    updated = _next_revision(record, current["revision"])
                    self._write(cursor, updated, expected_revision=current["revision"],
                                expected_status=status)
                    return {
                        "job": deepcopy(updated), "outcome": "cancel_requested",
                        "receipt": None, "in_flight": True,
                        "note": "a sent remote request can only be interrupted, "
                                "never declared unbilled",
                    }
                record = deepcopy(current)
                record["status"] = "cancelled"
                record["cancellation"] = {
                    "requested": True, "actor": actor, "reason": reason,
                    "at": _now(), "phase": status,
                    "billed_calls": record.get("billed_calls") or 0,
                }
                record["cancelled_at"] = _now()
                record["publish_outcome"] = "cancelled_before_publish"
                updated = _next_revision(record, current["revision"])
                self._write(cursor, updated, expected_revision=current["revision"],
                            expected_status=status)
        return {"job": deepcopy(updated), "outcome": "cancelled_before_publish",
                "receipt": None, "billed_calls": updated.get("billed_calls") or 0}

    # ------------------------------------------------------------- 发布
    def publish(
        self, job_id: str, *, expected_revision: int, pass_record: dict[str, Any],
        scores: list[dict[str, Any]], events: list[dict[str, Any]] | None = None,
        run_advance: dict[str, Any] | None = None,
        expected_run_revision: int | None = None,
        expected_run_status: str | None = None,
        receipt: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from .invocations import INVOCATION_TRANSITIONS  # noqa: F401  (parity import)

        stored_pass = deepcopy(pass_record)
        if not isinstance(stored_pass.get("id"), str) or not stored_pass["id"]:
            raise ValueError("scoring pass requires a nonempty id")
        rows = [deepcopy(row) for row in scores]
        for row in rows:
            row.setdefault("scoring_pass_id", stored_pass["id"])
            if row["scoring_pass_id"] != stored_pass["id"]:
                raise ValueError("score belongs to a different scoring pass")
        if (expected_run_revision is None) != (expected_run_status is None):
            raise ValueError("provide both expected run revision and status")
        with self._open() as connection:
            with connection.cursor() as cursor:
                current = self._locked(cursor, job_id)
                if current["status"] == "completed":
                    return {"job": deepcopy(current), "receipt": current.get("receipt"),
                            "published": False, "outcome": "already_completed"}
                if current["status"] == "cancelled":
                    return {"job": deepcopy(current), "receipt": None,
                            "published": False, "outcome": "already_cancelled"}
                if current["status"] not in {"settled", "prepared"}:
                    raise ScoringJobError(
                        f"job cannot publish from status {current['status']!r}"
                    )
                self._check(current, expected_revision, None)
                if stored_pass["id"] != current["reserved_pass_id"]:
                    raise ScoringJobError("pass id differs from the reserved pass id")
                if stored_pass.get("run_id") != current["run_id"]:
                    raise ScoringJobError(
                        "pass run_id differs from the job owner reference"
                    )
                if current["owner_kind"] == "subject":
                    cursor.execute(
                        "SELECT payload, revision FROM runs WHERE id = %s FOR UPDATE",
                        (current["run_id"],),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RunConflictError(
                            f"subject run is missing: {current['run_id']}"
                        )
                    run = self._row_payload(row)
                    if row[1] != expected_run_revision or (
                        run.get("status") != expected_run_status
                    ):
                        raise RunConflictError(
                            f"run revision or status changed: {current['run_id']}"
                        )
                    updated_run = {
                        **run,
                        **deepcopy(run_advance or {}),
                        "current_scoring_pass_id": stored_pass["id"],
                        "revision": row[1] + 1,
                    }
                    cursor.execute(
                        "UPDATE runs SET payload = %s, revision = %s WHERE id = %s "
                        "AND revision = %s AND payload->>'status' = %s",
                        (self._json(updated_run), updated_run["revision"],
                         current["run_id"], expected_run_revision,
                         expected_run_status),
                    )
                    if cursor.rowcount != 1:
                        raise RunConflictError(
                            f"run revision or status changed: {current['run_id']}"
                        )
                cursor.execute(
                    "INSERT INTO scoring_passes(id, run_id, payload) VALUES (%s, %s, %s)",
                    (stored_pass["id"], stored_pass["run_id"], self._json(stored_pass)),
                )
                for index, row in enumerate(rows):
                    cursor.execute(
                        "INSERT INTO score_sets(scoring_pass_id, case_id, trial_id, "
                        "metric_id, evaluator_id, evaluator_version, ordinal, payload) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            stored_pass["id"], row["case_id"], row.get("trial_id") or "",
                            row.get("metric_id") or "", row.get("evaluator_id") or "",
                            row.get("evaluator_version") or "", index, self._json(row),
                        ),
                    )
                record = deepcopy(current)
                record["status"] = "completed"
                record["completed_at"] = _now()
                record["receipt"] = deepcopy(receipt) if receipt is not None else {
                    "job_id": current["job_id"],
                    "scoring_pass_id": stored_pass["id"],
                    "published_at": _now(),
                }
                if result is not None:
                    record["result"] = deepcopy(result)
                updated = _next_revision(record, current["revision"])
                self._write(cursor, updated, expected_revision=current["revision"],
                            expected_status=current["status"])
                for event in events or []:
                    run_id = event["run_id"]
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))", (run_id,)
                    )
                    cursor.execute(
                        "SELECT COALESCE(MAX(seq), 0) + 1 FROM trace_events "
                        "WHERE run_id = %s",
                        (run_id,),
                    )
                    seq = cursor.fetchone()[0]
                    stored_event = {**deepcopy(event), "seq": seq}
                    cursor.execute(
                        "INSERT INTO trace_events(run_id, seq, payload) "
                        "VALUES (%s, %s, %s)",
                        (run_id, seq, self._json(stored_event)),
                    )
        return {"job": deepcopy(updated), "receipt": deepcopy(updated["receipt"]),
                "published": True, "outcome": "published"}

    # ------------------------------------------------------------- 恢复
    def recover_interrupted(self) -> list[str]:
        from .invocations import transition_invocation_pg

        touched: list[str] = []
        with self._open() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT job_id FROM scoring_jobs WHERE status = ANY(%s) "
                    "ORDER BY position",
                    (["prepared", "dispatching"],),
                )
                job_ids = [row[0] for row in cursor.fetchall()]
                for job_id in job_ids:
                    current = self._locked(cursor, job_id)
                    if current["status"] == "prepared":
                        if any(
                            item.get("status") == "dispatching"
                            for item in current.get("calls") or []
                        ):
                            continue
                        record = deepcopy(current)
                        record["status"] = "queued"
                        record["recovered_at"] = _now()
                        record["recovery"] = {
                            "reason": "prepared_without_dispatch_evidence",
                            "billed_calls": 0,
                        }
                    elif current["status"] == "dispatching":
                        for item in current.get("calls") or []:
                            if item.get("status") != "dispatching":
                                continue
                            transition_invocation_pg(
                                cursor, item["invocation_id"],
                                expected_revision=item["invocation_revision"],
                                expected_status="dispatching", status="settled",
                                changes={
                                    "outcome": "indeterminate",
                                    "result_summary": {
                                        "reason": "worker crashed after dispatch",
                                    },
                                    "settled_at": _now(),
                                },
                            )
                            item["status"] = "settled"
                            item["outcome"] = "indeterminate"
                            item["settled_at"] = _now()
                        record = deepcopy(current)
                        record["status"] = "indeterminate"
                        record["failure"] = {
                            "code": "CALL_OUTCOME_INDETERMINATE",
                            "message": "a dispatched judge call was interrupted "
                                       "before a durable result landed; it is never "
                                       "resent automatically",
                        }
                        record["indeterminate_at"] = _now()
                    else:
                        # Another recoverer may have already requeued or finalized it.
                        continue
                    updated = _next_revision(record, current["revision"])
                    self._write(cursor, updated, expected_revision=current["revision"],
                                expected_status=current["status"])
                    touched.append(updated["job_id"])
        return touched

    # ------------------------------------------------------------- 内部
    def _locked(self, cursor: Any, job_id: str) -> dict[str, Any]:
        cursor.execute(
            "SELECT payload FROM scoring_jobs WHERE job_id = %s FOR UPDATE", (job_id,)
        )
        row = cursor.fetchone()
        if row is None:
            raise ScoringJobError(f"scoring job is missing: {job_id}")
        return self._row_payload(row)

    @staticmethod
    def _check(
        current: dict[str, Any], expected_revision: int, expected_status: str | None,
    ) -> None:
        if current["revision"] != expected_revision:
            raise ScoringJobConflict(
                f"scoring job revision changed: {current['job_id']}"
            )
        if expected_status is not None and current["status"] != expected_status:
            raise ScoringJobConflict(
                f"scoring job status changed: {current['job_id']}"
            )

    def _write(
        self, cursor: Any, record: dict[str, Any], *, expected_revision: int,
        expected_status: str,
    ) -> None:
        stored = validate_scoring_job(record)
        cursor.execute(
            "UPDATE scoring_jobs SET payload = %s, status = %s, revision = %s, "
            "owner_kind = %s, owner_ref = %s, run_id = %s "
            "WHERE job_id = %s AND status = %s AND revision = %s",
            (
                self._json(stored), stored["status"], stored["revision"],
                stored["owner_kind"], stored["owner_ref"], stored["run_id"],
                stored["job_id"], expected_status, expected_revision,
            ),
        )
        if cursor.rowcount != 1:
            raise ScoringJobConflict(
                f"scoring job revision or status changed: {stored['job_id']}"
            )


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def scoring_jobs_for(store: Any) -> Any:
    """把 durable job 组件挂到既有 RunStore（SQLite / memory / PostgreSQL）。"""
    dsn = getattr(store, "dsn", None)
    if dsn:
        return PostgresScoringJobs(dsn)
    path = getattr(getattr(store, "runs", None), "_path", None)
    if path:
        return SQLiteScoringJobs(str(path))
    if all(
        hasattr(store, name)
        for name in ("runs", "events", "scoring_passes", "score_sets", "invocations")
    ):
        return MemoryScoringJobs(store)
    raise ScoringJobError(
        "unsupported storage backend for scoring jobs; expected SQLite, memory or PostgreSQL"
    )
