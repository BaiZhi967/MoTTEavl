"""Pure retention contracts: no repository, filesystem, or deletion operations."""
from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from motte_contracts.identity import canonical_sha256


DIGEST = "sha256:" + "a" * 64
NOW = datetime(2026, 9, 30, 3, 0, tzinfo=timezone.utc)


def models():
    return importlib.import_module("motte_storage.trace_retention_models")


def prefix_data(**changes):
    return {
        "run_id": "run-1", "run_revision": 2, "status": "completed",
        "first_seq": 1, "last_seq": 3, "keep_seq": 4, "event_count": 3,
        "events_sha256": DIGEST, **changes,
    }


def protection_data(**changes):
    body = {"run_ids": ["run-1", "run-2"], "event_seqs": {"run-2": [1, 3]}}
    body.update(changes)
    return {**body, "sha256": canonical_sha256(body)}


def plan_data(**changes):
    body = {
        "schema_version": 1, "store_identity_sha256": DIGEST,
        "config": {"enabled": True, "retention_days": 7},
        "cutoff": NOW.isoformat().replace("+00:00", "Z"),
        "protection_sha256": DIGEST, "prefixes": [prefix_data()],
        **changes,
    }
    return {**body, "plan_id": canonical_sha256(body)}


def receipt_data(**changes):
    return {
        "archive_id": "trace-archive-1", "plan_id": plan_data()["plan_id"],
        "prefix": prefix_data(), "artifact_id": "artifact-1", "sha256": DIGEST,
        "bytes": 128, "cutoff": NOW, "artifact_refs": {"artifact-2": DIGEST},
        "artifact_hashes": [DIGEST], "committed_at": None, **changes,
    }


def test_config_is_disabled_without_an_invented_default_retention():
    config = models().TraceRetentionConfig()
    assert config.model_dump() == {"enabled": False, "retention_days": None}
    assert models().TraceRetentionConfig(enabled=True, retention_days=1).enabled is True


@pytest.mark.parametrize("days", [None, False, True, 0, -1, 1.5, "7", float("inf")])
def test_enabled_config_requires_explicit_positive_strict_integer_days(days):
    with pytest.raises(ValueError):
        models().TraceRetentionConfig(enabled=True, retention_days=days)


@pytest.mark.parametrize("changes", [{"enabled": 1}, {"enabled": "false"}, {"extra": 1}])
def test_config_rejects_coerced_enabled_and_extra_fields(changes):
    with pytest.raises(ValueError):
        models().TraceRetentionConfig(**changes)


def test_disabled_config_still_validates_supplied_retention_days():
    with pytest.raises(ValueError):
        models().TraceRetentionConfig(enabled=False, retention_days=True)
    assert models().TraceRetentionConfig(enabled=False, retention_days=7).enabled is False


def test_utc_now_is_aware_utc():
    before = datetime.now(timezone.utc)
    observed = models().utc_now()
    after = datetime.now(timezone.utc)
    assert observed.tzinfo is timezone.utc
    assert before <= observed <= after


def test_unknown_legacy_time_is_none_without_payload_timestamp_fallback():
    event = models().StoredTraceEvent(
        run_id="run-1", seq=1, payload={"timestamp": "1900-01-01T00:00:00Z"}, stored_at=None,
    )
    assert event.stored_at is None
    assert event.model_dump(mode="json")["stored_at"] is None
    assert event.payload["timestamp"] == "1900-01-01T00:00:00Z"


@pytest.mark.parametrize("model_name,field,data", [
    ("StoredTraceEvent", "stored_at", {"run_id": "run-1", "seq": 1, "payload": {}}),
    ("TraceRetentionPlan", "cutoff", plan_data()),
    ("TraceArchiveReceipt", "cutoff", receipt_data()),
    ("TraceArchiveReceipt", "committed_at", receipt_data()),
])
def test_all_storage_timestamps_reject_naive_datetimes(model_name, field, data):
    with pytest.raises(ValueError):
        getattr(models(), model_name)(**{**data, field: NOW.replace(tzinfo=None)})


def test_timestamp_is_normalized_to_utc_and_roundtrips_from_json():
    offset_time = NOW.astimezone(timezone(timedelta(hours=8)))
    event = models().StoredTraceEvent(run_id="run-1", seq=1, payload={}, stored_at=offset_time)
    assert event.stored_at.tzinfo is timezone.utc
    assert event.model_dump(mode="json")["stored_at"] == "2026-09-30T03:00:00Z"
    assert type(event).model_validate_json(event.model_dump_json()) == event


