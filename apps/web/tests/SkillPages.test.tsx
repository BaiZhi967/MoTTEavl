import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { MemoryRouter } from "react-router-dom";
import {
  VALIDATION_SCOPES,
  SkillComparePage,
  SkillValidationPage,
  armCostOf,
  skillArmOf,
  skillTokenOverheadOf,
  toolCallCountOf,
} from "../src/evalTypes/skill/SkillPages";
import { ApiRequestError } from "../src/api/client";

const clientMocks = vi.hoisted(() => ({
  getSkills: vi.fn(),
  validateSkill: vi.fn(),
  testSkillFixture: vi.fn(),
  testSkillBehaviour: vi.fn(),
  getRun: vi.fn(),
  getReport: vi.fn(),
  compareRuns: vi.fn(),
}));

vi.mock("../src/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/client")>()),
  ...clientMocks,
}));

function wrap(node: React.ReactNode, route = "/skill") {
  return <MemoryRouter initialEntries={[route]}>{node}</MemoryRouter>;
}

const unavailable = () => new ApiRequestError(404, { code: "NOT_FOUND", message: "endpoint not registered" });

const INSTRUCTION_SKILL = {
  skill_id: "order-cancel",
  version: "1",
  kind: "instruction",
  lifecycle: "published",
  content_hash: "sha256:skill",
  injection_mode: "system-prompt",
  resource_manifest: [],
  requested_permissions: { tools: ["order.read"] },
  verification: null,
};

const EXECUTABLE_SKILL = {
  skill_id: "order-cancel-tool",
  version: "2",
  kind: "executable",
  lifecycle: "published",
  entrypoint: { interpreter: "python", argv: ["main.py"] },
  resource_manifest: [{ path: "main.py", sha256: "sha256:d", size_bytes: 120, media_type: "text/x-python" }],
};

const RUNS: Record<string, any> = {
  "run-1": {
    id: "run-1", status: "completed", scenario_version: "order-cancel@1",
    manifest: { skill_arm: "no-skill", model: "provider/model-a" },
    case_ids: ["case-1", "case-2"],
    scores: [{ case_id: "case-1", passed: true }, { case_id: "case-2", passed: false }],
    cases: [
      { case_id: "case-1", result: { usage: { tool_calls: 2 } } },
      { case_id: "case-2", result: { usage: { tool_calls: 2 } } },
    ],
  },
  "run-2": {
    id: "run-2", status: "completed", scenario_version: "order-cancel@1",
    manifest: { skill_arm: "skill-v1", model: "provider/model-a", skill_token_overhead: 1200 },
    case_ids: ["case-1", "case-2"],
    scores: [{ case_id: "case-1", passed: true }, { case_id: "case-2", passed: true }],
    cases: [
      { case_id: "case-1", result: { usage: { tool_calls: 3 } } },
      { case_id: "case-2", result: {} },
    ],
  },
  "run-3": {
    id: "run-3", status: "completed", scenario_version: "order-cancel@1",
    manifest: { skill_arm: "skill-v2", model: "provider/model-a", estimated_cost_usd: 0.42 },
    case_ids: ["case-1", "case-2"],
    scores: [{ case_id: "case-1", passed: true }],
    cases: [{ case_id: "case-1", result: { usage: { tool_calls: 5 } } }],
  },
};

const REPORTS: Record<string, any> = {
  "run-1": { cost: { total: 1.5, known_cases: 2, unknown_cases: 0, price_table_versions: ["v1"] } },
  "run-2": { cost: { total: null, known_cases: 1, unknown_cases: 1, price_table_versions: [] } },
  "run-3": { cost: { total: null, known_cases: 0, unknown_cases: 2, price_table_versions: [] } },
};

