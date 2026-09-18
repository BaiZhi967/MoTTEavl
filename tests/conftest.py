"""全局测试隔离。

测试套件必须与开发者本地状态无关：`create_app()` 不传 store 时会走默认 SQLite
（`MOTTE_DB_PATH`，默认 `var/runs.db`），曾把合成夹具数据集与场景写进本地开发库；
下载落盘的 `var/datasets/` 同理。这里在会话级把三个环境变量指到临时目录，
需要真实路径的测试继续用 `--db` / `monkeypatch.setenv` 显式覆盖。
"""
import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_motte_env(tmp_path_factory):
    root = tmp_path_factory.mktemp("motte-env")
    overrides = {
        "MOTTE_DB_PATH": str(root / "runs.db"),
        "MOTTE_DATASET_DIR": str(root / "datasets"),
        "ARTIFACT_ROOT": str(root / "artifacts"),
    }
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    yield root
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
