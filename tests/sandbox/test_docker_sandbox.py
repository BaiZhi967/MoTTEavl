"""DockerSandbox 安全契约测试（离线，FakeDockerClient，零 Docker 依赖）。"""
import pytest

from motte_sandbox.docker import DockerSandbox, UnsupportedOperation
from motte_sandbox.policy import CommandPolicy, PolicyViolationError, SandboxPolicy


class FakeContainer:
    def __init__(self, *, exit_code=0, output=b"ok\n", wait_timeout=None, container_id="cid-1"):
        self.id = container_id
        self._exit_code = exit_code
        self._output = output
        self._wait_timeout = wait_timeout
        self.killed = False
        self.removed = False

    def wait(self, timeout=None):
        if self._wait_timeout is not None:
            raise TimeoutError("wait timed out")
        return {"StatusCode": self._exit_code}

    def logs(self):
        return self._output

    def kill(self):
        self.killed = True

    def remove(self, force=False):
        self.removed = True


class FakeDockerClient:
    def __init__(self, container=None):
        self.container = container or FakeContainer()
        self.run_kwargs = None

    @property
    def containers(self):
        client = self

        class Containers:
            def run(self, image, **kwargs):
                client.run_kwargs = {"image": image, **kwargs}
                return client.container

        return Containers()


def make_sandbox(client, policy=None, tmp_path=None, **policy_kwargs):
    return DockerSandbox(
        policy or SandboxPolicy(**policy_kwargs),
        client=client,
        workspace_root=tmp_path / "sandboxes" if tmp_path else None,
    )


def test_container_security_invariants_are_encoded_in_run_kwargs(tmp_path):
    client = FakeDockerClient()
    sandbox = make_sandbox(client, tmp_path=tmp_path, environment=("SAFE_VAR",))
    import os

    os.environ["SAFE_VAR"] = "visible"
    os.environ["SECRET_LEAK"] = "hidden"
    try:
        result = sandbox.run(["echo", "hi"])
    finally:
        os.environ.pop("SAFE_VAR", None)
        os.environ.pop("SECRET_LEAK", None)

    kwargs = client.run_kwargs
    assert kwargs["network_mode"] == "none"
    assert kwargs["privileged"] is False
    assert kwargs["user"] == "nobody"
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges"]
    assert kwargs["mem_limit"] == "512m"
    assert kwargs["pids_limit"] == 64
    assert kwargs["tmpfs"] == {"/tmp": "size=256m", "/workspace": "size=256m"}
    assert kwargs["environment"] == {"SAFE_VAR": "visible"}  # 未声明变量绝不透传
    assert len(kwargs["volumes"]) == 1  # offline fake compatibility path only
    assert "run-" in next(iter(kwargs["volumes"]))
    assert kwargs["command"] == ["echo", "hi"]
    assert result["status"] == "exited" and result["exit_code"] == 0


def test_policy_rejects_non_none_network_and_privileged():
    with pytest.raises(PolicyViolationError, match="network"):
        SandboxPolicy(network="bridge")
    with pytest.raises(PolicyViolationError, match="privileged"):
        SandboxPolicy(privileged=True)
    with pytest.raises(PolicyViolationError, match="escape"):
        SandboxPolicy(workspace="/etc")
    with pytest.raises(PolicyViolationError, match="positive"):
        SandboxPolicy(memory_mb=0)


def test_command_policy_blocks_shell_strings_and_dangerous_programs():
    policy = SandboxPolicy(commands=CommandPolicy(allowed_programs=("python", "pytest")))
    with pytest.raises(PolicyViolationError, match="argv"):
        policy.commands.validate_command("echo hi")  # shell 字符串
    with pytest.raises(PolicyViolationError, match="argv"):
        policy.commands.validate_command([])
    with pytest.raises(PolicyViolationError, match="forbidden"):
        policy.commands.validate_command(["docker", "ps"])
    with pytest.raises(PolicyViolationError, match="not allowed"):
        policy.commands.validate_command(["curl", "http://x"])
    assert policy.commands.validate_command(["python", "-c", "print(1)"]) == ["python", "-c", "print(1)"]


def test_timeout_kills_container_and_cleanup_always_runs(tmp_path):
    client = FakeDockerClient(FakeContainer(wait_timeout=True))
    sandbox = make_sandbox(client, tmp_path=tmp_path, timeout_seconds=1)
    result = sandbox.run(["sleep", "100"])
    assert result["status"] == "timeout"
    assert client.container.killed
    assert client.container.removed
    cleanup_events = [event["type"] for event in sandbox.events()]
    assert cleanup_events[-1] == "cleaned_up"
    assert "timed_out" in cleanup_events


def test_policy_violation_in_start_still_runs_cleanup(tmp_path):
    client = FakeDockerClient()
    sandbox = make_sandbox(client, tmp_path=tmp_path)
    try:
        sandbox.prepare()
        with pytest.raises(PolicyViolationError):
            sandbox.start(["sudo", "rm", "-rf", "/"])
    finally:
        report = sandbox.cleanup()
    assert client.run_kwargs is None  # 从未创建容器
    assert report["workspace_removed"] is True
    assert sandbox.events()[-1]["type"] == "cleaned_up"


