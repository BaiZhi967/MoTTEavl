import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { BatchMonitor } from "../src/components/BatchMonitor";
import { FallbackMonitorPage, FallbackResultPage } from "../src/evalTypes/fallback/FallbackPages";
import { gridCells } from "../src/evalTypes/gsm8k/grid";
import { Gsm8kOperate } from "../src/evalTypes/gsm8k/Gsm8kOperate";
import { Gsm8kResult } from "../src/evalTypes/gsm8k/Gsm8kResult";
import { RunsOverviewPage } from "../src/pages/RunsOverviewPage";

const clientMocks = vi.hoisted(() => ({
  getRuns: vi.fn(),
  getRun: vi.fn(),
  cancelRun: vi.fn(),
  retryRun: vi.fn(),
  rescoreRun: vi.fn(),
  getReport: vi.fn(),
  getBenchmarkOverview: vi.fn(),
  importBenchmark: vi.fn(),
  createBenchmarkRun: vi.fn(),
  getModels: vi.fn(),
  subscribeRunEvents: vi.fn(() => () => undefined),
}));
vi.mock("../src/api/client", () => clientMocks);

/** 当前 location 探针，供跳转断言；本文件后续套件用例复用。 */
function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location">{location.pathname + location.search}</span>;
}

afterEach(cleanup);

const RUN = {
  id: "run-9", scenario_version: "unknown@9", status: "running",
  case_ids: ["case-1"], cases: [], scores: [],
};

describe("FallbackMonitorPage", () => {
  it("渲染运行概要与时间线，未知类型不 404", async () => {
    clientMocks.getRun.mockResolvedValue(RUN);
    render(
      <MemoryRouter initialEntries={["/runs/run-9/monitor"]}>
        <Routes>
          <Route path="/runs/:runId/monitor" element={<FallbackMonitorPage />} />
        </Routes>
      </MemoryRouter>
    );
    expect(await screen.findByText("运行 run-9")).toBeTruthy();
  });
});

describe("FallbackResultPage", () => {
  it("渲染场景与评分表", async () => {
    clientMocks.getRun.mockResolvedValue({ ...RUN, status: "completed", scores: [{ case_id: "case-1", passed: true }] });
    render(
      <MemoryRouter initialEntries={["/runs/run-9/result"]}>
        <FallbackResultPage />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText("unknown@9")).toBeTruthy());
    expect(screen.getByText(/通过率/)).toBeTruthy();
  });
});

describe("RunsOverviewPage", () => {
  it("展示类型与模型列，点击行跳转所属套件结果页", async () => {
    clientMocks.getRuns.mockResolvedValue({
      items: [
        { id: "run-42", scenario_version: "gsm8k-test-smoke@1", status: "completed", model: "glm-4.7", case_ids: [], cases: [], scores: [] },
        { id: "run-38", scenario_version: "direct-llm@1", status: "running", model: "qwen-max", case_ids: ["c1"], cases: [], scores: [] },
        { id: "run-31", scenario_version: "mystery@2", status: "failed", model: null, case_ids: [], cases: [], scores: [] },
      ],
      total: 3,
    });
    render(
      <MemoryRouter initialEntries={["/runs"]}>
        <RunsOverviewPage />
        <LocationProbe />
      </MemoryRouter>
    );
    expect(await screen.findByText("GSM8K 数学评测", { selector: "td" })).toBeTruthy();
    expect(screen.getByText("Direct LLM 评测", { selector: "td" })).toBeTruthy();
    expect(screen.getByText("通用", { selector: "td" })).toBeTruthy();
    expect(screen.getByText("glm-4.7")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "run-42" }));
    expect(screen.getByTestId("location").textContent).toBe("/gsm8k/runs/run-42/result");

    fireEvent.click(screen.getByRole("button", { name: "run-31" }));
    expect(screen.getByTestId("location").textContent).toBe("/runs/run-31/result");
  });
});

