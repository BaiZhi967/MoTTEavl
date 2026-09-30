"""Optional minimal coding-only dataset, Experiment and Skill witnesses.

The parent harness supplies the live HTTP/credential guard. There is no standalone
entrypoint, credential read, download, generated-code execution or implicit run.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
import hashlib
import json

from fastapi.testclient import TestClient

from scripts.opencode_go_integration_checks import (
    CODING_PROMPT, MODEL_ID, _app, _environment, _json, _reports, _require, _seed, _worker,
)

CALL_CAPS = {"check_direct_v2": 1, "check_experiment_cell": 1, "check_skill_ablation": 6}
V2_NAME = "integration-coding-v2"
WORKFLOW_NAME = "integration-coding-skill"


def _skill_instruction(version):
    return (f"Coding review instruction v{version}: "
            "Check the requested Python arithmetic fix carefully. "
            "Keep the function's names and return only the requested code.")


@contextmanager
def _skill_wire_evidence(arm):
    """Inspect actual serialized bodies while retaining the caller's guarded opener.

    Only booleans/hashes survive; no credentials, headers, prompts or source text
    are logged or returned. Both existing opener references are delegated to
    unchanged, so the parent endpoint/budget/authentication guard still owns I/O.
    """
    from motte_provider import transport

    seen = []
    originals = {name: getattr(transport, name)
                 for name in ("_safe_urlopen", "_bounded_urlopen")}

    def observe(opener):
        def send(request, *, timeout):
            body = json.loads(request.data)
            systems = "\n".join(message.get("content", "")
                                for message in body.get("messages", [])
                                if message.get("role") == "system")
            found = {version: _skill_instruction(version) in systems for version in ("1", "2")}
            expected = {"1": arm == "skill-v1", "2": arm == "skill-v2"}
            matches = found == expected
            _require(matches, "skill_wire_injection_mismatch")
            _require(len(seen) < 2, "skill_arm_wire_budget_exceeded")
            response = opener(request, timeout=timeout)
            seen.append({"arm": arm, "matches_expected_arm": matches,
                         "v1_present": found["1"], "v2_present": found["2"],
                         "system_sha256": hashlib.sha256(systems.encode()).hexdigest()})
            return response
        return send

    try:
        for name, opener in originals.items():
            setattr(transport, name, observe(opener))
        yield seen
        _require(bool(seen), "skill_wire_evidence_missing")
    finally:
        for name, opener in originals.items():
            setattr(transport, name, opener)


def _seed_v2(app, root):
    from motte_contracts.direct_llm_v2 import scenario_for_v2
    from motte_sdk.direct_llm_v2 import (
        normalize_direct_llm_v2_dataset, persist_direct_llm_v2_dataset,
    )
    from motte_sdk.publication import publication_audit

    case = {"case_id": "fix-add", "input": CODING_PROMPT, "expected": "return a + b",
            "metadata": {"source_line": 1, "source_id": "synthetic-coding-fixture",
                         "language": "en", "split": "test", "tags": ["coding-smoke"]}}
    source = root / "synthetic-coding.jsonl"
    source.write_text(json.dumps(case) + "\n", encoding="utf-8")
    raw = source.read_bytes()
    dataset = normalize_direct_llm_v2_dataset({
        "name": V2_NAME, "version": "1", "contract_version": 2,
        "eval": {"suite": "direct-llm", "id": "direct-llm-prompts", "version": 2,
                 "selected_count": 1, "selection": "all-rows-in-file-order",
                 "scorer": {"id": "exact", "version": "1", "config": {
                     "normalize_whitespace": True}}, "prompt_version": "coding-fix@1",
                 "max_output_tokens": 512, "max_retries": 0},
        "provenance": {
            "source_id": "synthetic-coding-fixture", "source_kind": "synthetic-test",
            "homepage": None, "upstream_revision": "generated-fixture-v1",
            "artifacts": [{"logical_name": source.name, "url": source.as_uri(),
                           "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}],
            "license": {"id": "synthetic-test-only", "status": "approved-test-only",
                        "evidence_urls": [], "commercial_use": "test-only",
                        "redistribution": "test-only", "reviewed_at": None},
            "converter": {"id": "bounded-coding-fixture", "version": "1", "config": {}},
            "synthetic": True,
        },
        "cases": [case], "profiles": [{"name": "smoke", "strategy": "fixed",
            "case_ids": ["fix-add"], "dimensions": [], "seed": None}],
    })
    audit = publication_audit(dataset, scenario_for_v2(dataset, version="1"),
                              {"dataset_fingerprint": dataset["dataset_fingerprint"],
                               "source": "synthetic-coding-fixture"},
                              actor="bounded-live-harness", entrypoint="sdk",
                              published_at=datetime.now(UTC).isoformat())
    receipt = persist_direct_llm_v2_dataset(dataset, app.state.resource_store,
                                           version="1", publication=audit)
    _require(app.state.resource_store.publications.get(audit["id"]) is not None,
             "publication_audit_missing")
    return receipt


def check_direct_v2(ctx):
    """One model call through v2 publication/profile/snapshot/deterministic scoring."""
    with _environment(ctx, "suite-direct-v2") as path:
        app = _app(path)
        with TestClient(app) as client:
            _seed(client, ctx)
            receipt = _seed_v2(app, path.parent)
            request = {"model": MODEL_ID, "scenario": receipt["scenario"],
                       "case_selection": {"mode": "profile", "profile": "smoke"}}
            dry = _json(client.post("/api/v1/benchmarks/direct-llm/dry-run", json=request))
            _require(dry["contract_version"] == 2 and dry["selected_count"] == 1,
                     "v2_preflight_mismatch")
            run = _json(client.post("/api/v1/benchmarks/direct-llm/runs", json=request), 202)
        app = _app(path)
        with TestClient(app) as client:
            result = _worker(app, run["id"])
            _require(result["scores"] and all(s.get("passed") is True for s in result["scores"]),
                     "v2_coding_score_failed")
            return {"ok": True, "run_id": run["id"], "contract_version": 2,
                    "profile": "smoke", "publication_verified": True,
                    **_reports(client, run["id"])}


def _experiment(client, spec):
    preview = _json(client.post("/api/v1/experiments/preview", json=spec))
    _require(not preview["violations"], "experiment_preflight_failed")
    _require(preview["max_potential_calls"] <= spec["budget_policy"]["max_total_calls"],
             "experiment_call_bound_failed")
    result = _json(client.post("/api/v1/experiments", json={
        **spec, "_preview_hash": preview["preview_hash"]}), 202)
    _require(not result["failed"] and len(result["cells"]) == spec["max_cells"],
             "experiment_allocation_failed")
    return result


def _experiment_result(client, app, spec, created, *, check_skill_wire=False):
    reports, runs, wire_evidence = [], [], []
    for cell in created["cells"]:
        manifest = app.state.run_service.store.runs.get(cell["run_id"])["manifest"]
        capture = (_skill_wire_evidence(manifest["skill_arm"])
                   if check_skill_wire else nullcontext([]))
        with capture as wire:
            result = _worker(app, cell["run_id"])
        wire_evidence.extend(wire)
        _require(result["scores"] and all(s.get("passed") is True for s in result["scores"]),
                 "experiment_coding_score_failed")
        runs.append(result)
        reports.append(_reports(client, cell["run_id"]))
    # Read and repeat allocation only; deterministic cell IDs prohibit new Runs.
    status = _json(client.get(f"/api/v1/experiments/{spec['experiment_id']}",
                              params={"version": "1"}))
    replay = _json(client.post(f"/api/v1/experiments/{spec['experiment_id']}/allocate",
                               json={"version": "1"}))
    _require(replay["allocated"] == 0, "experiment_duplicate_allocation")
    _require(len(app.state.run_service.store.runs.list()) == spec["max_cells"],
             "experiment_duplicate_runs")
    _require(status["cell_count"] == len(runs)
             and status["progress"]["allocated"] == len(runs)
             and {cell["run_id"] for cell in status["cells"]} == {run["id"] for run in runs},
             "experiment_status_mismatch")
    return runs, {"ok": True, "experiment_id": spec["experiment_id"],
                  "cell_count": len(created["cells"]), "completed_cells": len(runs),
                  "idempotent_allocation": True, "report_count": len(reports),
                  **({"wire_injection_verified": True, "wire_skill_evidence": wire_evidence}
                     if check_skill_wire else {})}


def check_experiment_cell(ctx):
    """One direct-v2 cell, one case, one model, one repeat, one HTTP call."""
    with _environment(ctx, "suite-experiment") as path:
        app = _app(path)
        with TestClient(app) as client:
            _seed(client, ctx)
            receipt = _seed_v2(app, path.parent)
            spec = {"experiment_id": "integration-one-cell", "version": "1",
                    "task_ref": {"suite": "direct-llm", "scenario_version": receipt["scenario"]},
                    "factors": {"model_profile": [MODEL_ID]}, "repeats": 1,
                    "selected_case_keys": ["fix-add"], "max_cells": 1,
                    "controlled_conditions": {"max_output_tokens": 512},
                    "budget_policy": {"max_total_calls": 1},
                    "created_by": "bounded-live-harness", "reason": "Synthetic coding witness"}
            created = _experiment(client, spec)
        app = _app(path)
        with TestClient(app) as client:
            _runs, result = _experiment_result(client, app, spec, created)
            return result


def check_skill_ablation(ctx):
    """Minimum legitimate three-arm Skill matrix, <=2 model steps in each arm."""
    with _environment(ctx, "suite-skill-ablation") as path:
        app = _app(path)
        with TestClient(app) as client:
            _seed(client, ctx)
            published = datetime.now(UTC).isoformat()
            hashes = []
            for version in ("1", "2"):
                skill = _json(client.post("/api/v1/skills/versions", json={
                    "skill_id": "integration-coding-review", "version": version,
                    "kind": "instruction", "injection_mode": "system-prompt",
                    "instruction": _skill_instruction(version),
                    "published_at": published,
                }), 201)
                hashes.append(skill["content_hash"])
            _json(client.post("/api/v1/workflows", json={
                "workflow_id": WORKFLOW_NAME, "version": "1", "published_at": published,
                "steps": [{"step_id": "review", "kind": "send_message", "message": CODING_PROMPT}],
                "target_requirements": {"multi_turn": True, "min_turns": 1,
                                        "skill_injection": True},
                "limits": {"max_total_steps": 2, "max_turns": 1, "wall_time_sec": 40},
            }), 201)
            _json(client.post("/api/v1/scenarios", json={
                "name": WORKFLOW_NAME, "version": "1", "evaluator": {
                    "evaluator_id": "workflow-assertions", "version": "1", "config": {
                        "metrics": [{"metric_id": "correct-add", "kind": "response-policy",
                                     "source": "final_output", "require": [
                                         {"text": "return a + b", "min_count": 1}]}]}}}), 201)
            spec = {"experiment_id": "integration-three-arm", "version": "1",
                    "task_ref": {"suite": "skill", "scenario_version": f"{WORKFLOW_NAME}@1"},
                    "suite_config": {"kind": "skill", "workflow_ref": f"{WORKFLOW_NAME}@1",
                        "agent_mode": "native-tool", "cases": ["fix-add"],
                        "budget_policy": "same-execution-budget", "execution_budget": {
                            "max_steps": 2, "max_tool_calls": 1, "wall_time_sec": 40,
                            "per_call_timeout_sec": 20}},
                    "factors": {"model_profile": [MODEL_ID], "skill_version": ["no-skill",
                        "integration-coding-review@1", "integration-coding-review@2"]},
                    "repeats": 1, "max_cells": 3,
                    "budget_policy": {"max_total_calls": 6},
                    "created_by": "bounded-live-harness", "reason": "Minimal coding Skill witness"}
            created = _experiment(client, spec)
        app = _app(path)
        with TestClient(app) as client:
            runs, result = _experiment_result(client, app, spec, created, check_skill_wire=True)
            arms = sorted(run["manifest"]["skill_arm"] for run in runs)
            snapshots = [run["manifest"]["resource_snapshots"].get("skill_injection")
                         for run in runs if run["manifest"]["skills"]]
            _require(arms == ["no-skill", "skill-v1", "skill-v2"]
                     and len(snapshots) == 2 and all(snapshots), "skill_arm_evidence_missing")
            return {**result, "arms": arms, "skill_snapshot_count": len(snapshots),
                    "skill_content_hashes": hashes, "quality_comparison": "smoke-only-n1"}
