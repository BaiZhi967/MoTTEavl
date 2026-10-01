"""Real loopback Judge sends obey the no-rebill contract, including old queues."""
from copy import deepcopy
import json

import pytest

from apps.worker.motte_worker.reporting import WorkerReporter
from apps.worker.motte_worker.runtime import WorkerLoop
from motte_sdk.scoring_jobs import FrozenProviderFactory, JudgeProviderSnapshot
from motte_sdk.service import RunService
from tests.integration.test_final_review_regressions import endpoint  # noqa: F401
from tests.sdk.test_judge_calibrations import calibration_environment


def legacy_freezer(monkeypatch, module, changes):
    """Generate a validly sealed old snapshot before Job compilation, not tamper a ledger."""
    original = module.freeze_judge_provider_snapshot

    def freeze(**kwargs):
        payload = original(**kwargs).model_dump(mode="json")
        for key, value in changes.items():
            if value == "absent":
                payload["transport"].pop(key, None)
            else:
                payload["transport"][key] = value
        return JudgeProviderSnapshot.seal(payload)

    monkeypatch.setattr(module, "freeze_judge_provider_snapshot", freeze)


@pytest.mark.parametrize("response_status", [307, 308, 429])
@pytest.mark.parametrize("legacy", [False, True])
def test_judge_worker_no_hidden_redirect_or_idempotent_retry(endpoint, tmp_path, monkeypatch, response_status, legacy):  # noqa: F811
    import motte_sdk.judge_calibrations as compiler
    from motte_provider.registry import adapter_for

    url, posts, responses = endpoint
    monkeypatch.setenv("R8_JUDGE_KEY", "synthetic-loopback-only")
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    # The real transport is explicitly idempotency-capable; zero retry must be
    # enforced rather than accidentally relying on absent headers.
    monkeypatch.setitem(adapter_for("openai_compatible").transport_kwargs,
                        "default_headers", {"Idempotency-Key": "synthetic-only"})
    service, scoring, resources, _, _, version, request = calibration_environment(tmp_path, count=1)
    connection = resources.providers.get("judge-conn")
    resources.providers.put({**connection, "base_url": url + "/v1", "max_retries": 1})
    if legacy:
        legacy_freezer(monkeypatch, compiler, {"follow_redirects": "absent", "max_retries": 1})
    execution = service.submit(version.reference, request)
    scoring.provider_factory = FrozenProviderFactory()
    frozen = deepcopy(scoring.jobs.get(execution.child_job_ids[0])["provider_snapshot"])
    answer = {"id": "synthetic-response", "model": "scripted-model", "choices": [{"message": {
        "content": json.dumps({"winner": "A", "criteria": [
            {"criterion_id": key, "preference": "A", "reason": "software-only", "evidence": ["event:10", "event:20"]}
            for key in execution.spec.criteria
        ]})}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    count = len(execution.plan)
    assert count == 4 and execution.allowance.max_calls == count
    if response_status == 429:
        responses.extend([(429, {"Retry-After": "0"}, {}), *[(200, {}, answer)] * count])
    else:
        for _ in range(count):
            responses.extend([(response_status, {"Location": "/resolved"}, {}), (200, {}, answer)])
    worker = WorkerLoop(RunService(service.store), scoring_jobs=scoring, reporter=WorkerReporter(enabled=False))
    result = worker.claim_and_execute()
    assert posts == ["/v1/chat/completions"], posts
    assert result["status"] in {"failed", "indeterminate"}
    stored = scoring.jobs.get(execution.child_job_ids[0])
    assert stored["provider_snapshot"] == frozen  # No old snapshot rewrite/upgrade.
    assert len(stored["calls"]) == 1
    assert worker.claim_and_execute() is None  # No automatic rebilling/re-enqueue.
    if legacy:
        assert "follow_redirects" not in frozen["transport"] and frozen["transport"]["max_retries"] == 1
    else:
        assert frozen["transport"]["follow_redirects"] is False
        assert type(frozen["transport"]["max_retries"]) is int and frozen["transport"]["max_retries"] == 0


@pytest.mark.parametrize("changes", [
    {"follow_redirects": "absent"}, {"follow_redirects": True},
    {"follow_redirects": 0}, {"follow_redirects": "false"},
    {"max_retries": "absent"}, {"max_retries": False}, {"max_retries": True},
    {"max_retries": "0"}, {"max_retries": 1},
])
def test_unproved_historical_transport_cannot_supply_subject_quality(monkeypatch, changes):
    import motte_sdk.scoring_jobs as jobs
    from motte_sdk.pairwise_quality import reconstruct_pairwise_quality
    from motte_storage.run_store import InMemoryRunStore
    from tests.sdk.test_pairwise_quality import completed_quality

    legacy_freezer(monkeypatch, jobs, changes)
    store = InMemoryRunStore()
    _, job, _ = completed_quality(store)
    before = deepcopy(store.scoring_jobs.get(job["job_id"]))
    result = reconstruct_pairwise_quality(store, job["reserved_pass_id"])
    assert result.value is None
    assert result.reasons and "transport" in str(result.reasons)
    assert store.scoring_jobs.get(job["job_id"]) == before
