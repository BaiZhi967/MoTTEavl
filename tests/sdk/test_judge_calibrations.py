"""Scripted software fixtures only: no real human/model qualification claims."""
from copy import deepcopy
import importlib
import importlib.util

import pytest
from pydantic import ValidationError

from motte_contracts.identity import canonical_sha256
from motte_eval.calibration import CalibrationSample, build_calibration_set, sample_content_sha256
from motte_eval.calibration_records import CalibrationImport, CalibrationPair, HumanReviewInput
from motte_eval.judge import JudgeBudgetError, build_judge_spec
from motte_sdk.scoring_jobs import ScoringJobRequest, ScoringJobService
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore
from tests.integration.test_judge_worker_flow import RecordingFactory, ScriptedProvider, seed_resources

TIME = "2026-09-30T01:00:00+00:00"
KINDS = ("clear_pass", "clear_fail", "borderline", "missing_evidence", "injection")


def lifecycle_module():
    assert importlib.util.find_spec("motte_sdk.judge_calibrations") is not None, (
        "server-owned calibration compiler/lifecycle is missing"
    )
    return importlib.import_module("motte_sdk.judge_calibrations")


def run_request(**changes):
    module = lifecycle_module()
    raw = dict(
        request_key="software-calibration-request",
        spec_request=dict(
            judge_profile_id="software-fixture", model="judge-model",
            rubric_id="answer-quality", rubric_version="1",
            parameters={"max_output_tokens": 100}, calibration_version="reviewed",
            budget={"max_calls": 120, "max_prompt_tokens": 2_000_000,
                    "max_completion_tokens": 12_000},
        ),
        authorisation={"authorised": True, "actor": "software-test-only", "max_calls": 120,
                       "max_total_tokens": 2_012_000, "hard_cost_cap_usd": 10.0},
        price_table_version="price-1",
    )
    raw.update(changes)
    return module.CalibrationRunRequest(**raw)


def import_data(request, *, count=30, mode="pairwise", source="human", price_known=True):
    spec_request = deepcopy(request.spec_request)
    spec_request["model"] = "scripted-model"
    spec_request["budget"].update(price_known=price_known,
                                  price_table_version="price-1" if price_known else None)
    spec = build_judge_spec(**spec_request, mode=mode)
    samples, pairs, reviews = [], {}, []
    for index in reversed(range(count)):
        sample_id = f"sample-{index:02d}"
        kind = KINDS[index % len(KINDS)]
        payload = dict(
            sample_id=sample_id, kind=kind, source=source,
            candidate_output="Software-only fixture, not actual human review",
            rubric_id=spec.rubric_id, rubric_version=spec.rubric_version,
            model=spec.model, judge_spec_sha256=spec.spec_sha256,
        )
        draft = CalibrationSample.model_construct(**payload, content_sha256="sha256:" + "0" * 64)
        samples.append(CalibrationSample.model_validate({
            **draft.model_dump(), "content_sha256": sample_content_sha256(draft),
        }))
        if mode == "pairwise":
            pairs[sample_id] = CalibrationPair(candidates=[
                {"candidate_id": "z-right", "content": f"Reference fixture {sample_id}",
                 "evidence_allowlist": ["event:20"]},
                {"candidate_id": "a-left", "content": f"Challenger fixture {sample_id}",
                 "evidence_allowlist": ["event:10"]},
            ])
        reviews.append(HumanReviewInput(
            sample_id=sample_id, annotator="software-only-annotator",
            reviewer="software-only-reviewer", reviewed_at=TIME,
            reason="Synthetic protocol test, no actual human acceptance",
            expected_outcome={"kind": "scored"},
            expected_criteria=({key: True for key in spec.criteria} if mode == "single" else {}),
            pairwise_gold=({key: {"kind": "candidate", "candidate_id": "a-left"}
                            for key in spec.criteria} if mode == "pairwise" else {}),
        ))
    calibration = build_calibration_set(
        "software-calibration", "imported", rubric_id=spec.rubric_id,
        rubric_version=spec.rubric_version, model=spec.model, samples=samples,
        judge_spec_sha256=spec.spec_sha256, config={"judge_spec": spec.model_dump(mode="json")},
    )
    return CalibrationImport(calibration=calibration, pairs=pairs), reviews


