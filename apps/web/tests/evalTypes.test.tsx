import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { BatchMonitor } from "../src/components/BatchMonitor";
import { DirectLlmCases } from "../src/evalTypes/directllm/DirectLlmCases";
import { DirectLlmCompare } from "../src/evalTypes/directllm/DirectLlmCompare";
import { DirectLlmMonitor } from "../src/evalTypes/directllm/DirectLlmMonitor";
import { DirectLlmOperate } from "../src/evalTypes/directllm/DirectLlmOperate";
import { DirectLlmResult } from "../src/evalTypes/directllm/DirectLlmResult";
import { FallbackMonitorPage, FallbackResultPage } from "../src/evalTypes/fallback/FallbackPages";
import { gridCells } from "../src/evalTypes/gsm8k/grid";
import { Gsm8kCases } from "../src/evalTypes/gsm8k/Gsm8kCases";
import { Gsm8kCompare } from "../src/evalTypes/gsm8k/Gsm8kCompare";
import { Gsm8kOperate } from "../src/evalTypes/gsm8k/Gsm8kOperate";
import { Gsm8kResult } from "../src/evalTypes/gsm8k/Gsm8kResult";
import { ReplayOperate } from "../src/evalTypes/replay/ReplayPages";
import { RunsOverviewPage } from "../src/pages/RunsOverviewPage";

