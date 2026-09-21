"""验收 F-01：旧形状的 SQLite 库必须能被启动路径原地升级。

背景：SQLite 侧没有 Alembic，表由各模块的 CREATE TABLE IF NOT EXISTS 建立，
既有库因此永远保持创建时的形状。旧实现在 score_sets 已经有 metric_id 时提前
返回，于是"有 metric_id、没有 trial_id"的中间形状永远补不上；trials 与
external_jobs 的主键也与当前 DDL 不同，补列根本修不了。结果是一个能跑完、
结算不了的部署（实测：no such column: repeat_index / table score_sets has no
column named trial_id）。

本文件钉住三件事：形状对齐、数据保留、以及对齐后真的能写入。
"""
from __future__ import annotations

import json
import sqlite3

from motte_storage.factory import create_run_store, create_resource_store

#: 三段历史形状都对不上当前 DDL，且都必须保留既有行。
LEGACY_SCHEMA = """
CREATE TABLE scoring_passes (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE score_sets (
  scoring_pass_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  metric_id TEXT NOT NULL DEFAULT '',
  ordinal INTEGER NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (scoring_pass_id, case_id, metric_id)
);
CREATE TABLE case_attempts (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, case_id TEXT NOT NULL,
  attempt_no INTEGER NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL,
  payload TEXT NOT NULL, UNIQUE (run_id, case_id, attempt_no)
);
CREATE INDEX case_attempts_run_idx ON case_attempts(run_id);
CREATE TABLE trials (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_key TEXT NOT NULL,
  status TEXT NOT NULL, payload TEXT NOT NULL,
  result_payload TEXT, finished_at TEXT
);
CREATE TABLE external_jobs (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, status TEXT NOT NULL,
  revision INTEGER NOT NULL, payload TEXT NOT NULL
);
"""

LEGACY_ROWS = """
INSERT INTO scoring_passes (id, run_id, payload) VALUES ('pass-1', 'run-1', '{}');
INSERT INTO score_sets (scoring_pass_id, case_id, metric_id, ordinal, payload)
  VALUES ('pass-1', 'case-1', 'legacy-metric', 0, '{"passed": true}');
INSERT INTO case_attempts (id, run_id, case_id, attempt_no, status, revision, payload)
  VALUES ('att-1', 'run-1', 'case-1', 1, 'settled', 1, '{}');
INSERT INTO trials (id, run_id, task_key, status, payload)
  VALUES ('trial-1', 'run-1', 'task-1', 'settled', '{"plan": 1}');
INSERT INTO external_jobs (id, run_id, status, revision, payload)
  VALUES ('job-1', 'run-1', 'settled', 1, '{"job": 1}');
"""


def legacy_database(path) -> str:
    connection = sqlite3.connect(str(path))
    connection.executescript(LEGACY_SCHEMA)
    connection.executescript(LEGACY_ROWS)
    connection.commit()
    connection.close()
    return str(path)


def columns(path, table):
    connection = sqlite3.connect(str(path))
    try:
        return [row[1] for row in connection.execute("PRAGMA table_info(" + table + ")")]
    finally:
        connection.close()


def rows(path, sql):
    connection = sqlite3.connect(str(path))
    try:
        return [tuple(row) for row in connection.execute(sql)]
    finally:
        connection.close()


def test_legacy_shapes_are_aligned_to_the_current_schema(tmp_path):
    """中间形状（有 metric_id、没有 trial_id）也必须被升级，而不是提前返回。"""
    path = legacy_database(tmp_path / "legacy.db")
    create_run_store(path)

    assert columns(path, "score_sets") == [
        "scoring_pass_id", "case_id", "trial_id", "metric_id",
        "evaluator_id", "evaluator_version", "ordinal", "payload",
    ]
    assert "trial_id" in columns(path, "case_attempts")
    for table in ("trials", "external_jobs"):
        names = columns(path, table)
        assert "trial_id" in names or "job_id" in names, names
    assert "repeat_index" in columns(path, "trials")
    assert "launch_token" in columns(path, "external_jobs")


def test_legacy_rows_survive_the_rebuild(tmp_path):
    """重建只搬运共有列，绝不丢已有行。"""
    path = legacy_database(tmp_path / "legacy.db")
    create_run_store(path)

    connection = sqlite3.connect(path)
    try:
        score = connection.execute(
            "SELECT case_id, metric_id, trial_id, evaluator_id, payload FROM score_sets"
        ).fetchone()
        assert score == ("case-1", "legacy-metric", "", "", '{"passed": true}')
        assert connection.execute("SELECT count(*) FROM case_attempts").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM trials").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM external_jobs").fetchone()[0] == 1
        # 主键换成新形状之后，旧行仍然按 (pass, case, '', metric, '', '') 唯一。
        assert connection.execute("SELECT count(*) FROM scoring_passes").fetchone()[0] == 1
    finally:
        connection.close()


def test_aligned_database_accepts_the_writes_that_used_to_fail(tmp_path):
    """升级后的库必须能写入带 trial_id 的分数与带 job_id 的外部作业。"""
    path = legacy_database(tmp_path / "legacy.db")
    store = create_run_store(path)
    store.runs.save({"id": "run-1", "status": "queued"})

    # 1) 多指标评分发布（旧库在 score_sets 写入时因缺 trial_id 失败）
    store.scoring_passes.append(
        {"id": "pass-2", "run_id": "run-1", "scorer_id": "workflow-assertions",
         "scorer_version": "1"},
        [{"case_id": "case-1", "metric_id": "metric-1",
          "evaluator_id": "workflow-assertions", "evaluator_version": "1",
          "passed": True}],
    )
    stored = store.score_sets.list_for_pass("pass-2")
    assert [(row["metric_id"], row["evaluator_id"]) for row in stored] == [
        ("metric-1", "workflow-assertions")
    ]

    # 2) Trial 计划（旧库在 trials 写入时因缺 repeat_index 失败）
    digest = "sha256:" + "a" * 64
    created = store.trials.create_plans([{
        "trial_id": "trial-2", "run_id": "run-1", "task_key": "task-1",
        "repeat_index": 0, "agent_config_hash": digest, "environment_hash": digest,
    }])
    assert created[0]["status"] == "created"
    assert store.trials.get("trial-2")["repeat_index"] == 0
    # 旧行按列名映射搬过来：旧 id 变成新的 trial_id，其余共有列原样保留
    migrated = rows(path, "SELECT trial_id, run_id, task_key, repeat_index, plan_hash,"
                          " created_at, payload FROM trials WHERE trial_id = 'trial-1'")
    assert migrated == [("trial-1", "run-1", "task-1", 0, "", "", '{"plan": 1}')]

    # 3) 外部 Job（旧库主键叫 id，缺 job_id / launch_token）
    store.external_jobs.begin_job({
        "job_id": "job-2", "run_id": "run-1", "status": "launching",
        "launch_token": "token-2",
    })
    assert store.external_jobs.get_job("job-2")["launch_token"] == "token-2"


def test_fresh_database_is_not_rebuilt(tmp_path):
    """新库不该被无谓重建（重复打开也必须幂等）。"""
    path = str(tmp_path / "fresh.db")
    create_run_store(path)
    before = columns(path, "score_sets")
    create_run_store(path)
    assert columns(path, "score_sets") == before


def test_resource_store_opens_the_same_legacy_database(tmp_path):
    """同一份旧库也要能被资源仓库打开（Run 与资源共用一个文件）。"""
    path = legacy_database(tmp_path / "legacy.db")
    create_resource_store(path)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT count(*) FROM score_sets").fetchone()[0] == 1
    finally:
        connection.close()
