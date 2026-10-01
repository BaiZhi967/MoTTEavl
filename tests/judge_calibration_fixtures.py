"""Software-only declared human-input protocol fixtures; no real human/model acceptance."""
from copy import deepcopy
from functools import partial
import json

import pytest

from apps.api.app.main import create_app
from fastapi.testclient import TestClient
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore
from tests.integration.test_judge_worker_flow import RecordingFactory, ScriptedProvider, seed_resources
from tests.sdk.test_judge_calibrations import import_data, run_request

ROOT = "/api/v1/judge-calibrations"

# Public path selectors must survive URI parsing unchanged; other IDs are opaque.
UNADDRESSABLE_SELECTORS = ("team/calibration", ".", "..", "./name", "name/..",
                          "name\\part", "%2f", "%2E%2e", "%252F", "%FF", "x\n", "x\x00", "x\x7f")
ADDRESSABLE_SELECTORS = ("naïve 中文 🐢", " spaced name ", "a?b#c", "a;b:@&=+$,",
                        "100%", "%zz", "...", "．．", "a.b-_~")


def selector_import(env, **selectors):
    from motte_eval.calibration import calibration_content_sha256
    body = deepcopy(env["import"])
    body["calibration"].update(selectors)
    body["calibration"]["content_sha256"] = calibration_content_sha256(body["calibration"])
    return body


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def lifecycle_env(request, tmp_path):
    if request.param == "postgres":
        from motte_storage.factory import create_run_store
        from motte_storage.migrations import upgrade
        dsn = request.getfixturevalue("isolated_pg_database")
        upgrade(dsn)
        reopen = partial(create_run_store, storage="postgres", dsn=dsn)
        store = reopen()
    elif request.param == "sqlite":
        reopen = partial(SQLiteRunStore, tmp_path / "lifecycle.db")
        store = reopen()
    else:
        store = InMemoryRunStore()
        def reopen():
            return store
    return environment(store, reopen)


def environment(store=None, reopen=None):
    store = store or InMemoryRunStore()
    resources = InMemoryResourceStore()
    seed_resources(resources, wire_model="scripted-model")
    provider = ScriptedProvider()
    factory = RecordingFactory(provider)
    request = run_request()
    imported, reviews = import_data(request)
    raw = imported.model_dump(mode="json")
    fixed = []
    for sample, review in zip(imported.calibration.samples, reviews, strict=True):
        review = review.model_dump(mode="json")
        if sample.kind == "missing_evidence":
            for candidate in raw["pairs"][sample.sample_id]["candidates"]:
                candidate["evidence_allowlist"] = []
            review.update(expected_outcome={"kind": "non_scored", "status": "missing_evidence"},
                          pairwise_gold={})
        elif sample.kind == "borderline":
            review["pairwise_gold"] = {key: {"kind": "tie"} for key in review["pairwise_gold"]}
        fixed.append(review)
    app = create_app(store, resource_store=resources, judge_provider_factory=factory)
    return {"store": store, "reopen": reopen or (lambda: store), "resources": resources,
            "provider": provider, "factory": factory, "app": app, "client": TestClient(app),
            "import": raw, "reviews": fixed, "request": request.model_dump(mode="json"),
            "id": imported.calibration.calibration_id}


def import_review(env):
    client = env["client"]
    response = client.post(ROOT, json=env["import"])
    assert response.status_code == 201, response.text
    parent = response.json()
    body = {"expected_parent_sha256": parent["content_sha256"], "new_version": "reviewed",
            "reviews": env["reviews"]}
    response = client.post(f"{ROOT}/{env['id']}/versions/imported/reviews", json=body)
    assert response.status_code == 201, response.text
    return parent, response.json(), body


def execute_body(env, version):
    return {"content_sha256": version["content_sha256"], "request": deepcopy(env["request"])}


def attach_scripted_adapter(env, execution):
    """Use the frozen real provider kind, scripting only its wire boundary."""
    from motte_provider.openai_compatible import OpenAICompatibleProvider
    from motte_provider.pricing import parse_price_table
    from motte_sdk.scoring_jobs import JudgeProviderSnapshot
    from tests.sdk.test_calibration_ledger import ScriptedCalibrationTransport
    samples = {sample.sample_id: sample for sample in execution.version.calibration.samples}
    responses = []
    for plan in execution.plan:
        kind = samples[plan["sample_id"]].kind
        winner = "tie" if kind in {"borderline", "missing_evidence"} else (
            "A" if plan["presentation_order"][0] == "a-left" else "B")
        responses.append(json.dumps({"winner": winner, "criteria": [
            {"criterion_id": key, "preference": winner, "reason": "software-only-fixture",
             "evidence": [] if kind == "missing_evidence" else ["event:10", "event:20"]}
            for key in execution.spec.criteria]}))
    snapshot = JudgeProviderSnapshot.model_validate(execution.provider_snapshot)
    provider = OpenAICompatibleProvider(
        ScriptedCalibrationTransport(responses), snapshot.model,
        parameters=snapshot.parameters, max_output_tokens=snapshot.max_output_tokens,
        price_table=parse_price_table(snapshot.price_table),
        identity_policy=snapshot.identity_policy or "report_only",
        identity_aliases=snapshot.identity_aliases,
        identity_alias_version=snapshot.identity_alias_version,
    )
    env["provider"] = env["factory"].provider = provider
    return provider