const clientMocks = vi.hoisted(() => ({
  getRuns: vi.fn(),
  getRun: vi.fn(),
  createRun: vi.fn(),
  cancelRun: vi.fn(),
  retryRun: vi.fn(),
  rescoreRun: vi.fn(),
  getReport: vi.fn(),
  getBenchmarkOverview: vi.fn(),
  importBenchmark: vi.fn(),
  createBenchmarkRun: vi.fn(),
  getBenchmarkCases: vi.fn(),
  getDirectLlmOverview: vi.fn(),
  getDirectLlmBuiltins: vi.fn(),
  getDirectLlmCases: vi.fn(),
  importDirectLlm: vi.fn(),
  createDirectLlmRun: vi.fn(),
  getModels: vi.fn(),
  subscribeRunEvents: vi.fn(() => () => undefined),
}));
/* 保留真实导出（如 modelLabel 纯函数），仅以 mock 覆盖网络调用。 */
vi.mock("../src/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/client")>()),
  ...clientMocks,
}));

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

  it("导出报告对任意终态开放，重新评分仍限 completed", async () => {
    clientMocks.getRun.mockResolvedValue({ ...RUN, status: "failed" });
    render(
      <MemoryRouter initialEntries={["/runs/run-9/result"]}>
        <FallbackResultPage />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText("unknown@9")).toBeTruthy());
    expect((screen.getByRole("button", { name: "导出报告" }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole("button", { name: "重新评分" }) as HTMLButtonElement).disabled).toBe(true);
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

  it("运行中的 run 点击进过程页；长 ID 截断显示", async () => {
    const longId = `run-${"a".repeat(36)}`;
    clientMocks.getRuns.mockResolvedValue({
      items: [
        { id: longId, scenario_version: "direct-llm@1", status: "running", model: "m", case_ids: [], cases: [], scores: [] },
      ],
      total: 1,
    });
    render(
      <MemoryRouter initialEntries={["/runs"]}>
        <RunsOverviewPage />
        <LocationProbe />
      </MemoryRouter>
    );
    const shortName = `run-${"a".repeat(9)}…${"a".repeat(4)}`;
    fireEvent.click(await screen.findByRole("button", { name: shortName }));
    expect(screen.getByTestId("location").textContent).toBe(`/direct-llm/monitor?runs=${longId}`);
  });

  it("「过程」入口对未知套件落到通用监控路由", async () => {
    clientMocks.getRuns.mockResolvedValue({
      items: [
        { id: "run-31", scenario_version: "mystery@2", status: "completed", model: null, case_ids: [], cases: [], scores: [] },
      ],
      total: 1,
    });
    render(
      <MemoryRouter initialEntries={["/runs"]}>
        <RunsOverviewPage />
        <LocationProbe />
      </MemoryRouter>
    );
    fireEvent.click(await screen.findByRole("button", { name: "过程" }));
    expect(screen.getByTestId("location").textContent).toBe("/runs/run-31/monitor");
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
    expect(clientMocks.createBenchmarkRun).toHaveBeenCalledWith({
      model: "glm-4.7", scenario: "gsm8k-test-smoke@1", case_selection: { mode: "all" } });
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

  it("默认一键下载官方最新全量数据集（commit/版本留空由服务端解析）", async () => {
    clientMocks.getBenchmarkOverview.mockResolvedValue({ items: [], total: 0 });
    clientMocks.importBenchmark.mockResolvedValue({
      imported: "gsm8k-test@1",
      scenario: "gsm8k-test-full@1",
      scope: "full",
      benchmark: "gsm8k-full",
      cases: 1319,
      revision: "5d0b5c9a1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b",
      source_sha256: "s",
      cases_sha256: "c",
    });
    render(
      <MemoryRouter initialEntries={["/gsm8k"]}>
        <Gsm8kOperate />
      </MemoryRouter>
    );
    fireEvent.click(await screen.findByRole("button", { name: /下载最新全量数据集/ }));
    await waitFor(() =>
      expect(clientMocks.importBenchmark).toHaveBeenCalledWith({
        revision: "", license: "MIT", name: "gsm8k-test", version: "", scope: "full",
      }));
    expect(await screen.findByText(/已导入 gsm8k-test@1 · 全量 · 1319 题 · revision 5d0b5c9/)).toBeTruthy();
  });

  it("高级设置可改范围 / commit / 版本，按钮文案随 commit 变化", async () => {
    clientMocks.getBenchmarkOverview.mockResolvedValue({ items: [], total: 0 });
    clientMocks.importBenchmark.mockResolvedValue({
      imported: "gsm8k-test@2",
      scenario: "gsm8k-test-smoke@2",
      scope: "smoke",
      benchmark: "gsm8k-20",
      cases: 20,
      revision: "0123456789abcdef0123456789abcdef01234567",
      source_sha256: "s",
      cases_sha256: "c",
    });
    render(
      <MemoryRouter initialEntries={["/gsm8k"]}>
        <Gsm8kOperate />
      </MemoryRouter>
    );
    // 高级设置默认收起，展开后才是次级字段
    fireEvent.click(await screen.findByText("高级设置"));
    fireEvent.change(screen.getByLabelText("题目范围"), { target: { value: "smoke" } });
    fireEvent.change(screen.getByLabelText(/数据集版本/), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText(/官方仓库 commit/),
      { target: { value: "0123456789abcdef0123456789abcdef01234567" } });
    fireEvent.click(screen.getByRole("button", { name: /按指定 commit 下载冒烟数据集/ }));
    await waitFor(() =>
      expect(clientMocks.importBenchmark).toHaveBeenCalledWith(expect.objectContaining({
        revision: "0123456789abcdef0123456789abcdef01234567",
        version: "2",
        scope: "smoke",
      })));
    expect(await screen.findByText(/已导入 gsm8k-test@2 · 冒烟 · 20 题/)).toBeTruthy();
  });

  it("导入失败就地显示服务端错误（例如版本冲突）", async () => {
    clientMocks.getBenchmarkOverview.mockResolvedValue({ items: [], total: 0 });
    clientMocks.importBenchmark.mockRejectedValue(
      new Error("benchmark version already exists with different content; use a new version"));
    render(
      <MemoryRouter initialEntries={["/gsm8k"]}>
        <Gsm8kOperate />
      </MemoryRouter>
    );
    fireEvent.click(await screen.findByRole("button", { name: /下载最新全量数据集/ }));
    expect(await screen.findByText(/use a new version/)).toBeTruthy();
  });

  it("多个数据集可选，跑测用选中的场景而不是列表第一个", async () => {
    clientMocks.getBenchmarkOverview.mockResolvedValue({
      items: [
        { scenario: "gsm8k-test-full@2", dataset: "gsm8k-test@2", scope: "full", cases: 1319, runs: [] },
        { scenario: "gsm8k-test-smoke@1", dataset: "gsm8k-test@1", scope: "smoke", cases: 20, runs: [] },
      ],
      total: 2,
    });
    clientMocks.createBenchmarkRun.mockResolvedValue({ id: "run-77", status: "queued" });
    render(
      <MemoryRouter initialEntries={["/gsm8k"]}>
        <Gsm8kOperate />
        <LocationProbe />
      </MemoryRouter>
    );
    const picker = (await screen.findByLabelText("运行数据集")) as HTMLSelectElement;
    // 默认选中题数最多的数据集（全量），切换到冒烟则发起更省的运行
    expect(picker.value).toBe("gsm8k-test-full@2");
    fireEvent.change(picker, { target: { value: "gsm8k-test-smoke@1" } });
    expect(screen.getByText(/gsm8k-test@1 · 冒烟 · 20 题/)).toBeTruthy();
    fireEvent.click(screen.getByLabelText("选择模型 glm-4.7"));
    fireEvent.click(screen.getByRole("button", { name: /20 题/ }));
    await waitFor(() =>
      expect(clientMocks.createBenchmarkRun).toHaveBeenCalledWith(
        { model: "glm-4.7", scenario: "gsm8k-test-smoke@1", case_selection: { mode: "all" } }));
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe("/gsm8k/monitor?runs=run-77"));
  });

  it("高级设置里改范围不改动已导入的数据集选择", async () => {
    clientMocks.getBenchmarkOverview.mockResolvedValue({
      items: [{ scenario: "gsm8k-test-smoke@1", dataset: "gsm8k-test@1", scope: "smoke", cases: 20, runs: [] }],
      total: 1,
    });
    render(
      <MemoryRouter initialEntries={["/gsm8k"]}>
        <Gsm8kOperate />
      </MemoryRouter>
    );
    // 默认全量 + 版本/commit 留空（服务端自动解析最新 commit 与版本号）
    fireEvent.click(await screen.findByText("高级设置"));
    expect((screen.getByLabelText("题目范围") as HTMLSelectElement).value).toBe("full");
    expect((screen.getByLabelText(/数据集版本/) as HTMLInputElement).value).toBe("");
    expect(screen.getByRole("button", { name: /下载最新全量数据集/ })).toBeTruthy();
  });

  it("synthetic 夹具数据集显式提示不是官方题目", async () => {
    clientMocks.getBenchmarkOverview.mockResolvedValue({
      items: [{
        scenario: "gsm8k-test-smoke@1", dataset: "gsm8k-test@1", scope: "smoke", cases: 20, runs: [],
        provenance: { synthetic: true, revision: "a".repeat(40) },
      }],
      total: 1,
    });
    render(
      <MemoryRouter initialEntries={["/gsm8k"]}>
        <Gsm8kOperate />
      </MemoryRouter>
    );
    expect(await screen.findByText(/合成夹具（synthetic）/)).toBeTruthy();
  });

  it("随机 N 题：题数与种子进 case_selection，按钮显示真实题数", async () => {
    clientMocks.createBenchmarkRun.mockResolvedValue({ id: "run-90", status: "queued" });
    render(
      <MemoryRouter initialEntries={["/gsm8k"]}>
        <Gsm8kOperate />
        <LocationProbe />
      </MemoryRouter>
    );
    await screen.findByText("gsm8k-test-smoke@1");
    fireEvent.change(screen.getByLabelText("本次运行题目"), { target: { value: "random" } });
    fireEvent.change(screen.getByLabelText("随机题数"), { target: { value: "7" } });
    fireEvent.change(screen.getByLabelText(/随机种子/), { target: { value: "deadbeef" } });
    fireEvent.click(screen.getByLabelText("选择模型 glm-4.7"));
    expect(screen.getByRole("button", { name: /1 个模型 × 7 题/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /发起跑测/ }));
    await waitFor(() =>
      expect(clientMocks.createBenchmarkRun).toHaveBeenCalledWith({
        model: "glm-4.7", scenario: "gsm8k-test-smoke@1",
        case_selection: { mode: "random", count: 7, seed: "deadbeef" } }));
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe("/gsm8k/monitor?runs=run-90"));
  });

  it("指定题目：读取题目页勾选，未选择时禁用发起", async () => {
    sessionStorage.clear();
    clientMocks.createBenchmarkRun.mockResolvedValue({ id: "run-91", status: "queued" });
    const { unmount } = render(
      <MemoryRouter initialEntries={["/gsm8k"]}>
        <Gsm8kOperate />
      </MemoryRouter>
    );
    await screen.findByText("gsm8k-test-smoke@1");
    fireEvent.change(screen.getByLabelText("本次运行题目"), { target: { value: "ids" } });
    expect(screen.getByText(/尚未选择题目/)).toBeTruthy();
    expect((screen.getByRole("button", { name: /发起跑测/ }) as HTMLButtonElement).disabled).toBe(true);
    unmount();

    sessionStorage.setItem("motte.gsm8k.case-selection",
      JSON.stringify({ dataset: "gsm8k-test@1", caseIds: ["gsm8k-test-0001", "gsm8k-test-0003"] }));
    render(
      <MemoryRouter initialEntries={["/gsm8k"]}>
        <Gsm8kOperate />
      </MemoryRouter>
    );
    await screen.findByText("gsm8k-test-smoke@1");
    fireEvent.change(screen.getByLabelText("本次运行题目"), { target: { value: "ids" } });
    expect(screen.getByText(/已选 2 题（gsm8k-test@1）/)).toBeTruthy();
    fireEvent.click(screen.getByLabelText("选择模型 glm-4.7"));
    fireEvent.click(screen.getByRole("button", { name: /1 个模型 × 2 题/ }));
    await waitFor(() =>
      expect(clientMocks.createBenchmarkRun).toHaveBeenCalledWith({
        model: "glm-4.7", scenario: "gsm8k-test-smoke@1",
        case_selection: { mode: "ids", case_ids: ["gsm8k-test-0001", "gsm8k-test-0003"] } }));
    sessionStorage.clear();
  });

  it("勾选支持推理的模型后出现思考强度选择并随运行提交", async () => {
    clientMocks.getModels.mockResolvedValue({
      items: [
        { id: "reasoner", provider: "local", capabilities: {}, context_window: 64000,
          reasoning: { supported: true, levels: ["low", "high"], control: null, default_level: "low" } },
        { id: "plain", provider: "local", capabilities: {}, context_window: 8000 },
      ],
    });
    clientMocks.createBenchmarkRun.mockResolvedValue({ id: "run-92", status: "queued" });
    render(
      <MemoryRouter initialEntries={["/gsm8k"]}>
        <Gsm8kOperate />
      </MemoryRouter>
    );
    await screen.findByText("gsm8k-test-smoke@1");
    expect(screen.queryByLabelText(/的思考强度/)).toBeNull(); // 未选中模型时不出现
    fireEvent.click(screen.getByLabelText("选择模型 reasoner"));
    fireEvent.click(screen.getByLabelText("选择模型 plain"));
    expect(screen.getByText(/输出上限固定 1024/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/reasoner 的思考强度/), { target: { value: "high" } });
    fireEvent.click(screen.getByRole("button", { name: /发起跑测/ }));
    await waitFor(() =>
      expect(clientMocks.createBenchmarkRun).toHaveBeenCalledWith(expect.objectContaining({
        model: "reasoner", reasoning_level: "high" })));
    const plainCall = clientMocks.createBenchmarkRun.mock.calls.find((call) => call[0].model === "plain");
    expect(plainCall && "reasoning_level" in plainCall[0]).toBe(false); // 不支持推理的模型不带等级
  });
});

