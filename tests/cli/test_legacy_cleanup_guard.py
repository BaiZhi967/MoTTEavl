"""The old unbound TTL command cannot bypass reference-aware GC safeguards."""
import json
import os
import time

from motte_cli.main import main


def test_cleanup_apply_is_a_structured_rejection_and_keeps_files(tmp_path, capsys):
    root = tmp_path / "artifacts"
    root.mkdir()
    evidence = root / "old.bin"
    evidence.write_bytes(b"precious evidence")
    old = time.time() - 365 * 86400
    os.utime(evidence, (old, old))

    exit_code = main(["cleanup-artifacts", "--artifacts-root", str(root),
                      "--older-than-days", "7", "--apply", "--mode", "local"])

    captured = capsys.readouterr()
    assert exit_code == 2
    error = json.loads(captured.err)
    assert error["error"]["code"] == "CLEANUP_UNSUPPORTED"
    assert "motte gc" in error["error"]["message"]
    assert evidence.read_bytes() == b"precious evidence"


def test_cleanup_diagnostic_dry_run_remains_available(tmp_path, capsys):
    root = tmp_path / "artifacts"
    root.mkdir()
    evidence = root / "old.bin"
    evidence.write_bytes(b"precious evidence")
    old = time.time() - 365 * 86400
    os.utime(evidence, (old, old))

    assert main(["cleanup-artifacts", "--artifacts-root", str(root),
                 "--older-than-days", "7", "--mode", "local"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert [row["name"] for row in report["deleted"]] == [evidence.name]
    assert evidence.exists()
