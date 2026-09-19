import pytest

from motte_contracts.identity import canonical_sha256
from motte_sdk.publication import publication_audit
from motte_storage.resource_store import InMemoryResourceStore, ResourceConflictError


def _bundle():
    fingerprint = "sha256:" + "1" * 64
    return (
        {"name": "managed", "version": "3", "dataset_fingerprint": fingerprint},
        {"name": "managed", "version": "3", "dataset": "managed@3"},
    )


def test_publication_audit_is_deterministic_and_publishes_atomically():
    dataset, scenario = _bundle()
    receipt = {"source_id": "fixture", "cases": 2, "artifacts": [{"sha256": "2" * 64}]}
    first = publication_audit(
        dataset, scenario, receipt,
        actor=" operator ", entrypoint=" cli ", published_at="2026-09-19T00:00:00Z",
    )
    second = publication_audit(
        dataset, scenario, receipt,
        actor="operator", entrypoint="cli", published_at="2026-09-19T00:00:00Z",
    )
    assert first == second
    assert first["receipt_sha256"] == "9b20b374b25a69b1aec8bf9ea27d1c978a06556155b40d61be3c378bdf51a764"

    store = InMemoryResourceStore()
    assert store.publish_dataset_scenario(
        dataset, scenario, publication=first
    ) == (dataset, scenario)
    assert store.publications.get(first["id"]) == first


def test_publication_audit_excludes_local_artifact_paths_and_normalizes_time():
    dataset, scenario = _bundle()
    receipt = {
        "source_id": "fixture",
        "dataset_fingerprint": dataset["dataset_fingerprint"],
        "artifact_paths": {"data": "/cache/one/data.jsonl"},
        "artifacts": [{"sha256": "2" * 64}],
    }
    first = publication_audit(
        dataset, scenario, receipt,
        actor=" operator ", entrypoint=" cli ", published_at="2026-09-19T08:00:00+08:00",
    )
    second = publication_audit(
        dataset, scenario, {**receipt, "artifact_paths": {"data": "D:/cache/two/data.jsonl"}},
        actor="operator", entrypoint="cli", published_at="2026-09-19T00:00:00Z",
    )
    assert first == second
    assert first["published_at"] == "2026-09-19T00:00:00Z"
    assert "artifact_paths" not in first["receipt"]


@pytest.mark.parametrize("published_at", ["2026-09-19", "not-a-time"])
def test_publication_audit_rejects_noncanonical_inputs(published_at):
    dataset, scenario = _bundle()
    with pytest.raises(ValueError, match="published_at"):
        publication_audit(
            dataset, scenario, {}, actor="operator", entrypoint="cli",
            published_at=published_at,
        )
    with pytest.raises(ValueError, match="receipt"):
        publication_audit(
            dataset, scenario, [], actor="operator", entrypoint="cli",
            published_at="2026-09-19T00:00:00Z",
        )
    with pytest.raises(ValueError, match="dataset_fingerprint"):
        publication_audit(
            dataset, scenario, {"dataset_fingerprint": "sha256:" + "0" * 64},
            actor="operator", entrypoint="cli", published_at="2026-09-19T00:00:00Z",
        )


@pytest.mark.parametrize(
    "receipt",
    [
        {"nested": {"artifact_paths": {"data": "/tmp/private"}}},
        {"body": "private dataset row"},
        {"metadata": {"secret": "token"}},
    ],
)
def test_publication_audit_rejects_nonportable_receipt_evidence(receipt):
    dataset, scenario = _bundle()
    with pytest.raises(ValueError, match="non-portable field"):
        publication_audit(
            dataset, scenario, receipt,
            actor="operator", entrypoint="cli", published_at="2026-09-19T00:00:00Z",
        )


def test_storage_rejects_forged_receipt_digest_even_with_rederived_id():
    dataset, scenario = _bundle()
    audit = publication_audit(
        dataset, scenario, {"source_id": "fixture"},
        actor="operator", entrypoint="cli", published_at="2026-09-19T00:00:00Z",
    )
    forged_digest = "f" * 64
    forged = {**audit, "receipt_sha256": forged_digest}
    identity = canonical_sha256({
        "dataset": forged["dataset"],
        "scenario": forged["scenario"],
        "dataset_fingerprint": forged["dataset_fingerprint"],
        "receipt_sha256": f"sha256:{forged_digest}",
        "actor": forged["actor"],
        "entrypoint": forged["entrypoint"],
    })
    forged["id"] = f"publication-{identity.removeprefix('sha256:')}"

    with pytest.raises(ValueError, match="receipt_sha256 does not match receipt"):
        InMemoryResourceStore().publications.put(forged)


def test_publication_audit_identity_covers_receipt_actor_and_entrypoint():
    dataset, scenario = _bundle()
    base = publication_audit(
        dataset, scenario, {"cases": 2},
        actor="operator", entrypoint="cli", published_at="2026-09-19T00:00:00Z",
    )
    changed_receipt = publication_audit(
        dataset, scenario, {"cases": 3},
        actor="operator", entrypoint="cli", published_at="2026-09-19T00:00:00Z",
    )
    changed_actor = publication_audit(
        dataset, scenario, {"cases": 2},
        actor="other", entrypoint="cli", published_at="2026-09-19T00:00:00Z",
    )
    assert len({base["id"], changed_receipt["id"], changed_actor["id"]}) == 3


def test_publication_audit_rejects_missing_identity_and_conflicting_replay():
    dataset, scenario = _bundle()
    with pytest.raises(ValueError, match="dataset_fingerprint"):
        publication_audit(
            {"name": "managed", "version": "3"}, scenario, {},
            actor="operator", entrypoint="cli", published_at="2026-09-19T00:00:00Z",
        )
    for field in ("actor", "entrypoint", "published_at"):
        arguments = {
            "actor": "operator", "entrypoint": "cli",
            "published_at": "2026-09-19T00:00:00Z",
        }
        arguments[field] = " "
        with pytest.raises(ValueError, match=field):
            publication_audit(dataset, scenario, {}, **arguments)

    audit = publication_audit(
        dataset, scenario, {},
        actor="operator", entrypoint="cli", published_at="2026-09-19T00:00:00Z",
    )
    store = InMemoryResourceStore()
    with pytest.raises(ValueError, match="fields are not canonical"):
        store.publications.put({"id": "incomplete"})
    with pytest.raises(ValueError, match="fields are not canonical"):
        store.publications.put({**audit, "unexpected": True})
    with pytest.raises(ValueError, match="canonical UTC RFC3339"):
        store.publications.put({**audit, "published_at": "2026-09-19T00:00:00+00:00"})
    store.publish_dataset_scenario(dataset, scenario, publication=audit)
    with pytest.raises(ResourceConflictError):
        store.publications.put({**audit, "published_at": "2026-09-20T00:00:00Z"})