describe("Gsm8kCases", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
    clientMocks.getBenchmarkOverview.mockResolvedValue({
      items: [{ scenario: "gsm8k-test-full@1", dataset: "gsm8k-test@1", scope: "full", cases: 60, runs: [] }],
      total: 1,
    });
    clientMocks.getBenchmarkCases.mockResolvedValue({
      dataset: "gsm8k-test@1", total: 3, dataset_total: 60, offset: 0, limit: 25, query: "",
      items: [
        { case_id: "gsm8k-test-0000", input: "Janet's ducks lay 16 eggs.", expected: "18", source_line: 1 },
        { case_id: "gsm8k-test-0001", input: "A robe takes 2 bolts of blue fiber.", expected: "3", source_line: 2 },
        { case_id: "gsm8k-test-0002", input: "Josh decides to try flipping a house.", expected: "4", source_line: 3 },
      ],
    });
  });

  it("搜索走后端查询参数", async () => {
    render(
      <MemoryRouter initialEntries={["/gsm8k/cases"]}>
        <Gsm8kCases />
      </MemoryRouter>
    );
    expect(await screen.findByText("Janet's ducks lay 16 eggs.")).toBeTruthy();
    expect(clientMocks.getBenchmarkCases).toHaveBeenCalledWith(
      { dataset: "gsm8k-test@1", offset: 0, limit: 25, query: "" });
    fireEvent.change(screen.getByLabelText("搜索题目关键词"), { target: { value: "ducks" } });
    fireEvent.click(screen.getByRole("button", { name: /搜索/ }));
    await waitFor(() => expect(clientMocks.getBenchmarkCases).toHaveBeenCalledWith(
      { dataset: "gsm8k-test@1", offset: 0, limit: 25, query: "ducks" }));
  });

  it("翻页带上 offset，末页禁用下一页", async () => {
    clientMocks.getBenchmarkCases.mockResolvedValue({
      dataset: "gsm8k-test@1", total: 60, dataset_total: 60, offset: 0, limit: 25, query: "",
      items: [{ case_id: "gsm8k-test-0000", input: "Janet's ducks lay 16 eggs.", expected: "18" }],
    });
    render(
      <MemoryRouter initialEntries={["/gsm8k/cases"]}>
        <Gsm8kCases />
      </MemoryRouter>
    );
    await screen.findByText(/匹配 60 \/ 数据集 60 题 · 第 1\/3 页/);
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(clientMocks.getBenchmarkCases).toHaveBeenCalledWith(
      expect.objectContaining({ offset: 25 })));
    await screen.findByText(/第 2\/3 页/);
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await screen.findByText(/第 3\/3 页/);
    expect((screen.getByRole("button", { name: "下一页" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "上一页" }));
    await screen.findByText(/第 2\/3 页/);
  });

  it("勾选题目后用 sessionStorage 带回操作页并发起指定题目", async () => {
    render(
      <MemoryRouter initialEntries={["/gsm8k/cases"]}>
        <Gsm8kCases />
        <LocationProbe />
      </MemoryRouter>
    );
    await screen.findByText("Janet's ducks lay 16 eggs.");
    fireEvent.click(screen.getByLabelText("选择 gsm8k-test-0000"));
    fireEvent.click(screen.getByLabelText("选择 gsm8k-test-0002"));
    expect(screen.getByText(/已选 2 题/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /用所选 2 题发起跑测/ }));
    expect(screen.getByTestId("location").textContent).toBe("/gsm8k");
    expect(JSON.parse(sessionStorage.getItem("motte.gsm8k.case-selection") ?? "{}")).toEqual({
      dataset: "gsm8k-test@1", caseIds: ["gsm8k-test-0000", "gsm8k-test-0002"] });

    // 回到操作页读同一份存储
    render(
      <MemoryRouter initialEntries={["/gsm8k"]}>
        <Gsm8kOperate />
      </MemoryRouter>
    );
    await screen.findByText("gsm8k-test-full@1");
    fireEvent.change(screen.getByLabelText("本次运行题目"), { target: { value: "ids" } });
    expect(screen.getByText(/已选 2 题（gsm8k-test@1）/)).toBeTruthy();
    sessionStorage.clear();
  });

  it("粘贴 case id 精确校验：越界与格式错误会被剔除并提示", async () => {
    render(
      <MemoryRouter initialEntries={["/gsm8k/cases"]}>
        <Gsm8kCases />
      </MemoryRouter>
    );
    await screen.findByText("Janet's ducks lay 16 eggs.");
    fireEvent.change(screen.getByLabelText("粘贴 case ID"),
      { target: { value: "gsm8k-test-0005, gsm8k-test-9999, oops" } });
    fireEvent.click(screen.getByRole("button", { name: "加入" }));
    expect(screen.getByText(/忽略 2 个不在该数据集内的 id/)).toBeTruthy();
    expect(screen.getByText(/已选 1 题/)).toBeTruthy();
  });

  it("全选本页、取消本页与清空已选", async () => {
    render(
      <MemoryRouter initialEntries={["/gsm8k/cases"]}>
        <Gsm8kCases />
      </MemoryRouter>
    );
    await screen.findByText("Janet's ducks lay 16 eggs.");
    fireEvent.click(screen.getByRole("button", { name: "全选本页" }));
    expect(screen.getByText(/已选 3 题/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "取消本页" }));
    expect(screen.getByText(/已选 0 题/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "全选本页" }));
    fireEvent.click(screen.getByRole("button", { name: "清空已选" }));
    expect(screen.getByText(/已选 0 题/)).toBeTruthy();
    expect(sessionStorage.getItem("motte.gsm8k.case-selection")).toBeNull();
  });
});

