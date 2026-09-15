"""真实 Docker Sandbox 冒烟：仅设置 MOTTE_SANDBOX_LIVE=1 时运行（操作者显式启动）。"""
import os

import pytest

from motte_sandbox.docker import DockerSandbox
from motte_sandbox.policy import SandboxPolicy

pytestmark = pytest.mark.skipif(
    os.environ.get("MOTTE_SANDBOX_LIVE") != "1", reason="set MOTTE_SANDBOX_LIVE=1 to run live sandbox tests"
)


def _sandbox(tmp_path, **kwargs):
    return DockerSandbox(SandboxPolicy(**kwargs), workspace_root=tmp_path / "sandboxes")


def test_live_echo_completes_and_cleans_up(tmp_path):
    sandbox = _sandbox(tmp_path, timeout_seconds=30)
    result = sandbox.run(["echo", "live-ok"])
    assert result["status"] == "exited"
    assert result["exit_code"] == 0
    assert "live-ok" in result["output"]
    assert sandbox.events()[-1]["type"] == "cleaned_up"


def test_live_network_is_denied_by_default(tmp_path):
    sandbox = _sandbox(tmp_path, timeout_seconds=30)
    result = sandbox.run(["sh", "-c", "wget -q -T 3 http://example.com || nc -z -w 3 example.com 80"])
    assert result["status"] == "exited"
    assert result["exit_code"] != 0  # 无网络下任何出站尝试都失败


def test_live_timeout_kills_and_cleans_up(tmp_path):
    sandbox = _sandbox(tmp_path, timeout_seconds=3)
    result = sandbox.run(["sleep", "60"])
    assert result["status"] == "timeout"
    assert sandbox.events()[-1]["type"] == "cleaned_up"


def test_live_workspace_artifacts_are_collected(tmp_path):
    sandbox = _sandbox(tmp_path, timeout_seconds=30)
    result = sandbox.run(["sh", "-c", "echo 42 > answer.txt"], files={"task.txt": "task"})
    assert result["status"] == "exited"
    names = [artifact["name"] for artifact in result["artifacts"]]
    assert names == ["answer.txt", "task.txt"]