describe("Gsm8kOperate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clientMocks.getBenchmarkOverview.mockResolvedValue({
      items: [{ scenario: "gsm8k-test-smoke@1", dataset: "gsm8k-test@1", cases: 20, runs: [] }],
      total: 1,
    });
    clientMocks.getModels.mockResolvedValue({
      items: [
        { id: "glm-4.7", provider: "zhipu", capabilities: {}, context_window: 128000 },
        { id: "qwen-max", provider: "zhipu", capabilities: {}, context_window: 32000 },
      ],
    });
  });

  it("勾选多个模型发起后跳转批次过程页", async () => {
    clientMocks.createBenchmarkRun
      .mockResolvedValueOnce({ id: "run-42", status: "queued" })
      .mockResolvedValueOnce({ id: "run-43", status: "queued" });
    render(
      <MemoryRouter initialEntries={["/gsm8k"]}>
        <Gsm8kOperate />
        <LocationProbe />
      </MemoryRouter>
    );
    await screen.findByText("gsm8k-test-smoke@1");
    fireEvent.click(screen.getByLabelText("选择模型 glm-4.7"));
    fireEvent.click(screen.getByLabelText("选择模型 qwen-max"));
    fireEvent.click(screen.getByRole("button", { name: /发起跑测/ }));
    await waitFor(() => expect(clientMocks.createBenchmarkRun).toHaveBeenCalledTimes(2));
    expect(clientMocks.createBenchmarkRun).toHaveBeenCalledWith({ model: "glm-4.7" });
    expect(screen.getByTestId("location").textContent).toBe("/gsm8k/monitor?runs=run-42,run-43");
  });

  it("个别模型失败不阻塞整批，错误就地显示且提供批次入口", async () => {
    clientMocks.createBenchmarkRun
      .mockRejectedValueOnce(new Error("MODEL_DISABLED"))
      .mockResolvedValueOnce({ id: "run-44", status: "queued" });
    render(
      <MemoryRouter initialEntries={["/gsm8k"]}>
        <Gsm8kOperate />
        <LocationProbe />
      </MemoryRouter>
    );
    await screen.findByText("gsm8k-test-smoke@1");
    fireEvent.click(screen.getByLabelText("选择模型 glm-4.7"));
    fireEvent.click(screen.getByLabelText("选择模型 qwen-max"));
    fireEvent.click(screen.getByRole("button", { name: /发起跑测/ }));
    expect(await screen.findByText(/MODEL_DISABLED/)).toBeTruthy();
    expect(screen.getByTestId("location").textContent).toBe("/gsm8k");
    expect(screen.getByText(/已创建 1 个运行/)).toBeTruthy();
    const link = await screen.findByRole("link", { name: /查看批次进度/ });
    expect(link.getAttribute("href")).toBe("/gsm8k/monitor?runs=run-44");
  });

  it("导入表单提交 revision / license / version", async () => {
    clientMocks.getBenchmarkOverview.mockResolvedValue({ items: [], total: 0 });
    clientMocks.importBenchmark.mockResolvedValue({
      imported: "gsm8k-test@2",
      scenario: "gsm8k-test-smoke@2",
      cases: 20,
      source_sha256: "s",
      cases_sha256: "c",
    });
    render(
      <MemoryRouter initialEntries={["/gsm8k"]}>
        <Gsm8kOperate />
      </MemoryRouter>
    );
    const revision = await screen.findByLabelText(/官方仓库 commit/);
    fireEvent.change(revision, { target: { value: "5d0b5c9a1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b" } });
    fireEvent.change(screen.getByLabelText(/数据集版本/), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: /下载并导入/ }));
    await waitFor(() =>
      expect(clientMocks.importBenchmark).toHaveBeenCalledWith(
        expect.objectContaining({
          revision: "5d0b5c9a1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b",
          license: "MIT",
          version: "2",
        })
      ));
  });
});

describe("gridCells", () => {
  it("由 scores 推导格子状态，无分为未跑", () => {
    const run = {
      case_ids: ["c1", "c2", "c3"],
      scores: [
        { case_id: "c1", outcome: "correct", passed: true },
        { case_id: "c2", outcome: "wrong", passed: false },
      ],
    } as any;
    expect(gridCells(run)).toEqual([
      { caseId: "c1", state: "pass" },
      { caseId: "c2", state: "fail" },
      { caseId: "c3", state: "pending" },
    ]);
  });
});

