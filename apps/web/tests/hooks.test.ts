import { describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { countDone, isTerminal, useRunEvents } from "../src/hooks/useRunEvents";
import type { TraceEvent } from "../src/api/client";

const handlers = vi.hoisted(() => ({
  onEvent: vi.fn(),
  onGap: vi.fn(),
}));
const snapshotMock = vi.hoisted(() => vi.fn());
vi.mock("../src/api/client", () => ({
  subscribeRunEvents: vi.fn(
    (
      runId: string,
      h: {
        onEvent: (e: TraceEvent) => void;
        onGap?: (gap: { type: "gap"; after: number; next_seq: number; partial: boolean }) => void;
      },
    ) => {
      handlers.onEvent.mockImplementation(h.onEvent);
      handlers.onGap.mockImplementation(h.onGap ?? (() => undefined));
      return () => undefined;
    },
  ),
  fetchRunEventsSnapshot: vi.fn((...args: unknown[]) => snapshotMock(...args)),
}));

describe("countDone", () => {
  it("按 case_id 去重统计完成事件", () => {
    const events = [
      { run_id: "r", seq: 1, type: "running", status: "running" },
      { run_id: "r", seq: 2, type: "model_response", case_id: "case-1" },
      { run_id: "r", seq: 3, type: "score", case_id: "case-1", passed: true },
      { run_id: "r", seq: 4, type: "case_call_failed", case_id: "case-2" },
      { run_id: "r", seq: 5, type: "model_response", case_id: "case-2" },
      { run_id: "r", seq: 6, type: "model_response", case_id: "case-3" },
    ] as TraceEvent[];
    expect(countDone(events)).toBe(3);
  });
});

describe("isTerminal", () => {
  it("识别终态", () => {
    expect(isTerminal("completed")).toBe(true);
    expect(isTerminal("failed")).toBe(true);
    expect(isTerminal("needs_review")).toBe(true);
    expect(isTerminal("running")).toBe(false);
    expect(isTerminal(null)).toBe(false);
  });
});

describe("useRunEvents", () => {
  it("订阅事件并回写状态", async () => {
    const { result } = renderHook(() => useRunEvents("run-1"));
    await act(async () => {
      handlers.onEvent({ run_id: "run-1", seq: 1, type: "running", status: "running" });
      handlers.onEvent({ run_id: "run-1", seq: 2, type: "model_response", case_id: "case-1", result: {} });
    });
    expect(result.current.events).toHaveLength(2);
    expect(result.current.status).toBe("running");
    expect(result.current.partial).toBe(false);
  });

  it("重复 seq 去重，事件不重复进入列表", async () => {
    const { result } = renderHook(() => useRunEvents("run-2"));
    await act(async () => {
      handlers.onEvent({ run_id: "run-2", seq: 1, type: "running", payload: { status: "running" } });
      handlers.onEvent({ run_id: "run-2", seq: 1, type: "running", payload: { status: "running" } });
      handlers.onEvent({ run_id: "run-2", seq: 2, type: "score", payload: {} });
    });
    expect(result.current.events).toHaveLength(2);
  });

  it("motte-gap 触发快照补齐并标记 partial", async () => {
    snapshotMock.mockResolvedValue({
      events: [
        { run_id: "run-3", seq: 4, type: "score", payload: {} },
        { run_id: "run-3", seq: 5, type: "completed", payload: { status: "completed" } },
      ],
      last_seq: 5,
      run_status: "completed",
      partial: false,
    });
    const { result } = renderHook(() => useRunEvents("run-3"));
    await act(async () => {
      handlers.onEvent({ run_id: "run-3", seq: 3, type: "model_response", payload: {} });
    });
    await act(async () => {
      handlers.onGap({ type: "gap", after: 3, next_seq: 4, partial: true });
    });
    await waitFor(() => {
      expect(result.current.partial).toBe(true);
      expect(result.current.events.map((event) => event.seq)).toEqual([3, 4, 5]);
      expect(result.current.status).toBe("completed");
    });
    expect(snapshotMock).toHaveBeenCalledWith("run-3", 3);
  });

  it("快照失败时保持 partial 不抛错", async () => {
    snapshotMock.mockRejectedValue(new Error("network down"));
    const { result } = renderHook(() => useRunEvents("run-4"));
    await act(async () => {
      handlers.onGap({ type: "gap", after: 0, next_seq: 2, partial: true });
    });
    await waitFor(() => {
      expect(result.current.partial).toBe(true);
      expect(result.current.events).toHaveLength(0);
    });
  });
});
