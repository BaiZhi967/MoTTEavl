"""验收 F-06：Judge 证据解析必须同时认 frozen_observation 与 observation。

真实事故：Scenario Run 把评分用的 FrozenObservation 放在 result.frozen_observation，
人读的 result.observation 是 workflow-observation@1（见 docs/operations/scenarios.md
4.4）；agent / CLI / Pi / Inspect 路径直接写 result.observation；基准路径两者都不写。
而 resolve_saved_observations 只读 observation，于是公共 Judge 入口：
* Scenario Run -> JUDGE_EVIDENCE_INVALID（17 条 FrozenObservation 校验错）；
* direct-llm / GSM8K 基准 Run -> JUDGE_EVIDENCE_MISSING。
两条路都不通，R8 声称"已接公共入口"在真实数据上不成立。

这里钉住解析顺序与仍然成立的拒绝语义（零调用、零发布由调用方保证）。
"""
from __future__ import annotations

import pytest

from motte_contracts.evaluation import observation_evidence_hash
from motte_sdk.scoring_jobs import JudgeEvidenceError, resolve_saved_observations
from motte_storage.run_store import SQLiteRunStore

RUN_ID = "run-1"
CASE_ID = "case-1"


def frozen_payload(**overrides):
    payload = {
        "observation_id": "obs-1",
        "run_id": RUN_ID,
        "case_id": CASE_ID,
        "attempt_id": "attempt-1",
        "final_output": "done",
        "termination": {"reason": "final_answer"},
        "event_refs": [{"kind": "event", "run_id": RUN_ID, "locator": "3"}],
        "artifact_refs": [],
        "coverage": {
            "complete": True, "events_captured": 1, "artifacts_captured": 0,
            "artifacts_expected": 0, "missing": [],
        },
        "usage": None,
    }
    payload.update(overrides)
    payload["evidence_hash"] = observation_evidence_hash(payload)
    return payload


def workflow_observation():
    """Scenario Run 的人读投影：不是 FrozenObservation，不能当评分证据用。"""
    return {"workflow": "acc-clarify-flow@1", "checkpoints": {"final-check": {}}}


def store_with(result, tmp_path):
    store = SQLiteRunStore(tmp_path / "runs.db")
    store.runs.save({"id": RUN_ID, "status": "completed", "case_ids": [CASE_ID]})
    store.case_runs.upsert({"run_id": RUN_ID, "case_id": CASE_ID, "result": result,
                            "outcome": "responded"})
    return store


def test_scenario_run_resolves_its_frozen_observation(tmp_path):
    """result 里两个键都在时，取契约证据而不是人读投影。"""
    frozen = frozen_payload()
    store = store_with({"frozen_observation": frozen,
                        "observation": workflow_observation()},
                       tmp_path)

    resolved = resolve_saved_observations(store, RUN_ID, [CASE_ID])

    assert resolved == {CASE_ID: frozen}


def test_agent_run_resolves_its_saved_observation(tmp_path):
    """agent / CLI / Pi / Inspect 路径只写 observation，必须继续可解析。"""
    saved = frozen_payload()
    store = store_with({"observation": saved}, tmp_path)

    assert resolve_saved_observations(store, RUN_ID, [CASE_ID]) == {CASE_ID: saved}


def test_benchmark_run_without_evidence_is_still_refused(tmp_path):
    """基准路径两个键都不写：仍然是具名 MISSING，不猜测、不放行。"""
    store = store_with({"content": "42", "usage": {"total_tokens": 3}}, tmp_path)

    with pytest.raises(JudgeEvidenceError) as error:
        resolve_saved_observations(store, RUN_ID, [CASE_ID])
    assert error.value.code == "JUDGE_EVIDENCE_MISSING"


def test_tampered_frozen_falls_back_to_verified_projection_only(tmp_path):
    """frozen 被篡改时回退到另一个候选，但校验照样重算 hash 与归属。"""
    tampered = frozen_payload()
    tampered["final_output"] = "forged"
    saved = frozen_payload()
    store = store_with({"frozen_observation": tampered, "observation": saved}, tmp_path)

    resolved = resolve_saved_observations(store, RUN_ID, [CASE_ID])

    assert resolved[CASE_ID]["final_output"] == "done"


def test_evidence_from_another_run_is_refused(tmp_path):
    """归属不符仍然是具名 INVALID，不因为有两个候选就放松。"""
    store = store_with({"frozen_observation": frozen_payload(run_id="run-other"),
                        "observation": workflow_observation()},
                       tmp_path)

    with pytest.raises(JudgeEvidenceError) as error:
        resolve_saved_observations(store, RUN_ID, [CASE_ID])
    assert error.value.code == "JUDGE_EVIDENCE_INVALID"
    assert "another run" in str(error.value)
