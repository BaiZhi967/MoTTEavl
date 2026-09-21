import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { MemoryRouter } from "react-router-dom";
import {
  ScenarioRunStepsPage,
  ScenarioWorkflowsPage,
  cleanupStatusOf,
  issuesForLocator,
  parseWorkflowDraft,
  stepStatusOf,
} from "../src/evalTypes/scenario/ScenarioPages";
import { ApiRequestError } from "../src/api/client";

const clientMocks = vi.hoisted(() => ({
  getWorkflows: vi.fn(),
  getWorkflow: vi.fn(),
  validateWorkflow: vi.fn(),
  publishWorkflow: vi.fn(),
  getRun: vi.fn(),
  getScenarioRunSteps: vi.fn(),
}));

vi.mock("../src/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/client")>()),
  ...clientMocks,
}));

function wrap(node: React.ReactNode, route = "/scenario") {
  return <MemoryRouter initialEntries={[route]}>{node}</MemoryRouter>;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

/** 服务端未注册端点：页面必须渲染成「能力不可用」，而不是崩溃或静默隐藏。 */
const unavailable = () => new ApiRequestError(404, { code: "NOT_FOUND", message: "endpoint not registered" });

const WORKFLOW_A = {
  workflow_id: "order-cancel-confirmed",
  version: "1",
  lifecycle: "published",
  published_at: "2026-09-20T00:00:00Z",
  content_hash: "sha256:a",
  limits: { max_total_steps: 20, max_turns: 4, wall_time_sec: 30 },
  failure_policy: "stop_case",
  steps: [{ step_id: "request", kind: "send_message" }],
};

const WORKFLOW_B = { ...WORKFLOW_A, workflow_id: "order-cancel-unconfirmed", content_hash: "sha256:b" };

const STEPS = {
  run_id: "run-1",
  status: "completed",
  workflow: { workflow_id: "order-cancel-confirmed", version: "1", content_hash: "sha256:a" },
  steps: [
    { step_id: "request", kind: "send_message", index: 1, status: "completed", duration_ms: 120 },
    {
      step_id: "tool-call", kind: "invoke_fixture_tool", index: 2, status: "unknown", tool_mode: "mock",
      unknown: true, duration_ms: null,
      assertions: [{ locator: "state.order.cancellation_count", op: "eq", passed: null, reason: "服务端未给出结论" }],
    },
    {
      step_id: "before-confirm", kind: "checkpoint", index: 3, status: "skipped",
      checkpoint: { label: "before-confirm", frozen: true, state_hash: "sha256:def" },
    },
  ],
  fixtures: [{
    fixture_id: "order-files", version: 1, kind: "files", owner: "run-1/case-1", isolated: false,
    prepare_error: { code: "FIXTURE_PREPARE_FAILED", message: "目录不属于本次运行" },
    snapshot: { complete: false },
    cleanup: { status: "failed", residual: ["tmp/order.json"], error: "残留未清理" },
  }],
};

const RUN = { id: "run-1", status: "completed", scenario_version: "order-cancel@1" };

beforeEach(() => {
  for (const mock of Object.values(clientMocks)) mock.mockReset();
  clientMocks.getWorkflows.mockResolvedValue({ items: [WORKFLOW_A], total: 1 });
  clientMocks.getWorkflow.mockResolvedValue(WORKFLOW_A);
});

afterEach(cleanup);

function draftText(overrides: Record<string, unknown> = {}) {
  return JSON.stringify({ ...WORKFLOW_A, ...overrides }, null, 2);
}

describe("ScenarioPages 纯函数", () => {
  it("JSON 语法错误定位到 workflow 根并给出 JSON_INVALID", () => {
    const parsed = parseWorkflowDraft("{ workflow_id: ");
    expect(parsed.value).toBeNull();
    expect(parsed.issues[0].locator).toBe("workflow");
    expect(parsed.issues[0].code).toBe("JSON_INVALID");
  });

  it("字段路径匹配覆盖子树（steps / steps[0] / steps[0].kind）", () => {
    const issues = [
      { locator: "steps[0].kind", code: "UNKNOWN_STEP_KIND", message: "未知步骤类型" },
      { locator: "limits.max_turns", code: "BUDGET_INVALID", message: "超出上限" },
    ];
    expect(issuesForLocator(issues, "steps")).toHaveLength(1);
    expect(issuesForLocator(issues, "limits.max_turns")).toHaveLength(1);
    expect(issuesForLocator(issues, "limits")).toHaveLength(1);
    expect(issuesForLocator(issues, "workflow_id")).toHaveLength(0);
  });

  it("缺状态的步骤是「未知」，不是成功；缺清理结论也是未知", () => {
    expect(stepStatusOf({ step_id: "s" } as any)).toBe("unknown");
    expect(stepStatusOf({ step_id: "s", status: "  " } as any)).toBe("unknown");
    expect(cleanupStatusOf({ fixture_id: "f", cleanup: { status: "failed" } } as any)).toBe("cleanup_failed");
    expect(cleanupStatusOf({ fixture_id: "f", cleanup: { error: "残留" } } as any)).toBe("cleanup_failed");
    expect(cleanupStatusOf({ fixture_id: "f" } as any)).toBe("unknown");
    expect(cleanupStatusOf({ fixture_id: "f", cleanup: { status: "cleaned" } } as any)).toBe("cleaned");
  });
});

describe("ScenarioWorkflowsPage", () => {
  it("逐字段校验：服务端错误就地显示，未通过校验前不能发布", async () => {
    clientMocks.validateWorkflow
      .mockResolvedValueOnce({
        ok: false,
        errors: [{ locator: "limits.max_turns", code: "BUDGET_INVALID", message: "max_turns 超出 DSL 上限" }],
      })
      .mockResolvedValueOnce({ ok: true, errors: [], content_hash: "sha256:new" });

    render(wrap(<ScenarioWorkflowsPage />));
    const editor = (await screen.findByLabelText(/WorkflowVersion DSL/)) as HTMLTextAreaElement;
    fireEvent.change(editor, { target: { value: draftText() } });

    const publish = screen.getByRole("button", { name: "发布版本" }) as HTMLButtonElement;
    expect(publish.disabled).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "校验（只读）" }));
    await waitFor(() => expect(screen.getByTestId("validation-result").textContent).toContain("失败"));
    expect(screen.getByText(/BUDGET_INVALID/)).toBeTruthy();

    // Schema 视图把服务端错误逐字段显示在对应字段下方
    const schemaTab = screen.getByRole("tab", { name: "Schema 字段" });
    fireEvent.mouseDown(schemaTab, { button: 0, ctrlKey: false });
    fireEvent.focus(schemaTab);
    await waitFor(() => expect(screen.getByTestId("field-error-limits.max_turns")).toBeTruthy());
    expect(screen.getByTestId("field-error-limits.max_turns").textContent).toContain("BUDGET_INVALID");
    expect((screen.getByRole("button", { name: "发布版本" }) as HTMLButtonElement).disabled).toBe(true);
    expect(clientMocks.publishWorkflow).not.toHaveBeenCalled();

    // 校验通过后才允许发布，且请求体就是当前草案
    fireEvent.mouseDown(screen.getByRole("tab", { name: "文本（JSON）" }), { button: 0, ctrlKey: false });
    fireEvent.click(screen.getByRole("button", { name: "校验（只读）" }));
    await waitFor(() => expect(screen.getByTestId("validation-result").textContent).toContain("通过"));
    await waitFor(() => expect((screen.getByRole("button", { name: "发布版本" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "发布版本" }));
    await waitFor(() => expect(clientMocks.publishWorkflow).toHaveBeenCalledTimes(1));
    expect(clientMocks.publishWorkflow.mock.calls[0][0].workflow_id).toBe("order-cancel-confirmed");
  });

  it("文本改动后上次校验结论失效：发布入口重新禁用并说明原因", async () => {
    clientMocks.validateWorkflow.mockResolvedValue({ ok: true, errors: [] });
    render(wrap(<ScenarioWorkflowsPage />));
    const editor = (await screen.findByLabelText(/WorkflowVersion DSL/)) as HTMLTextAreaElement;
    fireEvent.change(editor, { target: { value: draftText() } });
    fireEvent.click(screen.getByRole("button", { name: "校验（只读）" }));
    await waitFor(() => expect((screen.getByRole("button", { name: "发布版本" }) as HTMLButtonElement).disabled).toBe(false));

    fireEvent.change(editor, { target: { value: draftText({ description: "改了文本" }) } });
    await waitFor(() => expect(screen.getByTestId("validation-stale")).toBeTruthy());
    expect(screen.getByTestId("validation-stale").textContent).toContain("重新校验");
    expect((screen.getByRole("button", { name: "发布版本" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("JSON 语法错误就地显示，校验与发布都不可用", async () => {
    render(wrap(<ScenarioWorkflowsPage />));
    const editor = (await screen.findByLabelText(/WorkflowVersion DSL/)) as HTMLTextAreaElement;
    fireEvent.change(editor, { target: { value: "{ 坏掉的 JSON" } });
    await waitFor(() => expect(screen.getByTestId("field-error-workflow")).toBeTruthy());
    expect(screen.getByTestId("field-error-workflow").textContent).toContain("JSON_INVALID");
    expect((screen.getByRole("button", { name: "校验（只读）" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "发布版本" }) as HTMLButtonElement).disabled).toBe(true);
    // 草案内容原样保留，不被清空
    expect(editor.value).toBe("{ 坏掉的 JSON");
  });

  it("校验端点未注册时按「能力不可用」显示，并保留草稿内容", async () => {
    clientMocks.validateWorkflow.mockRejectedValue(unavailable());
    render(wrap(<ScenarioWorkflowsPage />));
    const editor = (await screen.findByLabelText(/WorkflowVersion DSL/)) as HTMLTextAreaElement;
    const draft = draftText();
    fireEvent.change(editor, { target: { value: draft } });
    fireEvent.click(screen.getByRole("button", { name: "校验（只读）" }));
    await waitFor(() => expect(screen.getByTestId("workflow-action-error")).toBeTruthy());
    expect(screen.getByTestId("workflow-action-error").textContent).toContain("能力不可用");
    expect(screen.getByTestId("workflow-action-error").textContent).toContain("404");
    expect(editor.value).toBe(draft);
    expect((screen.getByRole("button", { name: "发布版本" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("清单端点未注册时显示能力不可用，编辑器仍然可用（不静默隐藏入口）", async () => {
    clientMocks.getWorkflows.mockRejectedValue(unavailable());
    render(wrap(<ScenarioWorkflowsPage />));
    await waitFor(() => expect(screen.getByTestId("workflow-list-unavailable")).toBeTruthy());
    expect(screen.getByTestId("workflow-list-unavailable").textContent).toContain("能力不可用");
    const editor = screen.getByLabelText(/WorkflowVersion DSL/) as HTMLTextAreaElement;
    expect(editor.disabled).toBe(false);
  });

  it("切换版本时迟到的详情响应不得覆盖新选择", async () => {
    const first = deferred<any>();
    const second = deferred<any>();
    clientMocks.getWorkflows.mockResolvedValue({ items: [WORKFLOW_A, WORKFLOW_B], total: 2 });
    clientMocks.getWorkflow.mockImplementation((workflowId: string) =>
      workflowId === WORKFLOW_A.workflow_id ? first.promise : second.promise);
    render(wrap(<ScenarioWorkflowsPage />));
    const editor = (await screen.findByLabelText(/WorkflowVersion DSL/)) as HTMLTextAreaElement;

    fireEvent.click(screen.getByText("order-cancel-confirmed@1"));
    fireEvent.click(screen.getByText("order-cancel-unconfirmed@1"));
    second.resolve(WORKFLOW_B);
    await waitFor(() => expect(editor.value).toContain("order-cancel-unconfirmed"));

    first.resolve(WORKFLOW_A); // 迟到
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(editor.value).toContain("order-cancel-unconfirmed");
  });
});

describe("ScenarioRunStepsPage", () => {
  it("逐步状态、受控工具模式、未知结果、隔离与清理错误都如实显示", async () => {
    clientMocks.getRun.mockResolvedValue(RUN);
    clientMocks.getScenarioRunSteps.mockResolvedValue(STEPS);
    render(wrap(<ScenarioRunStepsPage />, "/scenario/runs/run-1"));

    await waitFor(() => expect(screen.getByText("before-confirm")).toBeTruthy());
    expect(screen.getAllByText("已完成").length).toBeGreaterThan(0); // 运行与步骤状态来自 STATUS_META
    expect(screen.getByText("已跳过")).toBeTruthy();            // 步骤状态来自 STATUS_META
    expect(screen.getAllByText("未知").length).toBeGreaterThan(0); // 步骤状态 / 工具模式 / 断言
    expect(screen.getByText("mock")).toBeTruthy();              // 受控工具模式
    expect(screen.getByText("清理失败")).toBeTruthy();          // fixture 清理状态
    expect(screen.getByText("未隔离")).toBeTruthy();
    expect(screen.getByText(/FIXTURE_PREPARE_FAILED/)).toBeTruthy();
    expect(screen.getByText("残留未清理")).toBeTruthy();

    // 展开未知步骤：未知结果必须写明「不得当作成功」，断言结论也是未知
    fireEvent.click(screen.getAllByRole("button", { name: "详情" })[1]);
    await waitFor(() => expect(screen.getByText(/该步骤存在未知结果/)).toBeTruthy());
    expect(screen.getByText("state.order.cancellation_count")).toBeTruthy();

    // checkpoint 的冻结证据在展开后可见（未展开时不渲染）；
    // 上一个步骤的按钮已变成「收起」，因此仍是按「详情」取第 2 个
    fireEvent.click(screen.getAllByRole("button", { name: "详情" })[1]);
    await waitFor(() => expect(screen.getByText("sha256:def")).toBeTruthy());
    expect(screen.getByText("已冻结")).toBeTruthy();
  });

  it("步骤明细端点未注册时按能力不可用显示，运行自身状态仍然可见", async () => {
    clientMocks.getRun.mockResolvedValue(RUN);
    clientMocks.getScenarioRunSteps.mockRejectedValue(unavailable());
    render(wrap(<ScenarioRunStepsPage />, "/scenario/runs/run-1"));
    await waitFor(() => expect(screen.getByTestId("scenario-steps-unavailable")).toBeTruthy());
    expect(screen.getByTestId("scenario-steps-unavailable").textContent).toContain("能力不可用");
    expect(screen.getByText("已完成")).toBeTruthy();
    expect(screen.getByText("order-cancel@1")).toBeTruthy();
  });
});