beforeEach(() => {
  for (const mock of Object.values(clientMocks)) mock.mockReset();
  clientMocks.getSkills.mockResolvedValue({ items: [INSTRUCTION_SKILL], total: 1 });
  clientMocks.getRun.mockImplementation((id: string) => Promise.resolve(RUNS[id]));
  clientMocks.getReport.mockImplementation((id: string) => Promise.resolve(REPORTS[id]));
  clientMocks.compareRuns.mockResolvedValue({
    eligible: false,
    reasons: ["SKILL_ATTRIBUTION_UNSAFE:budget_policy: skill arms must declare one of same_total, same_execution"],
    allowed_differences: ["ALLOWED_FACTOR:skill: 'no-skill' -> 'skill-v1'"],
    metric_eligibility: { quality: false, cost: false },
    case_diff: { added: [], removed: [], changed: [] },
  });
});

afterEach(cleanup);

describe("SkillPages 三臂取值", () => {
  it("缺证据时开销与工具次数是未知，不按 0 计", () => {
    expect(skillArmOf(RUNS["run-3"] as any)).toBe("skill-v2");
    expect(skillArmOf({ id: "r", scenario_version: "x@1", manifest: {} } as any)).toBe("未知");
    expect(skillTokenOverheadOf(RUNS["run-2"] as any)).toBe(1200);
    expect(skillTokenOverheadOf(RUNS["run-1"] as any)).toBeNull();
    expect(toolCallCountOf(RUNS["run-1"] as any)).toBe(4);
    expect(toolCallCountOf(RUNS["run-3"] as any)).toBe(5);
    // 只有一个 Case 报道了工具次数时也是未知（不能把缺报当成 0 再求和）
    expect(toolCallCountOf(RUNS["run-2"] as any)).toBe(3);
  });

  it("报道成本覆盖不完整时成本完整性为未知", () => {
    expect(armCostOf(RUNS["run-1"] as any, REPORTS["run-1"]).unknown_cost).toBe(false);
    expect(armCostOf(RUNS["run-2"] as any, REPORTS["run-2"]).unknown_cost).toBe(true);
    expect(armCostOf(RUNS["run-3"] as any, REPORTS["run-3"]).estimated_usd).toBe(0.42);
    expect(armCostOf(RUNS["run-3"] as any, REPORTS["run-3"]).reported_usd).toBeNull();
  });
});

describe("SkillValidationPage", () => {
  it("三种验证范围可见区分，缺条件时入口禁用并就地给出原因", async () => {
    render(wrap(<SkillValidationPage />));
    await waitFor(() => expect(screen.getByText("静态校验")).toBeTruthy());
    expect(VALIDATION_SCOPES.map((scope) => scope.label)).toEqual(["静态校验", "executable fixture", "固定 Agent 行为测试"]);
    expect(screen.getByText("executable fixture")).toBeTruthy();
    expect(screen.getByText("固定 Agent 行为测试")).toBeTruthy();
    expect(screen.getByText("能证明")).toBeTruthy();
    expect(screen.getByText("不能证明")).toBeTruthy();
    expect(screen.getByText(/指令能完成业务任务/)).toBeTruthy();

    expect((screen.getByTestId("scope-run-static") as HTMLButtonElement).disabled).toBe(false);
    const fixtureButton = screen.getByTestId("scope-run-executable_fixture") as HTMLButtonElement;
    expect(fixtureButton.disabled).toBe(true);
    expect(screen.getByTestId("scope-reason-executable_fixture").textContent).toContain("不是 executable");
    const behaviourButton = screen.getByTestId("scope-run-behaviour") as HTMLButtonElement;
    expect(behaviourButton.disabled).toBe(true);
    expect(screen.getByTestId("scope-reason-behaviour").textContent).toContain("必须显式指定 Agent 与模型");

    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: "builtin-agent" } });
    fireEvent.change(screen.getByLabelText("模型"), { target: { value: "provider/model-a" } });
    await waitFor(() => expect((screen.getByTestId("scope-run-behaviour") as HTMLButtonElement).disabled).toBe(false));
  });

  it("静态校验只读调用并显示逐项检查与既有 Run 引用", async () => {
    clientMocks.validateSkill.mockResolvedValue({
      status: "passed",
      checks: [
        { id: "manifest", locator: "manifest", passed: true, message: "字段齐全" },
        { id: "dependency", locator: "dependency_refs", passed: false, message: "依赖未固定" },
      ],
      run_id: "run-9",
      conditions: { kind: "instruction" },
    });
    render(wrap(<SkillValidationPage />));
    await waitFor(() => expect(screen.getByTestId("scope-run-static")).toBeTruthy());
    fireEvent.click(screen.getByTestId("scope-run-static"));
    await waitFor(() => expect(clientMocks.validateSkill).toHaveBeenCalledWith({ skill_id: "order-cancel", version: "1" }));
    await waitFor(() => expect(screen.getByText("依赖未固定")).toBeTruthy());
    expect(screen.getByText("未通过")).toBeTruthy();
    expect(screen.getByText("run-9")).toBeTruthy();
    expect(screen.queryByTestId("scope-reason-static")).toBeNull();
  });

  it("静态校验端点未注册 → 能力不可用，入口禁用并说明原因", async () => {
    clientMocks.validateSkill.mockRejectedValue(unavailable());
    render(wrap(<SkillValidationPage />));
    await waitFor(() => expect(screen.getByTestId("scope-run-static")).toBeTruthy());
    fireEvent.click(screen.getByTestId("scope-run-static"));
    await waitFor(() => expect(screen.getByTestId("scope-reason-static")).toBeTruthy());
    expect(screen.getByTestId("scope-reason-static").textContent).toContain("能力不可用");
    await waitFor(() => expect((screen.getByTestId("scope-run-static") as HTMLButtonElement).disabled).toBe(true));
  });
});

