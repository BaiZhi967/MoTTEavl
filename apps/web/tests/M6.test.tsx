import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { MemoryRouter } from "react-router-dom";
import { BaselinesPage } from "../src/pages/m6/BaselinesPage";
import { ComparePage } from "../src/pages/m6/ComparePage";
import { ExperimentsPage, experimentBadgeStatus } from "../src/pages/m6/ExperimentsPage";
import { GatePage, ruleBadgeStatus } from "../src/pages/m6/GatePage";
import { STATUS_META, STATUS_ORDER, statusTone } from "../src/components/statusMeta";
import { ApiRequestError } from "../src/api/client";

/** M6 页面测试：预览违规禁创建、创建后状态刷新、比较三级结论、Baseline CAS 冲突、
 * Gate 决策徽章与导出链接、能力不可用（404/501）与空态。 */

const clientMocks = vi.hoisted(() => ({
  previewExperiment: vi.fn(),
  createExperiment: vi.fn(),
  getExperiment: vi.fn(),
  cancelExperiment: vi.fn(),
  retryExperimentCell: vi.fn(),
  compareRunReports: vi.fn(),
  getComparisonStatistics: vi.fn(),
  getBaselines: vi.fn(),
  getBaseline: vi.fn(),
  getDefaultBaseline: vi.fn(),
  setDefaultBaseline: vi.fn(),
  createBaseline: vi.fn(),
  getGatePolicies: vi.fn(),
  publishGatePolicy: vi.fn(),
  deprecateGatePolicy: vi.fn(),
  evaluateVersionedGate: vi.fn(),
  getGateResult: vi.fn(),
}));

vi.mock("../src/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/client")>()),
  ...clientMocks,
}));

function wrap(node: React.ReactNode, route = "/") {
  return <MemoryRouter initialEntries={[route]}>{node}</MemoryRouter>;
}

beforeEach(() => {
  for (const mock of Object.values(clientMocks)) mock.mockReset();
  clientMocks.getComparisonStatistics.mockResolvedValue({
    applicable: false, reason: "insufficient_tasks", n_selected: 1,
    n_pairs: 1, missing_pairs: 0, policy_ref: "statistical_policy@1",
    policy_hash: "sha256:policy", method: "paired_task_cluster_bootstrap",
    unit: "task(case)", refs: {}, statistics: null,
  });
});

afterEach(cleanup);

// ---------------------------------------------------------------- STATUS_META

describe("M6 状态登记（STATUS_META）", () => {
  it("比较 / baseline / gate / rule 状态的语气与标签已登记", () => {
    expect(STATUS_META.comparable).toMatchObject({ label: "可比", tone: "success", scope: "comparison" });
    expect(STATUS_META.partially_comparable).toMatchObject({ label: "部分可比", tone: "warning" });
    expect(STATUS_META.not_comparable).toMatchObject({ label: "不可比", tone: "error" });
    expect(STATUS_META.formal).toMatchObject({ label: "正式", tone: "success", scope: "baseline" });
    expect(STATUS_META.diagnostic).toMatchObject({ label: "诊断", tone: "warning" });
    expect(statusTone("pass")).toBe("success");
    expect(statusTone("quality_fail")).toBe("error");
    expect(statusTone("insufficient_evidence")).toBe("warning");
    expect(statusTone("execution_error")).toBe("warning");
    expect(statusTone("safety_block")).toBe("error");
    expect(statusTone("draft")).toBe("info");
    expect(statusTone("deprecated")).toBe("neutral");
    expect(statusTone("allocated")).toBe("success");
    expect(statusTone("allocating")).toBe("info");
    expect(statusTone("insufficient")).toBe("warning");
    expect(statusTone("not_applicable")).toBe("neutral");
    expect(statusTone("skipped_diagnostic")).toBe("neutral");
    expect(ruleBadgeStatus("pass")).toBe("passed");
    expect(ruleBadgeStatus("fail")).toBe("failed");
    expect(ruleBadgeStatus("insufficient")).toBe("insufficient");
  });

  it("M6 状态不污染运行总览的状态过滤（scope 非 run）", () => {
    expect(STATUS_ORDER).toContain("failed");
    expect(STATUS_ORDER).toContain("cancelled");
    for (const status of ["comparable", "formal", "pass", "allocated", "insufficient", "published"]) {
      expect(STATUS_ORDER).not.toContain(status);
    }
  });
});

