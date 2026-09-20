"""M4-T05：runtime session 启动恢复（无网络 fixture，不自动重放）。

反例：Worker 重启后原进程仍在 → 只观察；原进程消失 → needs_review；
PID 被复用 → needs_review 且绝不触碰新进程；start_token 先落盘再 spawn。
fixture 进程经 SupervisedProcess 派生（受控 env，不经 shell）。
"""
from __future__ import annotations

import sys
import textwrap
import time
from pathlib import Path

from motte_harness.process import kill_tree, minimal_env
from motte_harness.session import (
    load_session,
    mark_spawned,
    mark_terminal,
    new_session_record,
    persist_session,
    recover_session,
)
from motte_harness.supervisor import SupervisedLimits, SupervisedProcess


def _script(tmp_path: Path, name: str, source: str) -> Path:
    script = tmp_path / name
    script.write_text(textwrap.dedent(source), encoding="utf-8")
    return script


def test_start_token_persisted_before_spawn(tmp_path):
    record = new_session_record(
        run_id="run-1", case_id="case-1", attempt_id="att-1",
        backend="claude-cli", argv=["claude", "-p", "hi"],
    )
    session_file = tmp_path / "sessions" / f"{record['session_id']}.json"
    persisted = persist_session(session_file, record)
    assert persisted["state"] == "prepared"
    assert persisted["start_token"].startswith("start-")
    # 再读：字段完整且未 spawn
    loaded = load_session(session_file)
    assert loaded["session_id"] == record["session_id"]
    assert loaded["pid"] is None


def test_recovery_observes_alive_process_without_replay(tmp_path):
    alive = _script(
        tmp_path, "alive.py",
        """
        import time

        time.sleep(30)
        """,
    )
    argv = [sys.executable, str(alive)]
    process = SupervisedProcess(
        argv, env=minimal_env(),
        limits=SupervisedLimits(total_timeout=60, idle_timeout=60),
    )
    process.start()
    session_file = None
    try:
        record = new_session_record(
            run_id="run-1", case_id="case-1", attempt_id="att-1",
            backend="codex-cli", argv=argv,
        )
        session_file = tmp_path / "sessions" / f"{record['session_id']}.json"
        persist_session(session_file, record)
        assert process.pid is not None
        mark_spawned(record, process.pid, path=session_file)

        verdict = recover_session(load_session(session_file))
        assert verdict["action"] == "observe"
        assert verdict["status"] == "process_alive"
        assert verdict["replayed"] is False
    finally:
        pid = process.pid
        process.interrupt(reason="test-end")
        if pid is not None:
            kill_tree(pid)

    # 进程消失后恢复：needs_review，不重放
    time.sleep(0.3)
    if session_file is not None:
        verdict = recover_session(load_session(session_file))
        assert verdict["action"] == "needs_review"
        assert verdict["replayed"] is False


def test_recovery_flags_pid_reuse_without_touching_new_process(tmp_path):
    # 记录一个从未属于我们的 PID 身份（命令行不同）→ 复用判定拒绝
    record = new_session_record(
        run_id="run-1", case_id="case-1", attempt_id="att-1",
        backend="claude-cli", argv=["definitely", "not", "this", "process"],
    )
    record = {
        **record,
        "state": "spawned",
        "pid": 4,  # Windows 上长期存在的系统进程；POSIX 上 kernel 线程
        "identity": {"create_time": None, "cmdline": ["definitely", "not", "this"]},
    }
    verdict = recover_session(record)
    assert verdict["action"] == "needs_review"
    assert verdict["status"] == "pid_reused"
    assert verdict["replayed"] is False


def test_terminal_sessions_are_not_recovered(tmp_path):
    record = new_session_record(
        run_id="run-2", case_id="case-1", attempt_id="att-1",
        backend="claude-cli", argv=["claude", "-p", "hi"],
    )
    session_file = tmp_path / "sessions" / f"{record['session_id']}.json"
    persist_session(session_file, record)
    mark_terminal(record, "completed", path=session_file, cleanup={"status": "ok"})
    verdict = recover_session(load_session(session_file))
    assert verdict["action"] == "already_terminal"
    assert verdict["replayed"] is False


def test_never_spawned_session_stays_prepared(tmp_path):
    record = new_session_record(
        run_id="run-3", case_id="case-1", attempt_id="att-1",
        backend="claude-cli", argv=["claude", "-p", "hi"],
    )
    verdict = recover_session(record)
    assert verdict["action"] == "never_spawned"