def calibration_environment(tmp_path, *, count=30, mode="pairwise", request=None, store=None,
                            price_known=True):
    module = lifecycle_module()
    store = store or SQLiteRunStore(tmp_path / "calibrations.db")
    resources = InMemoryResourceStore()
    seed_resources(resources, wire_model="scripted-model")
    if not price_known:
        resources.price_tables = InMemoryResourceStore().price_tables
    provider = ScriptedProvider()
    factory = RecordingFactory(provider)
    scoring = ScoringJobService(store, provider_factory=factory, clock=lambda: TIME)
    service = module.JudgeCalibrationService(store, resources, scoring_jobs=scoring, clock=lambda: TIME)
    request = request or run_request()
    imported, reviews = import_data(request, count=count, mode=mode, price_known=price_known)
    parent = service.import_version(imported)
    version = service.review(parent.reference, new_version="reviewed", reviews=reviews)
    return service, scoring, resources, provider, factory, version, request


def test_preflight_is_zero_write_zero_provider(tmp_path):
    service, scoring, resources, provider, factory, version, request = calibration_environment(tmp_path)
    before = list(service.store.calibrations.iter_records())
    result = service.preflight(version.reference, request)
    assert result["executed"] is False
    assert result["max_calls"] == 120
    assert result["sample_count"] == 30
    assert result["child_call_counts"] == [32, 32, 32, 24]
    assert result["preflight_sha256"].startswith("sha256:")
    assert scoring.jobs.list_by_status() == []
    assert list(service.store.calibrations.iter_records()) == before
    assert provider.calls == factory.calls == []


@pytest.mark.parametrize("memory", [False, True])
def test_120_calls_split_under_32_and_one_aggregate_cap(tmp_path, memory):
    env = calibration_environment(tmp_path, store=InMemoryRunStore() if memory else None)
    service, scoring, resources, provider, factory, version, request = env
    preflight = service.preflight(version.reference, request)
    request = request.model_copy(update={"expected_preflight_sha256": preflight["preflight_sha256"]})
    execution = service.submit(version.reference, request)
    children = [scoring.jobs.get(key) for key in execution.child_job_ids]
    plans = [plan for child in children for plan in child["plans"]]
    assert [len(child["plans"]) for child in children] == [32, 32, 32, 24]
    assert len({plan["call_id"] for plan in plans}) == 120
    assert execution.plan == [{**plan, "child_job_id": child["job_id"]}
                              for child in children for plan in child["plans"]]
    assert execution.plan_sha256 == canonical_sha256(execution.plan)
    assert execution.allowance.model_dump() == preflight["allowance"]
    assert execution.allowance.max_calls == 120
    assert execution.allowance.max_completion_tokens == 12_000
    assert execution.allowance.max_prompt_tokens == sum(
        plan["reservation"]["prompt_tokens"] for plan in plans)
    assert execution.allowance.max_cost_usd == round(sum(
        plan["reservation"]["cost_usd"] for plan in plans), 8)
    for child in children:
        assert child["judge_spec"] == version.spec.model_dump(mode="json")
        assert child["owner"]["calibration_job_id"] == execution.execution_id
        assert child["job_id"] != execution.execution_id
        assert child["run_id"] == f"calibration:{execution.execution_id}"
        assert child["owner"]["sample_ids"] == {plan["sample_id"]: plan["sample_id"]
                                                for plan in child["plans"]}
        assert child["request_key"].startswith(execution.execution_id + "/")
        assert child["allowance"]["max_calls"] <= 32
    for offset in range(0, 120, 4):
        group = plans[offset:offset + 4]
        assert [item["call_kind"] for item in group] == [
            "single", "repeat", "order_forward", "order_reverse"]
        assert [item["repeat_index"] for item in group] == [0, 1, 0, 0]
        assert [item["sample_id"] for item in group] == [f"sample-{offset // 4:02d}"] * 4
        assert [item["presentation_order"] for item in group] == [
            ["a-left", "z-right"], ["a-left", "z-right"],
            ["a-left", "z-right"], ["z-right", "a-left"]]
        assert len({item["pair_id"] for item in group}) == 1
    assert service.store.runs.list() == []
    assert provider.calls == factory.calls == []
    assert scoring.policy.max_calls_per_job == 32
    assert scoring.policy.automatic_model_retries is False


@pytest.mark.parametrize("limit", ["calls", "tokens", "cost"])
def test_insufficient_aggregate_authorisation_creates_zero_children(tmp_path, limit):
    service, scoring, _, _, _, version, request = calibration_environment(tmp_path)
    preflight = service.preflight(version.reference, request)
    limits = {"calls": {"max_calls": 119},
              "tokens": {"max_total_tokens": preflight["allowance"]["max_prompt_tokens"] + 11_999},
              "cost": {"hard_cost_cap_usd": preflight["allowance"]["max_cost_usd"] * .9}}
    request = request.model_copy(update={"authorisation": request.authorisation.model_copy(update=limits[limit])})
    with pytest.raises(JudgeBudgetError, match="aggregate"):
        service.submit(version.reference, request)
    assert scoring.jobs.list_by_status() == []
    assert service.store.calibrations.list_executions("software-calibration") == []