describe("SkillComparePage", () => {
  it("三臂对照显示条件差异、Skill 开销、工具次数与报道/估算/未知成本", async () => {
    render(wrap(<SkillComparePage />, "/skill/compare?runs=run-1,run-2,run-3"));
    await waitFor(() => expect(screen.getByTestId("arm-overhead-run-2")).toBeTruthy());
    expect(screen.getByText("no-skill")).toBeTruthy();
    expect(screen.getByText("skill-v1")).toBeTruthy();
    expect(screen.getByText("skill-v2")).toBeTruthy();

    expect(screen.getByTestId("arm-overhead-run-2").textContent).toBe("1200");
    expect(screen.getByTestId("arm-overhead-run-1").textContent).toBe("未知");
    expect(screen.getByTestId("arm-overhead-run-3").textContent).toBe("未知");
    expect(screen.getByTestId("arm-tool-calls-run-1").textContent).toBe("4");
    expect(screen.getByTestId("arm-cost-run-1").textContent).toContain("已知");
    expect(screen.getByTestId("arm-cost-run-2").textContent).toContain("未知");
    expect(screen.getByText("$1.5")).toBeTruthy();
    expect(screen.getByText("$0.42")).toBeTruthy();

    await waitFor(() => expect(screen.getAllByText(/ALLOWED_FACTOR:skill/).length).toBeGreaterThan(0));
    expect(screen.getAllByText(/SKILL_ATTRIBUTION_UNSAFE/).length).toBeGreaterThan(0);
    // 三臂两两比较，Skill 是允许变化的因子
    expect(clientMocks.compareRuns).toHaveBeenCalledTimes(3);
    expect(clientMocks.compareRuns.mock.calls[0][2]).toBe("skill");
  });

  it("比较端点未注册时按能力不可用显示，臂数据仍然可读", async () => {
    clientMocks.compareRuns.mockRejectedValue(unavailable());
    render(wrap(<SkillComparePage />, "/skill/compare?runs=run-1,run-2"));
    await waitFor(() => expect(screen.getByTestId("skill-comparison-unavailable")).toBeTruthy());
    expect(screen.getByTestId("skill-comparison-unavailable").textContent).toContain("能力不可用");
    expect(screen.getByTestId("arm-tool-calls-run-1").textContent).toBe("4");
  });
});
