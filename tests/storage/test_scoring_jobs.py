"""M5-T09b/T09c：durable ScoringJob 存储语义（Memory / SQLite 独立连接，PG 可选）。

覆盖：请求幂等键与内容 fingerprint 分离、claim CAS、prepared/dispatching/settled
先落库、取消（零调用 / 已发送只能中断 / 幂等）、单事务发布（ScoreSet + pass +
receipt + current 指针）、事务中断全有或全无、崩溃恢复隔离 job、calibration owner
不伪造 Run，以及 migration 0012 的降级保护。
"""
from __future__ import annotations

import os
from copy import deepcopy

import pytest

from motte_storage.integrity import RunConflictError
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore
from motte_storage.scoring_jobs import (
    MemoryScoringJobs,
    ScoringJobConflict,
    ScoringJobError,
    SQLiteScoringJobs,
    job_view,
    scoring_jobs_for,
)

REQUEST_KEY = "req-1"
FINGERPRINT = "sha256:" + "f" * 64


def make_run(store, run_id: str = "run-1", *, status: str = "completed") -> dict:
    run = {
        "id": run_id,
        "schema_version": 2,
        "revision": 1,
        "scenario_version": "replay@1",
        "status": status,
        "manifest": {"evaluation": {"scorer_id": "deterministic", "scorer_version": "1"}},
        "requested_manifest": {},
        "case_ids": ["case-1"],
        "created_at": "2026-09-21T00:00:00+00:00",
        "updated_at": "2026-09-21T00:00:00+00:00",
    }
    store.runs.create(run, event={"run_id": run_id, "type": "queued", "status": status})
    return store.runs.get(run_id)


def job_record(
    *, request_key: str = REQUEST_KEY, fingerprint: str = FINGERPRINT,
    owner: dict | None = None, job_id: str = "sjob-1", reserved_pass_id: str = "pass-1",
    status: str = "queued", revision: int = 1, mode: str = "single",
    publish_policy: str = "all_scored",
) -> dict:
    owner = owner or {"kind": "subject", "run_id": "run-1"}
    owner_ref = (
        f"run:{owner['run_id']}" if owner["kind"] == "subject"
        else f"calibration:{owner['calibration_job_id']}"
    )
    return {
        "schema_version": 1,
        "job_id": job_id,
        "request_key": request_key,
        "fingerprint": fingerprint,
        "owner": owner,
        "owner_kind": owner["kind"],
        "owner_ref": owner_ref,
        "run_id": owner["run_id"] if owner["kind"] == "subject" else owner_ref,
        "status": status,
        "revision": revision,
        "reserved_pass_id": reserved_pass_id,
        "mode": mode,
        "publish_policy": publish_policy,
        "judge_spec": {"judge_profile_id": "jp-1", "model": "judge-model"},
        "budget": {"max_calls": 4},
        "created_at": "2026-09-21T00:00:00+00:00",
        "calls": [],
    }


def invocation_record(call_id: str = "call-1", *, owner: dict | None = None) -> dict:
    owner = owner or {"kind": "subject", "run_id": "run-1", "case_id": "case-1"}
    run_id = owner["run_id"] if owner["kind"] == "subject" else (
        f"calibration:{owner['calibration_job_id']}"
    )
    case_id = owner["case_id"] if owner["kind"] == "subject" else owner["sample_id"]
    return {
        "id": f"inv-{call_id}",
        "run_id": run_id,
        "case_id": case_id,
        "kind": "model",
        "step": 1,
        "status": "prepared",
        "purpose": "judge",
        "job_id": "sjob-1",
        "owner": owner,
        "model": "judge-model",
        "request_summary": {},
        "prepared_at": "2026-09-21T00:00:00+00:00",
    }


def pass_record(pass_id: str = "pass-1", run_id: str = "run-1") -> dict:
    return {
        "id": pass_id,
        "run_id": run_id,
        "scorer_id": "judge:jp-1",
        "scorer_version": "answer-quality@1#abcdef",
        "created_at": "2026-09-21T00:00:00+00:00",
        "source": "judge",
        "purpose": "judge",
        "job_id": "sjob-1",
        "scores": [],
    }