def test_aggregate_uses_actual_child_output_ceilings_without_rebudgeting(tmp_path):
    request = run_request()
    raw = request.model_dump(mode="json")
    raw["spec_request"]["parameters"] = {"max_output_tokens": 1024}
    raw["spec_request"]["budget"]["max_completion_tokens"] = 12_000
    request = lifecycle_module().CalibrationRunRequest(**raw)
    service, scoring, _, _, _, version, request = calibration_environment(tmp_path, request=request)
    # Child ceilings are 375/375/375/500. They reserve 48,000, not a fictional
    # aggregate-first ceiling of 100. Refuse; do not mutate the frozen JudgeSpec.
    with pytest.raises(JudgeBudgetError, match="aggregate.*completion"):
        service.submit(version.reference, request)
    assert scoring.jobs.list_by_status() == []


def test_request_replay_after_resource_drift(tmp_path, monkeypatch):
    service, scoring, resources, provider, factory, version, request = calibration_environment(tmp_path)
    first = service.submit(version.reference, request)
    resources.providers.put({**resources.providers.get("judge-conn"),
                             "base_url": "http://drifted-software.local/v1", "generation": 2})
    import motte_sdk.judge_calibrations as compiler
    def unavailable(**kwargs):
        raise AssertionError("replay must not resolve mutable resources")
    monkeypatch.setattr(compiler, "freeze_judge_provider_snapshot", unavailable)
    assert service.submit(version.reference, request) == first
    assert len(scoring.jobs.list_by_status()) == 4
    assert provider.calls == factory.calls == []
    with pytest.raises(ValueError, match="conflict"):
        service.submit(version.reference, request.model_copy(update={"price_table_version": "changed"}))
    with pytest.raises(AssertionError, match="must not resolve"):
        service.submit(version.reference, request.model_copy(update={"request_key": "new-request"}))


def test_changed_expected_preflight_rejected_without_writes(tmp_path):
    service, scoring, _, _, _, version, request = calibration_environment(tmp_path)
    request = request.model_copy(update={"expected_preflight_sha256": "sha256:" + "b" * 64})
    with pytest.raises(ValueError, match="preflight"):
        service.submit(version.reference, request)
    assert scoring.jobs.list_by_status() == []


@pytest.mark.parametrize("field", ["observations", "owner", "calibration_plan", "call_count",
                                   "policy", "report", "qualification", "sample_ids"])
def test_client_plan_rejected(field):
    module = lifecycle_module()
    with pytest.raises(ValidationError):
        module.CalibrationRunRequest(**{**run_request().model_dump(), field: {}})


def test_cross_owner_or_client_plan_rejected(tmp_path):
    module = lifecycle_module()
    from motte_sdk.scoring_jobs import CalibrationPlanEntry
    request = run_request()
    draft, _ = import_data(request, count=1)
    from tests.integration.test_judge_worker_flow import observation
    spec = build_judge_spec(judge_profile_id="fixture", model="scripted-model",
                            rubric_id="answer-quality", rubric_version="1", budget={"max_calls": 4})
    with pytest.raises(ValidationError, match="calibration"):
        ScoringJobRequest(request_key="subject", judge_spec=spec, run_id="run-1",
                          observations={"sample-1": observation(case_id="sample-1")},
                          calibration_plan=[CalibrationPlanEntry(sample_id="sample-1", call_kind="single")])
    for location in ("root", "budget", "selector"):
        raw = request.model_dump()
        target = raw["spec_request"]
        if location == "budget":
            target = target["budget"]
        if location == "selector":
            target["input_selector"] = {}
            target = target["input_selector"]
        target["owner"] = "forged"
        with pytest.raises(ValidationError):
            module.CalibrationRunRequest(**raw)


def test_single_mode_is_two_distinct_diagnostic_calls_per_sample(tmp_path):
    service, scoring, _, _, _, version, request = calibration_environment(tmp_path, count=3, mode="single")
    execution = service.submit(version.reference, request)
    assert len(execution.plan) == 6
    assert [item["call_kind"] for item in execution.plan] == ["single", "repeat"] * 3
    assert all(item["mode"] == "single" for item in execution.plan)
    assert execution.policy.require_position_swap_consistency is True
    assert execution.allowance.max_calls == 6
    assert not service.store.runs.list()