def test_prepare_materializes_files_and_collects_artifacts(tmp_path):
    client = FakeDockerClient(FakeContainer(output=b"done"))

    def write_output(workspace):
        (workspace / "result.txt").write_text("42", encoding="utf-8")

    sandbox = make_sandbox(client, tmp_path=tmp_path)
    sandbox.prepare({"input.txt": "hello"})
    write_output(sandbox._host_workspace)
    sandbox.start(["python", "solve.py"])
    result = sandbox.collect()
    names = [artifact["name"] for artifact in result["artifacts"]]
    assert names == ["input.txt", "result.txt"]
    assert result["artifacts"][1]["sha256"]
    sandbox.cleanup()


def test_prepare_rejects_file_path_escape(tmp_path):
    sandbox = make_sandbox(FakeDockerClient(), tmp_path=tmp_path)
    with pytest.raises(PolicyViolationError, match="escape"):
        sandbox.prepare({"../escape.txt": "x"})


def test_output_is_capped_to_policy_limit(tmp_path):
    big_output = b"x" * 5000
    client = FakeDockerClient(FakeContainer(output=big_output))
    sandbox = make_sandbox(client, tmp_path=tmp_path, max_output_bytes=100)
    result = sandbox.run(["cat", "/dev/zero"])
    assert len(result["output"]) == 100
    assert result["output"] == "x" * 100


def test_send_is_unsupported_and_interrupt_is_idempotent(tmp_path):
    sandbox = make_sandbox(FakeDockerClient(), tmp_path=tmp_path)
    with pytest.raises(UnsupportedOperation):
        sandbox.send("line")
    assert sandbox.interrupt() == {"interrupted": False}


def test_workspaces_are_unique_across_instances_and_repeated_prepare(tmp_path):
    first = make_sandbox(FakeDockerClient(), tmp_path=tmp_path)
    second = make_sandbox(FakeDockerClient(), tmp_path=tmp_path)
    first.prepare({"same.txt": "first"})
    second.prepare({"same.txt": "second"})
    assert first._host_workspace != second._host_workspace
    assert (first._host_workspace / "same.txt").read_text() == "first"
    assert (second._host_workspace / "same.txt").read_text() == "second"
    first.cleanup()
    assert second._host_workspace.exists()
    second.cleanup()
    first.prepare({"again.txt": "new"})
    assert first._host_workspace.exists()
    first.cleanup()


def test_artifact_symlink_is_rejected_without_reading_target(tmp_path):
    sandbox = make_sandbox(FakeDockerClient(), tmp_path=tmp_path)
    sandbox.prepare()
    victim = tmp_path / "victim.txt"
    victim.write_text("secret", encoding="utf-8")
    link = sandbox._host_workspace / "link.txt"
    try:
        link.symlink_to(victim)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    sandbox.start(["echo", "ok"])
    with pytest.raises(PolicyViolationError, match="symlink"):
        sandbox.collect()
    sandbox.cleanup()


def test_artifact_limits_are_checked_before_file_read(tmp_path):
    client = FakeDockerClient()
    sandbox = make_sandbox(client, tmp_path=tmp_path, max_artifacts=1, max_artifact_bytes=3,
                           max_total_artifact_bytes=3)
    sandbox.prepare()
    (sandbox._host_workspace / "too-big.txt").write_bytes(b"1234")
    sandbox.start(["echo", "ok"])
    with pytest.raises(PolicyViolationError, match="bytes"):
        sandbox.collect()
    sandbox.cleanup()


def test_artifact_count_limit_is_checked_before_reads(tmp_path):
    sandbox = make_sandbox(FakeDockerClient(), tmp_path=tmp_path, max_artifacts=1)
    sandbox.prepare()
    (sandbox._host_workspace / "one.txt").write_bytes(b"1")
    (sandbox._host_workspace / "two.txt").write_bytes(b"2")
    sandbox.start(["echo", "ok"])
    with pytest.raises(PolicyViolationError, match="count"):
        sandbox.collect()
    sandbox.cleanup()


def test_artifact_total_limit_is_checked_before_reads(tmp_path):
    sandbox = make_sandbox(FakeDockerClient(), tmp_path=tmp_path, max_artifact_bytes=3,
                           max_total_artifact_bytes=3)
    sandbox.prepare()
    (sandbox._host_workspace / "one.txt").write_bytes(b"12")
    (sandbox._host_workspace / "two.txt").write_bytes(b"34")
    sandbox.start(["echo", "ok"])
    with pytest.raises(PolicyViolationError, match="total"):
        sandbox.collect()
    sandbox.cleanup()


class StreamLogContainer(FakeContainer):
    def logs(self, **kwargs):
        assert kwargs.get("stream") is True
        return iter((b"a" * 100, b"b" * 100, b"c" * 100))


def test_logs_are_consumed_as_bounded_stream(tmp_path):
    sandbox = make_sandbox(FakeDockerClient(StreamLogContainer()), tmp_path=tmp_path,
                           max_output_bytes=100)
    result = sandbox.run(["echo", "ok"])
    assert result["output"] == "c" * 100


def test_adapter_contract_methods_exist():
    sandbox = DockerSandbox()
    for method in ("prepare", "start", "send", "events", "interrupt", "collect", "cleanup"):
        assert callable(getattr(sandbox, method))