def score_row(case_id: str = "case-1", metric_id: str = "task_completion") -> dict:
    return {
        "case_id": case_id,
        "metric_id": metric_id,
        "evaluator_id": "judge:jp-1",
        "evaluator_version": "answer-quality@1#abcdef",
        "metric_status": "scored",
        "value": None,
        "passed": True,
    }


@pytest.fixture(params=["sqlite", "memory"])
def store(request, tmp_path):
    if request.param == "sqlite":
        return SQLiteRunStore(tmp_path / "runs.db")
    return InMemoryRunStore()


def jobs_for(store):
    return scoring_jobs_for(store)


def test_submit_reuses_same_content_and_conflicts_on_different_content(store):
    make_run(store)
    jobs = jobs_for(store)
    first = jobs.submit(job_record())
    assert first["created"] is True
    assert first["job"]["reserved_pass_id"] == "pass-1"

    reused = jobs.submit(job_record(job_id="sjob-2", reserved_pass_id="pass-2"))
    assert reused["created"] is False
    assert reused["job"]["job_id"] == "sjob-1"

    with pytest.raises(ScoringJobConflict):
        jobs.submit(job_record(fingerprint="sha256:" + "a" * 64, job_id="sjob-3"))
    # 冲突不会覆盖既有 job。
    assert jobs.get("sjob-1")["fingerprint"] == FINGERPRINT
    assert jobs.get("sjob-3") is None


def test_explicit_calibration_repeat_uses_a_new_request_key(store):
    jobs = jobs_for(store)
    owner = {
        "kind": "calibration", "calibration_job_id": "cjob-1",
        "sample_ids": {"sample-1": "sample-1"},
    }
    first = jobs.submit(job_record(
        owner=owner, job_id="sjob-c1", reserved_pass_id="pass-c1", request_key="cal-1",
    ))
    repeated = jobs.submit(job_record(
        owner=owner, job_id="sjob-c2", reserved_pass_id="pass-c2", request_key="cal-2",
    ))
    assert first["created"] is True and repeated["created"] is True
    assert first["job"]["run_id"] == "calibration:cjob-1"
    assert repeated["job"]["job_id"] == "sjob-c2"
    assert store.runs.get("calibration:cjob-1") is None


def test_claim_is_exclusive_and_cas_checked(store):
    make_run(store)
    jobs = jobs_for(store)
    jobs.submit(job_record())
    claimed = jobs.claim("sjob-1")
    assert claimed["status"] == "prepared"
    assert claimed["revision"] == 2
    assert jobs.claim("sjob-1") is None
    with pytest.raises(ScoringJobConflict):
        jobs.transition(
            "sjob-1", expected_revision=1, expected_status="queued",
            status="prepared", changes={},
        )


def test_begin_call_persists_prepared_and_dispatching_before_dispatch(store):
    make_run(store)
    jobs = jobs_for(store)
    jobs.submit(job_record())
    claimed = jobs.claim("sjob-1")
    current = jobs.begin_call(
        "sjob-1", expected_revision=claimed["revision"],
        invocation=invocation_record(),
        call={"call_id": "call-1", "case_id": "case-1", "mode": "single"},
    )
    assert current["status"] == "dispatching"
    stored = store.invocations.get("inv-call-1")
    assert stored["status"] == "dispatching"
    assert stored["purpose"] == "judge"
    assert stored["job_id"] == "sjob-1"
    assert stored["owner"]["kind"] == "subject"
    assert [item["job_id"] for item in store.invocations.list_for_job("sjob-1")] == [
        "sjob-1"
    ]
    call = current["calls"][0]
    assert call["status"] == "dispatching"
    assert call["invocation_revision"] == 2
    assert current["dispatch_started_at"]


def test_cancel_before_dispatch_is_zero_cost_and_idempotent(store):
    make_run(store)
    jobs = jobs_for(store)
    jobs.submit(job_record())
    first = jobs.cancel("sjob-1", actor="operator", reason="changed my mind")
    assert first["outcome"] == "cancelled"
    assert first["billed_calls"] == 0
    assert first["job"]["status"] == "cancelled"
    assert first["job"]["cancellation"]["phase"] == "queued"
    again = jobs.cancel("sjob-1", actor="operator", reason="changed my mind")
    assert again["outcome"] == "already_cancelled"
    assert jobs.claim("sjob-1") is None


