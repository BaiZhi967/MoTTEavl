import asyncio
import os

from motte_harness.claude import ClaudeHarness
from motte_harness.codex import CodexHarness
from motte_harness.install import classify_source, inspect_installation


def test_classify_source_by_realpath_markers():
    # 用不存在的路径，避免本机真实安装（realpath 解析）干扰判定
    assert classify_source("/opt/homebrew/bin/not-installed-xyz") == "homebrew"
    assert classify_source("/usr/local/Cellar/not-installed-xyz/1.0/bin/x") == "homebrew"
    assert classify_source("/home/u/.nvm/versions/node/v24/bin/not-installed-xyz") == "npm"
    assert classify_source("/home/u/.local/bin/not-installed-xyz") == "local"
    assert classify_source("/usr/bin/not-installed-xyz") == "system"
    assert classify_source("/tmp/somewhere/not-installed-xyz") == "unknown"


def test_inspect_reports_missing_binary_as_not_installed():
    report = asyncio.run(inspect_installation("definitely-missing-cli-xyz", name="demo"))
    assert report == {
        "name": "demo",
        "binary": "definitely-missing-cli-xyz",
        "installed": False,
        "path": None,
        "realpath": None,
        "source": None,
        "version": None,
        "version_ok": False,
        "error": None,
    }


def _write_script(path, body):
    if os.name == "nt":
        path = path.with_suffix(".py")
        lines = []
        # The fixtures only need --version success/failure behavior.
        if "exit 1" in body:
            lines.append("import sys; print('boom', file=sys.stderr); sys.exit(1)")
        else:
            version = "3.0.0" if "3.0.0" in body else "2.1.0 (Claude Code)"
            lines.append(f"import sys; print({version!r})")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
    return path


def test_inspect_reports_version_and_runnable(tmp_path):
    binary = tmp_path / "fake-claude"
    binary = _write_script(binary, 'if [ "$1" = "--version" ]; then echo "2.1.0 (Claude Code)"; exit 0; fi')
    report = asyncio.run(inspect_installation(str(binary), name="claude-cli"))
    assert report["installed"] is True
    assert report["version"] == "2.1.0"  # 提取 semver，忽略尾注
    assert report["version_ok"] is True
    harness_report = asyncio.run(ClaudeHarness(binary=str(binary)).inspect())
    assert harness_report["runnable"] is True
    assert harness_report["name"] == "claude-cli"


def test_inspect_flags_broken_installation(tmp_path):
    binary = tmp_path / "broken-cli"
    binary = _write_script(binary, 'if [ "$1" = "--version" ]; then echo "boom" >&2; exit 1; fi')
    report = asyncio.run(CodexHarness(binary=str(binary)).inspect())
    assert report["installed"] is True
    assert report["version"] is None
    assert report["version_ok"] is False
    assert report["runnable"] is False
    assert "--version failed" in report["error"]


def test_inspect_resolves_npm_symlink_source(tmp_path, monkeypatch):
    node_modules = tmp_path / "lib" / "node_modules" / "@vendor" / "cli" / "bin" / "cli.js"
    node_modules.parent.mkdir(parents=True)
    node_modules = _write_script(node_modules, 'if [ "$1" = "--version" ]; then echo "cli 3.0.0"; fi')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    link = bin_dir / "vendor-cli"
    if os.name == "nt":
        # Windows symlink creation needs elevated developer mode; exercise the
        # same npm realpath classification with the target path directly.
        link = node_modules
    else:
        os.symlink(node_modules, link)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    report = asyncio.run(inspect_installation(str(link) if os.name == "nt" else "vendor-cli"))
    assert report["installed"] is True
    assert report["path"] == str(link)
    assert report["source"] == "npm"
    assert report["version"] == "3.0.0"
