"""CLI comparison and statistics must describe the same immutable Pass pair."""
from __future__ import annotations

import json

import pytest

from motte_cli.main import main
from motte_sdk.comparisons import ComparisonService
from motte_storage.factory import create_run_store
from tests.cli.test_m6_cli import seed_pass, seed_run


@pytest.mark.parametrize("mode", ["local", "server"])
@pytest.mark.parametrize("explicit_passes", [(), ("baseline",), ("candidate",),
                                           ("baseline", "candidate")])
def test_compare_statistics_pins_first_result_when_current_moves(
    tmp_path, capsys, monkeypatch, mode, explicit_passes,
):
    db_path = tmp_path / "comparison.db"
    store = create_run_store(str(db_path))
    for side, values in [("baseline", {"a": True, "b": True}),
                         ("candidate", {"a": False, "b": True})]:
        seed_run(store, side, ["a", "b"])
        seed_pass(store, side, f"old-{side}", values)

    original_compare = ComparisonService.compare
    moved = False

    def compare_then_rescore(self, *args, **kwargs):
        nonlocal moved
        result = original_compare(self, *args, **kwargs)
        if not moved:
            moved = True
            # Deterministic rescore at the boundary between the CLI's two reads.
            # The real stores and real statistics still resolve every Pass.
            seed_pass(store, "baseline", "new-baseline", {"a": False, "b": False})
            seed_pass(store, "candidate", "new-candidate", {"a": True, "b": True})
        return result

    monkeypatch.setattr(ComparisonService, "compare", compare_then_rescore)
    transport = None
    if mode == "server":
        from apps.api.app.main import create_app
        from motte_sdk import MotteClient
        from tests.sdk.sync_asgi import SyncASGITransport

        transport = SyncASGITransport(create_app(store=store))
        monkeypatch.setattr(
            "motte_cli.remote.build_client",
            lambda args: MotteClient("http://testserver", transport=transport, retries=0),
        )
        mode_args = ["--mode", "server", "--api-url", "http://testserver"]
    else:
        mode_args = ["--mode", "local", "--db", str(db_path)]
    pass_args = [arg for side in explicit_passes
                 for arg in (f"--{side}-pass", f"old-{side}")]
    output = tmp_path / "comparison.json"
    try:
        code = main([
            "compare", "--baseline", "baseline", "--candidate", "candidate",
            "--statistics", "--json", str(output), *mode_args, *pass_args,
        ])
        captured = capsys.readouterr()
        assert code == 0, captured.err
        payload = json.loads(captured.out)
        assert moved
        assert store.scoring_passes.current("baseline")["id"] == "new-baseline"
        assert store.scoring_passes.current("candidate")["id"] == "new-candidate"
        assert payload["refs"]["baseline"]["scoring_pass_id"] == "old-baseline"
        assert payload["refs"]["candidate"]["scoring_pass_id"] == "old-candidate"
        assert payload["statistics"]["refs"] == payload["refs"]
        assert payload["statistics"]["statistics"]["mean_diff"] == -0.5
        assert json.loads(output.read_text(encoding="utf-8")) == payload
    finally:
        if transport is not None:
            transport.close()