@pytest.mark.parametrize("changes", [
    {"first_seq": 0}, {"first_seq": True}, {"last_seq": 0}, {"last_seq": 2},
    {"keep_seq": 3}, {"keep_seq": False}, {"event_count": 2}, {"event_count": True},
    {"run_revision": -1}, {"run_revision": True}, {"run_id": ""}, {"status": ""},
    {"events_sha256": "not-a-digest"}, {"extra": "forbidden"},
])
def test_prefix_requires_strict_logical_bounds_count_and_identity(changes):
    with pytest.raises(ValueError):
        models().TracePrefix(**prefix_data(**changes))


def test_prefix_can_follow_a_previously_trimmed_prefix_and_keep_a_later_highest_seq():
    prefix = models().TracePrefix(**prefix_data(first_seq=8, last_seq=10, keep_seq=15))
    assert (prefix.first_seq, prefix.last_seq, prefix.keep_seq, prefix.event_count) == (8, 10, 15, 3)


def test_protection_serialization_is_deterministic_for_sets_and_mapping_order():
    expected = protection_data()
    first = models().TraceProtection(
        run_ids=frozenset({"run-2", "run-1"}), event_seqs={"run-2": frozenset({3, 1})},
        sha256=expected["sha256"],
    )
    second = models().TraceProtection.model_validate_json(json.dumps(expected))
    assert first.model_dump(mode="json") == expected
    assert first.model_dump_json() == second.model_dump_json()
    assert first.run_ids == frozenset({"run-1", "run-2"})
    assert first.event_seqs == {"run-2": frozenset({1, 3})}


@pytest.mark.parametrize("changes", [
    {"run_ids": [""]}, {"event_seqs": {"": [1]}}, {"event_seqs": {"run-2": [0]}},
    {"event_seqs": {"run-2": [True]}}, {"sha256": DIGEST}, {"extra": 1},
])
def test_protection_rejects_invalid_pins_and_unbound_digest(changes):
    with pytest.raises(ValueError):
        models().TraceProtection(**{**protection_data(), **changes})


def test_plan_id_binds_canonical_body_excluding_itself():
    data = plan_data()
    plan = models().TraceRetentionPlan.model_validate(data)
    assert plan.plan_id == canonical_sha256(plan.model_dump(mode="json", exclude={"plan_id"}))
    assert type(plan).model_validate_json(plan.model_dump_json()) == plan
    with pytest.raises(ValueError):
        type(plan).model_validate({**data, "plan_id": DIGEST})
    with pytest.raises(ValueError):
        type(plan).model_validate({**data, "cutoff": "2026-09-29T03:00:00Z"})


def test_plan_prefix_order_is_canonical_and_duplicate_runs_are_rejected():
    prefixes = [prefix_data(run_id="run-1"), prefix_data(run_id="run-2")]
    data = plan_data(prefixes=prefixes)
    forward = models().TraceRetentionPlan.model_validate(data)
    reverse = models().TraceRetentionPlan.model_validate({**data, "prefixes": prefixes[::-1]})
    assert forward.model_dump_json() == reverse.model_dump_json()
    with pytest.raises(ValueError):
        models().TraceRetentionPlan.model_validate(plan_data(prefixes=[prefixes[0], prefixes[0]]))


def test_disabled_plan_cannot_have_cutoff_or_candidates():
    empty = plan_data(config={"enabled": False, "retention_days": None}, cutoff=None, prefixes=[])
    assert models().TraceRetentionPlan.model_validate(empty).cutoff is None
    for changes in ({"cutoff": NOW.isoformat().replace("+00:00", "Z")},
                    {"prefixes": [prefix_data()]}):
        with pytest.raises(ValueError):
            models().TraceRetentionPlan.model_validate({**empty, **changes})
    with pytest.raises(ValueError):
        models().TraceRetentionPlan.model_validate(plan_data(cutoff=None))


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"schema_version": 2},
    {"store_identity_sha256": "postgresql://user:secret@host/db"},
    {"dsn": "postgresql://user:secret@host/db"}, {"protection_sha256": "bad"},
])
def test_plan_is_versioned_and_store_identity_is_hash_only_without_dsn(changes):
    with pytest.raises(ValueError):
        models().TraceRetentionPlan.model_validate({**plan_data(), **changes})
    payload = models().TraceRetentionPlan.model_validate(plan_data()).model_dump_json()
    assert "store_identity_sha256" in payload
    assert "dsn" not in payload and "secret" not in payload