def test_import_and_reads_are_immutable_and_synthetic_review_stays_forbidden(tmp_path):
    from motte_eval.calibration import HumanReviewRequired
    service, scoring, _, provider, factory, version, request = calibration_environment(tmp_path, count=1)
    read = service.get_version(version.reference)
    read.pairs["sample-00"].candidates[0].evidence_allowlist.append("event:tampered")
    assert service.get_version(version.reference) == version
    assert len(service.list_versions("software-calibration")) == 2
    draft, reviews = import_data(request, count=1, source="synthetic_candidate")
    raw = draft.model_dump()
    raw["calibration"]["calibration_id"] = "synthetic-only"
    raw["calibration"].pop("content_sha256")
    raw["calibration"].pop("schema_version")
    raw["calibration"]["samples"] = draft.calibration.samples
    draft = CalibrationImport(calibration=build_calibration_set(**raw["calibration"]), pairs=draft.pairs)
    imported = service.import_version(draft)
    with pytest.raises(HumanReviewRequired):
        service.review(imported.reference, new_version="reviewed", reviews=reviews)
    assert scoring.jobs.list_by_status() == []
    assert provider.calls == factory.calls == []


def test_group_creation_retry_cannot_multiply_aggregate_spend(tmp_path, monkeypatch):
    service, scoring, _, provider, factory, version, request = calibration_environment(tmp_path)
    repo = service.store.calibrations
    real_insert = repo._insert_job
    attempted = []
    def crash_second(tx, row):
        attempted.append(row["job_id"])
        if len(attempted) == 2:
            raise RuntimeError("scripted child insertion crash")
        return real_insert(tx, row)
    monkeypatch.setattr(repo, "_insert_job", crash_second)
    with pytest.raises(RuntimeError, match="insertion crash"):
        service.submit(version.reference, request)
    assert scoring.jobs.list_by_status() == []
    assert repo.list_executions("software-calibration") == []
    monkeypatch.setattr(repo, "_insert_job", real_insert)
    execution = service.submit(version.reference, request)
    assert execution.child_job_ids[:2] == attempted
    assert service.submit(version.reference, request) == execution
    assert len(scoring.jobs.list_by_status()) == 4
    assert execution.allowance.max_calls == 120
    assert provider.calls == factory.calls == []


def test_compiler_rejects_cross_owner_pair(tmp_path):
    from motte_eval.judge import JudgeCandidateInput, JudgeCandidateRef, JudgeInputError, build_pairwise_input
    from motte_sdk.scoring_jobs import CalibrationPlanEntry
    service, scoring, _, _, _, version, request = calibration_environment(tmp_path, count=1)
    pair = build_pairwise_input(
        task_ref="sample-00",
        candidate_a=JudgeCandidateInput(candidate=JudgeCandidateRef(
            candidate_id="left", owner_kind="calibration", calibration_job_id="wrong",
            sample_id="sample-00"), content="software fixture"),
        candidate_b=JudgeCandidateInput(candidate=JudgeCandidateRef(
            candidate_id="right", owner_kind="calibration", calibration_job_id="right",
            sample_id="sample-00"), content="software fixture"),
    )
    internal = ScoringJobRequest(
        request_key="internal", judge_spec=version.spec, mode="pairwise",
        calibration_job_id="right", sample_ids={"sample-00": "sample-00"},
        pairwise_pairs=[pair], authorisation=request.authorisation,
        calibration_plan=[CalibrationPlanEntry(sample_id="sample-00", call_kind="single",
                                               candidate_order=["left", "right"])],
    )
    with pytest.raises(JudgeInputError, match="another owner"):
        scoring.compile_record(internal, job_id="child", reserved_pass_id="pass")
    assert scoring.jobs.list_by_status() == []


def test_compile_record_and_preflight_need_no_provider_factory(tmp_path):
    from motte_eval.judge import JudgeError
    service, scoring, _, _, _, version, request = calibration_environment(tmp_path, count=1)
    scoring.provider_factory = None
    assert service.preflight(version.reference, request)["max_calls"] == 4
    with pytest.raises(JudgeError, match="no judge provider"):
        service.submit(version.reference, request)
    assert scoring.jobs.list_by_status() == []