def test_cancel_of_a_prepared_job_settles_undispatched_invocations(store):
    make_run(store)
    jobs = jobs_for(store)
    jobs.submit(job_record())
    claimed = jobs.claim("sjob-1")
    record = jobs.get("sjob-1")
    record["calls"] = [{
        "call_id": "call-1", "case_id": "case-1", "mode": "single",
        "invocation_id": "inv-prepared", "invocation_revision": 1,
        "status": "prepared",
    }]
    store.invocations.create(invocation_record("prepared"))
    with_prepared = jobs.transition(
        "sjob-1", expected_revision=claimed["revision"], expected_status="prepared",
        status="prepared", changes={"calls": record["calls"]}, allow_same_status=True,
    )
    assert with_prepared["calls"][0]["status"] == "prepared"
    cancelled = jobs.cancel("sjob-1", actor="operator", reason="stop")
    assert cancelled["job"]["status"] == "cancelled"
    settled = store.invocations.get("inv-prepared")
    assert settled["status"] == "settled"
    assert settled["outcome"] == "failed"
    assert settled["result_summary"]["reason"] == "cancelled_before_dispatch"


def test_cancel_during_dispatch_only_requests_interruption(store):
    make_run(store)
    jobs = jobs_for(store)
    jobs.submit(job_record())
    claimed = jobs.claim("sjob-1")
    jobs.begin_call(
        "sjob-1", expected_revision=claimed["revision"],
        invocation=invocation_record(),
        call={"call_id": "call-1", "case_id": "case-1", "mode": "single"},
    )
    outcome = jobs.cancel("sjob-1", actor="operator", reason="stop now")
    assert outcome["outcome"] == "cancel_requested"
    assert outcome["in_flight"] is True
    assert outcome["job"]["status"] == "dispatching"
    assert "never declared unbilled" in outcome["note"]
    repeated = jobs.cancel("sjob-1", actor="operator", reason="stop now")
    assert repeated["outcome"] == "cancel_requested"
    assert repeated["job"]["revision"] == outcome["job"]["revision"]


def test_recovery_requeues_prepared_and_isolates_dispatching(store):
    make_run(store)
    jobs = jobs_for(store)
    jobs.submit(job_record(request_key="a", job_id="sjob-a", reserved_pass_id="pass-a"))
    jobs.submit(job_record(request_key="b", job_id="sjob-b", reserved_pass_id="pass-b"))
    prepared = jobs.claim("sjob-a")
    dispatched = jobs.claim("sjob-b")
    jobs.begin_call(
        "sjob-b", expected_revision=dispatched["revision"],
        invocation={**invocation_record("b"), "id": "inv-b", "job_id": "sjob-b"},
        call={"call_id": "call-1", "case_id": "case-1", "mode": "single"},
    )
    touched = jobs.recover_interrupted()
    assert sorted(touched) == ["sjob-a", "sjob-b"]
    assert jobs.get("sjob-a")["status"] == "queued"
    assert jobs.get("sjob-a")["recovery"]["billed_calls"] == 0
    assert jobs.get("sjob-b")["status"] == "indeterminate"
    invocation = store.invocations.get("inv-b")
    assert invocation["status"] == "settled"
    assert invocation["outcome"] == "indeterminate"
    # 未知结果既不被重发，也不会被 claim 再次领取。
    assert jobs.claim("sjob-b") is None
    cancelled = jobs.cancel("sjob-b", actor="operator", reason="stop")
    assert cancelled["outcome"] == "indeterminate"
    assert prepared["status"] == "prepared"


