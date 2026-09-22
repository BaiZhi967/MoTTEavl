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
      expect(screen.getByRole("button", { name: "预检" })).toBeTruthy();
    });
    expect((screen.getByRole("button", { name: "预检" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "创建运行" }) as HTMLButtonElement).disabled).toBe(true);
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
    fireEvent.click(screen.getByRole("button", { name: "预检" }));
    await waitFor(() => expect(screen.getByText(/scenario not found/)).toBeTruthy());
    // 表单仍在
    expect(screen.getByDisplayValue(/published-tools/)).toBeTruthy();

    clientMocks.dryRunAgentTasks.mockResolvedValue({
      scenario: "file-report@1", dataset: "file-report@1", mode: "native-tool",
      prompt_version: "builtin-react-native@1", selected_cases: 2,
      backend: "builtin-agent", budget: {},
    });
    fireEvent.click(screen.getByRole("button", { name: "预检" }));
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

describe("AgentResult review-round2 fixes", () => {
  const RUN = {
    id: "run-r2", status: "completed", scenario_version: "r2@1",
    case_ids: ["case-a", "case-b"],
    current_scoring_pass_id: "pass-2",
    manifest: { agent_config: { mode: "native-tool" }, agent: "builtin-agent@1" },
    scores: [
      { case_id: "case-a", metric_id: "file-content:report.txt", metric_status: "scored", passed: true, reason: null },
    ],
    error: null,
  };

  function setup() {
    clientMocks.getRun.mockResolvedValue(RUN);
    clientMocks.getScoringPasses.mockResolvedValue({
      items: [
        { id: "pass-1", summary: {} },
        { id: "pass-2", summary: {} },
      ],
      total: 2,
    });
  }

  it("#16 切换 Case 后产物内容按 case+path 重新加载", async () => {
    setup();
    const artifactsFor = (caseId: string) => ([
      { artifact_id: `a/${caseId}/report.txt`, path: "report.txt", available: true },
    ]);
    clientMocks.getAgentCaseDetail.mockImplementation(async (_runId, caseId) => ({
      run_id: "run-r2", case_id: caseId, outcome: null, pending: false,
      // steps 随 case 区分，供断言等待 case-b 详情真正渲染完成
      agent: { termination_reason: "final_answer",
               steps: caseId === "case-a" ? 1 : 2, tool_calls: 1 },
      events: [], capture_errors: [], cleanup: { status: "success" },
      observation: {}, artifacts: artifactsFor(caseId),
    }));
    clientMocks.getAgentArtifactContent.mockImplementation(
      async (_runId, caseId, _path) => ({ path: "report.txt", sha256_matches: true, content: `content-of-${caseId}` }),
    );
    renderAt("/agent-tasks/runs/run-r2/result", <AgentResult />);
    const caseSelect = await screen.findByLabelText(/任务/);
    fireEvent.change(caseSelect, { target: { value: "case-a" } });
    const toggleA = await screen.findByText("report.txt", { selector: "button *, button" });
    fireEvent.click(toggleA);
    await waitFor(() => expect(screen.getByText(/content-of-case-a/)).toBeTruthy());
    // 切到 case-b：同名产物必须请求并显示 case-b 的内容
    fireEvent.change(caseSelect, { target: { value: "case-b" } });
    // 等 case-b 详情真正渲染（steps=2 的特征出现）再展开同名产物
    await waitFor(() => expect(screen.getByText(/2 \/ 1/)).toBeTruthy());
    const toggleB = screen.getByText("report.txt", { selector: "button *, button" });
    fireEvent.click(toggleB);
    await waitFor(() => expect(screen.getByText(/content-of-case-b/)).toBeTruthy());
    expect(clientMocks.getAgentArtifactContent).toHaveBeenCalledWith("run-r2", "case-b", "report.txt");
    expect(screen.queryByText(/content-of-case-a/)).toBeNull(); // 不再串显 case-a 内容
  });

  it("#17 尚未执行的 case 显示等待态且不崩溃", async () => {
    setup();
    clientMocks.getAgentCaseDetail.mockResolvedValue({
      run_id: "run-r2", case_id: "case-a", outcome: null, pending: true,
      agent: null, events: [], capture_errors: [], cleanup: null,
      observation: null, artifacts: [],
    });
    renderAt("/agent-tasks/runs/run-r2/result", <AgentResult />);
    const caseSelect = await screen.findByLabelText(/任务/);
    fireEvent.change(caseSelect, { target: { value: "case-a" } });
    await waitFor(() => expect(screen.getByText(/尚未产生结果/)).toBeTruthy());
  });

  it("#18 迟到的历史批次响应不得覆盖新选择", async () => {
    setup();
    renderAt("/agent-tasks/runs/run-r2/result", <AgentResult />);
    const passSelect = await screen.findByLabelText(/查看批次/);
    expect(passSelect.value).toBe("pass-2");
    let resolveOld: (value: unknown) => void = () => {};
    const oldRequest = new Promise((resolve) => { resolveOld = resolve; });
    clientMocks.getReport.mockImplementation(async (_id, passId) => {
      if (passId === "pass-1") {
        await oldRequest; // pass-1 的响应被挂起
        return {
          summary: {},
          scores: [{ case_id: "case-a", metric_id: "stale-metric",
                     metric_status: "scored", passed: true, reason: null }],
        };
      }
      return { summary: {}, scores: [] };
    });
    // 选择 pass-1（请求被挂起），随即切回 pass-2（不发新请求）
    fireEvent.change(passSelect, { target: { value: "pass-1" } });
    fireEvent.change(passSelect, { target: { value: "pass-2" } });
    await waitFor(() => expect(clientMocks.getReport).toHaveBeenCalledTimes(1));
    expect(clientMocks.getReport).toHaveBeenCalledWith("run-r2", "pass-1");
    resolveOld({}); // 迟到的 pass-1 响应到达
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(screen.queryByText("stale-metric")).toBeNull(); // 不覆盖当前批次
    expect(screen.getByText(/当前显示：当前批次/)).toBeTruthy();
  });

  it("#9 迟到的历史批次失败响应不得把结果页替换成错误页", async () => {
    setup();
    renderAt("/agent-tasks/runs/run-r2/result", <AgentResult />);
    const passSelect = await screen.findByLabelText(/查看批次/);
    let rejectOld: (reason: unknown) => void = () => {};
    const oldRequest = new Promise((_resolve, reject) => { rejectOld = reject; });
    clientMocks.getReport.mockImplementation(async (_id, passId) => {
      if (passId === "pass-1") {
        await oldRequest; // pass-1 的请求被挂起，随后以失败结束
      }
      return { summary: {}, scores: [] };
    });
    fireEvent.change(passSelect, { target: { value: "pass-1" } });
    fireEvent.change(passSelect, { target: { value: "pass-2" } });
    await waitFor(() => expect(clientMocks.getReport).toHaveBeenCalledTimes(1));
    rejectOld(new Error("stale network failure"));
    await new Promise((resolve) => setTimeout(resolve, 10));
    // 迟到的 rejection 不得触发错误页（成功响应之外的路径也要做序号防护）
    expect(screen.queryByText(/读取失败/)).toBeNull();
    expect(screen.getByText(/当前显示：当前批次/)).toBeTruthy();
  });

  it("#8 重试进入子 Run 后样本下钻与产物缓存按 Run 重置", async () => {
    const parentRun = {
      id: "run-parent", status: "failed", scenario_version: "r2@1",
      case_ids: ["case-1"], current_scoring_pass_id: "",
      manifest: { agent_config: { mode: "native-tool" }, agent: "builtin-agent@1" },
      scores: [], error: { message: "boom" },
    };
    const childRun = { ...parentRun, id: "run-child", status: "completed", error: null };
    clientMocks.getRun.mockImplementation(async (id: string) =>
      id === "run-child" ? childRun : parentRun);
    clientMocks.getScoringPasses.mockResolvedValue({ items: [], total: 0 });
    clientMocks.retryRun.mockResolvedValue({ id: "run-child", parent_run_id: "run-parent" });
    clientMocks.getAgentCaseDetail.mockImplementation(async (runId: string, caseId: string) => ({
      run_id: runId, case_id: caseId, outcome: null, pending: false,
      agent: { termination_reason: "final_answer",
               steps: runId === "run-parent" ? 1 : 2, tool_calls: 1 },
      events: [], capture_errors: [], cleanup: { status: "success" },
      observation: {},
      artifacts: [{ artifact_id: `a/${runId}/${caseId}/report.txt`,
                    path: "report.txt", available: true }],
    }));
    clientMocks.getAgentArtifactContent.mockImplementation(async (runId: string) => ({
      path: "report.txt", sha256_matches: true, content: `content-of-${runId}`,
    }));
    renderAt("/agent-tasks/runs/run-parent/result", <AgentResult />);

    // 父 Run：选 case 并展开产物（缓存进 ArtifactViewer 状态）
    const caseSelect = await screen.findByLabelText(/任务/);
    fireEvent.change(caseSelect, { target: { value: "case-1" } });
    const toggleParent = await screen.findByText("report.txt", { selector: "button *, button" });
    fireEvent.click(toggleParent);
    await waitFor(() => expect(screen.getByText(/content-of-run-parent/)).toBeTruthy());

    // 重试 → 子 Run 路由（同路由参数变化，组件不重挂载）
    fireEvent.click(screen.getByText(/重试（子 Run）/));
    await waitFor(() => expect(clientMocks.getRun).toHaveBeenCalledWith("run-child"));

    // 旧 caseDetail 被重置：回到"选择任务"空态，父 Run 的产物内容不再显示
    await waitFor(() => expect(screen.getByText(/选择任务查看终止原因/)).toBeTruthy());
    expect(screen.queryByText(/content-of-run-parent/)).toBeNull();

    // 子 Run 重新下钻同 case 同路径产物：必须取回子 Run 的内容
    const caseSelectAgain = screen.getByLabelText(/任务/);
    fireEvent.change(caseSelectAgain, { target: { value: "case-1" } });
    const toggleChild = await screen.findByText("report.txt", { selector: "button *, button" });
    fireEvent.click(toggleChild);
    await waitFor(() => expect(screen.getByText(/content-of-run-child/)).toBeTruthy());
    expect(clientMocks.getAgentArtifactContent).toHaveBeenCalledWith("run-child", "case-1", "report.txt");
  });
});
