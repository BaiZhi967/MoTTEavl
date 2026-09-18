import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import App from "../src/App";

vi.mock("../src/api/client", () => ({
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
});