def test_publish_commits_everything_in_one_transaction(store):
    run = make_run(store)
    jobs = jobs_for(store)
    jobs.submit(job_record())
    claimed = jobs.claim("sjob-1")
    jobs.begin_call(
        "sjob-1", expected_revision=claimed["revision"],
        invocation=invocation_record(),
        call={"call_id": "call-1", "case_id": "case-1", "mode": "single"},
    )
    dispatched = jobs.get("sjob-1")
    settled = jobs.settle_call(
        "sjob-1", expected_revision=dispatched["revision"], call_id="call-1",
        outcome="succeeded", result_summary={"usage": {"total_tokens": 5}},
        job_status="settled",
    )
    assert settled["status"] == "settled"
    assert store.scoring_passes.list_for_run("run-1") == []

    receipt = {"job_id": "sjob-1", "scoring_pass_id": "pass-1", "published_at": "now"}
    published = jobs.publish(
        "sjob-1", expected_revision=settled["revision"],
        pass_record=pass_record(), scores=[score_row()],
        run_advance={"updated_at": "now"},
        expected_run_revision=run["revision"], expected_run_status="completed",
        receipt=receipt,
        events=[{"run_id": "run-1", "type": "scoring_pass_created",
                 "scoring_pass_id": "pass-1"}],
    )
    assert published["published"] is True
    assert published["receipt"] == receipt
    assert store.scoring_passes.get("pass-1")["source"] == "judge"
    rows = store.score_sets.list_for_pass("pass-1")
    assert [row["metric_id"] for row in rows] == ["task_completion"]
    assert rows[0]["scoring_pass_id"] == "pass-1"
    updated_run = store.runs.get("run-1")
    assert updated_run["current_scoring_pass_id"] == "pass-1"
    assert updated_run["status"] == "completed"
    assert updated_run["revision"] == run["revision"] + 1
    assert any(
        event["type"] == "scoring_pass_created" for event in store.events.list_for_run("run-1")
    )

    # 通知或 HTTP 响应丢失后重放：只返回原 receipt，不追加 pass、不收费。
    replay = jobs.publish(
        "sjob-1", expected_revision=jobs.get("sjob-1")["revision"],
        pass_record=pass_record(), scores=[score_row()],
        run_advance={"updated_at": "later"},
        expected_run_revision=updated_run["revision"], expected_run_status="completed",
        receipt={"job_id": "sjob-1", "scoring_pass_id": "pass-1", "published_at": "later"},
    )
    assert replay["outcome"] == "already_completed"
    assert replay["receipt"] == receipt
    assert len(store.scoring_passes.list_for_run("run-1")) == 1


def test_interrupted_publish_transaction_leaves_no_partial_state(store):
    run = make_run(store)
    jobs = jobs_for(store)
    jobs.submit(job_record())
    claimed = jobs.claim("sjob-1")
    jobs.begin_call(
        "sjob-1", expected_revision=claimed["revision"],
        invocation=invocation_record(),
        call={"call_id": "call-1", "case_id": "case-1", "mode": "single"},
    )
    settled = jobs.settle_call(
        "sjob-1", expected_revision=jobs.get("sjob-1")["revision"], call_id="call-1",
        outcome="succeeded", result_summary={}, job_status="settled",
    )
    # 竞争事务先推进了 Run（例如另一次离线评分）：CAS 拒绝，全有或全无。
    store.runs.transition(
        "run-1", expected_revision=run["revision"], expected_status="completed",
        status="completed", changes={"updated_at": "raced"},
    )
    with pytest.raises(RunConflictError):
        jobs.publish(
            "sjob-1", expected_revision=settled["revision"],
            pass_record=pass_record(), scores=[score_row()],
            run_advance={"updated_at": "now"},
            expected_run_revision=run["revision"], expected_run_status="completed",
        )
    assert store.scoring_passes.list_for_run("run-1") == []
    assert store.score_sets.list_for_pass("pass-1") == []
    assert jobs.get("sjob-1")["status"] == "settled"
    assert jobs.get("sjob-1")["receipt"] is None


def test_calibration_publish_does_not_touch_any_run(store):
    jobs = jobs_for(store)
    owner = {
        "kind": "calibration", "calibration_job_id": "cjob-1",
        "sample_ids": {"sample-1": "s1"},
    }
    jobs.submit(job_record(
        owner=owner, job_id="sjob-c", reserved_pass_id="pass-c", request_key="cal-1",
    ))
    claimed = jobs.claim("sjob-c")
    jobs.begin_call(
        "sjob-c", expected_revision=claimed["revision"],
        invocation=invocation_record(
            "c", owner={"kind": "calibration", "calibration_job_id": "cjob-1",
                        "sample_id": "s1"},
        ),
        call={"call_id": "call-1", "case_id": "sample-1", "mode": "single"},
    )
    settled = jobs.settle_call(
        "sjob-c", expected_revision=jobs.get("sjob-c")["revision"], call_id="call-1",
        outcome="succeeded", result_summary={}, job_status="settled",
    )
    published = jobs.publish(
        "sjob-c", expected_revision=settled["revision"],
        pass_record=pass_record("pass-c", "calibration:cjob-1"),
        scores=[score_row("sample-1")],
    )
    assert published["published"] is True
    assert store.runs.get("calibration:cjob-1") is None
    invocation = store.invocations.get("inv-c")
    assert invocation["owner"]["kind"] == "calibration"
    assert invocation["run_id"] == "calibration:cjob-1"
    assert invocation["case_id"] == "s1"


