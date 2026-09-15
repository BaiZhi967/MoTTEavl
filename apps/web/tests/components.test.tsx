import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import React from "react";
import { RunTimeline } from "../src/components/RunTimeline";
import { ScoreTable } from "../src/components/ScoreTable";
import { StatusBadge, statusLabel } from "../src/components/StatusBadge";

afterEach(cleanup);

const EVENTS = [
  { run_id: "run-1", seq: 1, type: "queued", status: "queued" },
  { run_id: "run-1", seq: 2, type: "preparing", status: "preparing" },
  { run_id: "run-1", seq: 3, type: "running", status: "running" },
  { run_id: "run-1", seq: 4, type: "model_response", case_id: "case-1", result: {} },
  { run_id: "run-1", seq: 5, type: "score", case_id: "case-1", passed: true },
  { run_id: "run-1", seq: 6, type: "completed", status: "completed" },
];

describe("StatusBadge", () => {
  it("映射运行状态为中文", () => {
    expect(statusLabel("queued")).toBe("排队中");
    expect(statusLabel("completed")).toBe("已完成");
    expect(statusLabel("profile_stale")).toBe("配置过期");
    expect(statusLabel("whatever")).toBe("whatever");
    render(<StatusBadge status="running" />);
    expect(screen.getByText("运行中")).toBeTruthy();
  });
});

describe("RunTimeline", () => {
  it("渲染全部事件并按类型过滤", async () => {
    render(<RunTimeline events={EVENTS as any} />);
    const timeline = document.querySelector(".timeline") as HTMLElement;
    expect(timeline.textContent).toContain("进入队列");
    expect(timeline.textContent).toContain("模型响应");
    expect(timeline.textContent).toContain("case-1 通过");

    const filter = screen.getByLabelText("事件过滤") as HTMLSelectElement;
    filter.value = "score";
    filter.dispatchEvent(new Event("change", { bubbles: true }));
    const filtered = document.querySelector(".timeline") as HTMLElement;
    expect(filtered.textContent).not.toContain("进入队列");
    expect(filtered.textContent).toContain("case-1");
  });

  it("空事件显示占位", () => {
    render(<RunTimeline events={[]} />);
    expect(screen.getByText("暂无事件")).toBeTruthy();
  });
});

describe("ScoreTable", () => {
  it("渲染评分与通过率", () => {
    render(<ScoreTable scores={[{ case_id: "case-1", passed: true }, { case_id: "case-2", passed: false }]} />);
    expect(screen.getByText(/共 2 项，通过 1，未通过 1/)).toBeTruthy();
    expect(screen.getByText("case-1")).toBeTruthy();
    expect(screen.getByText("未通过")).toBeTruthy();
  });
});
