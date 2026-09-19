import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { AgentCompare, AgentOperate, AgentResult } from "../src/evalTypes/agent/AgentPages";

const clientMocks = vi.hoisted(() => ({
  getAgentTasksOverview: vi.fn(async () => ({ items: [], total: 0 })),
  getModels: vi.fn(async () => ({ items: [] })),
  getAgentTasksCases: vi.fn(async () => ({ items: [], total: 0 })),
  importAgentTasks: vi.fn(),
  dryRunAgentTasks: vi.fn(),
  createAgentTasksRun: vi.fn(),
  getAgentCaseDetail: vi.fn(),
  getAgentArtifactContent: vi.fn(),
  getRunInvocations: vi.fn(async () => ({ items: [], total: 0 })),
  getRun: vi.fn(),
  getScoringPasses: vi.fn(async () => ({ items: [], total: 0 })),
  getReport: vi.fn(async () => ({ summary: {}, scores: [] })),
  cancelRun: vi.fn(),
  retryRun: vi.fn(),
  getRuns: vi.fn(),
}));

vi.mock("../src/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/client")>()),
  ...clientMocks,
}));

function renderAt(path: string, element: React.ReactNode) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/agent-tasks" element={element} />
        <Route path="/agent-tasks/runs/:runId/result" element={element} />
        <Route path="/agent-tasks/compare" element={element} />
        <Route path="/agent-tasks/runs/:childId/result" element={<div>child-result</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

const OVERVIEW = {
  items: [
    {
      scenario: "file-report@1", dataset: "file-report@1", suite: "agent-tasks", cases: 2,
      runs: [{ id: "run-existing", status: "completed", created_at: "2026-09-19T01:00:00Z", mode: "native-tool" }],
    },
  ],
  total: 1,
};

const MODELS = {
  items: [
    { id: "published-tools", lifecycle: "published", supports_tools: true },
    { id: "published-plain", lifecycle: "published", supports_tools: false },
    { id: "draft-model", lifecycle: "draft", supports_tools: true },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(cleanup);

describe("AgentOperate", () => {
  it("加载任务与模型并展示最近运行", async () => {
    clientMocks.getAgentTasksOverview.mockResolvedValue(OVERVIEW);
    clientMocks.getModels.mockResolvedValue(MODELS);
    renderAt("/agent-tasks", <AgentOperate />);
    await waitFor(() => {
      expect(screen.getByText(/file-report@1/)).toBeTruthy();
      expect(screen.getByText(/run-existing/)).toBeTruthy();
    });
  });

  it("空数据集时显示空态与导入指引", async () => {
    clientMocks.getAgentTasksOverview.mockResolvedValue({ items: [], total: 0 });
    clientMocks.getModels.mockResolvedValue(MODELS);
    renderAt("/agent-tasks", <AgentOperate />);
    await waitFor(() => {
      expect(screen.getByText(/还没有 Agent 任务数据集/)).toBeTruthy();
    });
  });

  it("native-tool 对不支持工具的模型显示禁用原因且不静默降级", async () => {
    clientMocks.getAgentTasksOverview.mockResolvedValue(OVERVIEW);
    clientMocks.getModels.mockResolvedValue(MODELS);
    renderAt("/agent-tasks", <AgentOperate />);
    const modelSelect = await screen.findByDisplayValue(/published-tools/);
    fireEvent.change(modelSelect, { target: { value: "published-plain" } });
    await waitFor(() => {
      expect(screen.getByText(/supports_tools=false/)).toBeTruthy();
      expect(screen.getByText("预检")).toBeTruthy();
    });
    expect((screen.getByText("预检") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByText("创建运行") as HTMLButtonElement).disabled).toBe(true);
  });

  it("预算非法输入就地报错且输入保留", async () => {
    clientMocks.getAgentTasksOverview.mockResolvedValue(OVERVIEW);
    clientMocks.getModels.mockResolvedValue(MODELS);
    renderAt("/agent-tasks", <AgentOperate />);
    const stepsInput = await screen.findByDisplayValue("8");
    fireEvent.change(stepsInput, { target: { value: "0" } });
    await waitFor(() => expect(screen.getByText(/步数预算须为/)).toBeTruthy());
    expect((stepsInput as HTMLInputElement).value).toBe("0"); // 错误输入保留
    fireEvent.change(stepsInput, { target: { value: "6" } });
    await waitFor(() => expect(screen.queryByText(/步数预算须为/)).toBeNull());
  });

  it("预检失败保留表单并显示结构化错误；成功显示摘要", async () => {
    clientMocks.getAgentTasksOverview.mockResolvedValue(OVERVIEW);
    clientMocks.getModels.mockResolvedValue(MODELS);
    clientMocks.dryRunAgentTasks.mockRejectedValue(new Error("scenario not found: missing@9"));
    renderAt("/agent-tasks", <AgentOperate />);
    await screen.findByDisplayValue(/published-tools/);
    fireEvent.click(screen.getByText("预检"));
    await waitFor(() => expect(screen.getByText(/scenario not found/)).toBeTruthy());
    // 表单仍在
    expect(screen.getByDisplayValue(/published-tools/)).toBeTruthy();

    clientMocks.dryRunAgentTasks.mockResolvedValue({
      scenario: "file-report@1", dataset: "file-report@1", mode: "native-tool",
      prompt_version: "builtin-react-native@1", selected_cases: 2,
      backend: "builtin-agent", budget: {},
    });
    fireEvent.click(screen.getByText("预检"));
    await waitFor(() => expect(screen.getByText("预检摘要")).toBeTruthy());
    expect(screen.getByText(/builtin-agent/)).toBeTruthy();
  });
});

describe("AgentResult", () => {
  const RUN = {
    id: "run-agent-view", status: "failed", scenario_version: "file-report@1",
    case_ids: ["case-1"],
    current_scoring_pass_id: "pass-2",
    manifest: { agent_config: { mode: "native-tool" }, agent: "builtin-agent@1" },
    scores: [
      { case_id: "case-1", metric_id: "file-content:report.json", metric_status: "scored", passed: true, reason: null },
      { case_id: "case-1", metric_id: "no-forbidden-write", metric_status: "insufficient_evidence", passed: null, reason: "workspace_snapshot_incomplete" },
    ],
    error: { message: "boom" },
  };

  function setupRun(overrides: Record<string, unknown> = {}) {
    clientMocks.getRun.mockResolvedValue({ ...RUN, ...overrides });
    clientMocks.getScoringPasses.mockResolvedValue({
      items: [
        { id: "pass-1", summary: { multi_metric: true } },
        { id: "pass-2", summary: { multi_metric: true } },
      ],
      total: 2,
    });
  }

  it("渲染多指标状态、缺口原因与历史 pass 切换", async () => {
    setupRun();
    renderAt("/agent-tasks/runs/run-agent-view/result", <AgentResult />);
    await waitFor(() => expect(screen.getByText("多指标结果")).toBeTruthy());
    expect(screen.getByText("file-content:report.json")).toBeTruthy();
    expect(screen.getByText("证据不足")).toBeTruthy();
    expect(screen.getByText("workspace_snapshot_incomplete")).toBeTruthy();
    // 历史 pass 下拉存在且包含当前标记
    const passSelect = screen.getByLabelText(/查看批次/) as HTMLSelectElement;
    expect(passSelect.value).toBe("pass-2");
    // 切到历史批次：按 pass id 只读拉取该批分数（不重新评分）
    clientMocks.getReport.mockResolvedValue({
      summary: {},
      scores: [
        { case_id: "case-1", metric_id: "file-content:report.json",
          metric_status: "scored", passed: false, reason: "content_mismatch" },
      ],
    });
    fireEvent.change(passSelect, { target: { value: "pass-1" } });
    await waitFor(() => expect(clientMocks.getReport).toHaveBeenCalledWith(
      "run-agent-view", "pass-1",
    ));
    await waitFor(() => expect(screen.getByText(/当前显示：历史批次/)).toBeTruthy());
    expect(screen.getByText("content_mismatch")).toBeTruthy();
    expect(clientMocks.getRun).toHaveBeenCalledTimes(1);
  });

  it("失败运行提供 retry 子 Run 入口并跳转", async () => {
    setupRun();
    clientMocks.retryRun.mockResolvedValue({ id: "run-child", parent_run_id: "run-agent-view" });
    renderAt("/agent-tasks/runs/run-agent-view/result", <AgentResult />);
    const retryButton = await screen.findByText(/重试（子 Run）/);
    fireEvent.click(retryButton);
    await waitFor(() => expect(clientMocks.retryRun).toHaveBeenCalledWith("run-agent-view"));
  });

  it("运行中的 Run 提供取消入口", async () => {
    setupRun({ status: "running", error: null });
    clientMocks.cancelRun.mockResolvedValue({ id: "run-agent-view", status: "cancelled" });
    renderAt("/agent-tasks/runs/run-agent-view/result", <AgentResult />);
    const cancelButton = await screen.findByText("取消运行");
    fireEvent.click(cancelButton);
    await waitFor(() => expect(clientMocks.cancelRun).toHaveBeenCalled());
  });

  it("样本下钻展示终止原因、长事件截断与产物不可用提示", async () => {
    setupRun();
    const events = Array.from({ length: 260 }, (_, index) => ({
      type: "model_request", step: index + 1,
    }));
    clientMocks.getAgentCaseDetail.mockResolvedValue({
      run_id: "run-agent-view", case_id: "case-1", outcome: null,
      agent: {
        final_output: "done", termination_reason: "max_steps", termination_detail: "budget exhausted",
        steps: 260, tool_calls: 12, prompt_version: "builtin-react-native@1",
        mode: "native-tool", duration_ms: 1234.5, budget: {},
      },
      events,
      capture_errors: ["out.txt: OSError: simulated"],
      cleanup: { status: "failed", residual: ["out.txt"], error: "simulated" },
      observation: { coverage: { complete: false } },
      artifacts: [
        { artifact_id: "a/report.json", path: "report.json", available: true },
        { artifact_id: "a/out.txt", path: "out.txt", available: false },
      ],
    });
    renderAt("/agent-tasks/runs/run-agent-view/result", <AgentResult />);
    const caseSelect = await screen.findByLabelText(/任务/);
    fireEvent.change(caseSelect, { target: { value: "case-1" } });
    await waitFor(() => expect(screen.getByText("步数预算耗尽")).toBeTruthy());
    expect(screen.getByText(/260 \/ 12/)).toBeTruthy();
    expect(screen.getByText(/前 200 条/)).toBeTruthy();
    expect(screen.getByText(/产物 out.txt 不可用/)).toBeTruthy();
    expect(screen.getByText(/清理失败/)).toBeTruthy();
    // 键盘可达：产物按钮是原生 button（可 focus）
    const artifactButton = screen.getByText("report.json", { selector: "button *, button" });
    expect(artifactButton).toBeTruthy();
  });
});

describe("AgentCompare", () => {
  it("两个运行并列展示且声明非正式比较", async () => {
    clientMocks.getRun.mockImplementation(async (id: string) => ({
      id, status: "completed",
      manifest: { resource_snapshots: { model_profile: { id: `model-${id}` } } },
      scores: id === "run-a"
        ? [{ case_id: "case-1", metric_id: "file-content:report.json", passed: true }]
        : [{ case_id: "case-1", metric_id: "file-content:report.json", passed: false }],
    }));
    window.history.replaceState({}, "", "/agent-tasks/compare?runs=run-a,run-b");
    renderAt("/agent-tasks/compare?runs=run-a,run-b", <AgentCompare />);
    await waitFor(() => expect(screen.getByText(/不是正式的可比性结论/)).toBeTruthy());
    expect(screen.getAllByText("通过").length).toBe(1);
    expect(screen.getAllByText("未通过").length).toBe(1);
  });
});
