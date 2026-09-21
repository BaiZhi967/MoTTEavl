import hashlib
import json
import pytest
from motte_harness.inspect import import_inspect_log, InspectLogError


def native_log(eval_id="eval-a", run_id="native-a", samples=None, version=2):
    return json.dumps({
        "version": version, "status": "success",
        "eval": {"eval_id": eval_id, "run_id": run_id, "task": "file-task",
                 "created": "2026-09-21T00:00:00Z", "model": "provider/model"},
        "plan": {"steps": []}, "stats": {},
        "samples": samples if samples is not None else [
            {"id": "one", "epoch": 1, "scores": {"exact": {"value": 1.0}},
             "output": {"choices": [{"message": {"role": "assistant", "content": "done"}}]}}
        ],
    }, indent=2)


def test_native_ids_distinguish_independent_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = import_inspect_log(native_log())
    second = import_inspect_log(native_log("eval-b", "native-b"))
    assert first["import_id"] != second["import_id"]
    assert first["header_model"] == "provider/model"
    assert first["source_identity"]["eval_id"] == "eval-a"


def test_changing_sample_count_does_not_change_source_identity(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import_inspect_log(native_log())
    with pytest.raises(InspectLogError, match="different content"):
        import_inspect_log(native_log(samples=[
            {"id": "one", "epoch": 1, "scores": {}},
            {"id": "two", "epoch": 1, "scores": {}},
        ]))


@pytest.mark.parametrize("version", [1, 999, True])
def test_unknown_schema_version_is_refused(version):
    with pytest.raises(InspectLogError) as error:
        import_inspect_log(native_log(version=version))
    assert error.value.code == "UNKNOWN_SCHEMA"


def test_duplicate_sample_epoch_is_refused():
    with pytest.raises(InspectLogError) as error:
        import_inspect_log(native_log(samples=[
            {"id": "one", "epoch": 1}, {"id": "one", "epoch": 1},
        ]))
    assert error.value.code == "DUPLICATE_SAMPLE"


def test_source_bytes_remain_exact_on_windows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from pathlib import Path
    text = native_log()
    report = import_inspect_log(text)
    assert Path(report["raw_frozen"]).read_bytes() == text.encode("utf-8")
    assert report["raw_sha256"] == "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def test_read_only_import_cannot_queue_retry_or_replace_native_scores(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from motte_sdk.inspect_import import import_inspect_run
    from motte_sdk.service import RunService
    from motte_storage.run_store import InMemoryRunStore

    service = RunService(InMemoryRunStore())
    report = import_inspect_run(service, native_log())
    run_id = report["run_id"]
    original = service.get_run(run_id)
    with pytest.raises(ValueError, match="read-only"):
        service.rescore(run_id)
    assert service.get_run(run_id)["scoring_pass"] == original["scoring_pass"]
    def interrupt_import(*args, **kwargs):
        raise RuntimeError("import interrupted")

    monkeypatch.setattr(service, "_append_scoring_pass", interrupt_import)
    with pytest.raises(RuntimeError, match="interrupted"):
        import_inspect_run(service, native_log("interrupted", "source-interrupted"))
    interrupted = next(row for row in service.store.runs.list() if row["id"] != run_id)
    assert interrupted["status"] == "needs_review"
    with pytest.raises(ValueError, match="read-only"):
        service.retry(interrupted["id"])
    assert len(service.store.runs.list()) == 2


def test_api_import_creates_queryable_terminal_run_and_immutable_scores(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from fastapi.testclient import TestClient
    from apps.api.app.main import create_app
    from motte_storage.run_store import SQLiteRunStore
    store = SQLiteRunStore(tmp_path / "runs.db")
    client = TestClient(create_app(store=store))
    response = client.post("/api/v1/inspect/import", json={"content": native_log()})
    assert response.status_code == 200, response.text
    report = response.json()
    run_id = report["run_id"]
    run = client.get(f"/api/v1/runs/{run_id}").json()
    assert run["status"] == "completed"
    assert run["manifest"]["import_source"]["kind"] == "inspect"
    assert len(run["scores"]) == 1
    assert run["scores"][0]["metric_id"] == "native.exact"
    assert run["scoring_pass"]["source"] == "inspect-native"
    case_id = run["case_ids"][0]
    evidence = client.get(f"/api/v1/runs/{run_id}/cases/{case_id}/agent").json()
    assert evidence["observation"]["coverage"]["complete"] is False
    assert evidence["observation"]["event_refs"][0]["kind"] == "artifact"
    again = client.post("/api/v1/inspect/import", json={"content": native_log()}).json()
    assert again["run_id"] == run_id
    assert again["scoring_pass_id"] == report["scoring_pass_id"]
    reopened = SQLiteRunStore(tmp_path / "runs.db")
    assert reopened.runs.get(run_id)["status"] == "completed"
    assert reopened.attempts.list_open(run_id) == []


def test_import_preview_redacts_json_secret_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from fastapi.testclient import TestClient
    from apps.api.app.main import create_app
    from motte_storage.run_store import InMemoryRunStore
    payload = json.loads(native_log())
    payload["metadata"] = {"password": "review_dummy_password_123"}
    client = TestClient(create_app(store=InMemoryRunStore()))
    imported = client.post("/api/v1/inspect/import",
                           json={"content": json.dumps(payload)}).json()
    run = client.get(f"/api/v1/runs/{imported['run_id']}").json()
    response = client.get(
        f"/api/v1/runs/{run['id']}/cases/{run['case_ids'][0]}/artifacts/content",
        params={"path": "inspect-source.json"},
    )
    assert response.status_code == 200
    assert "review_dummy_password_123" not in response.text
    assert response.json()["sha256_matches"] is True


def test_cli_preserves_crlf_source_bytes(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    from pathlib import Path
    from motte_cli.main import main
    data = native_log().replace("\n", "\r\n").encode()
    source = tmp_path / "native.json"
    source.write_bytes(data)
    assert main(["inspect-import", str(source), "--db", str(tmp_path / "runs.db"), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert Path(report["raw_frozen"]).read_bytes() == data
