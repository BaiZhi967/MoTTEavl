"""M3-T02：Trial 契约、存储与 CaseAttempt 冲突域（M3-A03，需求 4.2/4.3）。

反例覆盖：一 Task 三 Trial 全部保存且互不覆盖、旧无 Trial Run 仍可读写、
同 repeat 重复创建幂等/异内容冲突、transport retry 不产生新 Trial、
ScoreSet 既有 trial 维度可复用、Memory/SQLite/生产 factory 三后端一致。
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from motte_contracts.trial import (
    TrialPlan,
    TrialResult,
    VerifierObservation,
    VerifierStatus,
    compute_task_key,
    trial_id_for,
)
from motte_storage.integrity import RunConflictError
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore
from motte_storage.trials import MemoryTrials, SQLiteTrials, validate_plan, validate_result

TASKS = [
    compute_task_key(
        source_id="tb2-sample", dataset_revision="rev-1",
        normalized_relative_path=f"sample/task-{index}", task_content_hash=f"sha256:{index}" * 8,
    )
    for index in range(2)
]


def _plan(task_key: str, repeat_index: int, run_id: str = "run-1") -> dict[str, object]:
    return {
        "trial_id": trial_id_for(
            run_id=run_id, task_key=task_key, repeat_index=repeat_index,
            agent_config_hash="sha256:agent", environment_hash="sha256:env",
        ),
        "run_id": run_id,
        "task_key": task_key,
        "repeat_index": repeat_index,
        "agent_config_hash": "sha256:agent",
        "environment_hash": "sha256:env",
    }


def _result(
    trial_id: str, *, disposition: str, status: str, rewards: dict[str, float],
) -> dict[str, object]:
    return {
        "trial_id": trial_id,
        "source_trial_id": "native-trial-1",
        "disposition": disposition,
        "verifier_observation": {"status": status, "rewards": rewards},
        "termination": {"agent_exit": 0, "verifier_exit": 0},
    }


def _downgrade_to_pre_trial_schema(db_path: Path) -> dict[str, object]:
    """把库退回 M3 之前的形态：没有 trials 表、case_attempts 无 trial_id。

    用当前 store 建表后删掉新维度，比手抄一份旧 DDL 更忠实：除被删除的
    东西以外，其余 schema 与历史版本逐字相同。
    """
    store = SQLiteRunStore(db_path)
    run = store.runs.create({"id": "run-old", "scenario_version": "x@1", "status": "running"})
    attempt = store.attempts.begin(
        {"run_id": "run-old", "case_id": "case-old", "attempt_no": 1},
        expected_run_revision=run["revision"], expected_run_status=run["status"],
    )
    dispatched = store.attempts.dispatch(
        attempt["id"], expected_revision=attempt["revision"], run_id="run-old",
        expected_run_revision=run["revision"], expected_run_status=run["status"],
    )
    store.attempts.complete(
        attempt["id"], expected_revision=dispatched["revision"], status="succeeded",
    )
    legacy = store.attempts.get(attempt["id"])
    connection = sqlite3.connect(db_path)
    try:
        with connection:
            connection.execute("DROP TABLE IF EXISTS trials")
            connection.execute("ALTER TABLE case_attempts DROP COLUMN trial_id")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(case_attempts)")}
    finally:
        connection.close()
    assert "trial_id" not in columns, "fixture must reproduce a pre-M3 database"
    tables = {
        row[0]
        for row in sqlite3.connect(db_path).execute("SELECT name FROM sqlite_master")
    }
    assert "trials" not in tables
    # schema 已被降级，不再通过 store 读取（R24 起读取会核对 trial_id 索引列）；
    # 旧记录内容在降级前取好，升级后的可读性由下一个 store 实例验证。
    return legacy


def _plan_and_result_contract_rejections() -> None:
    with pytest.raises(ValueError, match="repeat_index"):
        validate_plan({"trial_id": "t", "run_id": "r", "task_key": "k", "repeat_index": -1,
                       "agent_config_hash": "a", "environment_hash": "e"})
    with pytest.raises(ValueError, match="task_key"):
        validate_plan({"trial_id": "t", "run_id": "r", "repeat_index": 0,
                       "agent_config_hash": "a", "environment_hash": "e"})
    with pytest.raises(ValueError, match="disposition"):
        validate_result({"trial_id": "t"})


@pytest.fixture(params=["memory", "sqlite"])
def trials(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "sqlite":
        return SQLiteTrials(str(tmp_path / "runs.db"))
    return MemoryTrials()


def test_planned_trials_do_not_overwrite_case(trials) -> None:
    """两 Task 各三 Trial：六个计划单元全部保存，互不覆盖（M3-A03）。"""
    plans = [_plan(task, repeat) for task in TASKS for repeat in range(3)]
    created = trials.create_plans(plans)

    assert [item["status"] for item in created] == ["created"] * 6
    assert len({item["trial_id"] for item in plans}) == 6
    stored = trials.list_for_run("run-1")
    assert len(stored) == 6
    assert sorted(row["repeat_index"] for row in stored) == [0, 0, 1, 1, 2, 2]
    for task_key in TASKS:
        assert len(trials.list_for_task("run-1", task_key)) == 3
    # 一个 Trial 落结果不影响其他 Trial 的计划与状态。
    first = str(plans[0]["trial_id"])
    assert trials.put_result(
        first, _result(first, disposition="succeeded", status="scored", rewards={"reward": 1.0}),
        source_hash="sha256:raw-1", parser_version="harbor-parser@1",
    )["status"] == "stored"
    remaining = trials.list_for_run("run-1")
    assert len(remaining) == 6
    assert sum(1 for row in remaining if row["result"] is None) == 5


def test_repeated_create_is_identical_and_conflict_is_explicit(trials) -> None:
    """同 repeat 重复创建 = identical；同 trial_id 异计划 = conflict 且保留原件。"""
    plan = _plan(TASKS[0], 0)
    assert [item["status"] for item in trials.create_plans([plan])] == ["created"]
    assert [item["status"] for item in trials.create_plans([plan])] == ["identical"]

    conflicting = {**plan, "agent_config_hash": "sha256:other"}
    outcome = trials.create_plans([conflicting])[0]
    assert outcome["status"] == "conflict"
    assert outcome["existing"]["plan_hash"] != outcome["incoming"]["plan_hash"]
    # 先写入的计划原样保留，冲突只登记不改写。
    assert trials.get(str(plan["trial_id"]))["plan"] == plan
    assert outcome["existing"]["plan"] == plan
    assert outcome["incoming"]["plan"] == conflicting


def test_result_import_is_idempotent_and_never_overwrites(trials) -> None:
    """同 source_hash 重复导入 = identical；异内容/异 hash = conflict（M3-A10）。"""
    plan = _plan(TASKS[0], 0)
    trials.create_plans([plan])
    trial_id = str(plan["trial_id"])
    result = _result(trial_id, disposition="failed", status="scored", rewards={"reward": 0.0})

    stored = trials.put_result(
        trial_id, result, source_hash="sha256:raw-1", parser_version="harbor-parser@1",
    )
    assert stored["status"] == "stored"
    repeat = trials.put_result(
        trial_id, result, source_hash="sha256:raw-1", parser_version="harbor-parser@1",
    )
    assert repeat["status"] == "identical"

    # 同一来源 hash 但内容不同（同一份原始证据被解析出不同结果）→ 冲突。
    changed = _result(trial_id, disposition="succeeded", status="scored", rewards={"reward": 1.0})
    conflict = trials.put_result(
        trial_id, changed, source_hash="sha256:raw-1", parser_version="harbor-parser@1",
    )
    assert conflict["status"] == "conflict"
    assert conflict["existing"]["result_hash"] != conflict["incoming"]["result_hash"]
    # 原始结果未被覆盖。
    assert trials.get(trial_id)["result"]["disposition"] == "failed"

    # 未知 trial 的迟到导入不会创建新计划。
    assert trials.put_result(
        "trial-does-not-exist", result, source_hash="sha256:x", parser_version="harbor-parser@1",
    )["status"] == "unknown_trial"
    assert len(trials.list_for_run("run-1")) == 1


def test_plan_and_result_validation_is_strict() -> None:
    """计划/结果的必填字段缺失时显式失败，不写半截记录。"""
    _plan_and_result_contract_rejections()


def test_create_plans_returns_records_detached_from_the_store() -> None:
    """返回值不是内部引用：改动返回值不改变已冻结计划与其 hash（review R25）。"""
    trials = MemoryTrials()
    plan = _plan(TASKS[0], 0)
    trial_id = str(plan["trial_id"])
    created = trials.create_plans([plan])[0]

    created["plan"]["plan"]["repeat_index"] = 99
    created["plan"]["plan_hash"] = "sha256:tampered"
    created["plan"]["status"] = "succeeded"
    assert trials.get(trial_id)["plan"] == plan
    assert trials.get(trial_id)["plan_hash"] != "sha256:tampered"
    assert trials.get(trial_id)["status"] == "pending"
    # 缓存 hash 与实际内容一致：原计划重复创建仍是 identical（不因外部改动变 conflict）。
    identical = trials.create_plans([plan])[0]
    assert identical["status"] == "identical"
    identical["plan"]["plan"]["repeat_index"] = 7
    assert trials.get(trial_id)["plan"] == plan

    # 冲突响应里的 existing 同样是副本。
    conflict = trials.create_plans([{**plan, "agent_config_hash": "sha256:other"}])[0]
    assert conflict["status"] == "conflict"
    conflict["existing"]["plan"]["repeat_index"] = 42
    conflict["existing"]["plan_hash"] = "sha256:tampered"
    assert trials.get(trial_id)["plan"] == plan
    assert trials.get(trial_id)["plan_hash"] != "sha256:tampered"
    assert trials.get(trial_id)["plan_hash"] == identical["plan"]["plan_hash"]


def test_put_result_returns_a_copy_of_the_frozen_result() -> None:
    """put_result 成功/identical 的返回值同样是副本，改不动已冻结结果（review R25）。"""
    trials = MemoryTrials()
    plan = _plan(TASKS[0], 0)
    trial_id = str(plan["trial_id"])
    trials.create_plans([plan])
    result = _result(trial_id, disposition="succeeded", status="scored", rewards={"reward": 1.0})

    stored = trials.put_result(
        trial_id, result, source_hash="sha256:raw", parser_version="p@1",
    )
    stored["result"]["disposition"] = "failed"
    stored["result"]["verifier_observation"]["rewards"]["reward"] = 0.0
    assert trials.get(trial_id)["result"]["disposition"] == "succeeded"
    assert trials.get(trial_id)["result"]["verifier_observation"]["rewards"] == {"reward": 1.0}
    repeat = trials.put_result(
        trial_id, result, source_hash="sha256:raw", parser_version="p@1",
    )
    assert repeat["status"] == "identical"


def test_put_result_rejects_result_of_another_trial(trials) -> None:
    """结果身份错配必须显式拒绝，绝不把别的 Trial/Run/Task 的结果写进目标（review R26）。"""
    plan = _plan(TASKS[0], 0)
    other = _plan(TASKS[0], 1)
    spare = _plan(TASKS[1], 0)
    trials.create_plans([plan, other, spare])
    trial_id = str(plan["trial_id"])
    valid = _result(trial_id, disposition="succeeded", status="scored", rewards={"reward": 1.0})
    assert trials.put_result(
        trial_id, valid, source_hash="sha256:raw", parser_version="p@1",
    )["status"] == "stored"

    foreign = _result(str(other["trial_id"]), disposition="failed", status="scored",
                      rewards={"reward": 0.0})
    with pytest.raises(ValueError, match="trial result identity mismatch: trial_id"):
        trials.put_result(trial_id, foreign, source_hash="sha256:other", parser_version="p@1")
    # 旧记录原样保留，别的 Trial 也不会被写入。
    assert trials.get(trial_id)["result"] == valid
    assert trials.get(str(other["trial_id"]))["result"] is None

    # 结果里出现的归属字段必须与冻结计划一致；错配同样拒绝且不改动旧记录。
    for field, value in (("run_id", "run-other"), ("task_key", TASKS[1]), ("repeat_index", 1)):
        mismatched = {**valid, field: value}
        with pytest.raises(ValueError, match=f"trial result identity mismatch: {field}"):
            trials.put_result(trial_id, mismatched, source_hash="sha256:raw", parser_version="p@1")
    assert trials.get(trial_id)["result"] == valid

    # 与冻结计划一致的归属字段照常接受。
    consistent = {
        **_result(str(spare["trial_id"]), disposition="failed", status="scored",
                  rewards={"reward": 0.0}),
        "run_id": "run-1", "task_key": TASKS[1], "repeat_index": 0,
    }
    assert trials.put_result(
        str(spare["trial_id"]), consistent, source_hash="sha256:spare", parser_version="p@1",
    )["status"] == "stored"


def test_transport_retry_does_not_create_a_trial(trials) -> None:
    """传输重试只增加 attempt，不增加 Trial（M3-G04）。"""
    plan = _plan(TASKS[0], 0)
    trials.create_plans([plan])
    before = len(trials.list_for_run("run-1"))
    # 重试路径只重复导入同一 Trial 的结果，不调用 create_plans。
    trial_id = str(plan["trial_id"])
    result = _result(trial_id, disposition="failed", status="scored", rewards={"reward": 0.0})
    trials.put_result(trial_id, result, source_hash="sha256:raw", parser_version="p@1")
    trials.put_result(trial_id, result, source_hash="sha256:raw", parser_version="p@1")
    assert len(trials.list_for_run("run-1")) == before


# ---------------------------------------------------------------- 契约层


def test_trial_plan_and_result_contracts_are_self_consistent() -> None:
    """契约：计划身份可重算；reward=0 是有效失败；非 scored 不得携带 reward。"""
    plan = TrialPlan(
        trial_id="trial-x", run_id="run-1", task_key=TASKS[0], repeat_index=0,
        agent_config_hash="sha256:a", environment_hash="sha256:e",
    )
    assert plan.trial_id == "trial-x"
    assert plan.plan_key.startswith("sha256:")

    zero = TrialResult(
        trial_id="trial-x", disposition="failed",
        verifier_observation=VerifierObservation(
            status=VerifierStatus.scored, rewards={"reward": 0.0},
        ),
    )
    assert zero.passed is False
    assert zero.verifier_observation.reward == 0.0

    one = zero.model_copy(update={
        "disposition": "succeeded",
        "verifier_observation": VerifierObservation(
            status=VerifierStatus.scored, rewards={"reward": 1.0},
        ),
    })
    assert one.passed is True

    missing = TrialResult(
        trial_id="trial-x", disposition="not_attempted",
        verifier_observation=VerifierObservation(status=VerifierStatus.missing_verifier_evidence),
    )
    assert missing.passed is None

    with pytest.raises(ValueError, match="scored verifier observation requires"):
        VerifierObservation(status=VerifierStatus.scored, rewards={})
    with pytest.raises(ValueError, match="must not carry rewards"):
        VerifierObservation(
            status=VerifierStatus.missing_verifier_evidence, rewards={"reward": 0.0},
        )
    with pytest.raises(ValueError, match="must be a number"):
        VerifierObservation(status=VerifierStatus.scored, rewards={"reward": True})
    with pytest.raises(ValueError, match="finite"):
        VerifierObservation(status=VerifierStatus.scored, rewards={"reward": float("nan")})
    with pytest.raises(ValueError, match="succeeded/failed disposition"):
        TrialResult(
            trial_id="trial-x", disposition="succeeded",
            verifier_observation=VerifierObservation(status=VerifierStatus.verifier_error),
        )


# ---------------------------------------------------------------- 生产装配


def test_production_run_store_exposes_trials_for_all_backends(tmp_path: Path) -> None:
    """生产 factory 装配的 store.trials 在重建后仍可读（不只孤立 repository）。"""
    db_path = tmp_path / "runs.db"
    sqlite_store = SQLiteRunStore(db_path)
    memory_store = InMemoryRunStore()
    plans = [_plan(task, repeat) for task in TASKS for repeat in range(3)]

    for store in (sqlite_store, memory_store):
        assert [item["status"] for item in store.trials.create_plans(plans)] == ["created"] * 6
        assert len(store.trials.list_for_run("run-1")) == 6

    reopened = SQLiteRunStore(db_path)
    assert len(reopened.trials.list_for_run("run-1")) == 6
    assert len({row["trial_id"] for row in reopened.trials.list_for_run("run-1")}) == 6


def test_attempt_conflict_domain_is_trial_scoped(tmp_path: Path) -> None:
    """同 Trial 只有一个开放 attempt；一个 Trial 完成不写死整个 Task。"""
    for store in (InMemoryRunStore(), SQLiteRunStore(tmp_path / "a.db")):
        run = store.runs.create({"id": "run-1", "scenario_version": "x@1", "status": "running"})
        first = _plan(TASKS[0], 0)
        second = _plan(TASKS[0], 1)

        attempt_a = store.attempts.begin({
            "run_id": "run-1", "case_id": TASKS[0], "attempt_no": 1,
            "trial_id": first["trial_id"],
        })
        with pytest.raises(RunConflictError, match="open attempt"):
            store.attempts.begin({
                "run_id": "run-1", "case_id": TASKS[0], "attempt_no": 2,
                "trial_id": first["trial_id"],
            })

        # 第二个 Trial 的 attempt 不被第一个 Trial 挡住。
        attempt_b = store.attempts.begin({
            "run_id": "run-1", "case_id": TASKS[0], "attempt_no": 2,
            "trial_id": second["trial_id"],
        })
        assert attempt_a["trial_id"] == first["trial_id"]
        assert attempt_b["trial_id"] == second["trial_id"]

        # Trial 维度的 attempt 不写任务级 CaseRun（complete 阶段拒绝）。
        dispatched_a = store.attempts.dispatch(
            attempt_a["id"], expected_revision=attempt_a["revision"], run_id="run-1",
            expected_run_revision=run["revision"], expected_run_status=run["status"],
        )
        dispatched_b = store.attempts.dispatch(
            attempt_b["id"], expected_revision=attempt_b["revision"], run_id="run-1",
            expected_run_revision=run["revision"], expected_run_status=run["status"],
        )
        with pytest.raises(RunConflictError, match="task-level case result"):
            store.attempts.complete(
                attempt_a["id"], expected_revision=dispatched_a["revision"],
                case_run={"run_id": "run-1", "case_id": TASKS[0], "result": "x"},
            )
        done = store.attempts.complete(
            attempt_a["id"], expected_revision=dispatched_a["revision"], status="succeeded",
        )
        assert done["status"] == "succeeded"
        # 第一个 Trial 完成后同 Task 仍未写死：另一个 Trial 可以独立完成。
        assert store.case_runs.get("run-1", TASKS[0]) is None
        store.attempts.complete(
            attempt_b["id"], expected_revision=dispatched_b["revision"], status="failed",
        )
        assert store.case_runs.get("run-1", TASKS[0]) is None


def test_attempt_trial_id_cannot_be_rewritten_by_a_transition(tmp_path: Path) -> None:
    """trial_id 与 run/case/attempt 身份一样不可变，改写不能绕过 Case 写保护（review R24）。"""
    for store in (InMemoryRunStore(), SQLiteRunStore(tmp_path / "immutable.db")):
        run = store.runs.create({"id": "run-1", "scenario_version": "x@1", "status": "running"})
        plan = _plan(TASKS[0], 0)
        attempt = store.attempts.begin({
            "run_id": "run-1", "case_id": TASKS[0], "attempt_no": 1,
            "trial_id": plan["trial_id"],
        })
        dispatched = store.attempts.dispatch(
            attempt["id"], expected_revision=attempt["revision"], run_id="run-1",
            expected_run_revision=run["revision"], expected_run_status=run["status"],
        )

        with pytest.raises(ValueError, match="identity"):
            store.attempts.transition(
                attempt["id"], expected_revision=dispatched["revision"],
                expected_status="dispatching", status="failed", changes={"trial_id": ""},
            )
        with pytest.raises(ValueError, match="identity"):
            store.attempts.complete(
                attempt["id"], expected_revision=dispatched["revision"],
                changes={"trial_id": ""},
            )
        with pytest.raises(ValueError, match="identity"):
            store.attempts.complete(
                attempt["id"], expected_revision=dispatched["revision"],
                changes={"trial_id": plan["trial_id"]},
            )
        # 身份未被改写：Trial 维度仍在，任务级 CaseRun 写入继续被拒。
        assert store.attempts.get(attempt["id"])["trial_id"] == plan["trial_id"]
        with pytest.raises(RunConflictError, match="task-level case result"):
            store.attempts.complete(
                attempt["id"], expected_revision=dispatched["revision"],
                case_run={"run_id": "run-1", "case_id": TASKS[0], "result": "x"},
            )
        assert store.case_runs.get("run-1", TASKS[0]) is None


def test_sqlite_attempt_rejects_payload_and_column_drift(tmp_path: Path) -> None:
    """payload 的 trial_id 与索引列脱节时读取即失败，不让身份漂移绕过保护（review R24）。"""
    db_path = tmp_path / "drift.db"
    store = SQLiteRunStore(db_path)
    store.runs.create({"id": "run-1", "scenario_version": "x@1", "status": "running"})
    plan = _plan(TASKS[0], 0)
    attempt = store.attempts.begin({
        "run_id": "run-1", "case_id": TASKS[0], "attempt_no": 1, "trial_id": plan["trial_id"],
    })
    connection = sqlite3.connect(db_path)
    try:
        with connection:
            row = connection.execute(
                "SELECT payload FROM case_attempts WHERE id = ?", (attempt["id"],),
            ).fetchone()
            payload = json.loads(row[0])
            payload["trial_id"] = ""
            connection.execute(
                "UPDATE case_attempts SET payload = ? WHERE id = ?",
                (json.dumps(payload, sort_keys=True), attempt["id"]),
            )
    finally:
        connection.close()

    with pytest.raises(RunConflictError, match="trial_id"):
        store.attempts.get(attempt["id"])
    with pytest.raises(RunConflictError, match="trial_id"):
        store.attempts.list_for_run("run-1")
    with pytest.raises(RunConflictError, match="trial_id"):
        store.attempts.transition(
            attempt["id"], expected_revision=attempt["revision"],
            expected_status="prepared", status="failed",
        )
    with pytest.raises(RunConflictError, match="trial_id"):
        store.attempts.complete(
            attempt["id"], expected_revision=attempt["revision"], status="failed",
            case_run={"run_id": "run-1", "case_id": TASKS[0], "result": "x"},
        )
    assert store.case_runs.get("run-1", TASKS[0]) is None, "漂移状态不得写入任务级 CaseRun"


def test_legacy_attempt_and_case_semantics_unchanged(tmp_path: Path) -> None:
    """旧无 Trial 路径保持 M2 行为：已有 CaseRun 仍然阻止新 attempt。"""
    for store in (InMemoryRunStore(), SQLiteRunStore(tmp_path / "legacy.db")):
        run = store.runs.create({"id": "run-l", "scenario_version": "x@1", "status": "running"})
        attempt = store.attempts.begin(
            {"run_id": "run-l", "case_id": "case-1", "attempt_no": 1},
            expected_run_revision=run["revision"], expected_run_status=run["status"],
        )
        assert attempt["trial_id"] == ""
        dispatched = store.attempts.dispatch(
            attempt["id"], expected_revision=attempt["revision"], run_id="run-l",
            expected_run_revision=run["revision"], expected_run_status=run["status"],
        )
        store.attempts.complete(
            attempt["id"], expected_revision=dispatched["revision"], status="succeeded",
            case_run={"run_id": "run-l", "case_id": "case-1", "outcome": "responded",
                      "result": "answer"},
        )
        with pytest.raises(RunConflictError, match="case result already exists"):
            store.attempts.begin({"run_id": "run-l", "case_id": "case-1", "attempt_no": 2})
        assert store.case_runs.get("run-l", "case-1")["result"] == "answer"


def test_sqlite_upgrade_adds_trial_dimension_to_existing_database(tmp_path: Path) -> None:
    """旧库（无 trials 表、无 trial_id 列）升级后旧数据可读、新 Trial 可写。"""
    db_path = tmp_path / "old.db"
    legacy_attempt = _downgrade_to_pre_trial_schema(db_path)
    assert legacy_attempt["status"] == "succeeded"

    store = SQLiteRunStore(db_path)
    inspector = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in inspector.execute("PRAGMA table_info(case_attempts)")}
    finally:
        inspector.close()
    assert "trial_id" in columns
    assert [item["id"] for item in store.attempts.list_for_run("run-old")] == [
        legacy_attempt["id"],
    ]
    assert store.trials.list_for_run("run-old") == []

    plan = _plan(TASKS[0], 0, run_id="run-old")
    assert [item["status"] for item in store.trials.create_plans([plan])] == ["created"]
    assert len(SQLiteRunStore(db_path).trials.list_for_run("run-old")) == 1


def test_scoreset_reuses_existing_trial_dimension(tmp_path: Path) -> None:
    """多 Trial 的同 Case 分数共存于同一 ScoringPass（复用 M1 的 trial 维度）。"""
    for store in (InMemoryRunStore(), SQLiteRunStore(tmp_path / "scores.db")):
        record = store.scoring_passes.append(
            {
                "id": "pass-1", "run_id": "run-1", "status": "succeeded",
                "scorer_id": "harbor", "scorer_version": "harbor@1",
                "summary": {"aggregate": {}},
            },
            [
                {
                    "case_id": "task-key", "trial_id": "trial-a", "metric_id": "reward",
                    "evaluator_id": "harbor-verifier", "evaluator_version": "1",
                    "value": 1.0, "passed": True, "outcome": "scored", "scoring_pass_id": "pass-1",
                },
                {
                    "case_id": "task-key", "trial_id": "trial-b", "metric_id": "reward",
                    "evaluator_id": "harbor-verifier", "evaluator_version": "1",
                    "value": 0.0, "passed": False, "outcome": "scored", "scoring_pass_id": "pass-1",
                },
            ],
        )
        assert record["id"] == "pass-1"
        assert store.score_sets.list_for_case("pass-1", "task-key") != []
        assert store.score_sets.get_metric(
            "pass-1", "task-key", "reward", "harbor-verifier", "1", trial_id="trial-a",
        )["value"] == 1.0
        assert store.score_sets.get_metric(
            "pass-1", "task-key", "reward", "harbor-verifier", "1", trial_id="trial-b",
        )["value"] == 0.0
        # 同一 Case 出现多 Trial 后，按 Case 单值读取必须显式失败而不是挑一条。
        with pytest.raises(ValueError, match="ambiguous"):
            store.score_sets.get("pass-1", "task-key")


@pytest.mark.skipif(
    not os.environ.get("MOTTE_PG_DSN"),
    reason="real PostgreSQL required (MOTTE_PG_DSN)",
)
def test_postgres_put_result_rejects_identity_mismatch() -> None:  # pragma: no cover - 需真实 PG
    """PG 后端与 Memory/SQLite 同语义：结果身份错配显式拒绝且不落盘（review R26）。"""
    from uuid import uuid4

    from motte_storage.postgres import create_postgres_run_store

    dsn = os.environ["MOTTE_PG_DSN"]
    store = create_postgres_run_store(dsn, migrate=True)
    run_id = f"pg-identity-{uuid4().hex}"
    plans = [_plan(TASKS[0], 0, run_id=run_id), _plan(TASKS[0], 1, run_id=run_id)]
    assert [item["status"] for item in store.trials.create_plans(plans)] == ["created", "created"]
    trial_id = str(plans[0]["trial_id"])
    foreign = str(plans[1]["trial_id"])

    with pytest.raises(ValueError, match="trial result identity mismatch: trial_id"):
        store.trials.put_result(
            trial_id,
            _result(foreign, disposition="failed", status="scored", rewards={"reward": 0.0}),
            source_hash="sha256:foreign", parser_version="harbor-parser@1",
        )
    with pytest.raises(ValueError, match="trial result identity mismatch: run_id"):
        store.trials.put_result(
            trial_id,
            {**_result(trial_id, disposition="failed", status="scored", rewards={}),
             "run_id": "run-other"},
            source_hash="sha256:other-run", parser_version="harbor-parser@1",
        )
    assert store.trials.get(trial_id)["result"] is None, "错配结果不得落盘"


@pytest.mark.skipif(
    not os.environ.get("MOTTE_PG_DSN"),
    reason="real PostgreSQL required (MOTTE_PG_DSN)",
)
def test_postgres_attempt_trial_identity_is_immutable() -> None:  # pragma: no cover - 需真实 PG
    """PG 后端：trial_id 不可改写，payload 与索引列脱节时读取即失败（review R24）。"""
    from uuid import uuid4

    import psycopg
    from psycopg.types.json import Json

    from motte_storage.postgres import create_postgres_run_store

    dsn = os.environ["MOTTE_PG_DSN"]
    store = create_postgres_run_store(dsn, migrate=True)
    run_id = f"pg-attempt-{uuid4().hex}"
    run = store.runs.create({"id": run_id, "scenario_version": "x@1", "status": "running"})
    plan = _plan(TASKS[0], 0, run_id=run_id)
    attempt = store.attempts.begin({
        "run_id": run_id, "case_id": TASKS[0], "attempt_no": 1, "trial_id": plan["trial_id"],
    })
    dispatched = store.attempts.dispatch(
        attempt["id"], expected_revision=attempt["revision"], run_id=run_id,
        expected_run_revision=run["revision"], expected_run_status=run["status"],
    )
    with pytest.raises(ValueError, match="identity"):
        store.attempts.transition(
            attempt["id"], expected_revision=dispatched["revision"],
            expected_status="dispatching", status="failed", changes={"trial_id": ""},
        )
    with pytest.raises(ValueError, match="identity"):
        store.attempts.complete(
            attempt["id"], expected_revision=dispatched["revision"], changes={"trial_id": ""},
        )
    assert store.attempts.get(attempt["id"])["trial_id"] == plan["trial_id"]

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM case_attempts WHERE id = %s", (attempt["id"],),
            )
            payload = dict(cursor.fetchone()[0])
            payload["trial_id"] = ""
            cursor.execute(
                "UPDATE case_attempts SET payload = %s WHERE id = %s",
                (Json(payload), attempt["id"]),
            )
    with pytest.raises(RunConflictError, match="trial_id"):
        store.attempts.get(attempt["id"])
    with pytest.raises(RunConflictError, match="trial_id"):
        store.attempts.complete(
            attempt["id"], expected_revision=dispatched["revision"], status="failed",
            case_run={"run_id": run_id, "case_id": TASKS[0], "result": "x"},
        )


def _pg_concurrent_first_write(
    dsn: str, held_plan: dict[str, object], incoming_plan: dict[str, object],
) -> list[dict[str, object]]:
    """两连接同步屏障：A 持未提交的同一 trial_id 行，B 首写随后进入。

    B 的返回值/异常原样带回：``UniqueViolation`` 正是不原子首写的反例（review R22）。
    """
    import threading
    import time

    import psycopg
    from psycopg.types.json import Json

    from motte_storage.pg_audit_store import PgTrials
    from motte_storage.trials import _plan_record

    held = _plan_record(dict(held_plan))
    holder = psycopg.connect(dsn)
    outcome: dict[str, object] = {}
    try:
        with holder.cursor() as cursor:
            cursor.execute(
                "INSERT INTO trials(trial_id, run_id, task_key, repeat_index, status, plan_hash, payload, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",  # noqa: E501
                (held["trial_id"], held["run_id"], held["task_key"], int(held["repeat_index"]),
                 held["status"], held["plan_hash"], Json(held), held["created_at"]),
            )
        started = threading.Event()

        def writer() -> None:
            started.set()
            try:
                outcome["result"] = PgTrials(dsn).create_plans([dict(incoming_plan)])
            except BaseException as error:  # noqa: BLE001 - 反例需要原样观察
                outcome["error"] = error

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        assert started.wait(timeout=5), "writer thread did not start"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with holder.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_locks WHERE NOT granted LIMIT 1")
                if cursor.fetchone() is not None:
                    break
            time.sleep(0.05)
        else:
            raise AssertionError("concurrent first write never blocked on the held row")
        holder.commit()
        thread.join(timeout=30)
        assert not thread.is_alive(), "concurrent create_plans did not return"
    finally:
        holder.close()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


@pytest.mark.skipif(
    not os.environ.get("MOTTE_PG_DSN"),
    reason="real PostgreSQL required (MOTTE_PG_DSN)",
)
def test_postgres_create_plans_concurrent_first_write() -> None:  # pragma: no cover - 需真实 PG
    """两个事务同时首写同一 trial_id：第二个必须 identical/conflict（review R22）。"""
    from uuid import uuid4

    from motte_storage.postgres import create_postgres_run_store

    dsn = os.environ["MOTTE_PG_DSN"]
    store = create_postgres_run_store(dsn, migrate=True)

    # 相同 payload 竞争 → identical（重复创建是 no-op）。
    held = _plan(TASKS[0], 0, run_id=f"pg-race-{uuid4().hex}")
    same = _pg_concurrent_first_write(dsn, held, held)
    assert [item["status"] for item in same] == ["identical"]
    assert store.trials.get(str(held["trial_id"]))["plan"] == held

    # 异内容竞争 → conflict，先写入（已提交）者保留。
    held_other = _plan(TASKS[1], 0, run_id=f"pg-race-{uuid4().hex}")
    changed = {**held_other, "agent_config_hash": "sha256:other"}
    conflict = _pg_concurrent_first_write(dsn, held_other, changed)
    assert [item["status"] for item in conflict] == ["conflict"]
    assert conflict[0]["existing"]["plan"] == held_other
    assert conflict[0]["incoming"]["plan"] == changed
    assert store.trials.get(str(held_other["trial_id"]))["plan"] == held_other


@pytest.mark.skipif(
    not os.environ.get("MOTTE_PG_DSN"),
    reason="real PostgreSQL required (MOTTE_PG_DSN)",
)
def test_postgres_trials_roundtrip_and_two_connection_race() -> None:  # pragma: no cover - 需真实 PG
    """真实 PostgreSQL 的 Trial 往返、幂等与两连接并发（skip 不计通过）。

    每个执行轮次用独立 run_id，因此在长期存在的 PG 上重复运行仍然可复现。
    """
    from uuid import uuid4

    from motte_storage.postgres import create_postgres_run_store
    from motte_storage.trials import _canonical

    run_id = f"pg-run-{uuid4().hex}"
    dsn = os.environ["MOTTE_PG_DSN"]
    store = create_postgres_run_store(dsn, migrate=True)
    second = create_postgres_run_store(dsn)
    plans = [_plan(task, repeat, run_id=run_id) for task in TASKS for repeat in range(3)]

    # 两个独立连接（两个 store 实例）各写一次同一计划：只可能有一个 created，
    # 另一个必须是 identical，绝不产生重复 Trial。
    first_statuses = [item["status"] for item in store.trials.create_plans(plans)]
    second_statuses = [item["status"] for item in second.trials.create_plans(plans)]
    assert first_statuses == ["created"] * 6
    assert second_statuses == ["identical"] * 6
    assert len(store.trials.list_for_run(run_id)) == 6

    # 异内容同 trial_id：冲突并保留先写入的计划。
    conflicting = {**plans[0], "agent_config_hash": "sha256:other"}
    outcome = second.trials.create_plans([conflicting])[0]
    assert outcome["status"] == "conflict"
    assert second.trials.get(str(plans[0]["trial_id"]))["plan"] == plans[0]

    trial_id = str(plans[0]["trial_id"])
    result = _result(trial_id, disposition="succeeded", status="scored", rewards={"reward": 1.0})
    assert store.trials.put_result(
        trial_id, result, source_hash="sha256:pg", parser_version="harbor-parser@1",
    )["status"] == "stored"
    assert second.trials.put_result(
        trial_id, result, source_hash="sha256:pg", parser_version="harbor-parser@1",
    )["status"] == "identical"
    assert _canonical(store.trials.get(trial_id)["result"]) == _canonical(result)

    # 跨 Run 隔离：另一个 run 的 trial 不出现在本 run 的视图里。
    other = [_plan(TASKS[0], 0, run_id=f"pg-other-{uuid4().hex}")]
    store.trials.create_plans(other)
    assert len(store.trials.list_for_run(run_id)) == 6
