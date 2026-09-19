"""M1-T03：多指标 ScoringPass 落库、读取与不可变历史。

验收反例：
- 同一 Case 两指标分别可读；重复 metric key / legacy 混用被存储层拒绝。
- rescore 生成新 pass；旧 pass 字节内容不变；Provider 调用数不增加（spy=0 次新增）。
- SQLite / InMemory（/PG 有 DSN 时）三 store 结果一致。
- 旧 schema SQLite 库升级后旧行仍可读。
"""
from __future__ import annotations

import json
from copy import deepcopy

import pytest

from motte_eval.observation import metric_result_to_score
from motte_contracts.evaluation import MetricResult, MetricStatus
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore as MakeSQLite
from motte_sdk.execution_backends import build_execution_handle
from motte_sdk.service import RunService

REPLAY_MANIFEST = {
    "provider": {
        "kind": "replay",
        "fixture": {
            "case-1": {"output": {"content": "alpha"}, "expected": "alpha"},
            "case-2": {"output": {"content": "beta"}, "expected": "beta"},
        },
    },
}


def _metric(metric_id: str, passed: bool) -> MetricResult:
    return MetricResult(
        metric_id=metric_id, status=MetricStatus.scored, value=1.0 if passed else 0.0,
        passed=passed, evaluator_id="agent-deterministic", evaluator_version="1",
    )


def _multi_metric_scores() -> list[dict]:
    rows = []
    for case_id in ("case-1", "case-2"):
        rows.append(metric_result_to_score(_metric("file-content:report.json", True), case_id))
        rows.append(metric_result_to_score(_metric("tool-call:write_file", False), case_id))
    return rows


class InvokeSpy:
    def __init__(self, inner):  # noqa: ANN001
        self.inner = inner
        self.calls = 0

    def __call__(self, case_id):  # noqa: ANN001
        self.calls += 1
        return self.inner(case_id)

    def __getattr__(self, name):  # noqa: ANN001
        # 代理 expected_for / fixture 等发现协议，保持被包裹 provider 的行为
        return getattr(self.inner, name)


def _execute_replay(store) -> tuple[RunService, str, InvokeSpy]:
    from motte_sdk.replay_run import ReplayProvider

    replay = ReplayProvider(REPLAY_MANIFEST["provider"]["fixture"])
    spy = InvokeSpy(replay.invoke)
    service = RunService(store, provider=spy)
    run = service.create_run("replay@1", deepcopy(REPLAY_MANIFEST))
    service.execute(run["id"])
    return service, run["id"], spy


def test_append_multimetric_without_history_mutation(tmp_path):
    store = MakeSQLite(tmp_path / "runs.db")
    service, run_id, spy = _execute_replay(store)
    assert service.get_run(run_id)["status"] == "completed"
    first_pass = service.store.scoring_passes.current(run_id)
    first_bytes = json.dumps(first_pass, sort_keys=True).encode("utf-8")
    calls_before = spy.calls

    # rescore：新 pass；旧 pass 字节不变；零 Provider 调用
    service.rescore(run_id)
    assert spy.calls == calls_before
    passes = service.store.scoring_passes.list_for_run(run_id)
    assert len(passes) == 2
    assert json.dumps(passes[0], sort_keys=True).encode("utf-8") == first_bytes
    assert service.get_run(run_id)["current_scoring_pass_id"] == passes[1]["id"]

    # 追加多指标 pass：旧 pass 仍不变，新 pass 两 Case 两指标分别可读
    third = service._append_scoring_pass(run_id, _multi_metric_scores(), source="rescore")
    passes = service.store.scoring_passes.list_for_run(run_id)
    assert len(passes) == 3
    assert json.dumps(passes[0], sort_keys=True).encode("utf-8") == first_bytes

    rows = service.store.score_sets.list_for_pass(third["id"])
    assert len(rows) == 4
    metric_rows = service.store.score_sets.get_metric(
        third["id"], "case-1", "file-content:report.json",
        "agent-deterministic", "1",
    )
    assert metric_rows is not None and metric_rows["passed"] is True
    case_rows = service.store.score_sets.list_for_case(third["id"], "case-2")
    assert sorted(row["metric_id"] for row in case_rows) == [
        "file-content:report.json", "tool-call:write_file",
    ]

    # 汇总摘要包含指标维度
    stored_pass = service.store.scoring_passes.get(third["id"])
    assert stored_pass["summary"]["multi_metric"] is True
    assert stored_pass["summary"]["metrics"]["file-content:report.json"]["passed"] == 2
    assert stored_pass["summary"]["metrics"]["tool-call:write_file"]["failed"] == 2


def test_duplicate_metric_key_rejected_by_store(tmp_path):
    store = MakeSQLite(tmp_path / "runs.db")
    service, run_id, _ = _execute_replay(store)
    scores = _multi_metric_scores()
    duplicate = deepcopy(scores[0])
    scores.append(duplicate)
    with pytest.raises(ValueError, match="unique"):
        service._append_scoring_pass(run_id, scores, source="rescore")