@pytest.mark.parametrize("changes", [
    {"bytes": -1}, {"bytes": 0}, {"bytes": True}, {"sha256": "bad"},
    {"archive_id": ""}, {"artifact_id": ""}, {"plan_id": "bad"},
    {"artifact_refs": {"": None}}, {"artifact_refs": {"artifact-2": "bad"}},
    {"artifact_hashes": ["bad"]}, {"extra": 1},
])
def test_archive_receipt_validates_identity_size_and_nested_evidence(changes):
    with pytest.raises(ValueError):
        models().TraceArchiveReceipt(**receipt_data(**changes))


def test_receipt_supports_pending_and_committed_states_and_canonical_evidence():
    refs = {"artifact-z": None, "artifact-a": DIGEST}
    second_hash = "sha256:" + "b" * 64
    pending = models().TraceArchiveReceipt(**receipt_data(
        artifact_refs=refs, artifact_hashes=[second_hash, DIGEST, second_hash],
    ))
    assert pending.committed_at is None
    assert list(pending.model_dump(mode="json")["artifact_refs"]) == ["artifact-a", "artifact-z"]
    assert pending.artifact_hashes == [DIGEST, second_hash]
    committed = type(pending)(**{**pending.model_dump(), "committed_at": NOW})
    assert committed.committed_at == NOW
    assert type(committed).model_validate_json(committed.model_dump_json()) == committed


@pytest.mark.parametrize("data", [
    {"events": [], "trimmed_through": -1}, {"events": [], "trimmed_through": True},
    {"events": [{"seq": 2}], "trimmed_through": 2},
    {"events": [{"seq": True}], "trimmed_through": 0},
    {"events": [{"seq": 3}, {"seq": 2}], "trimmed_through": 1},
    {"events": [{"seq": 2}, {"seq": 2}], "trimmed_through": 1},
    {"events": [{}], "trimmed_through": 0},
])
def test_window_validates_trim_boundary_and_monotonic_visible_sequences(data):
    with pytest.raises(ValueError):
        models().TraceEventWindow(**data)


def test_empty_and_paginated_windows_preserve_known_trim_boundary():
    assert models().TraceEventWindow(events=[], trimmed_through=9).trimmed_through == 9
    window = models().TraceEventWindow(events=[{"seq": 12}, {"seq": 14}], trimmed_through=9)
    assert [event["seq"] for event in window.events] == [12, 14]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), object()])
def test_nested_trace_payload_rejects_nonfinite_or_non_json_values(bad):
    with pytest.raises(ValueError):
        models().StoredTraceEvent(
            run_id="run-1", seq=1, payload={"nested": [{"value": bad}]}, stored_at=None,
        )
    with pytest.raises(ValueError):
        models().TraceEventWindow(events=[{"seq": 1, "value": bad}], trimmed_through=0)


def test_frozen_models_snapshot_input_and_return_detached_mutable_readbacks():
    payload = {"nested": [{"value": 1}]}
    event = models().StoredTraceEvent(run_id="run-1", seq=1, payload=payload, stored_at=None)
    payload["nested"][0]["value"] = 2
    event.payload["nested"][0]["value"] = 3
    dumped = event.model_dump()
    dumped["payload"]["nested"][0]["value"] = 4
    assert event.payload == {"nested": [{"value": 1}]}
    with pytest.raises(ValidationError):
        event.seq = 2
    plan = models().TraceRetentionPlan.model_validate(plan_data())
    plan.prefixes.clear()
    assert len(plan.prefixes) == 1
    protection = models().TraceProtection(**protection_data())
    protection.event_seqs.clear()
    assert protection.event_seqs == {"run-2": frozenset({1, 3})}


