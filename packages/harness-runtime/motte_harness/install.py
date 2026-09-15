"""本地 CLI 安装检测：是否安装、路径、版本、安装来源（npm/homebrew/local/system）。

来源判定基于 realpath 的路径启发式（npm 全局安装通常是把 symlink 放进
/usr/local/bin 指向 node_modules，realpath 能还原真实位置）。
"""
from __future__ import annotations

import os
import re
import shutil
from typing import Any

from .process import ProcessRunner

_SEMVER = re.compile(r"\d+\.\d+(?:\.\d+)?(?:[-+][\w.]+)?")

_SOURCE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "npm",
        ("/node_modules/", "/.npm/", "/npm-global/", "/.volta/", "/.nvm/", "/pnpm/", "/.pnpm/", "/.fnm/"),
    ),
    ("homebrew", ("/opt/homebrew/", "/usr/local/Cellar/", "/linuxbrew/")),
    (
        "local",
        ("/.local/", "/.claude/", "/.codex/", "/.cargo/", "/go/bin/"),
    ),
    ("system", ("/usr/bin/", "/usr/local/bin/", "/bin/", "/sbin/")),
)


def classify_source(path: str) -> str:
    """按解析后的真实路径判定安装来源。"""
    real = os.path.realpath(path)
    for source, markers in _SOURCE_RULES:
        if any(marker in real for marker in markers):
            return source
    return "unknown"


def _parse_version(stdout: str) -> str | None:
    """从 --version 输出提取语义化版本号（如 "2.1.0 (Claude Code)" → "2.1.0"）。"""
    first_line = stdout.strip().splitlines()[0] if stdout.strip() else ""
    match = _SEMVER.search(first_line)
    if match:
        return match.group(0)
    return first_line.split()[-1] if first_line else None


async def inspect_installation(
    binary: str,
    *,
    name: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """检测一个 CLI 的本地安装情况（不抛异常，缺失即 installed=False）。"""
    report: dict[str, Any] = {
        "name": name or binary,
        "binary": binary,
        "installed": False,
        "path": None,
        "realpath": None,
        "source": None,
        "version": None,
        "version_ok": False,
        "error": None,
    }
    path = shutil.which(binary)
    if path is None:
        return report
    report.update(
        installed=True,
        path=path,
        realpath=os.path.realpath(path),
        source=classify_source(path),
    )
    result = await ProcessRunner(timeout=timeout).run([path, "--version"])
    if result["status"] == "exited" and result["exit_code"] == 0:
        report["version"] = _parse_version(result["stdout"])
        report["version_ok"] = report["version"] is not None
        report["error"] = None if report["version_ok"] else "version output was empty"
    else:
        report["error"] = (
            f"--version failed: status={result['status']} exit_code={result['exit_code']} "
            f"stderr={result['stderr'].strip()[:120]}"
        )
    return report
