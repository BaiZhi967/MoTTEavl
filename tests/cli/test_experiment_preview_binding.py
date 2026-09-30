"""Preview-bound experiment creation through local and real HTTP CLI paths."""
from __future__ import annotations

import json

import pytest

from motte_cli.main import main
from motte_storage.factory import create_run_store, default_content_store
from motte_storage.resource_store import SQLiteResourceStore
from tests.cli.test_m6_cli import exp_db, experiment_spec


@pytest.fixture(params=["local", "server"])
def experiment_cli(request, capsys, monkeypatch):
    db_path = request.getfixturevalue("exp_db")
    store = create_run_store(str(db_path))
    resources = SQLiteResourceStore(str(db_path), content_store=default_content_store())
    if request.param == "server":
        from apps.api.app.main import create_app
        from motte_sdk import MotteClient
        from tests.sdk.sync_asgi import SyncASGITransport

        app = create_app(store, resources)
        monkeypatch.setattr(
            "motte_cli.remote.build_client",
            lambda args: MotteClient(
                "http://testserver", transport=SyncASGITransport(app), retries=0,
            ),
        )
        mode_args = ["--mode", "server", "--api-url", "http://testserver"]
    else:
        mode_args = ["--mode", "local", "--db", str(db_path)]
    spec = json.dumps(experiment_spec())

    def invoke(command, *args):
        try:
            code = main(["experiment", command, "--spec", spec, *mode_args, *args])
        except SystemExit as error:
            code = error.code
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return invoke, store, resources


def test_experiment_create_cli_binds_preview_and_exports_receipt(experiment_cli):
    invoke, store, _resources = experiment_cli
    code, out, err = invoke("preview")
    assert code == 0, err
    preview_hash = json.loads(out)["preview_hash"]

    code, out, err = invoke("create", "--preview-hash", preview_hash)

    assert code == 0, err
    created = json.loads(out)
    assert created["preview_hash"] == preview_hash
    assert created["preflight_mode"] == "preview_bound"
    assert created["allocated"] == 2
    assert created["failed"] == []
    assert len(store.runs.list()) == 2


def test_experiment_create_cli_rejects_stale_preview_before_writes(experiment_cli):
    invoke, store, resources = experiment_cli
    code, out, err = invoke("preview")
    assert code == 0, err
    preview_hash = json.loads(out)["preview_hash"]
    model = resources.models.get("probe")
    resources.models.put({**model, "model": "probe-2"})

    code, out, err = invoke("create", "--preview-hash", preview_hash)

    assert code == 2
    assert out == ""
    assert json.loads(err)["error"]["code"] == "PREVIEW_STALE"
    assert store.experiments.list_specs() == []
    assert store.experiments.list_cells("exp-cli", "1") == []
    assert store.runs.list() == []


def test_experiment_create_cli_without_hash_exports_revalidation_receipt(experiment_cli):
    invoke, _store, _resources = experiment_cli

    code, out, err = invoke("create")

    assert code == 0, err
    created = json.loads(out)
    assert created["preview_hash"].startswith("sha256:")
    assert created["preflight_mode"] == "create_revalidated"
    assert created["failed"] == []
