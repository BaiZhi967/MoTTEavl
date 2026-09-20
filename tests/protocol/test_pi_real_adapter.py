"""M4-T03：真实 Pi SDK bridge 适配器（离线 scripted model）。

主断言 test_real_pi_session_no_echo_fallback：真实上游 Agent loop + SDK 官方
faux provider 驱动 bridge 侧工作区工具真实写文件；prompt 不出现在任何输出
路径（无 echo），无 SDK 时显式不可用（不跳过、不假成功）。
"""
from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import pytest

from motte_agent.pi import PiBridgeError, PiBridgeSession

NODE = shutil.which("node")
BRIDGE = Path(__file__).resolve().parents[2] / "bridges" / "pi" / "bridge.mjs"


def _real_session(tmp_path: Path, *, responses=None, tools=None, budgets=None) -> PiBridgeSession:
    return PiBridgeSession(
        run_id="run-real",
        case_id="case-write",
        session_id="session-real-1",
        operation_id="operation-real-1",
        workspace=str(tmp_path / "workspace"),
        model_config={"id": "scripted-1", "name": "Scripted"},
        responses=responses or [
            [{"type": "toolCall", "name": "write_file",
              "arguments": {"path": "hello.txt", "content": "hello from real sdk"}}],
            [{"type": "text", "text": "Created hello.txt."}],
        ],
        tools=tools or ["read_file", "write_file", "list_files"],
        budgets=budgets or {"max_steps": 8, "max_tool_calls": 8},
        bridge_path=BRIDGE,
        node_binary=NODE,
        idle_timeout=15.0,
        total_timeout=60.0,
    )


@pytest.mark.skipif(NODE is None, reason="node is required for the Pi bridge")
def test_real_pi_session_no_echo_fallback():
    import tempfile

    with tempfile.TemporaryDirectory() as workspace_root:
        tmp_path = Path(workspace_root)
        session = _real_session(tmp_path)
        session.start()

        result = session.run("please create hello.txt with the word hello")
        session.close()

        # 真实 SDK 工具任务完成：文件真实落盘
        written = (tmp_path / "workspace" / "hello.txt").read_text(encoding="utf-8")
        assert written == "hello from real sdk"

        assert result["status"] == "completed"
        assert result["tool_calls"] == 1
        assert result["steps"] >= 2
        assert result["final_output"] == "Created hello.txt."
        tool_call = result["tool_calls_detail"][0]
        assert tool_call["name"] == "write_file"
        assert tool_call["arguments"]["path"] == "hello.txt"

        # 无 echo：prompt 文本不得出现在任何输出
        joined = "\n".join(result["outputs"])
        assert "please create hello.txt" not in joined
        # scripted model 不产真实计量：usage 必须诚实保持未上报
        assert result["usage"]["reported"] is False
        assert result["usage"]["total_tokens"] is None


@pytest.mark.skipif(NODE is None, reason="node is required for the Pi bridge")
def test_real_pi_session_reports_sdk_version_and_identity():
    import tempfile

    with tempfile.TemporaryDirectory() as workspace_root:
        tmp_path = Path(workspace_root)
        session = _real_session(tmp_path)
        ready = session.start()
        session.close()

        assert ready["sdk_version"] == "0.73.1"
        assert ready["session_id"] == "session-real-1"
        assert ready["operation_id"] == "operation-real-1"


@pytest.mark.skipif(NODE is None, reason="node is required for the Pi bridge")
def test_real_pi_session_missing_sdk_is_explicitly_unavailable(tmp_path):
    # 复制 bridge 但移除 session 模块：SDK 加载失败必须显式
    # PI_BACKEND_UNAVAILABLE，不得回退 echo/builtin，也不得被当作通过。
    broken_dir = tmp_path / "broken-bridge"
    broken_dir.mkdir()
    (broken_dir / "bridge.mjs").write_text(
        BRIDGE.read_text(encoding="utf-8").replace(
            'await import("./session.mjs")',
            'await import("./missing_session.mjs")',
        ),
        encoding="utf-8",
    )
    session = PiBridgeSession(
        run_id="run-broken",
        case_id="case-broken",
        session_id="session-broken",
        operation_id="operation-broken",
        workspace=str(tmp_path / "workspace"),
        responses=[[{"type": "text", "text": "x"}]],
        bridge_path=broken_dir / "bridge.mjs",
        node_binary=NODE,
        idle_timeout=10.0,
        total_timeout=20.0,
    )
    with pytest.raises(PiBridgeError) as raised:
        session.start()
    assert raised.value.code == "PI_BACKEND_UNAVAILABLE"
    session.close()


@pytest.mark.skipif(NODE is None, reason="node is required for the Pi bridge")
def test_real_pi_session_workspace_policy_blocks_escape(tmp_path):
    # 工具参数试图逃出工作区：bridge 侧沙箱拒绝，任务以错误工具结果收尾
    session = _real_session(
        tmp_path,
        responses=[
            [{"type": "toolCall", "name": "write_file",
              "arguments": {"path": "../escape.txt", "content": "nope"}}],
            [{"type": "text", "text": "blocked"}],
        ],
    )
    session.start()
    result = session.run("try to escape the workspace")
    session.close()

    assert not (tmp_path / "escape.txt").exists()
    assert result["status"] == "completed"
    # 逃逸写被拒绝：目标文件不存在，工具结果事件标记错误


@pytest.mark.skipif(NODE is None, reason="node is required for the Pi bridge")
def test_real_pi_session_budget_stops_after_max_tool_calls(tmp_path):
    responses = [
        [{"type": "toolCall", "name": "write_file",
          "arguments": {"path": f"f{i}.txt", "content": "x"}}]
        for i in range(6)
    ]
    session = _real_session(
        tmp_path, responses=responses, budgets={"max_steps": 8, "max_tool_calls": 2},
    )
    session.start()
    result = session.run("keep writing files")
    session.close()

    files = sorted(p.name for p in (tmp_path / "workspace").glob("*.txt"))
    assert len(files) <= 2
    assert result["budget_stop"] is True


@pytest.mark.skipif(NODE is None, reason="node is required for the Pi bridge")
def test_real_pi_session_interrupt_cancels_long_run(tmp_path):
    # tokens_per_second 拉长真实流式时长，interrupt 在中途发出：结果必须取消
    session = PiBridgeSession(
        run_id="run-cancel",
        case_id="case-cancel",
        session_id="session-cancel",
        operation_id="operation-cancel",
        workspace=str(tmp_path / "workspace"),
        model_config={"id": "scripted-1"},
        responses=[[{"type": "text", "text": "word " * 4000}]],
        tools=["read_file", "write_file", "list_files"],
        budgets={"max_steps": 4, "tokens_per_second": 50},
        bridge_path=BRIDGE,
        node_binary=NODE,
        idle_timeout=30.0,
        total_timeout=60.0,
    )
    session.start()

    import threading

    outcome = {}

    def runner():
        try:
            outcome["result"] = session.run("write a long essay about testing")
        except PiBridgeError as error:
            outcome["error"] = error

    worker = threading.Thread(target=runner, daemon=True)
    worker.start()
    import time

    time.sleep(1.5)
    session.interrupt()
    worker.join(timeout=30)
    session.close()

    assert worker.is_alive() is False
    if "result" in outcome:
        assert outcome["result"]["status"] == "cancelled"
        assert outcome["result"]["interrupted"] is True
    else:
        # bridge 在中断窗口内自然完成也被接受，但绝不能是协议错误冒充取消
        assert outcome["error"].code not in ("PI_PROTOCOL_INVALID",)
