import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { MemoryRouter } from "react-router-dom";
import {
  CevalCases,
  CevalCompare,
  CevalMonitor,
  CevalOperate,
  CevalResult,
} from "../src/evalTypes/ceval/CevalPages";

const clientMocks = vi.hoisted(() => ({
  getExternalCatalog: vi.fn(),
  prepareCevalDataset: vi.fn(),
  getCevalPreflight: vi.fn(),
  getCevalCases: vi.fn(),
  createCevalRun: vi.fn(),
  getExternalJobs: vi.fn(),
  compareRuns: vi.fn(),
  evaluateRunGate: vi.fn(),
  getRuns: vi.fn(),
  getRun: vi.fn(),
}));

vi.mock("../src/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/client")>()),
  ...clientMocks,
}));

function wrap(node: React.ReactNode, route = "/ceval") {
  return <MemoryRouter initialEntries={[route]}>{node}</MemoryRouter>;
}

beforeEach(() => {
  clientMocks.getExternalCatalog.mockResolvedValue({
    benchmarks: [{
      benchmark_id: "ceval", benchmark_version: "1", status: "prepared",
      blockers: ["RUNNER_NOT_CONNECTED"],
      dataset: { state: "ready", provenance: "user-supplied", revision: "rev-1", rows: 4, gold_rows: 4, unscored: false },
    }],
  });
  clientMocks.getCevalCases.mockResolvedValue({
    cases: [
      { case_id: "logic-1", subject: "logic", has_gold: true },
      { case_id: "logic-2", subject: "logic", has_gold: false },
    ],
    total: 2,
  });
  clientMocks.getRuns.mockResolvedValue({ items: [], total: 0 });
  clientMocks.getExternalJobs.mockResolvedValue({ jobs: [] });
});

afterEach(cleanup);

describe("CevalPages", () => {
  it("五页可导航渲染，操作页展示 Catalog 状态与阻塞原因", async () => {
    const { unmount } = render(wrap(<CevalOperate />));
    await waitFor(() => {
      expect(screen.getByTestId("catalog-status").textContent).toBe("prepared");
    });
    expect(screen.getByText(/RUNNER_NOT_CONNECTED/)).toBeTruthy();
    unmount();

    render(wrap(<CevalCases />, "/ceval/cases"));
    await waitFor(() => expect(screen.getByText("logic-1")).toBeTruthy());
    expect(screen.getByText(/unscored/)).toBeTruthy();
  });

  it("监控页列出 ceval-external 运行与外部 Job 状态；范围标签持续显示", async () => {
    clientMocks.getRuns.mockResolvedValue({
      items: [{
        id: "run-c1", scenario_version: "ceval-external@1", status: "completed",
        manifest: { scope: "custom-subset" }, cases: [], scores: [],
      }],
      total: 1,
    });
    clientMocks.getExternalJobs.mockResolvedValue({
      jobs: [{ job_id: "job-1", run_id: "run-c1", status: "settled", launch_token: "launch-x" }],
    });
    render(wrap(<CevalMonitor />, "/ceval/monitor"));
    await waitFor(() => expect(screen.getByText("settled")).toBeTruthy());
    expect(screen.getByText(/custom-subset（自选子集）/)).toBeTruthy();
  });

  it("结果页显示范围、部分失败与 not_attempted，费用按 unknown 呈现", async () => {
    clientMocks.getRun.mockResolvedValue({
      id: "run-r1", status: "failed", scenario_version: "ceval-external@1",
      manifest: { scope: "smoke", external_benchmark: { dataset_revision: "rev-9" } },
      cases: [
        { case_id: "c1", outcome: "responded", result: { prediction: "A", gold: "A" } },
        { case_id: "c2", outcome: "call_failed", result: { error: { code: "RUNNER_EXIT" } } },
        { case_id: "c3", outcome: "not_attempted", result: null },
      ],
    });
    render(wrap(<CevalResult />, "/ceval/runs/run-r1/result"));
    await waitFor(() => expect(screen.getByTestId("scope-label")).toBeTruthy());
    expect(screen.getByTestId("scope-label").textContent).toContain("smoke");
    expect(screen.getByTestId("partial-failure").textContent).toContain("1 条样本执行失败");
    expect(screen.getByTestId("not-attempted").textContent).toContain("c3");
    expect(screen.getByTestId("job-status").textContent).toContain("unknown");
  });

  it("比较页：缺覆盖/费用未知不显示放行，逐规则给出不足原因", async () => {
    clientMocks.compareRuns.mockResolvedValue({
      eligible: true,
      reasons: [],
      metric_eligibility: { quality: true, cost: false },
      case_diff: { added: [], removed: [], changed: [] },
    });
    clientMocks.evaluateRunGate.mockResolvedValue({
      schema: "gate-lite@1",
      passed: false,
      rules: [
        { id: "metric_threshold", passed: true, reason: "0.9 gte 0.5" },
        { id: "coverage", passed: false, reason: "coverage 0.9 < required 1" },
        { id: "cost_known", passed: false, reason: "cost unknown; hard cost gate cannot pass" },
      ],
    });
    render(wrap(<CevalCompare />, "/ceval/compare"));
    fireEvent.change(screen.getByLabelText("baseline"), { target: { value: "run-a" } });
    fireEvent.change(screen.getByLabelText("candidate"), { target: { value: "run-b" } });
    fireEvent.click(screen.getByRole("button", { name: "比较" }));
    await waitFor(() => expect(screen.getByTestId("gate-panel")).toBeTruthy());
    expect(screen.queryByTestId("gate-pass")).toBeNull();
    const blocked = screen.getByTestId("gate-blocked");
    expect(blocked.textContent).toContain("coverage");
    expect(blocked.textContent).toContain("cost_known");
  });

  it("操作页：创建被拒时保留表单并显示错误（无绕过按钮）", async () => {
    clientMocks.createCevalRun.mockRejectedValue(new Error("422 RUNNER_NOT_CONNECTED"));
    render(wrap(<CevalOperate />));
    await waitFor(() => expect(screen.getByTestId("catalog-status").textContent).toBe("prepared"));
    fireEvent.click(screen.getByRole("button", { name: "排队执行" }));
    await waitFor(() => expect(screen.getByTestId("run-error").textContent).toContain("RUNNER_NOT_CONNECTED"));
    expect(screen.queryByRole("button", { name: /绕过/ })).toBeNull();
  });
});
