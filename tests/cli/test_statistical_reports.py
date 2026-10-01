"""Local and HTTP publication share stored envelopes, exports, and failure exits."""
from __future__ import annotations

import json
import sqlite3
import xml.etree.ElementTree as ET
from contextlib import closing

import pytest

from motte_cli.main import main
from motte_sdk import MotteClient
from motte_sdk.comparisons import ComparisonService
from motte_storage.run_store import SQLiteRunStore
from tests.sdk.test_m6_comparison_service import append_pass
from tests.sdk.test_statistical_reports import _seed


@pytest.fixture(params=["local", "server", "server-alias"])
def mode(request, tmp_path, monkeypatch):
    path = tmp_path / "reports.db"
    store = _seed(SQLiteRunStore(path))
    if request.param == "local":
        yield store, ["--db", str(path)]
    else:
        from apps.api.app.main import create_app
        from tests.sdk.sync_asgi import SyncASGITransport
        app = create_app(store=store)
        monkeypatch.setattr("motte_cli.remote.build_client", lambda args: MotteClient("http://testserver", transport=SyncASGITransport(app), retries=0))
        args = (["--server", "http://testserver"] if request.param == "server-alias" else ["--mode", "server", "--api-url", "http://testserver"])
        yield store, args


@pytest.mark.parametrize("inapplicable", [False, True])
def test_publish_get_and_exports_survive_restart_and_drift(mode, inapplicable, capsys, tmp_path, monkeypatch):
    store, mode_args = mode
    if inapplicable:
        append_pass(store, "base", "old-empty", [])
    command = ["statistical-report", "publish", "--baseline", "base", "--candidate", "candidate",
               "--baseline-pass", "old-empty" if inapplicable else "old-base",
               "--candidate-pass", "old-candidate", "--factors", "model,model", "--k", "1", *mode_args]
    assert main(command) == 0
    published = json.loads(capsys.readouterr().out)
    assert main(command) == 0
    assert json.loads(capsys.readouterr().out) == published
    assert published["body"]["result"]["inputs"]["allowed_factors"] == ["model"]
    assert SQLiteRunStore(store.runs._path).statistical_reports.get(published["report_id"]) == published
    append_pass(store, "base", "new-base", [])
    with closing(sqlite3.connect(store.runs._path)) as connection, connection:
        row = store.case_runs.get("base", "a")
        row["result"]["metering"]["latency_ms"] = 900
        connection.execute("UPDATE case_runs SET payload=? WHERE run_id=? AND case_id=?", (json.dumps(row), "base", "a"))

    def forbidden(*args, **kwargs):
        pytest.fail("stored CLI export must not calculate")

    monkeypatch.setattr(ComparisonService, "paired_statistics", forbidden)
    assert main(["statistical-report", "get", published["report_id"], *mode_args]) == 0
    assert json.loads(capsys.readouterr().out) == published
    for fmt in ("json", "junit"):
        output = tmp_path / f"report.{fmt}"
        assert main(["statistical-report", "export", published["report_id"], "--format", fmt, "--output", str(output), *mode_args]) == 0
        stdout = capsys.readouterr().out
        assert output.read_text() == stdout
        actual = json.loads(stdout) if fmt == "json" else json.loads(ET.fromstring(stdout).findtext("system-out"))
        assert actual == published
        if fmt == "junit":
            from motte_sdk.export import statistical_report_to_junit
            assert stdout == statistical_report_to_junit(published) + "\n"
            assert (ET.fromstring(stdout).find(".//skipped") is not None) is inapplicable


@pytest.mark.parametrize("extra", [["--k", "0"], ["--k", "true"], ["--k", "1.5"], ["--factors", "model,"], ["--baseline-pass", "old-candidate"], ["--candidate-pass", "absent"]])
def test_invalid_publish_has_error_exit_and_no_report(mode, extra, capsys):
    store, mode_args = mode
    assert main(["statistical-report", "publish", "--baseline", "base", "--candidate", "candidate", *extra, *mode_args]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"]
    assert store.statistical_reports.list() == []


@pytest.mark.parametrize("fmt", ["json", "junit", "csv"])
def test_failed_export_never_writes_partial_file(mode, fmt, capsys, tmp_path):
    _store, mode_args = mode
    output = tmp_path / "result"
    assert main(["statistical-report", "export", "absent", "--format", fmt, "--output", str(output), *mode_args]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"]
    assert not output.exists()
    output.write_text("keep existing\n")
    assert main(["statistical-report", "export", "absent", "--format", fmt, "--output", str(output), *mode_args]) == 2
    capsys.readouterr()
    assert output.read_text() == "keep existing\n"


def test_compare_statistics_remains_read_only(tmp_path, capsys):
    path = tmp_path / "legacy.db"
    store = _seed(SQLiteRunStore(path))
    assert main(["compare", "--baseline", "base", "--candidate", "candidate", "--statistics", "--db", str(path)]) == 0
    assert "statistics" in json.loads(capsys.readouterr().out)
    assert store.statistical_reports.list() == []


def test_export_write_failure_preserves_existing_file(mode, capsys, tmp_path, monkeypatch):
    _store, mode_args = mode
    assert main(["statistical-report", "publish", "--baseline", "base", "--candidate", "candidate", *mode_args]) == 0
    report = json.loads(capsys.readouterr().out)
    target = tmp_path / "export" / "report.json"
    target.parent.mkdir()
    target.write_text("existing\n")

    def fail_replace(*args, **kwargs):
        raise OSError("synthetic replacement failure")

    monkeypatch.setattr("motte_cli.main.os.replace", fail_replace)
    assert main(["statistical-report", "export", report["report_id"], "--output", str(target), *mode_args]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "EXPORT_FAILED"
    assert target.read_text() == "existing\n"
    assert list(target.parent.iterdir()) == [target]