describe("BatchMonitor", () => {
  it("全部终态后显示对比入口与单个结果链接", async () => {
    clientMocks.getRun.mockImplementation(async (id: string) => ({
      id, scenario_version: "gsm8k-test-smoke@1", status: "completed", model: id,
      case_ids: ["c1"], cases: [{ case_id: "c1", result: {} }], scores: [],
    }));
    render(
      <MemoryRouter initialEntries={["/gsm8k/monitor"]}>
        <BatchMonitor
          runIds={["run-42", "run-43"]}
          resultPath={(id) => `/gsm8k/runs/${id}/result`}
          comparePath="/gsm8k/compare?runs=run-42,run-43"
        />
        <LocationProbe />
      </MemoryRouter>
    );
    const compare = await screen.findByRole("link", { name: /查看对比结果/ });
    expect(compare.getAttribute("href")).toBe("/gsm8k/compare?runs=run-42,run-43");
    const idToggle = screen.getByRole("button", { name: "run-42" });
    fireEvent.click(idToggle);
    expect(idToggle.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(screen.getAllByRole("link", { name: "结果" })[0]);
    expect(screen.getByTestId("location").textContent).toBe("/gsm8k/runs/run-42/result");
  });

  it("排队中显示 Worker 提示", async () => {
    clientMocks.getRun.mockResolvedValue({
      id: "run-45", scenario_version: "gsm8k-test-smoke@1", status: "queued", model: "m",
      case_ids: ["c1"], cases: [], scores: [],
    });
    render(
      <MemoryRouter initialEntries={["/gsm8k/monitor"]}>
        <BatchMonitor runIds={["run-45"]} resultPath={(id) => `/gsm8k/runs/${id}/result`} />
      </MemoryRouter>
    );
    expect(await screen.findByText(/等待 Worker 领取/)).toBeTruthy();
  });
});

const GSM8K_RUN = {
  id: "run-42",
  scenario_version: "gsm8k-test-smoke@1",
  status: "completed",
  model: "glm-4.7",
  case_ids: ["case-1", "case-2"],
  cases: [
    { case_id: "case-1", result: { content: "72", usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 } } },
    { case_id: "case-2", result: { content: "答非所问", error: { class: "extraction", message: "无法解析数字" }, usage: { prompt_tokens: 90, completion_tokens: 30, total_tokens: 120 } } },
  ],
  scores: [
    { case_id: "case-1", outcome: "correct", passed: true },
    { case_id: "case-2", outcome: "wrong", passed: false },
  ],
  manifest: {
    benchmark_snapshot: {
      dataset: {
        cases: [
          { case_id: "case-1", input: { question: "Natalia 四月卖了 48 个夹子，五月卖了一半。共多少？" }, expected: "72" },
          { case_id: "case-2", input: { question: "每周存 18 元，四周共多少？" }, expected: "72" },
        ],
      },
    },
  },
};

describe("Gsm8kResult", () => {
  it("渲染指标卡与钻取表，展开失败行看输出与期望", async () => {
    clientMocks.getRun.mockResolvedValue(GSM8K_RUN);
    clientMocks.getReport.mockResolvedValue({
      run_id: "run-42", scenario_version: "gsm8k-test-smoke@1", status: "completed", generated_at: "",
      summary: { cases: 2, scored: 2, passed: 1, failed: 1, pass_rate: 0.5 },
      cost: { total: 0.42, price_table_versions: ["v3"] }, scores: [],
    });
    render(
      <MemoryRouter initialEntries={["/gsm8k/runs/run-42/result"]}>
        <Gsm8kResult />
      </MemoryRouter>
    );
    expect(await screen.findByText("50%")).toBeTruthy();
    expect(screen.getByText("¥0.42")).toBeTruthy();
    expect(screen.getByText("case-2").textContent).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "case-2" }));
    expect(screen.getByText(/无法解析数字/)).toBeTruthy();
    expect(screen.getByText(/Natalia|每周存/)).toBeTruthy();
    expect(screen.getByText(/期望/)).toBeTruthy();
  });
});