def test_result_requires_committed_receipts_for_the_same_plan_and_exact_trim_count():
    receipt = models().TraceArchiveReceipt(**receipt_data(committed_at=NOW))
    result = models().TraceRetentionResult(
        plan_id=receipt.plan_id, trimmed_events=3, receipts=[receipt],
    )
    assert result.trimmed_events == 3
    assert type(result).model_validate_json(result.model_dump_json()) == result
    for changes in ({"trimmed_events": 2}, {"trimmed_events": True}, {"plan_id": DIGEST},
                    {"receipts": [receipt, receipt]},
                    {"receipts": [models().TraceArchiveReceipt(**receipt_data())]}):
        with pytest.raises(ValueError):
            models().TraceRetentionResult(**{**result.model_dump(), **changes})
    assert models().TraceRetentionResult(
        plan_id=receipt.plan_id, trimmed_events=0, receipts=[],
    ).trimmed_events == 0


def test_retention_error_types_are_value_errors():
    for name in ("TraceRetentionDisabled", "TraceRetentionPlanChanged", "TraceArchiveInvalid"):
        assert issubclass(getattr(models(), name), ValueError)


@pytest.mark.parametrize("seqs", [[1, True], [True, 1], [1, 1.0], [1, "1"]])
def test_protection_checks_original_sequence_types_before_set_deduplication(seqs):
    with pytest.raises(ValueError):
        models().TraceProtection(**{
            **protection_data(event_seqs={"run-2": [1]}), "event_seqs": {"run-2": seqs},
        })


def test_canonical_protection_ignores_mapping_and_set_iteration_order():
    data = protection_data(event_seqs={"run-1": [2, 4], "run-2": [1, 3]})
    first = models().TraceProtection(**data)
    second = models().TraceProtection(**{
        **data, "event_seqs": {"run-2": frozenset({3, 1}), "run-1": frozenset({4, 2})},
    })
    assert first.model_dump_json() == second.model_dump_json()
    assert type(first).model_validate_json(first.model_dump_json()) == first


def test_raw_artifact_hashes_normalize_before_plan_identity_validation():
    plan = models().TraceRetentionPlan.model_validate({
        **plan_data(), "store_identity_sha256": "a" * 64, "protection_sha256": "a" * 64,
        "prefixes": [prefix_data(events_sha256="a" * 64)],
    })
    assert plan.model_dump(mode="json") == plan_data()
    receipt = models().TraceArchiveReceipt(**receipt_data(
        sha256="a" * 64, artifact_refs={"artifact-2": "a" * 64}, artifact_hashes=["a" * 64],
    ))
    assert receipt.sha256 == DIGEST
    assert receipt.artifact_refs == {"artifact-2": DIGEST}
    assert receipt.artifact_hashes == [DIGEST]


def test_pending_receipt_can_omit_db_assigned_commit_time():
    data = receipt_data()
    del data["committed_at"]
    assert models().TraceArchiveReceipt(**data).committed_at is None


def test_revalidation_rejects_unchecked_copies_including_nested_models():
    config = models().TraceRetentionConfig(enabled=True, retention_days=7)
    forged_config = config.model_copy(update={"retention_days": True})
    with pytest.raises(ValueError):
        type(config).model_validate(forged_config)
    plan = models().TraceRetentionPlan.model_validate(plan_data())
    forged_plan = plan.model_copy(update={"config": forged_config})
    with pytest.raises(ValueError):
        type(plan).model_validate(forged_plan)
    forged_prefix = models().TracePrefix.model_construct(**prefix_data(keep_seq=1))
    with pytest.raises(ValueError):
        models().TraceRetentionPlan.model_validate({**plan_data(), "prefixes": [forged_prefix]})


@pytest.mark.parametrize("timestamp", [1, True, "2026-09-30T03:00:00", float("nan"),
                                      datetime.min.replace(tzinfo=timezone(timedelta(hours=8)))])
def test_timestamp_rejects_epoch_coercion_naive_iso_and_utc_overflow(timestamp):
    with pytest.raises(ValueError):
        models().StoredTraceEvent(run_id="run-1", seq=1, payload={}, stored_at=timestamp)


def test_model_fields_match_the_approved_shared_interface():
    expected = {
        "TraceRetentionConfig": "enabled retention_days",
        "StoredTraceEvent": "run_id seq payload stored_at",
        "TracePrefix": "run_id run_revision status first_seq last_seq keep_seq event_count events_sha256",
        "TraceProtection": "run_ids event_seqs sha256",
        "TraceRetentionPlan": (
            "schema_version plan_id store_identity_sha256 config cutoff protection_sha256 prefixes"
        ),
        "TraceArchiveReceipt": (
            "archive_id plan_id prefix artifact_id sha256 bytes cutoff artifact_refs artifact_hashes "
            "committed_at reference_document"
        ),
        "TracePassReference": "pass_id owning_run_id expected_run_id",
        "TraceReferenceDocument": "schema_version run_ids event_seqs pass_references",
        "TraceEventWindow": "events trimmed_through",
        "TraceRetentionResult": "plan_id trimmed_events receipts",
    }
    for name, fields in expected.items():
        assert set(getattr(models(), name).model_fields) == set(fields.split())


