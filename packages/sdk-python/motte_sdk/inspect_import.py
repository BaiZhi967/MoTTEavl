"""Read-only Inspect imports use platform Run/Observation/immutable ScoreSet queries.

An import is never queued for execution. An interrupted database import remains
needs_review and an identical upload can finish the deterministic storage work.
"""
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path

from motte_contracts.evaluation import FrozenObservation, observation_evidence_hash
from motte_harness.inspect import InspectLogError, import_inspect_log
from motte_storage.artifacts import ArtifactStore
from motte_storage.integrity import RunConflictError
from motte_trace.redaction import redact_secrets


def import_inspect_run(service, content: str, *, name: str | None = None):
    report = import_inspect_log(content, name=name)
    run_id = "run-" + report["import_id"]
    store = service.store
    source = {
        "kind": "inspect", "import_id": report["import_id"],
        "identity": report["source_identity"], "sha256": report["source_sha256"],
        "parser_version": report["parser_version"], "schema": report["schema"],
    }
    artifact_id = f"imports/{report['import_id']}/source.json"
    artifact_store = ArtifactStore(Path(os.environ.get("ARTIFACT_ROOT", "var/artifacts")))
    artifact_store.put_bytes(artifact_id, content.encode("utf-8"), kind="inspect-source",
                             media_type="application/json")
    case_rows, scores = [], []
    for sample in report["samples"]:
        key = json.dumps([sample["id"], sample["epoch"]], ensure_ascii=False)
        case_id = "sample-" + hashlib.sha256(key.encode()).hexdigest()[:24]
        output = redact_secrets(sample.get("output"))
        evidence = {
            "observation_id": f"obs-{report['import_id']}-{case_id}",
            "run_id": run_id, "case_id": case_id, "final_output": output,
            "termination": {"reason": "error" if sample.get("error") else "invalid_state",
                            "detail": "imported native log; not executed by platform"},
            "event_refs": [{"kind": "artifact", "run_id": run_id, "locator": artifact_id}],
            "artifact_refs": [{
                "artifact_id": artifact_id, "path": "inspect-source.json",
                "media_type": "application/json", "size_bytes": len(content.encode("utf-8")),
                "sha256": report["source_sha256"].removeprefix("sha256:"),
                "available": True, "truncated": False,
            }],
            "coverage": {"complete": False, "missing": ["imported_tool_trajectory_unverified"],
                         "artifacts_expected": 1, "artifacts_captured": 1},
            "usage": {"reported": False}, "tool_calls": [], "processes": [],
        }
        evidence["evidence_hash"] = observation_evidence_hash(evidence)
        observation = FrozenObservation.model_validate(evidence).model_dump(mode="json")
        case_rows.append({
            "run_id": run_id, "case_id": case_id, "outcome": "imported",
            "result": {
                "agent": {"final_output": output, "termination_reason": evidence["termination"]["reason"],
                          "runtime": {"backend": "inspect-import@1",
                                      "parser_version": report["parser_version"]}},
                "observation": observation,
                "imported_sample": {"id": sample["id"], "epoch": sample["epoch"]},
            },
        })
        native_scores = sample["scores"] or {"missing": {"value": None}}
        for scorer, score in native_scores.items():
            value = score.get("value")
            numeric = (
                type(value) in (int, float)
                and -1.7976931348623157e308 <= value <= 1.7976931348623157e308
                and math.isfinite(value)
            )
            boolean = type(value) is bool
            scores.append({
                "case_id": case_id, "metric_id": f"native.{scorer}",
                "evaluator_id": "inspect-native", "evaluator_version": report["parser_version"],
                "scorer_version": report["parser_version"],
                "metric_status": "scored" if numeric or boolean else "insufficient_evidence",
                "value": float(value) if numeric else None,
                "passed": value if boolean else None,
                "denominator": numeric or boolean,
                "reason": None if numeric or boolean else "native_score_not_numeric_or_missing",
                "details": {"source": "inspect-native", "imported": True,
                            "sample_id": sample["id"], "epoch": sample["epoch"],
                            "native_score": redact_secrets(score),
                            "source_ref": artifact_id, "source_sha256": report["source_sha256"]},
            })
    current = store.runs.get(run_id)
    if current is None:
        now = datetime.now(UTC).isoformat()
        manifest = {
            "schema_version": 2,
            "execution": {"backend_id": "inspect-import", "backend_version": "1"},
            "import_source": source, "model": report["header_model"],
            "evaluation": {"scorer_id": "inspect-native", "scorer_version": report["parser_version"]},
        }
        try:
            store.runs.create({
                "id": run_id, "revision": 1, "schema_version": 2,
                "scenario_version": "inspect-import@1", "manifest": manifest,
                "requested_manifest": {"import_source": source},
                "case_ids": [row["case_id"] for row in case_rows],
                "status": "needs_review", "created_at": now, "updated_at": now,
            }, event={"run_id": run_id, "type": "inspect_import_started", "source": source})
        except RunConflictError:
            pass  # Another identical uploader won the insert; CAS below arbitrates.
        current = store.runs.get(run_id)
    if (current["manifest"].get("import_source") or {}).get("sha256") != report["source_sha256"]:
        raise InspectLogError("IMPORT_IDENTITY_CONFLICT", "stored Run has different source content")
    if current["status"] != "completed":
        try:
            service._append_scoring_pass(
                run_id, scores, source="inspect-native", final_status="completed",
                case_rows=case_rows, skip_aggregate=True,
                extra_summary={"imported": True, "source_identity": report["source_identity"],
                               "source_status": report["header_status"]},
            )
        except RunConflictError:
            if store.runs.get(run_id)["status"] != "completed":
                raise
    run = service.get_run(run_id)
    return {**redact_secrets(report), "run_id": run_id,
            "scoring_pass_id": run["current_scoring_pass_id"]}