def test_live_provider_drift_changes_preflight_and_requires_matching_hash(tmp_path):
    service, scoring, resources, _, _, version, request = calibration_environment(tmp_path, count=1)
    first = service.preflight(version.reference, request)
    resources.providers.put({**resources.providers.get("judge-conn"),
                             "base_url": "http://software-changed.local/v1", "generation": 2})
    changed = service.preflight(version.reference, request)
    assert changed["provider_snapshot_sha256"] != first["provider_snapshot_sha256"]
    assert changed["preflight_sha256"] != first["preflight_sha256"]
    with pytest.raises(ValueError, match="preflight"):
        service.submit(version.reference, request.model_copy(update={
            "expected_preflight_sha256": first["preflight_sha256"],
        }))
    assert scoring.jobs.list_by_status() == []


def test_legacy_execution_has_missing_not_zero_allowance_without_rehash():
    from motte_eval.calibration_records import CalibrationExecution
    from tests.evaluators.test_m8_calibration_records import execution_fixture
    legacy = execution_fixture().model_dump(mode="json")
    legacy.pop("allowance", None)
    legacy["content_sha256"] = canonical_sha256({key: value for key, value in legacy.items()
                                                if key not in {"content_sha256", "recorded_at"}})
    restored = CalibrationExecution.model_validate(legacy)
    assert restored.allowance is None
    assert restored.model_dump(mode="json") == legacy


def test_invalid_frozen_evidence_rejected_before_any_child_or_provider(tmp_path):
    from motte_eval.calibration_records import CalibrationVersion
    from motte_eval.judge import JudgeInputError
    service, scoring, resources, provider, factory, version, request = calibration_environment(tmp_path, count=1)
    raw = version.model_dump(mode="json")
    raw["pairs"]["sample-00"]["candidates"][0]["evidence_allowlist"] = ["event:not-a-sequence"]
    invalid = CalibrationVersion.seal(raw)
    with pytest.raises(JudgeInputError, match="evidence"):
        lifecycle_module().compile_calibration_execution(invalid, request, resources, scoring)
    assert scoring.jobs.list_by_status() == []
    assert provider.calls == factory.calls == []


def test_unknown_pricing_stays_unknown_and_cannot_claim_a_money_cap(tmp_path):
    request = run_request(price_table_version=None)
    request = request.model_copy(update={"authorisation": request.authorisation.model_copy(
        update={"hard_cost_cap_usd": None})})
    service, scoring, _, _, _, version, request = calibration_environment(
        tmp_path, count=1, request=request, price_known=False,
    )
    preview = service.preflight(version.reference, request)
    assert preview["allowance"]["max_cost_usd"] is None
    execution = service.submit(version.reference, request)
    assert execution.allowance.max_cost_usd is None
    child = scoring.jobs.get(execution.child_job_ids[0])
    assert child["cost_total_usd"] is None
    assert child["price_coverage"]["known"] is False
    assert all(plan["reservation"]["cost_usd"] is None for plan in child["plans"])
    capped = request.model_copy(update={
        "request_key": "unknown-capped", "authorisation": request.authorisation.model_copy(
            update={"hard_cost_cap_usd": 10.0}),
    })
    with pytest.raises(JudgeBudgetError, match="unknown"):
        service.submit(version.reference, capped)
    assert len(scoring.jobs.list_by_status()) == 1


def test_resolves_published_provider_once_and_plan_is_deterministic(tmp_path, monkeypatch):
    service, scoring, resources, _, _, version, request = calibration_environment(tmp_path)
    module = lifecycle_module()
    original = module.freeze_judge_provider_snapshot
    snapshots = []
    def record_resolution(**kwargs):
        result = original(**kwargs)
        snapshots.append(result)
        return result
    monkeypatch.setattr(module, "freeze_judge_provider_snapshot", record_resolution)
    first, children = module.compile_calibration_execution(version, request, resources, scoring)
    assert len(snapshots) == 1
    second, _ = module.compile_calibration_execution(version, request, resources, scoring)
    assert len(snapshots) == 2
    assert first.plan == second.plan
    assert first.child_job_ids == second.child_job_ids
    assert all(row["provider_snapshot"]["snapshot_sha256"] == snapshots[0].snapshot_sha256
               for row in children)
    assert scoring.jobs.list_by_status() == []


def test_concurrent_submits_publish_only_one_group(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    service, scoring, _, provider, _, version, request = calibration_environment(tmp_path, count=9)
    barrier = Barrier(3)
    def submit(_):
        barrier.wait(timeout=10)
        return service.submit(version.reference, request)
    with ThreadPoolExecutor(max_workers=3) as pool:
        executions = list(pool.map(submit, range(3)))
    assert all(item == executions[0] for item in executions)
    assert len(scoring.jobs.list_by_status()) == 2
    assert sum(row["allowance"]["max_calls"] for row in scoring.jobs.list_by_status()) == 36
    assert provider.calls == []