@pytest.mark.parametrize("model_name,data", [
    ("StoredTraceEvent", {"run_id": "run-1", "seq": 1, "payload": {}, "stored_at": None}),
    ("TraceProtection", protection_data()), ("TraceRetentionPlan", plan_data()),
    ("TraceEventWindow", {"events": [], "trimmed_through": 0}),
    ("TraceRetentionResult", {"plan_id": DIGEST, "trimmed_events": 0, "receipts": []}),
])
def test_every_shared_model_rejects_extra_fields(model_name, data):
    with pytest.raises(ValueError):
        getattr(models(), model_name)(**{**data, "unexpected": "forbidden"})


@pytest.mark.parametrize("seq", [0, -1, True, 1.0, "1"])
def test_stored_event_requires_a_positive_strict_sequence(seq):
    with pytest.raises(ValueError):
        models().StoredTraceEvent(run_id="run-1", seq=seq, payload={}, stored_at=None)


def test_trace_evidence_rejects_non_string_mapping_keys_without_coercion():
    with pytest.raises(ValueError):
        models().StoredTraceEvent(run_id="run-1", seq=1, payload={"nested": {1: "bad"}}, stored_at=None)


def public_readback(model, mode):
    if mode == "dict":
        return dict(model)
    return {name: value for name, value in model}


@pytest.mark.parametrize("mode", ["dict", "iteration"])
def test_public_iteration_detaches_nested_trace_evidence(mode):
    event = models().StoredTraceEvent(
        run_id="run-1", seq=1, payload={"nested": [{"value": 1}]}, stored_at=None,
    )
    original_json = event.model_dump_json()
    original_identity = canonical_sha256(event)
    readback = public_readback(event, mode)
    readback["payload"]["nested"][0]["value"] = 99
    assert event.payload == {"nested": [{"value": 1}]}
    assert event.model_dump_json() == original_json
    assert canonical_sha256(event) == original_identity
    assert type(event).model_validate(event).model_dump_json() == original_json


@pytest.mark.parametrize("mode", ["dict", "iteration"])
def test_public_iteration_detaches_both_archive_evidence_reference_containers(mode):
    receipt = models().TraceArchiveReceipt(**receipt_data())
    original_json = receipt.model_dump_json()
    original_identity = canonical_sha256(receipt)
    readback = public_readback(receipt, mode)
    readback["artifact_refs"].clear()
    readback["artifact_hashes"].clear()
    assert receipt.artifact_refs == {"artifact-2": DIGEST}
    assert receipt.artifact_hashes == [DIGEST]
    assert receipt.sha256 == DIGEST
    assert receipt.model_dump_json() == original_json
    assert canonical_sha256(receipt) == original_identity
    revalidated = type(receipt).model_validate(receipt)
    assert revalidated.artifact_refs == {"artifact-2": DIGEST}
    assert revalidated.artifact_hashes == [DIGEST]
    assert revalidated.model_dump_json() == original_json


@pytest.mark.parametrize("mode", ["dict", "iteration"])
def test_public_iteration_detaches_other_shared_collection_fields(mode):
    protection = models().TraceProtection(**protection_data())
    plan = models().TraceRetentionPlan.model_validate(plan_data())
    window = models().TraceEventWindow(events=[{"seq": 1, "nested": [1]}], trimmed_through=0)
    receipt = models().TraceArchiveReceipt(**receipt_data(committed_at=NOW))
    result = models().TraceRetentionResult(
        plan_id=receipt.plan_id, trimmed_events=3, receipts=[receipt],
    )
    originals = [record.model_dump_json() for record in (protection, plan, window, result)]
    public_readback(protection, mode)["event_seqs"].clear()
    public_readback(plan, mode)["prefixes"].clear()
    public_readback(window, mode)["events"][0]["nested"].clear()
    public_readback(result, mode)["receipts"].clear()
    assert [record.model_dump_json() for record in (protection, plan, window, result)] == originals
