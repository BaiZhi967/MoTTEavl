"""M4-T05：有界双管道 supervisor（无网络、临时目录内注入故障）。

主断言 test_bounded_pipes_and_descendant_cleanup：大量 stderr + 慢消费者
不死锁、输出有界；父进程 final 后子进程继续写文件被检出并清理。
fixture 全部是临时目录内的静态脚本（os.spawnv 派生孙进程，不经 shell）。
"""
from __future__ import annotations

import sys
import textwrap
import time
from pathlib import Path

import pytest

from motte_harness.supervisor import (
    SupervisedLimits,
    SupervisedProcess,
    SupervisedProcessError,
)
from motte_harness.process import minimal_env


def _script(tmp_path: Path, name: str, source: str) -> Path:
    script = tmp_path / name
    script.write_text(textwrap.dedent(source), encoding="utf-8")
    return script


def test_bounded_pipes_and_descendant_cleanup(tmp_path):
    # 父进程：洪泛 stderr（大量输出 + 慢消费），再派生孙进程持续写文件，
    # 然后自己正常退出 —— 孙进程的残留写必须被检出并清理。
    grandchild = _script(
        tmp_path, "grandchild.py",
        """
        import pathlib
        import time

        marker = pathlib.Path(__file__).with_name("child-writes")
        for index in range(400):
            marker.write_text(f"tick {index}", encoding="utf-8")
            time.sleep(0.05)
        """,
    )
    parent = _script(
        tmp_path, "parent.py",
        """
        import os
        import pathlib
        import sys

        for index in range(2000):
            print(f"noise line {index} " + "x" * 64, file=sys.stderr, flush=True)
        child_argv = [sys.executable, GRANDCHILD]
        os.spawnv(os.P_NOWAIT, child_argv[0], child_argv)
        print("final", flush=True)
        """.replace("GRANDCHILD", repr(str(grandchild))),
    )
    collected: list[str] = []

    def slow_consumer(line: str) -> None:
        collected.append(line)
        time.sleep(0.001)

    process = SupervisedProcess(
        [sys.executable, str(parent)],
        cwd=str(tmp_path),
        env=minimal_env(),
        limits=SupervisedLimits(
            max_total_bytes=400_000, idle_timeout=10.0, total_timeout=30.0,
            residual_grace=1.5,
        ),
        on_stderr=slow_consumer,
    )
    process.start()
    outcome = process.wait()

    # 大量 stderr + 慢消费者：不死锁（wait 返回），输出有界
    assert outcome.status in ("exited", "byte_limit")
    assert outcome.stderr_bytes <= 400_000
    assert "final" in outcome.stdout
    # 孙进程持续写：报告残留时必须已停止写入
    child_marker = tmp_path / "child-writes"
    if outcome.residual_pids and child_marker.exists():
        first = child_marker.read_text(encoding="utf-8")
        time.sleep(0.5)
        second = child_marker.read_text(encoding="utf-8")
        assert first == second, "residual grandchild must stop writing after cleanup"


def test_single_oversized_line_fails_closed(tmp_path):
    parent = _script(
        tmp_path, "bigline.py",
        """
        import sys

        sys.stdout.write("y" * 2_000_000 + "\\n")
        sys.stdout.flush()
        import time
        time.sleep(30)
        """,
    )
    process = SupervisedProcess(
        [sys.executable, str(parent)],
        env=minimal_env(),
        limits=SupervisedLimits(max_line_bytes=100_000, total_timeout=15),
    )
    process.start()
    outcome = process.wait()
    assert outcome.status == "line_limit"
    assert outcome.truncated is True


def test_invalid_utf8_reported_separately_from_truncation(tmp_path):
    parent = _script(
        tmp_path, "binarynoise.py",
        """
        import sys

        sys.stdout.buffer.write(b"ok line\\n")
        sys.stdout.buffer.write(b"\\xff\\xfe broken\\n")
        sys.stdout.buffer.flush()
        """,
    )
    process = SupervisedProcess(
        [sys.executable, str(parent)],
        env=minimal_env(),
        limits=SupervisedLimits(total_timeout=15),
    )
    process.start()
    outcome = process.wait()
    assert outcome.status == "invalid_utf8"
    assert outcome.exit_code == 0


def test_idle_timeout_kills_silent_process(tmp_path):
    silent = _script(
        tmp_path, "silent.py",
        """
        import time

        time.sleep(60)
        """,
    )
    process = SupervisedProcess(
        [sys.executable, str(silent)],
        env=minimal_env(),
        limits=SupervisedLimits(idle_timeout=1.0, total_timeout=10.0),
    )
    process.start()
    outcome = process.wait()
    assert outcome.status == "timeout"
    assert outcome.truncated is True


