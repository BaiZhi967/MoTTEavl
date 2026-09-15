"""Harness 探测：binary 是否存在、版本号。"""
from __future__ import annotations

import shutil
from typing import Any

from .process import ProcessRunner


def probe_version(name, version=None):
    return {"name": name, "version": version, "available": version is not None}


async def probe_binary(binary: str, *, name: str | None = None, timeout: float = 10.0) -> dict[str, Any]:
    """执行 `<binary> --version` 并解析首行版本；binary 缺失时 available=False。"""
    if shutil.which(binary) is None and "/" not in binary:
        return {"name": name or binary, "version": None, "available": False}
    result = await ProcessRunner(timeout=timeout).run([binary, "--version"])
    version = None
    if result["status"] == "exited" and result["exit_code"] == 0:
        first_line = result["stdout"].strip().splitlines()[0] if result["stdout"].strip() else ""
        version = first_line.split()[-1] if first_line else None
    return {"name": name or binary, "version": version, "available": version is not None}
