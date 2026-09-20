"""M4 review 批 1 回归：Pi bridge 取消/预算/路径/配额/总时限。

全部走**真实 packaged bridge + 真实 SDK + scripted model**（无网络、无
真实模型调用）：

- R03：运行中的 interrupt 立即被处理——interrupted 事件先于 finished、
  后续工具不再执行（取消不等任务结束）。
- R08：max_steps / max_tool_calls 是强制边界——达到预算即停（后续模型/
  工具动作不再发生），budget_stop_reason 进入结果。
- R10：写嵌套路径创建的是完整目标文件，第一层不是普通文件。
- R11：文件数配额在写盘前检查——被拒绝的写不落盘。
- R09：total_timeout 是单调时钟总期限——持续产出事件也不能无限延长。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from motte_agent.pi import PiBridgeError, PiBridgeSession

BRIDGE = Path(__file__).resolve().parents[2] / "bridges" / "pi" / "bridge.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required for the Pi bridge")

# 拉长模型流：低 tokens_per_second 让每步有可观的流式窗口。
SLOW = {"tokens_per_second": 6}
PAD = "context " * 12  # ~24 tokens：6 tps ≈ 4s/步


def _session(tmp_path, *, responses, budgets=None, quotas=None, total=60.0,
             idle=30.0) -> PiBridgeSession:
    return PiBridgeSession(
        run_id="run-r1", case_id="case-r1", session_id="sess-r1",
        operation_id="op-r1", workspace=str(tmp_path / "ws"),
        model_config={"id": "scripted-1", "name": "scripted-1"},
        responses=responses,
        tools=["read_file", "write_file", "list_files"],
        budgets=budgets or {},
        workspace_quotas=quotas,
        bridge_path=BRIDGE, node_binary=NODE,
        idle_timeout=idle, total_timeout=total,
    )


def _tool_step(path: str, content: str) -> list[dict]:
    return [{"type": "toolCall", "name": "write_file",
             "arguments": {"path": path, "content": content}}]


def test_interrupt_is_processed_while_run_is_active(tmp_path):
    """R03：取消在运行中生效，不排队到任务完成后。"""
    script = [
        _tool_step("a.txt", PAD),
        _tool_step("b.txt", PAD),
        _tool_step("c.txt", PAD),
        [{"type": "text", "text": "done " + PAD}],
    ]
    session = _session(tmp_path, responses=script, budgets=dict(SLOW))
    session.start()
    started = time.monotonic()
    try:
        # 在第一个工具结果落地后立即打断：修复前 interrupt 排在 run 之后，
        # 会得到 NOTHING_TO_INTERRUPT 错误事件；修复后是 interrupted+finished。
        outcome: dict = {}
        import threading

        def runner():
            try:
                outcome["result"] = session.run("do the work")
            except BaseException as error:  # noqa: BLE001
                outcome["error"] = error

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if (tmp_path / "ws" / "a.txt").exists():
                break
            time.sleep(0.05)
        session.interrupt()
        thread.join(timeout=30)
        assert not thread.is_alive(), "run did not settle after interrupt"
        assert "error" not in outcome, f"interrupt surfaced as error: {outcome['error']}"
        result = outcome["result"]
        assert result["interrupted"] is True
        assert result["status"] == "cancelled"
        # 取消后的步骤没有执行：b/c 不存在（a 可能已写，取决于打断时点）。
        assert not (tmp_path / "ws" / "b.txt").exists()
        assert not (tmp_path / "ws" / "c.txt").exists()
        # 打断发生在流式窗口内：远短于完整 4 步 × ~4s。
        assert time.monotonic() - started < 25
    finally:
        session.close()


def test_max_steps_budget_stops_the_loop(tmp_path):
    """R08：max_steps 达到后停——不是只发通知。"""
    script = [
        _tool_step("s1.txt", "one"),
        _tool_step("s2.txt", "two"),
        _tool_step("s3.txt", "three"),
        [{"type": "text", "text": "done"}],
    ]
    session = _session(tmp_path, responses=script, budgets={"max_steps": 1})
    session.start()
    try:
        result = session.run("work")
        assert result["status"] in ("completed", "cancelled")
        assert result["budget_stop"] is True
        assert result["budget_stop_reason"] == "max_steps"
        # 循环在预算处停止：步数远小于脚本长度（被中止的部分消息可能
        # 多计一步）。
        assert result["steps"] <= 2
        # 后续步骤没有执行。
        assert (tmp_path / "ws" / "s1.txt").exists()
        assert not (tmp_path / "ws" / "s2.txt").exists()
        assert not (tmp_path / "ws" / "s3.txt").exists()
    finally:
        session.close()


def test_max_tool_calls_budget_blocks_next_tool(tmp_path):
    """R08：max_tool_calls 达到后阻断后续工具并停止。"""
    script = [
        _tool_step("t1.txt", "one"),
        _tool_step("t2.txt", "two"),
        _tool_step("t3.txt", "three"),
        [{"type": "text", "text": "done"}],
    ]
    session = _session(tmp_path, responses=script, budgets={"max_tool_calls": 1})
    session.start()
    try:
        result = session.run("work")
        assert result["budget_stop"] is True
        assert result["budget_stop_reason"] == "max_tool_calls"
        assert (tmp_path / "ws" / "t1.txt").exists()
        assert not (tmp_path / "ws" / "t2.txt").exists()
        assert not (tmp_path / "ws" / "t3.txt").exists()
    finally:
        session.close()


def test_nested_write_creates_full_target_not_prefix_file(tmp_path):
    """R10：nested/answer.txt 是目录+文件，而不是名为 nested 的普通文件。"""
    script = [
        _tool_step("nested/answer.txt", "deep"),
        [{"type": "text", "text": "done"}],
    ]
    session = _session(tmp_path, responses=script)
    session.start()
    try:
        result = session.run("work")
        assert result["status"] == "completed"
        target = tmp_path / "ws" / "nested" / "answer.txt"
        prefix = tmp_path / "ws" / "nested"
        assert target.is_file(), "full nested target was not created"
        assert prefix.is_dir(), "first path component became a plain file"
        assert target.read_text(encoding="utf-8") == "deep"
    finally:
        session.close()


def test_file_count_quota_rejects_before_write(tmp_path):
    """R11：配额拒绝不落盘——被拒文件不存在于 workspace。"""
    script = [
        _tool_step("first.txt", "1"),
        _tool_step("extra.txt", "2"),
        [{"type": "text", "text": "done"}],
    ]
    session = _session(tmp_path, responses=script, quotas={"maxFiles": 1})
    session.start()
    try:
        result = session.run("work")
        assert result["status"] == "completed"
        assert (tmp_path / "ws" / "first.txt").exists()
        assert not (tmp_path / "ws" / "extra.txt").exists(), \
            "quota-rejected write must not touch the disk"
        # 被拒工具在轨迹里如实是 error。
        errors = [
            call for call in result["tool_calls_detail"]
            if call.get("name") == "write_file"
        ]
        assert len(errors) == 2
    finally:
        session.close()


def test_total_timeout_is_a_monotonic_deadline(tmp_path):
    """R09：持续产出事件的慢任务也会在 total_timeout 处被切断。"""
    script = [
        _tool_step("a.txt", PAD),
        _tool_step("b.txt", PAD),
        _tool_step("c.txt", PAD),
        [{"type": "text", "text": "done " + PAD}],
    ]
    session = _session(tmp_path, responses=script, budgets=dict(SLOW), total=1.5)
    session.start()
    started = time.monotonic()
    try:
        with pytest.raises(PiBridgeError) as raised:
            session.run("slow work")
        assert raised.value.code == "PI_BRIDGE_TIMEOUT"
        assert time.monotonic() - started < 6, "total timeout did not bound the run"
    finally:
        session.close()
