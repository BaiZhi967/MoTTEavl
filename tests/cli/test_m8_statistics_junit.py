"""Public HTTP and CLI consume one canonical statistics export."""
import json
import xml.etree.ElementTree as ET
from fastapi.testclient import TestClient
from apps.api.app.main import create_app
from motte_cli.main import main
from motte_storage.factory import create_run_store
from tests.cli.test_m6_cli import seed_run, seed_pass


def test_statistics_junit_http_and_cli_preserve_canonical_result(tmp_path, capsys):
    path = tmp_path / "stats.db"
    store = create_run_store(str(path))
    for run_id in ("base", "candidate"):
        seed_run(store, run_id, ["one"])
        seed_pass(store, run_id, run_id + "-pass", {"one": True})
    http = TestClient(create_app(store=store))
    args = {"baseline": "base", "candidate": "candidate"}
    result = http.get("/api/v1/comparisons/statistics", params=args).json()
    response = http.get("/api/v1/comparisons/statistics", params={**args, "format": "junit"})
    assert response.status_code == 200
    xml = ET.fromstring(response.text)
    assert json.loads(xml.findtext("system-out")) == result
    assert xml.find(".//skipped") is not None
    output = tmp_path / "statistics.xml"
    code = main(["compare", "--baseline", "base", "--candidate", "candidate", "--statistics",
                 "--format", "junit", "--output", str(output), "--db", str(path)])
    assert code == 0
    printed = capsys.readouterr().out.strip()
    assert output.read_text().strip() == printed
    assert json.loads(ET.fromstring(printed).findtext("system-out")) == result