// ---------------------------------------------------------------- 实验

const CLEAN_OUTCOME = {
  experiment_id: "exp-demo",
  version: "1",
  created: true,
  allocated: 2,
  skipped_existing: 0,
  failed: [],
  spec: { experiment_id: "exp-demo", version: "1" },
  cell_count: 2,
  progress: { pending: 0, allocating: 0, allocated: 2, failed: 0, cancelled: 0 },
  cells: [
    {
      cell_id: "sha256:aaaa1111bbbb",
      repeat_index: 0,
      factor_assignment: { model_profile: "model-a" },
      allocation_status: "allocated",
      run_id: "run-exp-1",
      superseding_run_ids: [],
    },
    {
      cell_id: "sha256:cccc2222dddd",
      repeat_index: 1,
      factor_assignment: { model_profile: "model-b" },
      allocation_status: "allocated",
      run_id: "run-exp-2",
      superseding_run_ids: [],
    },
  ],
};

describe("ExperimentsPage", () => {
  it("预览违规显示原因列表，创建入口禁用", async () => {
    clientMocks.previewExperiment.mockResolvedValue({
      experiment_id: "exp-demo",
      version: "1",
      cells: [],
      cell_count: 48,
      max_potential_calls: 9600,
      budget: { max_total_calls: 100, max_total_tokens: 100000 },
      violations: [
        { code: "MATRIX_TOO_LARGE", message: "cell_count 48 exceeds max_cells 20" },
        { code: "BUDGET_EXCEEDED", message: "max_potential_calls 9600 exceeds budget max_total_calls 100" },
      ],
    });
    render(wrap(<ExperimentsPage />));

    fireEvent.click(screen.getByTestId("experiment-preview-button"));
    await waitFor(() => expect(screen.getByTestId("experiment-preview-violations")).toBeTruthy());
    expect(screen.getByTestId("experiment-preview-violations").textContent).toContain("MATRIX_TOO_LARGE");
    expect(screen.getByTestId("experiment-preview-violations").textContent).toContain("BUDGET_EXCEEDED");
    expect(screen.getByTestId("experiment-create-reason").textContent).toContain("违规");
    const create = screen.getByTestId("experiment-create-button") as HTMLButtonElement;
    expect(create.disabled).toBe(true);
    expect(clientMocks.createExperiment).not.toHaveBeenCalled();
  });

  it("预览通过后创建（202），状态视图显示 cell 表与分配徽章", async () => {
    clientMocks.previewExperiment.mockResolvedValue({
      experiment_id: "exp-demo",
      version: "1",
      cells: [],
      cell_count: 2,
      max_potential_calls: 40,
      budget: { max_total_calls: 100, max_total_tokens: 100000 },
      violations: [],
    });
    clientMocks.createExperiment.mockResolvedValue(CLEAN_OUTCOME);
    render(wrap(<ExperimentsPage />));

    fireEvent.click(screen.getByTestId("experiment-preview-button"));
    await waitFor(() => expect(screen.getByTestId("experiment-preview-clean")).toBeTruthy());
    await waitFor(() => expect((screen.getByTestId("experiment-create-button") as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByTestId("experiment-create-button"));

    await waitFor(() => expect(clientMocks.createExperiment).toHaveBeenCalledTimes(1));
    // 创建体就是预览过的 spec（服务端零猜测）
    expect(clientMocks.createExperiment.mock.calls[0][0]).toMatchObject({ experiment_id: "exp-demo", version: "1" });
    // 状态刷新：cell 表 + run 链接 + 分配徽章 + 清单条目
    await waitFor(() => expect(screen.getByTestId("experiment-cells-table").textContent).toContain("run-exp-1"));
    expect(screen.getByTestId("experiment-cells-table").textContent).toContain("model_profile=model-a");
    expect(screen.getAllByText("已分配").length).toBeGreaterThan(0);
    expect(screen.getByTestId("experiment-create-notice").textContent).toContain("202");
    expect(screen.getAllByText("exp-demo@1").length).toBeGreaterThan(0);
    expect(screen.getAllByText("已完成").length).toBeGreaterThan(0);
    expect(experimentBadgeStatus(CLEAN_OUTCOME)).toBe("completed");
  });

  it("按 ID 载入实验后可两步确认取消；取消只作用于本实验", async () => {
    clientMocks.getExperiment.mockResolvedValue(CLEAN_OUTCOME);
    clientMocks.cancelExperiment.mockResolvedValue({
      ...CLEAN_OUTCOME,
      reason: "experiment cancelled",
      cancelled_at: "2026-09-21T12:00:00Z",
      cancelled_cells: ["sha256:aaaa1111bbbb", "sha256:cccc2222dddd"],
      cancelled_runs: [],
      progress: { pending: 0, allocating: 0, allocated: 0, failed: 0, cancelled: 2 },
      cells: CLEAN_OUTCOME.cells.map((cell) => ({ ...cell, allocation_status: "cancelled", run_id: null })),
    });
    render(wrap(<ExperimentsPage />));

    fireEvent.change(screen.getByLabelText(/按 ID 载入/), { target: { value: "exp-demo@1" } });
    fireEvent.click(screen.getByRole("button", { name: "载入" }));
    await waitFor(() => expect(clientMocks.getExperiment).toHaveBeenCalledWith("exp-demo", "1"));
    await waitFor(() => expect(screen.getByTestId("experiment-cells-table").textContent).toContain("run-exp-1"));

    // 两步确认：先出现确认按钮，确认后才真正调用
    fireEvent.click(screen.getByRole("button", { name: "取消实验" }));
    expect(clientMocks.cancelExperiment).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("experiment-cancel-confirm"));
    await waitFor(() => expect(clientMocks.cancelExperiment).toHaveBeenCalledTimes(1));
    expect(clientMocks.cancelExperiment.mock.calls[0][0]).toBe("exp-demo");
    expect(clientMocks.cancelExperiment.mock.calls[0][1]).toMatchObject({ version: "1" });
    await waitFor(() => expect(screen.getByTestId("experiment-action-notice").textContent).toContain("已取消"));
    expect(screen.getAllByText("已取消").length).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------- 比较

describe("ComparePage", () => {
  it("显示固定 pass 统计与缺失资格，统计请求失败不抹掉比较结果", async () => {
    clientMocks.compareRunReports.mockResolvedValue({
      eligible: true, level: "comparable", structural_reasons: [], metric_reasons: [],
      metric_eligibility: { quality: true }, case_diff: { added: [], removed: [], changed: [] },
    });
    clientMocks.getComparisonStatistics.mockResolvedValueOnce({
      applicable: false, reason: "missing_or_uncertain_case", n_selected: 3,
      n_pairs: 2, missing_pairs: 1, policy_ref: "statistical_policy@1",
      policy_hash: "sha256:policy", method: "paired_task_cluster_bootstrap",
      unit: "task(case)", refs: {
        baseline: { run_id: "run-b", scoring_pass_id: "pass-b" },
        candidate: { run_id: "run-c", scoring_pass_id: "pass-c" },
      }, statistics: null,
    }).mockRejectedValueOnce(new Error("statistics offline"));
    render(wrap(<ComparePage />));
    fireEvent.change(screen.getByLabelText(/基线 Run/), { target: { value: "run-b" } });
    fireEvent.change(screen.getByLabelText(/候选 Run/), { target: { value: "run-c" } });
    fireEvent.change(screen.getByLabelText(/基线 ScoringPass/), { target: { value: "pass-b" } });
    fireEvent.change(screen.getByLabelText(/候选 ScoringPass/), { target: { value: "pass-c" } });
    fireEvent.click(screen.getByTestId("compare-submit"));
    await waitFor(() => expect(screen.getByTestId("compare-statistics").textContent).toContain("missing_or_uncertain_case"));
    expect(screen.getByTestId("compare-statistics").textContent).toContain("2 / 3");
    expect(screen.getByTestId("compare-statistics").textContent).toContain("pass-b");
    expect(clientMocks.getComparisonStatistics.mock.calls[0][0]).toMatchObject({
      baseline_pass: "pass-b", candidate_pass: "pass-c", factors: ["model"],
    });
    fireEvent.click(screen.getByTestId("compare-submit"));
    await waitFor(() => expect(screen.getByTestId("compare-statistics-error").textContent).toContain("statistics offline"));
    expect(screen.getByTestId("compare-result")).toBeTruthy();
  });
  it("渲染三级结论、分组原因、case diff 与指标资格；model 因子默认允许", async () => {
    clientMocks.compareRunReports.mockResolvedValue({
      eligible: false,
      level: "not_comparable",
      reasons: ["judge differs"],
      structural_reasons: ["judge_id judge-a differs from judge-b (not allowed by policy)"],
      metric_reasons: ["cost.total_usd: unknown on candidate"],
      metric_eligibility: { accuracy: true, "cost.total_usd": false },
      case_diff: { added: ["case-3"], removed: ["case-1"], changed: ["case-2"] },
      allowed_differences: ["model"],
    });
    render(wrap(<ComparePage />));

    // model 默认勾选，judge 未勾选（未允许的条件必须一致）
    expect((screen.getByLabelText("model") as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText("judge") as HTMLInputElement).checked).toBe(false);

    fireEvent.change(screen.getByLabelText(/基线 Run/), { target: { value: "run-b" } });
    fireEvent.change(screen.getByLabelText(/候选 Run/), { target: { value: "run-c" } });
    fireEvent.click(screen.getByTestId("compare-submit"));

    await waitFor(() => expect(clientMocks.compareRunReports).toHaveBeenCalledTimes(1));
    expect(clientMocks.compareRunReports.mock.calls[0][0]).toMatchObject({
      baseline: "run-b",
      candidate: "run-c",
      factors: ["model"],
    });

    // 三级结论徽章 + eligible 说明
    const result = await waitFor(() => screen.getByTestId("compare-result"));
    expect(screen.getAllByText("不可比").length).toBeGreaterThan(0);
    expect(result.textContent).toContain("质量指标不可比");
    // 政策显式允许的差异可审计
    expect(result.textContent).toContain("model");
    // 结构性 / 指标原因分组
    expect(screen.getByTestId("compare-structural-reasons").textContent)
      .toContain("judge_id judge-a differs from judge-b");
    expect(screen.getByTestId("compare-metric-reasons").textContent).toContain("cost.total_usd: unknown on candidate");
    // case diff 三组 mono 清单
    expect(screen.getByTestId("compare-diff-added").textContent).toContain("case-3");
    expect(screen.getByTestId("compare-diff-removed").textContent).toContain("case-1");
    expect(screen.getByTestId("compare-diff-changed").textContent).toContain("case-2");
    // 指标资格表：可比 / 资格不足（不折算成 0）
    const eligibility = screen.getByTestId("compare-metric-eligibility").textContent ?? "";
    expect(eligibility).toContain("accuracy");
    expect(eligibility).toContain("可比");
    expect(eligibility).toContain("资格不足");
  });

  it("比较失败照实显示服务端错误码 + 消息，不清空输入", async () => {
    clientMocks.compareRunReports.mockRejectedValue(
      new ApiRequestError(404, { code: "RUN_NOT_FOUND", message: "run run-missing not found" }),
    );
    render(wrap(<ComparePage />));
    fireEvent.change(screen.getByLabelText(/基线 Run/), { target: { value: "run-missing" } });
    fireEvent.change(screen.getByLabelText(/候选 Run/), { target: { value: "run-c" } });
    fireEvent.click(screen.getByTestId("compare-submit"));
    await waitFor(() => expect(screen.getByTestId("compare-error").textContent).toContain("RUN_NOT_FOUND"));
    expect((screen.getByLabelText(/基线 Run/) as HTMLInputElement).value).toBe("run-missing");
  });
});

// ---------------------------------------------------------------- Baseline

const BASELINE = {
  baseline_id: "blt-1",
  entries: [{ cell_key: null, ref: { run_id: "run-b", scoring_pass_id: "pass-b" } }],
  comparison_policy_hash: "sha256:policy",
  eligibility: "formal",
  created_by: "ops",
  reason: "freeze release report",
  source_note: null,
  created_at: "2026-09-20T08:00:00Z",
  metrics: { accuracy: 0.91, "cost.total_usd": null },
};

describe("BaselinesPage", () => {
  it("清单 + 详情：资格徽章、entries 与未知指标（null 不渲染成 0）", async () => {
    clientMocks.getBaselines.mockResolvedValue({ items: [BASELINE], total: 1 });
    clientMocks.getBaseline.mockResolvedValue(BASELINE);
    render(wrap(<BaselinesPage />));

    await waitFor(() => expect(screen.getAllByText("blt-1").length).toBeGreaterThan(0));
    expect(screen.getAllByText("正式").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByText("blt-1"));
    await waitFor(() => expect(clientMocks.getBaseline).toHaveBeenCalledWith("blt-1"));
    const detail = screen.getByTestId("baseline-detail-kv").textContent ?? "";
    expect(detail).toContain("sha256:policy");
    expect(screen.getByTestId("baseline-entries-table").textContent).toContain("run-b");
    expect(screen.getByTestId("baseline-entries-table").textContent).toContain("pass-b");
    // cost.total_usd 为 null：显示未知，不填 0
    const metrics = screen.getByTestId("baseline-metrics").textContent ?? "";
    expect(metrics).toContain("accuracy");
    expect(metrics).toContain("0.91");
    expect(metrics).toContain("未知");
  });

  it("指针切换 409 CAS_CONFLICT：错误就地显示，表单内容保留", async () => {
    clientMocks.getBaselines.mockResolvedValue({ items: [BASELINE], total: 1 });
    clientMocks.getDefaultBaseline.mockResolvedValue({
      scope: "release",
      pointer: {
        scope: "release",
        baseline_id: "blt-old",
        updated_by: "ops",
        reason: "prev",
        comparison_policy_hash: "sha256:policy",
        updated_at: "2026-09-20T09:00:00Z",
        position: 3,
      },
    });
    clientMocks.setDefaultBaseline.mockRejectedValue(
      new ApiRequestError(409, {
        code: "CAS_CONFLICT",
        message: "default baseline for scope release is 'blt-old', expected 'blt-stale'",
      }),
    );
    render(wrap(<BaselinesPage />));

    fireEvent.change(screen.getByLabelText("scope"), { target: { value: "release" } });
    fireEvent.click(screen.getByRole("button", { name: "读取指针" }));
    await waitFor(() => expect(screen.getByTestId("baseline-pointer-current").textContent).toContain("blt-old"));

    fireEvent.change(screen.getByLabelText("目标 baseline_id"), { target: { value: "blt-new" } });
    fireEvent.change(screen.getByLabelText(/expected_current/), { target: { value: "blt-stale" } });
    fireEvent.change(screen.getByLabelText(/操作者/), { target: { value: "ops-2" } });
    fireEvent.change(screen.getByLabelText(/切换理由/), { target: { value: "refresh baseline" } });
    fireEvent.click(screen.getByTestId("baseline-pointer-submit"));

    await waitFor(() => expect(clientMocks.setDefaultBaseline).toHaveBeenCalledTimes(1));
    expect(clientMocks.setDefaultBaseline.mock.calls[0][0]).toMatchObject({
      scope: "release",
      baseline_id: "blt-new",
      expected_current: "blt-stale",
      updated_by: "ops-2",
      reason: "refresh baseline",
    });
    const error = screen.getByTestId("baseline-pointer-error");
    expect(error.textContent).toContain("CAS_CONFLICT");
    expect(error.textContent).toContain("blt-old");
    // 失败不清表单：目标与 CAS 期望原样保留
    expect((screen.getByLabelText("目标 baseline_id") as HTMLInputElement).value).toBe("blt-new");
    expect((screen.getByLabelText(/expected_current/) as HTMLInputElement).value).toBe("blt-stale");
  });

  it("空态：暂无 Baseline", async () => {
    clientMocks.getBaselines.mockResolvedValue({ items: [], total: 0 });
    render(wrap(<BaselinesPage />));
    await waitFor(() => expect(screen.getByTestId("baseline-list-empty").textContent).toBe("暂无 Baseline"));
  });
});

// ---------------------------------------------------------------- 门禁

const POLICY = {
  policy_id: "release-gate",
  version: "1",
  lifecycle: "published",
  diagnostic: false,
  created_by: "ops",
  reason: "release guard",
  created_at: "2026-09-20T10:00:00Z",
  rules: [
    { rule_id: "accuracy-floor", kind: "metric_threshold", metric_id: "accuracy", operator: "gte", threshold: 0.8 },
  ],
};

const GATE_RESULT = {
  gate_result_id: "sha256:result-1",
  policy_id: "release-gate",
  policy_version: "1",
  policy_content_hash: "sha256:policycontent",
  baseline: null,
  candidates: [{ run_id: "run-9", scoring_pass_id: "pass-9" }],
  decision: "quality_fail",
  rule_results: [
    { rule_id: "accuracy-floor", kind: "metric_threshold", status: "fail", severity: "block", decision: "quality_fail", reason: "accuracy 0.5 < 0.8" },
    { rule_id: "coverage-min", kind: "coverage", status: "pass", severity: "block", reason: "coverage 1.0 >= 0.9" },
    { rule_id: "cost-known", kind: "cost", status: "insufficient", severity: "block", reason: "cost unknown: no price table" },
  ],
  evaluation_input_hash: "sha256:input",
  result_semantics_hash: "sha256:semantics",
  conclusion_hash: "sha256:conclusion",
  evaluated_at: "2026-09-21T10:00:00Z",
  suggested_actions: ["共同重评分后重新求值"],
  exit_code: 1,
};

describe("GatePage", () => {
  it("求值结果：决策徽章 + 退出码 + 逐规则表 + 导出链接", async () => {
    clientMocks.getGatePolicies.mockResolvedValue({ items: [POLICY], total: 1 });
    clientMocks.evaluateVersionedGate.mockResolvedValue(GATE_RESULT);
    render(wrap(<GatePage />));

    await waitFor(() => expect(screen.getAllByText("release-gate@1").length).toBeGreaterThan(0));
    expect(screen.getAllByText("已发布").length).toBeGreaterThan(0);

    fireEvent.change(screen.getByLabelText(/Run（必填/), { target: { value: "run-9" } });
    fireEvent.click(screen.getByTestId("gate-evaluate-submit"));
    await waitFor(() => expect(clientMocks.evaluateVersionedGate).toHaveBeenCalledTimes(1));
    expect(clientMocks.evaluateVersionedGate.mock.calls[0][0]).toMatchObject({
      run_id: "run-9",
      policy_id: "release-gate",
      policy_version: "1",
    });

    await waitFor(() => expect(screen.getByTestId("gate-result-kv").textContent).toContain("sha256:result-1"));
    // 决策徽章与退出码
    expect(screen.getAllByText("质量失败").length).toBeGreaterThan(0);
    expect(screen.getByText("退出码 1")).toBeTruthy();
    // 逐规则表：三类规则状态徽章（失败 / 通过 / 证据不足）+ reason 全文
    const rules = screen.getByTestId("gate-rules-table").textContent ?? "";
    expect(rules).toContain("accuracy-floor");
    expect(rules).toContain("accuracy 0.5 < 0.8");
    expect(rules).toContain("cost unknown: no price table");
    expect(screen.getAllByText("失败").length).toBeGreaterThan(0);
    expect(screen.getAllByText("通过").length).toBeGreaterThan(0);
    expect(screen.getAllByText("证据不足").length).toBeGreaterThan(0);
    expect(screen.getByText("共同重评分后重新求值")).toBeTruthy();
    // 导出：json / junit 直连导出端点
    const links = screen.getByTestId("gate-export-links").querySelectorAll("a");
    expect(links.length).toBe(2);
    expect(links[0].getAttribute("href")).toContain("/api/v1/gates/results/sha256%3Aresult-1/export?format=json");
    expect(links[1].getAttribute("href")).toContain("format=junit");
  });

  it("政策端点 501：按能力不可用显示具名原因，不静默隐藏", async () => {
    clientMocks.getGatePolicies.mockRejectedValue(
      new ApiRequestError(501, { code: "GATE_STORE_UNAVAILABLE", message: "gate store not wired" }),
    );
    render(wrap(<GatePage />));
    await waitFor(() => expect(screen.getByTestId("gate-list-unavailable")).toBeTruthy());
    expect(screen.getByTestId("gate-list-unavailable").textContent).toContain("能力不可用");
    expect(screen.getByTestId("gate-list-unavailable").textContent).toContain("501");
    // 入口保留：政策详情占位仍在
    expect(screen.getByText("政策详情")).toBeTruthy();
  });

  it("空态：暂无 Gate 政策", async () => {
    clientMocks.getGatePolicies.mockResolvedValue({ items: [], total: 0 });
    render(wrap(<GatePage />));
    await waitFor(() => expect(screen.getByTestId("gate-list-empty").textContent).toBe("暂无 Gate 政策"));
  });
});