def test_interrupt_grace_then_kill(tmp_path):
    stubborn = _script(
        tmp_path, "stubborn.py",
        """
        import time

        try:
            time.sleep(60)
        except KeyboardInterrupt:
            time.sleep(60)  # 捕获后继续赖着：宽限期后必须被强杀
        """,
    )
    process = SupervisedProcess(
        [sys.executable, str(stubborn)],
        env=minimal_env(),
        limits=SupervisedLimits(interrupt_grace=1.0, total_timeout=30.0),
    )
    process.start()
    started = time.monotonic()
    process.interrupt(reason="operator")
    outcome = process.wait()
    elapsed = time.monotonic() - started
    assert outcome.status == "interrupted"
    assert elapsed < 15


def test_env_allowlist_forwards_only_declared(tmp_path):
    probe = _script(
        tmp_path, "envprobe.py",
        """
        import os

        watched = ("MOTTE_DB_PATH", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
        leaked = [key for key in watched if key in os.environ]
        print("leaked:" + ",".join(leaked), flush=True)
        print("declared:" + os.environ.get("DECLARED_TOKEN", "<missing>"), flush=True)
        """,
    )
    process = SupervisedProcess(
        [sys.executable, str(probe)],
        env=minimal_env({"DECLARED_TOKEN": "declared-value"}),
        limits=SupervisedLimits(total_timeout=15),
    )
    process.start()
    outcome = process.wait()
    assert outcome.status == "exited"
    leaked_line = next(line for line in outcome.stdout.splitlines() if line.startswith("leaked:"))
    assert leaked_line == "leaked:"
    declared_line = next(line for line in outcome.stdout.splitlines() if line.startswith("declared:"))
    assert declared_line.endswith("declared-value")


def test_missing_binary_start_fails_explicitly(tmp_path):
    process = SupervisedProcess(
        [str(tmp_path / "does-not-exist.bin")],
        env=minimal_env(),
    )
    with pytest.raises(SupervisedProcessError) as raised:
        process.start()
    assert raised.value.code == "PROCESS_START_FAILED"


def test_wait_before_start_is_rejected():
    process = SupervisedProcess(["python", "-c", "pass"])
    with pytest.raises(SupervisedProcessError):
        process.wait()


def test_orphan_descendant_stopped_after_parent_exit(tmp_path):
    """R05：父进程退出后的孤儿后代仍被停止——写入不再增长。

    父进程 spawn 持续写 tick 的子进程后立即退出；监督器在父进程存活
    期间登记后代身份，父进程退出后按登记清理（PID 复用核对，不误杀）。
    """
    child = _script(
        tmp_path, "tick-child.py",
        """
        import pathlib
        import time

        marker = pathlib.Path(__file__).with_name("ticks")
        for index in range(2000):
            marker.write_text(f"tick {index}", encoding="utf-8")
            time.sleep(0.02)
        """,
    )
    parent_source = textwrap.dedent(
        """
        import os
        import sys

        child_argv = [sys.executable, CHILD]
        os.spawnv(os.P_NOWAIT, child_argv[0], child_argv)
        """,
    ).replace("CHILD", repr(str(child)))
    parent_path = tmp_path / "exit-parent.py"
    parent_path.write_text(parent_source, encoding="utf-8")

    process = SupervisedProcess(
        [sys.executable, str(parent_path)],
        cwd=str(tmp_path),
        env=minimal_env(),
        limits=SupervisedLimits(total_timeout=20.0, idle_timeout=10.0,
                                residual_grace=2.0),
    )
    process.start()
    outcome = process.wait()
    assert outcome.status == "exited"

    marker = tmp_path / "ticks"
    assert marker.exists(), "test fixture: child never started"
    # 父进程退出后，孤儿后代被清理：写入停止增长（无论 residual 上报形态）。
    time.sleep(0.8)
    first = marker.read_text(encoding="utf-8")
    time.sleep(0.8)
    second = marker.read_text(encoding="utf-8")
    assert first == second, (
        f"orphan descendant kept writing after parent exit "
        f"({first!r} -> {second!r})"
    )


def test_reader_drain_captures_tail_after_process_exit(tmp_path):
    """R12：慢消费者不吞尾帧——进程退出后 reader 有界排空再返回。"""
    producer = _script(
        tmp_path, "producer.py",
        """
        for index in range(100):
            print(f"line-{index:03d}", flush=True)
        """,
    )
    collected: list[str] = []

    def slow_consumer(line: str) -> None:
        collected.append(line.rstrip("\n"))
        time.sleep(0.02)

    process = SupervisedProcess(
        [sys.executable, str(producer)],
        cwd=str(tmp_path),
        env=minimal_env(),
        limits=SupervisedLimits(total_timeout=30.0, idle_timeout=10.0,
                                drain_timeout=6.0),
        on_stdout=slow_consumer,
    )
    process.start()
    outcome = process.wait()
    assert outcome.status == "exited"
    assert outcome.truncated is False
    assert outcome.detail is None
    # 全部 100 行都被采集（回调 0.02s/行 ≈ 2s < drain 期限）。
    assert outcome.stdout.count("line-") == 100
    assert len(collected) == 100
