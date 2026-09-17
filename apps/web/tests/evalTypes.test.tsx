import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { FallbackMonitorPage, FallbackResultPage } from "../src/evalTypes/fallback/FallbackPages";

const clientMocks = vi.hoisted(() => ({
  getRun: vi.fn(),
  cancelRun: vi.fn(),
  retryRun: vi.fn(),
  rescoreRun: vi.fn(),
  getReport: vi.fn(),
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
