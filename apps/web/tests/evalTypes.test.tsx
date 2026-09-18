import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { BatchMonitor } from "../src/components/BatchMonitor";
import { DirectLlmOperate, DirectLlmResult } from "../src/evalTypes/directllm/DirectLlmPages";
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

describe("DirectLlmOperate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clientMocks.getModels.mockResolvedValue({
      items: [
        { id: "glm-4.7", provider: "zhipu", capabilities: {}, reasoning: { supported: true, levels: ["low", "high"], default_level: "high", control: "{}" } },
      ],
    });
  });

  it("选模型填 case 后按 manifest.model 创建并跳批次过程页", async () => {
    clientMocks.createRun.mockResolvedValue({ id: "run-51", status: "queued" });
    render(
      <MemoryRouter initialEntries={["/direct-llm"]}>
        <DirectLlmOperate />
        <LocationProbe />
      </MemoryRouter>
    );
    await screen.findByLabelText("选择模型 glm-4.7");
    fireEvent.click(screen.getByLabelText("选择模型 glm-4.7"));
    fireEvent.change(screen.getByLabelText("Case 列表（逗号分隔）"), { target: { value: "case-1, case-2" } });
    fireEvent.click(screen.getByRole("button", { name: /发起评测/ }));
    await waitFor(() => expect(clientMocks.createRun).toHaveBeenCalledWith({
      scenario_version: "direct-llm@1",
      manifest: { model: "glm-4.7" },
      case_ids: ["case-1", "case-2"],
    }));
    /* createRun 断言通过时 promise 可能尚未落地，跳转需另行等待 */
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/direct-llm/monitor?runs=run-51"));
  });

  it("个别模型失败不阻塞整批，错误就地显示且提供批次入口", async () => {
    clientMocks.getModels.mockResolvedValue({
      items: [
        { id: "glm-4.7", provider: "zhipu", capabilities: {} },
        { id: "qwen-max", provider: "zhipu", capabilities: {} },
      ],
    });
    clientMocks.createRun
      .mockRejectedValueOnce(new Error("MODEL_DISABLED"))
      .mockResolvedValueOnce({ id: "run-52", status: "queued" });
    render(
      <MemoryRouter initialEntries={["/direct-llm"]}>
        <DirectLlmOperate />
        <LocationProbe />
      </MemoryRouter>
    );
    await screen.findByLabelText("选择模型 glm-4.7");
    fireEvent.click(screen.getByLabelText("选择模型 glm-4.7"));
    fireEvent.click(screen.getByLabelText("选择模型 qwen-max"));
    fireEvent.change(screen.getByLabelText("Case 列表（逗号分隔）"), { target: { value: "case-1" } });
    fireEvent.click(screen.getByRole("button", { name: /发起评测/ }));
    expect(await screen.findByText(/MODEL_DISABLED/)).toBeTruthy();
    expect(screen.getByTestId("location").textContent).toBe("/direct-llm");
    expect(screen.getByText(/已创建 1 个运行/)).toBeTruthy();
    const link = await screen.findByRole("link", { name: /查看批次进度/ });
    expect(link.getAttribute("href")).toBe("/direct-llm/monitor?runs=run-52");
  });

  it("多选时隐藏的推理等级下拉值不随批生效", async () => {
    clientMocks.getModels.mockResolvedValue({
      items: [
        { id: "glm-4.7", provider: "zhipu", capabilities: {}, reasoning: { supported: true, levels: ["low", "high"], default_level: "high", control: "{}" } },
        { id: "qwen-max", provider: "zhipu", capabilities: {} },
      ],
    });
    clientMocks.createRun.mockResolvedValue({ id: "run-53", status: "queued" });
    render(
      <MemoryRouter initialEntries={["/direct-llm"]}>
        <DirectLlmOperate />
      </MemoryRouter>
    );
    await screen.findByLabelText("选择模型 glm-4.7");
    fireEvent.click(screen.getByLabelText("选择模型 glm-4.7"));
    fireEvent.change(screen.getByLabelText(/推理等级/), { target: { value: "low" } });
    /* 加入第二个模型后下拉隐藏，此前的 low 不应写进任一 manifest */
    fireEvent.click(screen.getByLabelText("选择模型 qwen-max"));
    fireEvent.change(screen.getByLabelText("Case 列表（逗号分隔）"), { target: { value: "case-1" } });
    fireEvent.click(screen.getByRole("button", { name: /发起评测/ }));
    await waitFor(() => expect(clientMocks.createRun).toHaveBeenCalledTimes(2));
    for (const call of clientMocks.createRun.mock.calls) {
      expect(call[0].manifest.reasoning_level).toBeUndefined();
    }
  });
});

describe("DirectLlmResult", () => {
  it("期望对比钻取，模型列从 manifest 摘要", async () => {
    clientMocks.getRun.mockResolvedValue({
      id: "run-51", scenario_version: "direct-llm@1", status: "completed",
      manifest: { model: "glm-4.7" },
      case_ids: ["case-1"],
      cases: [{ case_id: "case-1", result: { content: "42" }, expected: 42 }],
      scores: [{ case_id: "case-1", passed: false }],
    });
    render(
      <MemoryRouter initialEntries={["/direct-llm/runs/run-51/result"]}>
        <DirectLlmResult />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText("case-1")).toBeTruthy());
    expect(screen.getByText("glm-4.7", { selector: ".metric-value" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "case-1" }));
    expect(screen.getByText(/期望/)).toBeTruthy();
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
