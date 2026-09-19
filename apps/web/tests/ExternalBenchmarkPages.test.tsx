import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { CevalCompare, CevalResult } from "../src/evalTypes/ceval/CevalPages";
import { makeExternalPages } from "../src/evalTypes/external/ExternalBenchmarkPages";

const clientMocks = vi.hoisted(() => ({
  getRun: vi.fn(),
  getExternalJobs: vi.fn(),
  getExternalCatalog: vi.fn(),
  compareRuns: vi.fn(),
  evaluateRunGate: vi.fn(),
}));

vi.mock("../src/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/client")>()),
  ...clientMocks,
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

afterEach(cleanup);

describe("外部基准页面：请求身份保护（review R15）", () => {
  beforeEach(() => {
    clientMocks.getRun.mockReset();
    clientMocks.getExternalJobs.mockReset();
    clientMocks.compareRuns.mockReset();
    clientMocks.evaluateRunGate.mockReset();
    clientMocks.getExternalJobs.mockResolvedValue({ jobs: [] });
  });

  it("结果页 A→B 切换：旧 run 的晚到响应不覆盖当前页面", async () => {
    const slowA = deferred<any>();
    clientMocks.getRun.mockImplementation((runId: string) => (
      runId === "run-a" ? slowA.promise : Promise.resolve({
        id: "run-b", status: "completed", scenario_version: "ceval-external@1",
        manifest: { scope: "full" }, cases: [], scores: [],
      })
    ));
    const resultRoute = (
      <Routes>
        <Route path="/ceval/runs/:runId/result" element={<CevalResult />} />
      </Routes>
    );
    const view = render(
      <MemoryRouter initialEntries={["/ceval/runs/run-a/result"]}>{resultRoute}</MemoryRouter>,
    );
    rerenderAt(view, "/ceval/runs/run-b/result", resultRoute);
    await waitFor(() => expect(screen.getByTestId("scope-label").textContent).toContain("full"));
    // 旧请求随后带着 run-a 的数据返回：不得覆盖 run-b 的页面。
    await act(async () => {
      slowA.resolve({
        id: "run-a", status: "failed", scenario_version: "ceval-external@1",
        manifest: { scope: "smoke" }, cases: [], scores: [],
      });
    });
    expect(screen.getByTestId("scope-label").textContent).toContain("full");
    expect(screen.getByTestId("scope-label").textContent).not.toContain("smoke");
  });

  it("比较页重复提交：第一组慢的 compare 晚到不覆盖第二组结果", async () => {
    const slowCompare = deferred<any>();
    // 按 baseline 区分快慢：run-1 的 compare 慢（且“可比”），
    // run-3 的立即返回不可比——与提交顺序无关地确定。
    clientMocks.compareRuns.mockImplementation((baseline: string) => (
      baseline === "run-1" ? slowCompare.promise : Promise.resolve({
        eligible: false, reasons: ["FEWSHOT_CHANGED:few_shot"],
        metric_eligibility: { quality: false }, case_diff: { added: [], removed: [], changed: [] },
      })
    ));
    clientMocks.evaluateRunGate.mockResolvedValue({
      schema: "gate-lite@2", passed: false,
      rules: [{ id: "comparable", passed: false, reason: "reports not comparable" }],
    });
    render(
      <MemoryRouter initialEntries={["/ceval/compare"]}>
        <CevalCompare />
      </MemoryRouter>,
    );
    fireEvent.change(screen.getByLabelText("baseline"), { target: { value: "run-1" } });
    fireEvent.change(screen.getByLabelText("candidate"), { target: { value: "run-2" } });
    fireEvent.click(screen.getByRole("button", { name: "比较" }));
    // 输入改动 + 第二组提交（立即完成）。
    fireEvent.change(screen.getByLabelText("baseline"), { target: { value: "run-3" } });
    fireEvent.change(screen.getByLabelText("candidate"), { target: { value: "run-4" } });
    fireEvent.click(screen.getByRole("button", { name: "比较" }));
    await waitFor(() => expect(screen.getByTestId("comparison-panel")).toBeTruthy());
    expect(screen.getByTestId("comparison-panel").textContent).toContain("FEWSHOT_CHANGED");
    // 第一组的 compare 现在才回来（eligible=true）：不覆盖第二组。
    await act(async () => {
      slowCompare.resolve({
        eligible: true, reasons: [], metric_eligibility: { quality: true },
        case_diff: { added: [], removed: [], changed: [] },
      });
    });
    expect(screen.getByTestId("comparison-panel").textContent).toContain("FEWSHOT_CHANGED");
    expect(screen.getByTestId("comparison-panel").textContent).not.toContain("两份报告可比");
  });

  it("比较页：第一组慢的 gate（通过）不覆盖第二组的未通过结论", async () => {
    const slowGate = deferred<any>();
    clientMocks.compareRuns.mockImplementation((
      baseline: string, candidate: string,
    ) => Promise.resolve({
      eligible: true, reasons: [],
      metric_eligibility: { quality: true },
      case_diff: { added: [], removed: [], changed: [`x-${baseline}-${candidate}`] },
    }));
    // 按 run_id 区分快慢：candidate=run-2 的 gate 慢（且通过），
    // candidate=run-4 的 gate 立即返回未通过——与提交顺序无关地确定。
    clientMocks.evaluateRunGate.mockImplementation((body: { run_id: string }) => (
      body.run_id === "run-2" ? slowGate.promise : Promise.resolve({
        schema: "gate-lite@2", passed: false,
        rules: [{ id: "coverage", passed: false, reason: "coverage 0.75 < required 1" }],
      })
    ));
    render(
      <MemoryRouter initialEntries={["/ceval/compare"]}>
        <CevalCompare />
      </MemoryRouter>,
    );
    const submit = (baseline: string, candidate: string) => {
      fireEvent.change(screen.getByLabelText("baseline"), { target: { value: baseline } });
      fireEvent.change(screen.getByLabelText("candidate"), { target: { value: candidate } });
      fireEvent.click(screen.getByRole("button", { name: "比较" }));
    };
    submit("run-1", "run-2");
    submit("run-3", "run-4");
    await waitFor(() => expect(screen.getByTestId("gate-panel")).toBeTruthy());
    expect(screen.queryByTestId("gate-pass")).toBeNull();
    expect(screen.getByTestId("gate-blocked").textContent).toContain("coverage");
    // 旧的通过结论晚到：不显示放行。
    await act(async () => {
      slowGate.resolve({
        schema: "gate-lite@2", passed: true, rules: [],
      });
    });
    expect(screen.queryByTestId("gate-pass")).toBeNull();
  });

  it("比较页：第一组晚到的失败不把错误写到第二组结果上", async () => {
    const slowFailure = deferred<any>();
    clientMocks.compareRuns.mockImplementationOnce(() => slowFailure.promise);
    clientMocks.compareRuns.mockResolvedValueOnce({
      eligible: true, reasons: [],
      metric_eligibility: { quality: true }, case_diff: { added: [], removed: [], changed: [] },
    });
    clientMocks.evaluateRunGate.mockResolvedValue({
      schema: "gate-lite@2", passed: true, rules: [],
    });
    render(
      <MemoryRouter initialEntries={["/ceval/compare"]}>
        <CevalCompare />
      </MemoryRouter>,
    );
    const submit = (baseline: string, candidate: string) => {
      fireEvent.change(screen.getByLabelText("baseline"), { target: { value: baseline } });
      fireEvent.change(screen.getByLabelText("candidate"), { target: { value: candidate } });
      fireEvent.click(screen.getByRole("button", { name: "比较" }));
    };
    submit("run-1", "run-2");
    submit("run-3", "run-4");
    await waitFor(() => expect(screen.getByTestId("gate-pass")).toBeTruthy());
    await act(async () => {
      slowFailure.reject(new Error("422 stale failure"));
    });
    expect(screen.queryByText(/stale failure/)).toBeNull();
    expect(screen.getByTestId("gate-pass")).toBeTruthy();
  });

  it("R2-11：只修改比较输入（不再次提交）时，在途旧响应被丢弃", async () => {
    const slowOld = deferred<any>();
    clientMocks.compareRuns.mockImplementation((baseline: string) => (
      baseline === "run-old" ? slowOld.promise : Promise.resolve({
        eligible: false, reasons: ["SPLIT_CHANGED:split"],
        metric_eligibility: { quality: false },
        case_diff: { added: [], removed: [], changed: [] },
      })
    ));
    clientMocks.evaluateRunGate.mockResolvedValue({
      schema: "gate-lite@2", passed: false,
      rules: [{ id: "comparable", passed: false, reason: "reports not comparable" }],
    });
    render(
      <MemoryRouter initialEntries={["/ceval/compare"]}>
        <CevalCompare />
      </MemoryRouter>,
    );
    // 提交旧候选 → 请求在途；改成新候选但不再提交。
    fireEvent.change(screen.getByLabelText("baseline"), { target: { value: "run-old" } });
    fireEvent.change(screen.getByLabelText("candidate"), { target: { value: "cand-old" } });
    fireEvent.click(screen.getByRole("button", { name: "比较" }));
    fireEvent.change(screen.getByLabelText("candidate"), { target: { value: "cand-new" } });
    // 旧请求此时才回来：界面输入已是新 Run，不得重新显示旧组结论。
    await act(async () => {
      slowOld.resolve({
        eligible: true, reasons: [],
        metric_eligibility: { quality: true },
        case_diff: { added: [], removed: [], changed: [] },
      });
    });
    expect((screen.getByLabelText("candidate") as HTMLInputElement).value).toBe("cand-new");
    expect(screen.queryByTestId("comparison-panel")).toBeNull();
    expect(screen.queryByTestId("gate-panel")).toBeNull();
    expect(screen.queryByText(/两份报告可比/)).toBeNull();
  });

  it("结果页展示 native/diagnostic 双栏指标（review R09）", async () => {
    clientMocks.getRun.mockResolvedValue({
      id: "run-m", status: "completed", scenario_version: "ceval-external@1",
      manifest: { scope: "smoke" },
      cases: [{ case_id: "c1", outcome: "responded", result: { prediction: "A", gold: "A" } }],
    });
    clientMocks.getExternalJobs.mockResolvedValue({
      jobs: [{
        job_id: "job-1", run_id: "run-m", status: "settled", launch_token: "launch-x",
        metrics: {
          parser_version: "ceval-opencompass-parser@1",
          ceval_native: { "ceval_logic/accuracy": 0.5 },
          ceval_diagnostic: { per_subject: { logic: 0.5 }, aggregate: { accuracy: 0.5 } },
        },
      }],
    });
    render(
      <MemoryRouter initialEntries={["/ceval/runs/run-m/result"]}>
        <CevalResult />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByTestId("metric-columns")).toBeTruthy());
    expect(screen.getByText("native.*（Runner 原始聚合）")).toBeTruthy();
    expect(screen.getByText("diagnostic.*（平台重算）")).toBeTruthy();
    expect(screen.getByText("ceval_logic/accuracy")).toBeTruthy();
  });

  it("CMMLU 页面共用骨架且独立路由（review R16）", async () => {
    clientMocks.getExternalCatalog.mockResolvedValue({
      benchmarks: [{
        benchmark_id: "cmmlu", benchmark_version: "1", status: "prepared",
        blockers: [],
        dataset: { state: "ready", provenance: "user-supplied", revision: "r", rows: 1, gold_rows: 1, unscored: false },
      }],
    });
    const pages = makeExternalPages("cmmlu", { title: "CMMLU（独立身份的外部基准）" });
    render(
      <MemoryRouter initialEntries={["/cmmlu"]}>
        <pages.Operate />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText(/CMMLU（独立身份的外部基准）/)).toBeTruthy());
    expect(screen.getByTestId("catalog-status").textContent).toBe("prepared");
    expect(clientMocks.getExternalCatalog).toHaveBeenCalled();
  });
});

/** MemoryRouter 按新地址重挂载：等价于路由切换到另一个 runId。 */
function rerenderAt(
  view: { rerender: (node: React.ReactElement) => void },
  route: string,
  element: React.ReactElement,
) {
  view.rerender(
    <MemoryRouter key={route} initialEntries={[route]}>{element}</MemoryRouter>,
  );
}
