import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { RuntimeCommands } from "../src/components/RuntimeCommands";
import { getRuntimeCommands, getRuntimeSessions, sendRuntimeCommand, ApiRequestError } from "../src/api/client";

vi.mock("../src/api/client", async (original) => ({
  ...await original<typeof import("../src/api/client")>(),
  getRuntimeCommands: vi.fn(), getRuntimeSessions: vi.fn(), sendRuntimeCommand: vi.fn(),
}));
const session = {
  run_id: "run-1", case_id: "case-1", attempt_id: "attempt-1", session_id: "session-1",
  state: "active", revision: 7, control_revision: 3, native_thread_id: "thread-1", active_turn_id: "turn-1",
  pending_approvals: [{ approval_id: "approval-1", method: "item/fileChange/requestApproval", item_id: "item-1",
    summary: "修改 answer.txt", request_hash: "sha256:bound-request", expires_at: "2099-01-01T00:00:00Z", state: "pending" }],
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getRuntimeSessions).mockResolvedValue({ items: [session], total: 1 });
  vi.mocked(getRuntimeCommands).mockResolvedValue({ items: [], total: 0 });
  vi.mocked(sendRuntimeCommand).mockResolvedValue({ command_id: "command-1", status: "queued" });
});
afterEach(cleanup);

it("消息绑定正在显示的会话和控制版本，202 只显示排队", async () => {
  render(<RuntimeCommands runId="run-1" terminal={false} />);
  const input = await screen.findByLabelText("补充消息");
  fireEvent.change(input, { target: { value: "请补充测试" } });
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await screen.findByText("command-1：排队等待投递");
  expect(sendRuntimeCommand).toHaveBeenCalledTimes(1);
  expect(sendRuntimeCommand).toHaveBeenCalledWith("run-1", expect.objectContaining({
    kind: "user_message", content: "请补充测试", case_id: "case-1", session_id: "session-1",
    expected_session_revision: 3, dedupe_key: expect.any(String),
  }));
  expect(screen.queryByText("已收到原生确认")).toBeNull();
});

it("审批只提交原始请求 ID 与内容 hash，确认范围不表示工具成功", async () => {
  vi.mocked(getRuntimeCommands).mockResolvedValue({ total: 1, items: [{
    id: "older", type: "approve", status: "acknowledged", ack_evidence: { kind: "request_resolved" },
  }] });
  render(<RuntimeCommands runId="run-1" terminal={false} />);
  fireEvent.click(await screen.findByRole("button", { name: "批准此请求" }));
  await waitFor(() => expect(sendRuntimeCommand).toHaveBeenCalledTimes(1));
  expect(sendRuntimeCommand).toHaveBeenCalledWith("run-1", expect.objectContaining({
    kind: "approve", payload: { approval_id: "approval-1", request_hash: "sha256:bound-request" },
  }));
  expect(screen.getByText("原始审批请求已结束，不代表动作执行成功")).toBeTruthy();
});

it("终态、未激活会话和过期审批不提供可用授权按钮", async () => {
  vi.mocked(getRuntimeSessions).mockResolvedValue({ total: 1, items: [{ ...session,
    pending_approvals: [{ ...session.pending_approvals[0], expires_at: "2000-01-01T00:00:00Z" }],
  }] });
  const view = render(<RuntimeCommands runId="run-1" terminal={false} />);
  expect((await screen.findByRole("button", { name: "批准此请求" }) as HTMLButtonElement).disabled).toBe(true);
  view.rerender(<RuntimeCommands runId="run-1" terminal />);
  expect((screen.getByRole("button", { name: "请求中断" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "请求中断" }));
  expect(sendRuntimeCommand).not.toHaveBeenCalled();
});

it("响应丢失不会自动重发，读取后仍未找到命令则保留不确定提示", async () => {
  vi.mocked(sendRuntimeCommand).mockRejectedValue(new TypeError("network disconnected"));
  render(<RuntimeCommands runId="run-1" terminal={false} />);
  fireEvent.click(await screen.findByRole("button", { name: "请求中断" }));
  await screen.findByText(/提交结果尚未确认/);
  fireEvent.click(screen.getByRole("button", { name: "刷新交互状态" }));
  await waitFor(() => expect(getRuntimeCommands).toHaveBeenCalledTimes(3));
  expect(sendRuntimeCommand).toHaveBeenCalledTimes(1);
  expect((screen.getByRole("button", { name: "请求中断" }) as HTMLButtonElement).disabled).toBe(true);
});

it("明确的过期拒绝保留错误但不会伪装送达不确定", async () => {
  vi.mocked(sendRuntimeCommand).mockRejectedValue(new ApiRequestError(409, { code: "STALE_SESSION", message: "会话版本已变化" }));
  render(<RuntimeCommands runId="run-1" terminal={false} />);
  fireEvent.click(await screen.findByRole("button", { name: "请求中断" }));
  await screen.findByText(/会话版本已变化/);
  expect(screen.queryByText(/提交结果尚未确认/)).toBeNull();
});