describe("gridCells", () => {
  it("由 scores 推导格子状态：实际错答标红，not_attempted 与无分同为未跑", () => {
    const run = {
      case_ids: ["c1", "c2", "c3", "c4"],
      scores: [
        { case_id: "c1", outcome: "correct", passed: true },
        { case_id: "c2", outcome: "wrong_answer", passed: false },
        { case_id: "c3", outcome: "not_attempted", passed: false },
      ],
    } as any;
    expect(gridCells(run)).toEqual([
      { caseId: "c1", state: "pass" },
      { caseId: "c2", state: "fail" },
      { caseId: "c3", state: "pending" },
      { caseId: "c4", state: "pending" },
    ]);
  });
});

describe("BatchMonitor", () => {
  it("全部终态后显示对比入口与单个结果链接，模型列从 manifest 摘要", async () => {
    /* 详情端点不回顶层 model（仅列表端点回）；两分支各覆盖一次 */
    clientMocks.getRun.mockImplementation(async (id: string) => ({
      id, scenario_version: "gsm8k-test-smoke@1", status: "completed",
      manifest: id === "run-42" ? { model: "glm-4.7" } : { provider: { model: "qwen-max" } },
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
    expect(screen.getByText("glm-4.7", { selector: "span.mono" })).toBeTruthy();
    expect(screen.getByText("qwen-max", { selector: "span.mono" })).toBeTruthy();
    const idToggle = screen.getByRole("button", { name: "run-42" });
    fireEvent.click(idToggle);
    expect(idToggle.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(screen.getAllByRole("link", { name: "结果" })[0]);
    expect(screen.getByTestId("location").textContent).toBe("/gsm8k/runs/run-42/result");
  });

  it("排队中显示 Worker 提示", async () => {
    clientMocks.getRun.mockResolvedValue({
      id: "run-45", scenario_version: "gsm8k-test-smoke@1", status: "queued",
      manifest: { model: "m" },
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
  case_ids: ["case-1", "case-2", "case-3"],
  cases: [
    { case_id: "case-1", result: { content: "72", usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 } } },
    { case_id: "case-2", result: { content: "答非所问", error: { class: "extraction", message: "无法解析数字" }, usage: { prompt_tokens: 90, completion_tokens: 30, total_tokens: 120 } } },
  ],
  scores: [
    { case_id: "case-1", outcome: "correct", passed: true },
    { case_id: "case-2", outcome: "wrong_answer", passed: false },
    { case_id: "case-3", outcome: "not_attempted", passed: false },
  ],
  manifest: {
    model: "glm-4.7",
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
    expect(await screen.findByText("33%")).toBeTruthy();
    expect(screen.getByText("¥0.42")).toBeTruthy();
    expect(screen.getByText("成本 · pt v3")).toBeTruthy();
    expect(screen.getByText(/应答 2/)).toBeTruthy();
    expect(screen.getByText("case-2").textContent).toBeTruthy();
    /* 词汇表对齐真实 scorer：wrong_answer 显示答错，not_attempted 显示未尝试 */
    expect(screen.getByText("答错", { selector: ".status-badge" })).toBeTruthy();
    expect(screen.getByText("未尝试", { selector: ".status-badge" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "case-2" }));
    expect(screen.getByText(/无法解析数字/)).toBeTruthy();
    expect(screen.getByText(/Natalia|每周存/)).toBeTruthy();
    expect(screen.getByText(/期望/)).toBeTruthy();
  });

  it("显示本次运行的题目选择（子集运行给出版本化证据）", async () => {
    clientMocks.getRun.mockResolvedValue({
      ...GSM8K_RUN,
      manifest: {
        ...GSM8K_RUN.manifest,
        benchmark_provenance: {
          selected_count: 1319,
          run_selection: { mode: "random", count: 100, seed: "deadbeefcafe" },
        },
      },
    });
    clientMocks.getReport.mockResolvedValue({ cost: null, scores: [] });
    render(
      <MemoryRouter initialEntries={["/gsm8k/runs/run-42/result"]}>
        <Gsm8kResult />
      </MemoryRouter>
    );
    expect(await screen.findByText(/本次题目：随机 100 题（seed deadbeef…，可复现）/)).toBeTruthy();
  });
});

describe("Gsm8kCompare", () => {
  it("并排指标与答错重合", async () => {
    clientMocks.getRun.mockImplementation(async (id: string) => ({
      id,
      scenario_version: "gsm8k-test-smoke@1",
      status: "completed",
      case_ids: ["case-1", "case-2", "case-3"],
      cases: [],
      scores: id === "run-41"
        ? [{ case_id: "case-1", passed: true }, { case_id: "case-2", passed: false }, { case_id: "case-3", passed: true }]
        : [{ case_id: "case-1", passed: true }, { case_id: "case-2", passed: false }, { case_id: "case-3", passed: false }],
      /* 详情端点不回顶层 model；run-41 走 manifest.model、run-42 走 manifest.provider.model */
      manifest: {
        ...(id === "run-41" ? { model: "model-run-41" } : { provider: { model: "model-run-42" } }),
        benchmark_snapshot: { dataset: { cases: [
          { case_id: "case-1", input: { question: "Q1" }, expected: "1" },
          { case_id: "case-2", input: { question: "Q2" }, expected: "2" },
          { case_id: "case-3", input: { question: "Q3" }, expected: "3" },
        ] } },
      },
    }));
    clientMocks.getReport.mockImplementation(async (id: string) =>
      id === "run-41" ? { cost: { total: 0.42 } } : { cost: { total: null } }
    );
    render(
      <MemoryRouter initialEntries={["/gsm8k/compare?runs=run-41,run-42"]}>
        <Gsm8kCompare />
      </MemoryRouter>
    );
    expect(await screen.findByText("model-run-41")).toBeTruthy();
    expect(screen.getByText("model-run-42")).toBeTruthy();
    expect(screen.getByText("67%")).toBeTruthy();
    expect(screen.getByText("33%")).toBeTruthy();
    expect(screen.getByText("¥0.42")).toBeTruthy();
    expect(screen.getByText("—", { selector: "td" })).toBeTruthy();
    /* 答错题重合单元格与逐题下钻按钮都含 case-2 文本，getByText(/case-2/) 会命中多个元素，按选择器分别断言 */
    expect(screen.getByText("case-2", { selector: "td" })).toBeTruthy();
    expect(screen.getByText("case-2", { selector: "button" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "case-2" }));
    expect(screen.getByText("Q2")).toBeTruthy();
    expect(screen.getByText("2", { selector: "span.mono" })).toBeTruthy();
    /* 无结果回退文案不加引号（JSON.stringify 路径只用于真实对象） */
    expect(screen.getAllByText("（无结果）").length).toBe(2);
  });

  it("缺少 runs 参数时不崩溃", async () => {
    render(
      <MemoryRouter initialEntries={["/gsm8k/compare"]}>
        <Gsm8kCompare />
      </MemoryRouter>
    );
    expect(await screen.findByText("GSM8K · 多模型对比")).toBeTruthy();
    expect(screen.getByText("无")).toBeTruthy();
    /* 空列时 colSpan 至少为 1，避免渲染 colspan="0" */
    expect(screen.getByText("无").getAttribute("colspan")).toBe("1");
  });
});

const DIRECT_PRESET = {
  scenario: "direct-llm-classify@1",
  dataset: "direct-llm-classify@1",
  suite: "direct-llm",
  eval: {
    suite: "direct-llm", scorer: "contains", scorer_version: "direct-llm-answer-v1",
    prompt_version: "direct-llm-verbatim-v1", selected_count: 3, max_output_tokens: 1024,
    max_retries: 0,
  },
  provenance: { source: "builtin:direct-llm-classify", license: "internal-sample" },
  cases: 3,
  runs: [],
};

const DIRECT_BUILTIN = {
  id: "direct-llm-classify", label: "四分类打标（contains）",
  description: "把工单分到 billing/technical/account/other，输出含标签即通过。",
  scorer: "contains", source: "builtin:direct-llm-classify", importable: true, cases: 8,
};

const REASONING_MODEL = {
  id: "glm-4.7", provider: "zhipu", capabilities: {},
  reasoning: { supported: true, levels: ["low", "high"], default_level: "high", control: "{}" },
};

function directRun(overrides: Record<string, any> = {}) {
  return {
    id: "run-51", scenario_version: "direct-llm-classify@1", status: "completed",
    manifest: {
      model: "glm-4.7",
      benchmark_provenance: {
        suite: "direct-llm", scorer: "contains", scorer_version: "direct-llm-answer-v1",
        selected_count: 3, run_selection: { mode: "all", count: 3, seed: null },
      },
      benchmark_snapshot: {
        dataset: {
          cases: [
            { case_id: "case-1", input: "工单 1：重复扣费", expected: "billing", metadata: { source_line: 1 } },
            { case_id: "case-2", input: "工单 2：报表乱码", expected: "technical", metadata: { source_line: 2 } },
            { case_id: "case-3", input: "工单 3：开放问题", metadata: { source_line: 3 } },
          ],
        },
      },
    },
    case_ids: ["case-1", "case-2", "case-3"],
    cases: [
      { case_id: "case-1", result: { content: "billing" } },
      { case_id: "case-2", result: { content: "other" } },
    ],
    scores: [
      { case_id: "case-1", passed: true, outcome: "correct", judged: true, scorer: "contains" },
      { case_id: "case-2", passed: false, outcome: "wrong_answer", judged: true, scorer: "contains" },
      { case_id: "case-3", passed: false, outcome: "no_expectation", judged: false, scorer: "contains" },
    ],
    ...overrides,
  };
}

describe("DirectLlmOperate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clientMocks.getModels.mockResolvedValue({
      items: [REASONING_MODEL, { id: "qwen-max", provider: "zhipu", capabilities: {} }],
    });
    clientMocks.getDirectLlmOverview.mockResolvedValue({ items: [DIRECT_PRESET], total: 1 });
    clientMocks.getDirectLlmBuiltins.mockResolvedValue({ items: [DIRECT_BUILTIN], total: 1 });
  });

  it("展示 pinned 数据集与评分器，选模型后按 manifest.model 创建并跳批次过程页", async () => {
    clientMocks.createDirectLlmRun.mockResolvedValue({ id: "run-51", status: "queued" });
    render(
      <MemoryRouter initialEntries={["/direct-llm"]}>
        <DirectLlmOperate />
        <LocationProbe />
      </MemoryRouter>
    );
    expect(await screen.findByText("direct-llm-classify@1", { selector: ".mono" })).toBeTruthy();
    expect(screen.getByText(/3 题 · contains（输出包含期望串）/)).toBeTruthy();
    expect(screen.getByText(/来源 builtin:direct-llm-classify/)).toBeTruthy();

    fireEvent.click(screen.getByLabelText("选择模型 glm-4.7"));
    fireEvent.click(screen.getByRole("button", { name: /发起评测/ }));
    await waitFor(() => expect(clientMocks.createDirectLlmRun).toHaveBeenCalledWith({
      model: "glm-4.7",
      scenario: "direct-llm-classify@1",
      case_selection: { mode: "all" },
    }));
    await waitFor(() => expect(screen.getByTestId("location").textContent)
      .toBe("/direct-llm/monitor?runs=run-51"));
  });

  it("随机子集与跑测参数一起进 manifest", async () => {
    clientMocks.createDirectLlmRun.mockResolvedValue({ id: "run-52", status: "queued" });
    render(
      <MemoryRouter initialEntries={["/direct-llm"]}>
        <DirectLlmOperate />
      </MemoryRouter>
    );
    await screen.findByLabelText("选择模型 glm-4.7");
    fireEvent.click(screen.getByLabelText("选择模型 glm-4.7"));
    fireEvent.change(screen.getByLabelText("本次运行题目"), { target: { value: "random" } });
    fireEvent.change(screen.getByLabelText("随机题数"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("随机种子（写进运行快照，可复现）"),
      { target: { value: "deadbeef" } });
    fireEvent.change(screen.getByLabelText("temperature"), { target: { value: "0.3" } });
    fireEvent.change(screen.getByLabelText("max_output_tokens"), { target: { value: "256" } });
    fireEvent.change(screen.getByLabelText(/思考强度/), { target: { value: "low" } });
    fireEvent.click(screen.getByRole("button", { name: /发起评测/ }));
    await waitFor(() => expect(clientMocks.createDirectLlmRun).toHaveBeenCalledWith({
      model: "glm-4.7",
      scenario: "direct-llm-classify@1",
      case_selection: { mode: "random", count: 2, seed: "deadbeef" },
      parameters: { temperature: 0.3, max_output_tokens: 256 },
      reasoning_level: "low",
    }));
  });

  it("思考强度按模型各自记录，不支持推理的模型不带该字段", async () => {
    clientMocks.createDirectLlmRun.mockResolvedValue({ id: "run-53", status: "queued" });
    render(
      <MemoryRouter initialEntries={["/direct-llm"]}>
        <DirectLlmOperate />
      </MemoryRouter>
    );
    await screen.findByLabelText("选择模型 glm-4.7");
    fireEvent.click(screen.getByLabelText("选择模型 glm-4.7"));
    fireEvent.change(screen.getByLabelText(/思考强度/), { target: { value: "low" } });
    /* 加入第二个不声明推理能力的模型：批次里只有 glm-4.7 带 low */
    fireEvent.click(screen.getByLabelText("选择模型 qwen-max"));
    fireEvent.click(screen.getByRole("button", { name: /发起评测/ }));
    await waitFor(() => expect(clientMocks.createDirectLlmRun).toHaveBeenCalledTimes(2));
    const byModel = new Map(clientMocks.createDirectLlmRun.mock.calls
      .map((call) => [call[0].model, call[0]]));
    expect(byModel.get("glm-4.7")!.reasoning_level).toBe("low");
    expect(byModel.get("qwen-max")!.reasoning_level).toBeUndefined();
    /* 取消勾选 glm-4.7 后，它的强度不会跟着别的模型走 */
    fireEvent.click(screen.getByLabelText("选择模型 glm-4.7"));
    clientMocks.createDirectLlmRun.mockClear();
    fireEvent.click(screen.getByRole("button", { name: /发起评测/ }));
    await waitFor(() => expect(clientMocks.createDirectLlmRun).toHaveBeenCalledTimes(1));
    expect(clientMocks.createDirectLlmRun.mock.calls[0][0].reasoning_level).toBeUndefined();
  });

  it("导入内置样例后就地提示回执并切到新场景", async () => {
    clientMocks.importDirectLlm.mockResolvedValue({
      imported: "direct-llm-classify@1", scenario: "direct-llm-classify@1", suite: "direct-llm",
      scorer: "contains", cases: 8, source: "builtin:direct-llm-classify",
      source_sha256: "a".repeat(64), cases_sha256: "b".repeat(64),
    });
    render(
      <MemoryRouter initialEntries={["/direct-llm"]}>
        <DirectLlmOperate />
      </MemoryRouter>
    );
    await screen.findByLabelText("内置样例");
    fireEvent.click(screen.getByRole("button", { name: "导入所选样例" }));
    await waitFor(() => expect(clientMocks.importDirectLlm).toHaveBeenCalledWith({
      builtin: "direct-llm-classify", version: "",
    }));
    expect(await screen.findByText(/已导入 direct-llm-classify@1 · .* · 8 题/)).toBeTruthy();
  });

  it("粘贴 JSONL 导入时提交内容与高级设置", async () => {
    clientMocks.importDirectLlm.mockResolvedValue({
      imported: "my-set@1", scenario: "my-set@1", suite: "direct-llm", scorer: "regex",
      cases: 2, source: "manual", source_sha256: "a".repeat(64), cases_sha256: "b".repeat(64),
    });
    render(
      <MemoryRouter initialEntries={["/direct-llm"]}>
        <DirectLlmOperate />
      </MemoryRouter>
    );
    await screen.findByLabelText("JSONL 内容");
    fireEvent.change(screen.getByLabelText("JSONL 内容"), {
      target: { value: '{"input": "1+1=?", "expected": "2"}' },
    });
    fireEvent.change(screen.getByLabelText("数据集名"), { target: { value: "my-set" } });
    fireEvent.change(screen.getByLabelText("默认评分器"), { target: { value: "regex" } });
    fireEvent.change(screen.getByLabelText("来源标记"), { target: { value: "manual" } });
    fireEvent.click(screen.getByRole("button", { name: "导入 JSONL" }));
    await waitFor(() => expect(clientMocks.importDirectLlm).toHaveBeenCalledWith(
      expect.objectContaining({
        content: '{"input": "1+1=?", "expected": "2"}',
        name: "my-set", scorer: "regex", source: "manual", license: "internal-sample",
      }),
    ));
    expect(await screen.findByText(/已导入 my-set@1/)).toBeTruthy();
  });

  it("导入失败就地报错，不影响其它卡片", async () => {
    clientMocks.importDirectLlm.mockRejectedValue(new Error("invalid JSON at source line 2"));
    render(
      <MemoryRouter initialEntries={["/direct-llm"]}>
        <DirectLlmOperate />
      </MemoryRouter>
    );
    await screen.findByLabelText("内置样例");
    fireEvent.click(screen.getByRole("button", { name: "导入所选样例" }));
    expect(await screen.findByText(/invalid JSON at source line 2/)).toBeTruthy();
  });

  it("没有数据集时禁用发起并提示导入", async () => {
    clientMocks.getDirectLlmOverview.mockResolvedValue({ items: [], total: 0 });
    render(
      <MemoryRouter initialEntries={["/direct-llm"]}>
        <DirectLlmOperate />
      </MemoryRouter>
    );
    expect(await screen.findByText(/尚未导入数据集/)).toBeTruthy();
    expect((screen.getByRole("button", { name: /发起评测/ }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("个别模型失败不阻塞整批，错误就地显示且提供批次入口", async () => {
    clientMocks.createDirectLlmRun
      .mockRejectedValueOnce(new Error("MODEL_DISABLED"))
      .mockResolvedValueOnce({ id: "run-55", status: "queued" });
    render(
      <MemoryRouter initialEntries={["/direct-llm"]}>
        <DirectLlmOperate />
        <LocationProbe />
      </MemoryRouter>
    );
    await screen.findByLabelText("选择模型 glm-4.7");
    fireEvent.click(screen.getByLabelText("选择模型 glm-4.7"));
    fireEvent.click(screen.getByLabelText("选择模型 qwen-max"));
    fireEvent.click(screen.getByRole("button", { name: /发起评测/ }));
    expect(await screen.findByText(/MODEL_DISABLED/)).toBeTruthy();
    expect(screen.getByTestId("location").textContent).toBe("/direct-llm");
    const link = await screen.findByRole("link", { name: /查看批次进度/ });
    expect(link.getAttribute("href")).toBe("/direct-llm/monitor?runs=run-55");
  });
});

describe("DirectLlmCases", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
    clientMocks.getDirectLlmOverview.mockResolvedValue({ items: [DIRECT_PRESET], total: 1 });
    clientMocks.getDirectLlmCases.mockResolvedValue({
      dataset: "direct-llm-classify@1", total: 3, dataset_total: 3, offset: 0, limit: 25, query: "",
      items: [
        { case_id: "case-1", input: "工单 1：重复扣费", expected: "billing", scorer: "contains", source_line: 1 },
        { case_id: "case-2", input: "工单 2：报表乱码", expected: "technical", scorer: "contains", source_line: 2 },
        { case_id: "case-3", input: "工单 3：开放问题", expected: null, scorer: "exact", source_line: 3 },
      ],
    });
  });

  it("浏览题目并勾选后带着选择回到操作页", async () => {
    render(
      <MemoryRouter initialEntries={["/direct-llm/cases"]}>
        <DirectLlmCases />
        <LocationProbe />
      </MemoryRouter>
    );
    expect(await screen.findByText("工单 1：重复扣费")).toBeTruthy();
    /* 没有 expected 的题显式标注为无判定 */
    expect(screen.getByText("（无判定）")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("选择 case-2"));
    fireEvent.click(screen.getByRole("button", { name: /用所选 1 题发起评测/ }));
    expect(screen.getByTestId("location").textContent).toBe("/direct-llm");
    expect(JSON.parse(sessionStorage.getItem("motte.direct-llm.case-selection")!)).toEqual({
      dataset: "direct-llm-classify@1", caseIds: ["case-2"],
    });
  });

  it("粘贴 case id 合并去重，未知 id 交由服务端拒绝", async () => {
    render(
      <MemoryRouter initialEntries={["/direct-llm/cases"]}>
        <DirectLlmCases />
      </MemoryRouter>
    );
    await screen.findByText("工单 1：重复扣费");
    fireEvent.change(screen.getByLabelText("粘贴 case ID"), {
      target: { value: "case-1, case-9 case-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "加入" }));
    expect(await screen.findByText(/已加入 2 个 id/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /用所选 2 题发起评测/ }));
    expect(JSON.parse(sessionStorage.getItem("motte.direct-llm.case-selection")!).caseIds)
      .toEqual(["case-1", "case-9"]);
  });

  it("搜索把关键词交给服务端并从第一页重查", async () => {
    render(
      <MemoryRouter initialEntries={["/direct-llm/cases"]}>
        <DirectLlmCases />
      </MemoryRouter>
    );
    await screen.findByText("工单 1：重复扣费");
    fireEvent.change(screen.getByLabelText("搜索题目关键词"), { target: { value: "报表" } });
    fireEvent.click(screen.getByRole("button", { name: "搜索" }));
    await waitFor(() => expect(clientMocks.getDirectLlmCases).toHaveBeenLastCalledWith({
      dataset: "direct-llm-classify@1", offset: 0, limit: 25, query: "报表",
    }));
  });
});

describe("DirectLlmMonitor", () => {
  it("逐题格子按 outcome 着色并附运行时间线", async () => {
    clientMocks.getRun.mockResolvedValue(directRun());
    render(
      <MemoryRouter initialEntries={["/direct-llm/monitor?runs=run-51"]}>
        <DirectLlmMonitor />
      </MemoryRouter>
    );
    expect(await screen.findByText("运行过程")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "run-51" }));
    await waitFor(() => expect(document.querySelectorAll(".grid-cell").length).toBe(3));
  });
});

describe("DirectLlmResult", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clientMocks.getReport.mockResolvedValue({
      run_id: "run-51", scenario_version: "direct-llm-classify@1", status: "completed",
      generated_at: "2026-09-20T00:00:00Z",
      summary: { cases: 3, scored: 2, passed: 1, failed: 2, pass_rate: 0.5, judged: 2 },
      cost: { total: 0.0015, price_table_versions: ["v1"] },
      scores: [],
    });
  });

  it("通过率用判定题数做分母，无判定单独计数", async () => {
    clientMocks.getRun.mockResolvedValue(directRun());
    render(
      <MemoryRouter initialEntries={["/direct-llm/runs/run-51/result"]}>
        <DirectLlmResult />
      </MemoryRouter>
    );
    expect(await screen.findByText(/通过率 · 1\/2/)).toBeTruthy();
    expect(screen.getByText("50%", { selector: ".metric-value" })).toBeTruthy();
    expect(screen.getByText(/判定题数（选中 3 · 无判定 1）/)).toBeTruthy();
    expect(screen.getByText("glm-4.7", { selector: ".metric-value" })).toBeTruthy();
    expect(screen.getByText("¥0.0015", { selector: ".metric-value" })).toBeTruthy();
    expect(screen.getByText("通过")).toBeTruthy();
    expect(screen.getByText("不通过")).toBeTruthy();
    expect(screen.getByText("无判定")).toBeTruthy();
  });

  it("钻取显示题面、输出、期望与生效评分器", async () => {
    clientMocks.getRun.mockResolvedValue(directRun());
    render(
      <MemoryRouter initialEntries={["/direct-llm/runs/run-51/result"]}>
        <DirectLlmResult />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText("case-3")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /case-3/ }));
    const detail = document.querySelector(".drill-detail") as HTMLElement;
    expect(within(detail).getByText("工单 3：开放问题")).toBeTruthy();
    expect(within(detail).getByText("（无判定）")).toBeTruthy();
    expect(within(detail).getByText("评分器")).toBeTruthy();
    /* 无判定题也照常显示它生效的评分器（数据集默认 contains） */
    expect(within(detail).getByText("contains")).toBeTruthy();
  });

  it("failure 信封的调用失败行给出脱敏错误类", async () => {
    clientMocks.getRun.mockResolvedValue(directRun({
      status: "failed",
      cases: [{ case_id: "case-1", result: { error: { class: "auth", message: "denied" } } }],
      scores: [{ case_id: "case-1", passed: false, outcome: "call_failed", judged: true, scorer: "contains" }],
      error: { class: "auth", message: "denied" },
    }));
    render(
      <MemoryRouter initialEntries={["/direct-llm/runs/run-51/result"]}>
        <DirectLlmResult />
      </MemoryRouter>
    );
    expect(await screen.findByText("调用失败")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /case-1/ }));
    expect(screen.getByText(/auth：denied/)).toBeTruthy();
  });
});

