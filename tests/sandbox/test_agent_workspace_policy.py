"""M1-T06：受控 workspace——路径穿越 / symlink / 设备文件 / 配额 / Case 隔离。

验收对应：A07（配额耗尽后清理可查）、A12（同一路径名跨 Case 不互相污染）、
G14（gold 与隐藏断言不进入工具可见空间——由 backend 侧保证，这里测 workspace 不含额外文件）。
"""
from __future__ import annotations

import os
import stat

import pytest

from motte_sandbox.workspace import (
    CaseWorkspace,
    WorkspacePolicyError,
    WorkspaceQuotas,
)


def test_workspace_escape_and_quota_rejection(tmp_path):
    workspace = CaseWorkspace(tmp_path / "run-1" / "case-1")
    workspace.write_text("input.json", "[1, 2]")

    # 路径穿越与形状拒绝
    for bad in ("../outside.txt", "/etc/passwd", "a\\b.txt", "", "..", "a/../../x", "." * 600):
        with pytest.raises(WorkspacePolicyError):
            workspace._safe_target(bad)
    with pytest.raises(WorkspacePolicyError):
        workspace.write_text("../escape.txt", "x")

    # 指向宿主的 symlink：读和写都拒绝
    outside = tmp_path / "host-secret.txt"
    outside.write_text("secret")
    link = workspace.root / "secret-link"
    os.symlink(outside, link)
    with pytest.raises(WorkspacePolicyError, match="symlink"):
        workspace.read_text("secret-link")
    with pytest.raises(WorkspacePolicyError, match="symlink"):
        workspace.write_text("secret-link", "tamper")

    # workspace 内部自建 symlink 同样拒绝（防 TOCTOU 链接替换）
    inside_link = workspace.root / "self-link"
    os.symlink(workspace.root / "input.json", inside_link)
    with pytest.raises(WorkspacePolicyError, match="symlink"):
        workspace.read_text("self-link")

    # 设备 / 管道文件拒绝
    fifo = workspace.root / "pipe"
    if hasattr(os, "mkfifo"):
        os.mkfifo(fifo)
        with pytest.raises(WorkspacePolicyError):
            workspace.read_text("pipe")
        with pytest.raises(WorkspacePolicyError):
            workspace.write_text("pipe", "x")

    # 配额：单文件超限、总量超限、文件数超限分别可区分
    tiny = CaseWorkspace(
        tmp_path / "run-1" / "case-quotas",
        quotas=WorkspaceQuotas(max_file_bytes=20, max_total_bytes=20, max_files=2),
    )
    tiny.write_text("a.txt", "12345")
    with pytest.raises(WorkspacePolicyError) as exc:
        tiny.write_text("b.txt", "x" * 21)
    assert exc.value.code == "quota_file_bytes"
    with pytest.raises(WorkspacePolicyError) as exc:
        tiny.write_text("b.txt", "x" * 16)
    assert exc.value.code == "quota_total_bytes"
    tiny.write_text("b.txt", "1234567890")  # 5 + 10 = 15 <= 20，允许
    with pytest.raises(WorkspacePolicyError) as exc:
        tiny.write_text("c.txt", "1")  # 总量 16 <= 20，但文件数达到上限
    assert exc.value.code == "quota_file_count"
    # 配额拒绝后已有文件仍可读，snapshot 可用，清理结果可查
    assert tiny.read_text("a.txt") == "12345"
    snapshot = tiny.snapshot()
    assert snapshot["complete"] is True
    assert set(snapshot["files"]) == {"a.txt", "b.txt"}
    cleanup = tiny.cleanup()
    assert cleanup == {"status": "success", "residual": []}


def test_same_path_names_do_not_cross_cases(tmp_path):
    case_a = CaseWorkspace(tmp_path / "run-1" / "case-a")
    case_b = CaseWorkspace(tmp_path / "run-1" / "case-b")
    case_a.write_text("report.json", "from-a")
    case_b.write_text("report.json", "from-b")
    assert case_a.read_text("report.json") == "from-a"
    assert case_b.read_text("report.json") == "from-b"
    # case-a 的穿越不能到达 case-b
    with pytest.raises(WorkspacePolicyError):
        case_a.write_text("../case-b/report.json", "tamper")


def test_fixture_materialize_and_read_roundtrip(tmp_path):
    workspace = CaseWorkspace(tmp_path / "run-1" / "case-1")
    workspace.materialize_fixture({
        "input.json": '{"items": [{"enabled": true}]}',
        "notes/readme.md": "# notes",
    })
    assert '"enabled": true' in workspace.read_text("input.json")
    assert workspace.read_text("notes/readme.md") == "# notes"
    files = workspace.list_files()
    assert files == ["input.json", "notes/readme.md"]
    assert workspace.total_bytes() > 0


def test_cleanup_failure_reports_residual(tmp_path, monkeypatch):
    workspace = CaseWorkspace(tmp_path / "run-1" / "case-1")
    workspace.write_text("a.txt", "x")

    import shutil as shutil_module

    def broken_rmtree(path, *args, **kwargs):  # noqa: ANN001
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr(shutil_module, "rmtree", broken_rmtree)
    result = workspace.cleanup()
    assert result["status"] == "failed"
    assert result["residual"] == ["a.txt"]
    assert "simulated cleanup failure" in result["error"]


def test_regular_dir_components_are_fine(tmp_path):
    workspace = CaseWorkspace(tmp_path / "run-1" / "case-1")
    workspace.write_text("deep/nested/file.txt", "ok")
    assert workspace.read_text("deep/nested/file.txt") == "ok"
    info = (workspace.root / "deep" / "nested" / "file.txt").stat()
    assert stat.S_ISREG(info.st_mode)
