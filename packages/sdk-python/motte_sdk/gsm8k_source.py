"""GSM8K 官方源文件下载与落盘：API 与 CLI 共用的唯一联网点。

离线路径（本地已有 test.jsonl）不需要本模块；测试通过 monkeypatch
``urllib.request.urlopen`` 注入假响应，因此这里始终按模块属性调用。
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

OFFICIAL_SOURCE = "https://github.com/openai/grade-school-math"
REPO_API = "https://api.github.com/repos/openai/grade-school-math"
DATA_PATH = "grade_school_math/data/{split}.jsonl"
RAW_URL = ("https://raw.githubusercontent.com/openai/grade-school-math/{revision}"
          "/grade_school_math/data/{split}.jsonl")
DEFAULT_DATASET_DIR = "var/datasets/gsm8k"
REVISION_PATTERN = re.compile(r"[0-9a-fA-F]{40}")
USER_AGENT = "motteavl-gsm8k-import"


class SourceUnavailable(RuntimeError):
    """官方源不可达或返回非 200；调用方映射为 502 SOURCE_UNAVAILABLE。"""


def official_url(revision: str, *, split: str = "test") -> str:
    if not REVISION_PATTERN.fullmatch(revision or ""):
        raise ValueError("official source revision must be a pinned 40-character commit hash")
    return RAW_URL.format(revision=revision, split=split)


def _get(url: str, *, timeout: float, accept: str) -> bytes:
    request = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise SourceUnavailable(f"source returned HTTP {response.status}: {url}")
            return response.read()
    except OSError as error:
        raise SourceUnavailable(f"download failed: {error}") from error


def fetch_official_jsonl(revision: str, *, split: str = "test", timeout: float = 30.0) -> bytes:
    """按 pinned commit 下载官方 split 原始字节；不做任何解析。"""
    url = official_url(revision, split=split)
    return _get(url, timeout=timeout, accept="application/json")


def _first_sha(payload: Any) -> str | None:
    sha = payload[0].get("sha") if isinstance(payload, list) and payload else None
    return sha if isinstance(sha, str) and REVISION_PATTERN.fullmatch(sha) else None


def _unresolved(reason: str) -> SourceUnavailable:
    return SourceUnavailable(
        f"cannot resolve the latest official commit ({reason}); "
        "pass an explicit 40-character commit instead")


def latest_revision(*, split: str = "test", timeout: float = 30.0) -> str:
    """解析官方仓库中该 split 数据文件的最新 commit（需联网，未认证限额 60 次/小时）。

    先查改动该文件的最后一次提交，查不到（改名/空结果）再退回默认分支 HEAD。源不可达时立即
    失败并提示改用显式 commit（避免在最坏情况下连续等两个超时）。返回 40 位 sha，后续下载与
    provenance 都用它，因此「最新」只是一次解析动作，落库的仍是可复核的固定 revision。
    """
    file_url = f"{REPO_API}/commits?per_page=1&path={DATA_PATH.format(split=split)}"
    try:
        payload = json.loads(_get(file_url, timeout=timeout, accept="application/vnd.github+json"))
    except ValueError:
        payload = None
    except SourceUnavailable as error:
        raise _unresolved(str(error)) from error
    sha = _first_sha(payload)
    if sha:
        return sha
    try:
        payload = json.loads(_get(f"{REPO_API}/commits?per_page=1", timeout=timeout,
                                  accept="application/vnd.github+json"))
    except (ValueError, SourceUnavailable) as error:
        raise _unresolved(str(error)) from error
    sha = _first_sha(payload)
    if sha:
        return sha
    raise _unresolved("unexpected GitHub response")


def dataset_dir(directory: str | Path | None = None) -> Path:
    return Path(directory) if directory is not None else Path(
        os.environ.get("MOTTE_DATASET_DIR", DEFAULT_DATASET_DIR))


def store_source_file(raw: bytes, *, revision: str, split: str = "test",
                      directory: str | Path | None = None) -> Path:
    """按 revision 落盘源文件（受控目录，默认已被 .gitignore 覆盖），便于事后核对题面来源。"""
    target = dataset_dir(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{split}-{revision}.jsonl"
    path.write_bytes(raw)
    return path