def test_mixed_legacy_and_metric_rows_rejected(tmp_path):
    store = MakeSQLite(tmp_path / "runs.db")
    service, run_id, _ = _execute_replay(store)
    mixed = _multi_metric_scores() + [{"case_id": "case-1", "passed": True}]
    with pytest.raises(ValueError, match="mix"):
        service._append_scoring_pass(run_id, mixed, source="rescore")


def test_get_is_ambiguous_for_multi_metric_rows(tmp_path):
    store = MakeSQLite(tmp_path / "runs.db")
    service, run_id, _ = _execute_replay(store)
    third = service._append_scoring_pass(run_id, _multi_metric_scores(), source="rescore")
    with pytest.raises(ValueError, match="ambiguous"):
        service.store.score_sets.get(third["id"], "case-1")
    # legacy 单指标 pass 不受影响
    legacy_pass = service.store.scoring_passes.list_for_run(run_id)[0]
    legacy_row = service.store.score_sets.get(legacy_pass["id"], "case-1")
    assert legacy_row is not None and "metric_id" not in legacy_row


def test_sqlite_and_memory_stores_agree(tmp_path):
    scores = _multi_metric_scores()
    outputs = {}
    for label, store in (
        ("sqlite", MakeSQLite(tmp_path / "runs.db")), ("memory", InMemoryRunStore()),
    ):
        service, run_id, _ = _execute_replay(store)
        appended = service._append_scoring_pass(run_id, deepcopy(scores), source="rescore")
        rows = service.store.score_sets.list_for_pass(appended["id"])
        outputs[label] = sorted(
            (row["case_id"], row["metric_id"], row["passed"]) for row in rows
        )
    assert outputs["sqlite"] == outputs["memory"]
    assert len(outputs["sqlite"]) == 4


def test_sqlite_upgrades_legacy_score_sets_in_place(tmp_path):
    """旧 schema（case-only 主键）库升级后：旧行可读、新复合键可写。"""
    import sqlite3

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE scoring_passes (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE score_sets (
              scoring_pass_id TEXT NOT NULL,
              case_id TEXT NOT NULL,
              ordinal INTEGER NOT NULL,
              payload TEXT NOT NULL,
              PRIMARY KEY (scoring_pass_id, case_id)
            );
        """)
        connection.execute(
            "INSERT INTO scoring_passes(id, run_id, payload) VALUES ('pass-old', 'run-old', ?)",
            (json.dumps({"id": "pass-old", "run_id": "run-old"}),),
        )
        connection.execute(
            "INSERT INTO score_sets(scoring_pass_id, case_id, ordinal, payload) "
            "VALUES ('pass-old', 'case-1', 0, ?)",
            (json.dumps({"case_id": "case-1", "passed": True}),),
        )

    store = MakeSQLite(path)
    rows = store.score_sets.list_for_pass("pass-old")
    assert len(rows) == 1 and rows[0]["passed"] is True
    columns = {row[1] for row in store and _table_columns(path, "score_sets")}
    assert {"trial_id", "metric_id", "evaluator_id", "evaluator_version"} <= columns
    # 新复合键行可写入同一张表
    import sqlite3 as sql3

    with sql3.connect(path) as connection:
        connection.execute(
            "INSERT INTO score_sets(scoring_pass_id, case_id, trial_id, metric_id, "
            "evaluator_id, evaluator_version, ordinal, payload) "
            "VALUES ('pass-old', 'case-1', '', 'm', 'e', '1', 1, '{}')"
        )
    rows = store.score_sets.list_for_pass("pass-old")
    assert len(rows) == 2


def _table_columns(path, table):  # noqa: ANN001
    import sqlite3

    with sqlite3.connect(path) as connection:
        return connection.execute(f"PRAGMA table_info({table})").fetchall()


@pytest.mark.skipif(
    not __import__("os").environ.get("MOTTE_PG_DSN"),
    reason="set MOTTE_PG_DSN to run PostgreSQL integration tests",
)
def test_postgres_multi_metric_score_sets():
    """PG：migration 0004 后多指标复合键落库与查询（独立测试库）。"""
    import os

    from motte_storage.migrations import downgrade, revision_ids, upgrade
    from motte_storage.postgres import create_postgres_run_store

    dsn = os.environ["MOTTE_PG_DSN"]
    for _ in range(len(revision_ids())):
        downgrade(dsn, 1) if __import__("motte_storage.migrations", fromlist=["current"]).current(dsn) else None
    upgrade(dsn)
    store = create_postgres_run_store(dsn)
    service, run_id, _ = _execute_replay(store)
    appended = service._append_scoring_pass(run_id, _multi_metric_scores(), source="rescore")
    rows = store.score_sets.list_for_pass(appended["id"])
    assert len(rows) == 4
    metric_row = store.score_sets.get_metric(
        appended["id"], "case-1", "file-content:report.json", "agent-deterministic", "1",
    )
    assert metric_row is not None and metric_row["passed"] is True
    with pytest.raises(ValueError, match="ambiguous"):
        store.score_sets.get(appended["id"], "case-1")
