import { describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { countDone, isTerminal, useRunEvents } from "../src/hooks/useRunEvents";
import type { TraceEvent } from "../src/api/client";

const handlers = vi.hoisted(() => ({ onEvent: vi.fn() }));
vi.mock("../src/api/client", () => ({
  subscribeRunEvents: vi.fn((runId: string, h: { onEvent: (e: TraceEvent) => void }) => {
    handlers.onEvent.mockImplementation(h.onEvent);
    return () => undefined;
  }),
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
  });
});