describe("DirectLlmCompare", () => {
  it("按判定题数比较通过率与共同不通过，并可逐题下钻", async () => {
    const other = directRun({
      id: "run-52",
      manifest: {
        model: "qwen-max",
        benchmark_provenance: { suite: "direct-llm", scorer: "contains" },
        benchmark_snapshot: directRun().manifest.benchmark_snapshot,
      },
      cases: [
        { case_id: "case-1", result: { content: "other" } },
        { case_id: "case-2", result: { content: "billing" } },
      ],
      scores: [
        { case_id: "case-1", passed: false, outcome: "wrong_answer", judged: true, scorer: "contains" },
        { case_id: "case-2", passed: false, outcome: "wrong_answer", judged: true, scorer: "contains" },
        { case_id: "case-3", passed: false, outcome: "no_expectation", judged: false, scorer: "contains" },
      ],
    });
    clientMocks.getRun.mockImplementation(async (id: string) =>
      id === "run-52" ? other : directRun());
    clientMocks.getReport.mockResolvedValue({
      cost: { total: 0.002, price_table_versions: ["v1"] },
      summary: { cases: 3, scored: 2, passed: 2, failed: 1, pass_rate: 0.5 },
      scores: [],
    });
    render(
      <MemoryRouter initialEntries={["/direct-llm/compare?runs=run-51,run-52"]}>
        <DirectLlmCompare />
      </MemoryRouter>
    );
    expect(await screen.findByText("Direct LLM · 多模型对比")).toBeTruthy();
    await waitFor(() => expect(screen.getByText("glm-4.7")).toBeTruthy());
    /* glm-4.7 判 2 题对 1 题（50%），qwen-max 判 2 题全错（0%）；不判定的 case-3 不进分母 */
    expect(screen.getByText("50%")).toBeTruthy();
    expect(screen.getByText("0%")).toBeTruthy();
    const shared = screen.getByText("共同不通过").closest("tr")!;
    expect(shared.textContent).toContain("case-2");
    expect(shared.textContent).not.toContain("case-3");
    fireEvent.click(screen.getByRole("button", { name: "case-1" }));
    const detail = document.querySelector(".drill-detail") as HTMLElement;
    expect(within(detail).getByText("工单 1：重复扣费")).toBeTruthy();
    /* 「billing」既是期望答案也是 glm-4.7 的输出；qwen-max 的输出并排展示在下面 */
    expect(within(detail).getAllByText("billing")).toHaveLength(2);
    expect(within(detail).getByText("other")).toBeTruthy();
  });

  it("缺少 runs 参数时不崩溃", async () => {
    render(
      <MemoryRouter initialEntries={["/direct-llm/compare"]}>
        <DirectLlmCompare />
      </MemoryRouter>
    );
    expect(await screen.findByText("Direct LLM · 多模型对比")).toBeTruthy();
    expect(screen.getByText("无")).toBeTruthy();
    expect(screen.getByText("无").getAttribute("colspan")).toBe("1");
  });
});
describe("ReplayOperate", () => {
  it("手写 Manifest 作为 inline provider 创建运行", async () => {
    clientMocks.createRun.mockResolvedValue({ id: "run-61", status: "queued" });
    render(
      <MemoryRouter initialEntries={["/replay"]}>
        <ReplayOperate />
        <LocationProbe />
      </MemoryRouter>
    );
    fireEvent.change(screen.getByLabelText("Case 列表（逗号分隔）"), { target: { value: "case-1" } });
    fireEvent.change(screen.getByLabelText(/Manifest JSON/), {
      target: { value: '{"provider":{"kind":"replay","model":"fixture-model"}}' },
    });
    fireEvent.click(screen.getByRole("button", { name: /创建回放/ }));
    await waitFor(() => expect(clientMocks.createRun).toHaveBeenCalledWith({
      scenario_version: "replay@1",
      manifest: { provider: { kind: "replay", model: "fixture-model" } },
      case_ids: ["case-1"],
    }));
    expect(screen.getByTestId("location").textContent).toBe("/replay/monitor?runs=run-61");
  });
});