def test_memory_and_sqlite_report_the_same_job_outcomes(tmp_path):
    outcomes: dict[str, list] = {}
    for label, store in (
        ("sqlite", SQLiteRunStore(tmp_path / "runs.db")),
        ("memory", InMemoryRunStore()),
    ):
        make_run(store)
        jobs = jobs_for(store)
        jobs.submit(job_record())
        claimed = jobs.claim("sjob-1")
        jobs.begin_call(
            "sjob-1", expected_revision=claimed["revision"],
            invocation=invocation_record(),
            call={"call_id": "call-1", "case_id": "case-1", "mode": "single"},
        )
        settled = jobs.settle_call(
            "sjob-1", expected_revision=jobs.get("sjob-1")["revision"], call_id="call-1",
            outcome="succeeded", result_summary={"cost_usd": 0.5}, job_status="settled",
        )
        published = jobs.publish(
            "sjob-1", expected_revision=settled["revision"],
            pass_record=pass_record(), scores=[score_row()],
            expected_run_revision=store.runs.get("run-1")["revision"],
            expected_run_status="completed",
        )
        outcomes[label] = [
            published["outcome"],
            jobs.get("sjob-1")["status"],
            store.runs.get("run-1")["current_scoring_pass_id"],
            len(store.score_sets.list_for_pass("pass-1")),
            store.invocations.get("inv-call-1")["status"],
        ]
    assert outcomes["sqlite"] == outcomes["memory"] == [
        "published", "completed", "pass-1", 1, "settled",
    ]


def test_job_view_reports_cost_and_terminal_state(store):
    jobs = jobs_for(store)
    jobs.submit(job_record())
    view = job_view(jobs.get("sjob-1"))
    assert view["terminal"] is False
    assert view["billed_calls"] == 0
    jobs.cancel("sjob-1", actor="operator", reason="nope")
    cancelled = job_view(jobs.get("sjob-1"))
    assert cancelled["terminal"] is True


def test_invocation_owner_union_is_validated_not_fabricated():
    from motte_storage.invocations import validate_invocation

    # subject owner 必须匹配真实 run/case（NOT NULL 约束保持不变）。
    subject = validate_invocation(invocation_record())
    assert subject["owner"]["kind"] == "subject"
    assert subject["run_id"] == "run-1" and subject["case_id"] == "case-1"

    # calibration owner 只能用显式命名空间，且必须是 judge 用途的 model 调用。
    calibration = validate_invocation(invocation_record(
        "cal", owner={"kind": "calibration", "calibration_job_id": "cjob-1",
                      "sample_id": "s1"},
    ))
    assert calibration["run_id"] == "calibration:cjob-1"
    assert calibration["case_id"] == "s1"

    with pytest.raises(ValueError, match="namespaced owner reference"):
        validate_invocation(invocation_record(
            "bad", owner={"kind": "calibration", "calibration_job_id": "cjob-1",
                          "sample_id": "s1"},
        ) | {"run_id": "run-1"})
    without_job = {
        key: value for key, value in invocation_record().items() if key != "job_id"
    }
    with pytest.raises(ValueError, match="scoring job id"):
        validate_invocation({**without_job, "purpose": "judge"})
    with pytest.raises(ValueError, match="only judge invocations"):
        validate_invocation(
            {**invocation_record(), "purpose": "subject", "job_id": "sjob-1"}
        )


def test_unknown_backend_is_rejected():
    class Weird:
        pass

    with pytest.raises(ScoringJobError):
        scoring_jobs_for(Weird())


