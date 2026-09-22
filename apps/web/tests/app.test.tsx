import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import App from "../src/App";

vi.mock("../src/api/client", () => ({
  getApiToken: vi.fn(() => ""),
  setApiToken: vi.fn(),
  getRuns: vi.fn(async () => ({ items: [], total: 0 })),
  createRun: vi.fn(),
  getRun: vi.fn(),
  cancelRun: vi.fn(),
  retryRun: vi.fn(),
  rescoreRun: vi.fn(),
  getReport: vi.fn(),
  getModels: vi.fn(async () => ({ items: [] })),
  getProviders: vi.fn(async () => ({ items: [] })),
  getCredentials: vi.fn(async () => ({ items: [] })),
  getProviderKinds: vi.fn(async () => ({ items: [] })),
  publishModel: vi.fn(),
  getAgents: vi.fn(async () => ({ items: [] })),
  getHarnesses: vi.fn(async () => ({ items: [] })),
  getBenchmarkOverview: vi.fn(async () => ({ items: [], total: 0 })),
  importBenchmark: vi.fn(),
  createBenchmarkRun: vi.fn(),
  getTerminalBenchOverview: vi.fn(async () => ({
    items: [],
    total: 0,
    runner: { adapter_id: "terminal-bench-harbor", harbor_version: "0.23.0", connected: false },
    benchmark: "terminal-bench",
  })),
  getTerminalBenchTasks: vi.fn(async () => ({ items: [], total: 0 })),
  getTerminalBenchPreflight: vi.fn(),
  createTerminalBenchRun: vi.fn(),
  getRunTasks: vi.fn(async () => ({ run_id: "", items: [], total: 0 })),
  getRunTaskTrials: vi.fn(),
  getRunTrial: vi.fn(),
  subscribeRunEvents: vi.fn(() => () => undefined),
}));

/** 渲染 App 并附带当前 location 探针，供跳转断言（后续任务复用）。 */
export function renderWithLocation(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
      <Routes>
        <Route path="*" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>
  );
}

function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location">{location.pathname + location.search}</span>;
}

afterEach(cleanup);

describe("App 骨架", () => {
  it("侧边栏含通用导航组与品牌", async () => {
    renderWithLocation("/providers");
    expect(await screen.findByText("MoTTEavl")).toBeTruthy();
    expect(screen.getByRole("link", { name: /Provider 与模型/ })).toBeTruthy();
    expect(screen.getByRole("link", { name: /运行/ })).toBeTruthy();
    expect(screen.getByRole("link", { name: /Agent \/ Harness/ })).toBeTruthy();
  });

  it("按路径渲染对应页面", async () => {
    renderWithLocation("/harnesses");
    await waitFor(() => expect(screen.getByText("Harness 安装情况")).toBeTruthy());
  });
});

describe("应用路由（收尾）", () => {
  it("根路径重定向到 GSM8K 专区", async () => {
    renderWithLocation("/");
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/gsm8k"));
  });

  it("侧边栏含三个类型入口", () => {
    renderWithLocation("/gsm8k");
    expect(screen.getByRole("link", { name: /GSM8K 数学评测/ })).toBeTruthy();
    expect(screen.getByRole("link", { name: /Direct LLM 评测/ })).toBeTruthy();
    expect(screen.getByRole("link", { name: /^Replay$/ })).toBeTruthy();
  });

  it("侧边栏与路由含 Terminal-Bench（Harbor）专区", async () => {
    renderWithLocation("/terminal-bench");
    expect(screen.getByRole("link", { name: /Terminal-Bench（Harbor）/ })).toBeTruthy();
    // 操作页渲染（数据集版本一栏出现即说明路由与客户端装配正确）
    await waitFor(() => expect(screen.getByTestId("tb-dataset-revision")).toBeTruthy());
  });
});

describe("两级导航（DESIGN.md 3.2）", () => {
  it("套件页顶栏出现分段页签与面包屑", () => {
    renderWithLocation("/gsm8k");
    expect(screen.getByRole("link", { name: "操作" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "题目" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "监控" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "对比" })).toBeTruthy();
    // 「结果」页签落在运行总览并按套件标签过滤
    const resultTab = screen.getByRole("link", { name: "结果" });
    expect(resultTab.getAttribute("href")).toContain("/runs?type=");
    // 面包屑（评测套件 / gsm8k）
    expect(screen.getByText("gsm8k")).toBeTruthy();
  });

  it("「结果」页签的落地页按套件标签过滤运行总览", async () => {
    renderWithLocation(`/runs?type=${encodeURIComponent("GSM8K 数学评测")}`);
    await waitFor(() => expect(screen.getAllByText("运行总览").length).toBeGreaterThan(0));
  });

  it("Terminal-Bench 页签用「任务」替代「题目」", () => {
    renderWithLocation("/terminal-bench");
    expect(screen.getByRole("link", { name: "任务" })).toBeTruthy();
  });

  it("侧栏底部连接状态条常显", () => {
    renderWithLocation("/gsm8k");
    expect(screen.getByRole("button", { name: /API 未认证/ })).toBeTruthy();
  });
});