def test_migration_0012_follows_0011_and_guards_downgrade(tmp_path):
    import importlib.util
    from pathlib import Path

    from sqlalchemy import create_engine, text

    from motte_storage.migrations import revision_ids

    ids = revision_ids()
    assert "0012_scoring_jobs" in ids
    assert ids.index("0011_workflow_resources") < ids.index("0012_scoring_jobs")

    path = Path(__file__).resolve().parents[2] / "migrations" / "versions" / (
        "0012_scoring_jobs.py"
    )
    module_spec = importlib.util.spec_from_file_location("migration_0012", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    assert module.revision == "0012_scoring_jobs"
    assert module.down_revision == "0011_workflow_resources"
    assert any("scoring_jobs" in statement for statement in module.DOWN_STATEMENTS)

    engine = create_engine(f"sqlite:///{tmp_path / 'guard.db'}")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE scoring_jobs (job_id TEXT PRIMARY KEY, payload TEXT)"
        ))
    assert module.downgrade_blockers(engine.connect()) == []
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO scoring_jobs(job_id, payload) VALUES ('j', '{}')")
        )
    blockers = module.downgrade_blockers(engine.connect())
    assert any("durable scoring job" in item for item in blockers)
    engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("MOTTE_PG_DSN"),
    reason="set MOTTE_PG_DSN to run the PostgreSQL scoring-job transaction test",
)
def test_postgres_scoring_job_transactions():
    from motte_storage.migrations import upgrade
    from motte_storage.postgres import create_postgres_run_store

    dsn = os.environ["MOTTE_PG_DSN"]
    upgrade(dsn)
    store = create_postgres_run_store(dsn)
    make_run(store, run_id=f"run-pg-{os.getpid()}")
    jobs = scoring_jobs_for(store)
    record = job_record(
        request_key=f"pg-{os.getpid()}", job_id=f"sjob-pg-{os.getpid()}",
        reserved_pass_id=f"pass-pg-{os.getpid()}",
        owner={"kind": "subject", "run_id": f"run-pg-{os.getpid()}"},
    )
    first = jobs.submit(record)
    assert first["created"] is True
    assert jobs.submit(record)["created"] is False
    claimed = jobs.claim(record["job_id"])
    jobs.begin_call(
        record["job_id"], expected_revision=claimed["revision"],
        invocation={**invocation_record("pg"), "id": "inv-pg",
                    "job_id": record["job_id"], "run_id": f"run-pg-{os.getpid()}"},
        call={"call_id": "call-1", "case_id": "case-1", "mode": "single"},
    )
    settled = jobs.settle_call(
        record["job_id"], expected_revision=jobs.get(record["job_id"])["revision"],
        call_id="call-1", outcome="succeeded", result_summary={}, job_status="settled",
    )
    published = jobs.publish(
        record["job_id"], expected_revision=settled["revision"],
        pass_record=pass_record(record["reserved_pass_id"], f"run-pg-{os.getpid()}"),
        scores=[score_row()],
        expected_run_revision=store.runs.get(f"run-pg-{os.getpid()}")["revision"],
        expected_run_status="completed",
    )
    assert published["outcome"] == "published"
    assert len(store.score_sets.list_for_pass(record["reserved_pass_id"])) == 1


def test_repeated_submit_of_a_settled_job_does_not_re_run(store):
    """同一幂等键在 job 完成后再提交：复用原 job，不产生第二次执行。"""
    make_run(store)
    jobs = jobs_for(store)
    first = jobs.submit(job_record())
    cancelled = jobs.cancel("sjob-1", actor="operator", reason="no")
    again = jobs.submit(job_record(job_id="sjob-2", reserved_pass_id="pass-2"))
    assert again["created"] is False
    assert again["job"]["status"] == "cancelled"
    assert again["job"]["job_id"] == first["job"]["job_id"]
    assert cancelled["outcome"] == "cancelled"


def test_deepcopy_isolation_between_reads(store):
    jobs = jobs_for(store)
    jobs.submit(job_record())
    snapshot = jobs.get("sjob-1")
    snapshot["status"] = "completed"
    assert jobs.get("sjob-1")["status"] == "queued"
    assert deepcopy(snapshot) is not jobs.get("sjob-1")
