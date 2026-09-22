import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { MemoryRouter } from "react-router-dom";
import {
  JudgesPage,
  calibrationGateText,
  calibrationStatusOf,
  preflightCostSummary,
} from "../src/pages/judges/JudgesPage";
import { ApiRequestError } from "../src/api/client";

const clientMocks = vi.hoisted(() => ({
  getJudges: vi.fn(),
  preflightJudge: vi.fn(),
  submitJudgeJob: vi.fn(),
  cancelJudgeJob: vi.fn(),
  getJudgeJob: vi.fn(),
  getScoringPasses: vi.fn(),
  getReport: vi.fn(),
}));

vi.mock("../src/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/client")>()),
  ...clientMocks,
}));

function wrap(node: React.ReactNode, route = "/judges") {
  return <MemoryRouter initialEntries={[route]}>{node}</MemoryRouter>;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const unavailable = () => new ApiRequestError(404, { code: "NOT_FOUND", message: "endpoint not registered" });

const JUDGE_EXPERIMENTAL = {
  judge_id: "answer-quality",
  version: "1",
  lifecycle: "published",
  provider: "provider-a",
  model: "provider-a/judge-model",
  spec_sha256: "sha256:spec",
  modes: ["single"],
  model_resource_id: "judge-model",
  budget: { max_calls: 40, hard_cost_cap_usd: null },
  spec: {
    judge_profile_id: "answer-quality",
    model: "judge-model",
    rubric_id: "answer-quality",
    rubric_version: "2",
    criteria: ["correctness"],
    parameters: {},
    missing_evidence_policy: "insufficient_evidence",
    budget: { max_calls: 40, max_prompt_tokens: 40000, max_completion_tokens: 4096, hard_cost_cap_usd: null },
  },
  evidence: {
    selector: { fields: ["final_output"] },
    case_ids: ["case-1"],
    observation_refs: ["obs-1"],
    missing_policy: "insufficient_evidence",
  },
  rubric: {
    rubric_id: "answer-quality",
    version: "2",
    content_sha256: "sha256:rubric",
    scale: "pass_fail",
    missing_evidence_policy: "insufficient_evidence",
    criteria: [{ criterion_id: "correctness", description: "回答是否正确", scale: "pass_fail", weight: 1 }],
  },
  calibration: {
    status: "experimental",
    samples: 12,
    human_reviewed: 0,
    required_samples: 30,
    synthetic_only: true,
    reasons: ["人工复核样本不足"],
    disagreements: [{ criterion: "correctness", metric: "false_pass_rate", value: 0.1, basis: "人工复核 12 条" }],
    policy: { thresholds: { false_pass_rate: 0.05 } },
    position_swap: { consistent: 10, inconsistent: 2 },
    repeat_stability: { stable: 9, unstable: 3 },
  },
  cost: { reported_usd: null, estimated_usd: null, unknown_cost: true, unknown_calls: 3 },
};

const JUDGE_CALIBRATED = {
  ...JUDGE_EXPERIMENTAL,
  judge_id: "safety-policy",
  version: "2",
  calibration: {
    ...JUDGE_EXPERIMENTAL.calibration,
    status: "calibrated",
    samples: 40,
    human_reviewed: 40,
    synthetic_only: false,
    reasons: [],
  },
};

const PREFLIGHT_UNKNOWN_PRICE = {
  mode: "score",
  model: "provider-a/judge-model",
  sample_count: 2,
  repeats: 1,
  max_calls: 4,
  token_ceiling: { total: 2000, budget_prompt: null, budget_completion: null },
  price_coverage: { known: false, price_table_version: null, input_per_million: null, output_per_million: null, estimated_cost_usd: null },
  authorised: true,
  budget_executable: true,
  hard_monetary_cap: false,
  reasons: ["price is unknown: no precise monetary hard cap can be claimed"],
};

beforeEach(() => {
  for (const mock of Object.values(clientMocks)) mock.mockReset();
  clientMocks.getJudges.mockResolvedValue({ items: [JUDGE_EXPERIMENTAL], total: 1 });
});

afterEach(cleanup);

describe("JudgesPage 纯函数", () => {
  it("缺校准字段是未运行，未知状态不按已校准处理", () => {
    expect(calibrationStatusOf(null)).toBe("not_run");
    expect(calibrationStatusOf({ status: "" })).toBe("not_run");
    expect(calibrationStatusOf({ status: "calibrated" })).toBe("calibrated");
    expect(calibrationGateText("calibrated")).toContain("可用于正式阻断 Gate");
    expect(calibrationGateText("experimental")).toContain("不进入正式阻断 Gate");
    expect(calibrationGateText("not_run")).toContain("不构成已校准声明");
    expect(calibrationGateText("mystery")).toContain("不得按已校准使用");
  });

  it("费用覆盖：未知价格不得伪装成估算", () => {
    expect(preflightCostSummary(null)).toBe("未预检");
    expect(preflightCostSummary({ price_coverage: { known: false } } as any)).toContain("未知");
    expect(preflightCostSummary({ price_coverage: { known: true, estimated_cost_usd: 0.25, price_table_version: "v1" } } as any))
      .toContain("$0.25");
  });
});

describe("JudgesPage 详情", () => {
  it("显示证据、rubric、校准状态（experimental）与独立成本", async () => {
    render(wrap(<JudgesPage />));
    await waitFor(() => expect(screen.getAllByText("answer-quality@1").length).toBeGreaterThan(0));
    expect(screen.getByTestId("judge-calibration-status").textContent).toContain("实验性");
    expect(screen.getByTestId("judge-calibration-status").textContent).toContain("不进入正式阻断 Gate");
    expect(screen.getByTestId("judge-synthetic-only")).toBeTruthy();
    expect(screen.getByTestId("judge-gate-eligibility").textContent).toContain("不进入正式阻断 Gate");
    // 证据
    expect(screen.getByText(/final_output/)).toBeTruthy();
    expect(screen.getAllByText("insufficient_evidence").length).toBeGreaterThan(0);
    // rubric 与逐项 criterion
    expect(screen.getByText("answer-quality@2")).toBeTruthy();
    expect(screen.getAllByText("correctness").length).toBeGreaterThan(0);
    expect(screen.getByText("回答是否正确")).toBeTruthy();
    // 校准分歧与政策
    expect(screen.getByText("false_pass_rate")).toBeTruthy();
    expect(screen.getByText(/人工复核 12 条/)).toBeTruthy();
    // 独立成本：缺证据是未知
    expect(screen.getByTestId("judge-independent-cost").textContent).toContain("未知");
    expect(screen.getByTestId("judge-independent-cost").textContent).toContain("3 次调用未报道成本");
  });

  it("calibrated 与 experimental 在清单与详情都区分 Gate 资格", async () => {
    clientMocks.getJudges.mockResolvedValue({ items: [JUDGE_EXPERIMENTAL, JUDGE_CALIBRATED], total: 2 });
    render(wrap(<JudgesPage />));
    await waitFor(() => expect(screen.getByText("safety-policy@2")).toBeTruthy());
    expect(screen.getAllByText("实验性").length).toBeGreaterThan(0);
    expect(screen.getAllByText("已校准").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByText("safety-policy@2"));
    await waitFor(() => expect(screen.getByTestId("judge-calibration-status").textContent).toContain("已校准"));
    expect(screen.getByTestId("judge-calibration-status").textContent).toContain("可用于正式阻断 Gate");
    expect(screen.queryByTestId("judge-gate-eligibility")).toBeNull();
  });

  it("Judge 清单端点未注册时按能力不可用显示，不崩溃", async () => {
    clientMocks.getJudges.mockRejectedValue(unavailable());
    render(wrap(<JudgesPage />));
    await waitFor(() => expect(screen.getByTestId("judge-list-unavailable")).toBeTruthy());
    expect(screen.getByTestId("judge-list-unavailable").textContent).toContain("能力不可用");
    expect(screen.getByText("Judge 详情")).toBeTruthy();
  });
});

describe("JudgesPage 付费提交", () => {
  async function openJudge() {
    render(wrap(<JudgesPage />));
    await waitFor(() => expect(screen.getByTestId("judge-paid-summary")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("被评 Run"), { target: { value: "run-1" } });
  }

  it("显示用途、模型、样本数、最大调用次数与已知/未知费用，且必须显式确认后才提交", async () => {
    clientMocks.preflightJudge.mockResolvedValue(PREFLIGHT_UNKNOWN_PRICE);
    clientMocks.submitJudgeJob.mockResolvedValue({
      job_id: "job-1", status: "settled", judge_id: "answer-quality", version: "1", purpose: "score",
      model: "provider-a/judge-model", scoring_pass_id: "pass-9", max_calls: 4, calls_made: 0,
      cost: { reported_usd: null, estimated_usd: null, unknown_cost: true, unknown_calls: 0 },
    });
    await openJudge();

    const summary = screen.getByTestId("judge-paid-summary").textContent ?? "";
    expect(summary).toContain("用途");
    expect(summary).toContain("provider-a/judge-model");
    expect(summary).toContain("样本数");
    expect(summary).toContain("最大调用次数");
    expect(summary).toContain("未预检");
    expect(summary).toContain("已知 / 未知费用");

    const submit = screen.getByTestId("judge-submit-button") as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    expect(screen.getByTestId("judge-submit-reason").textContent).toContain("必须先读取只读预检");
    expect(clientMocks.submitJudgeJob).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "读取预检（只读，零调用）" }));
    await waitFor(() => expect(clientMocks.preflightJudge).toHaveBeenCalledTimes(1));
    expect(clientMocks.preflightJudge.mock.calls[0][0]).toMatchObject({
      run_id: "run-1", mode: "single",
      spec: { judge_profile_id: "answer-quality", model: "judge-model" },
      authorisation: {
        authorised: true, actor: "web-operator-preflight", max_calls: 40,
        max_total_tokens: 1_763_840, hard_cost_cap_usd: null,
      },
    });
    await waitFor(() => expect(screen.getByTestId("judge-paid-summary").textContent).toContain("4"));
    expect(screen.getByTestId("judge-cost-coverage").textContent).toContain("未知");
    expect(screen.getByTestId("judge-hard-cap-unknown")).toBeTruthy();
    expect(screen.getByTestId("judge-preflight-reasons").textContent).toContain("price is unknown");
    // 预检完成但尚未显式确认：仍然不能提交
    expect((screen.getByTestId("judge-submit-button") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("judge-submit-reason").textContent).toContain("需要显式确认");

    fireEvent.click(screen.getByRole("switch", { name: "确认付费提交" }));
    await waitFor(() => expect((screen.getByTestId("judge-submit-button") as HTMLButtonElement).disabled).toBe(false));

    fireEvent.click(screen.getByTestId("judge-submit-button"));
    await waitFor(() => expect(clientMocks.submitJudgeJob).toHaveBeenCalledTimes(1));
    const body = clientMocks.submitJudgeJob.mock.calls[0][0];
    expect(body.request_key).toMatch(/^judge-web-/);
    expect(body.authorisation).toMatchObject({ authorised: true, actor: "web-operator", max_calls: 4 });
    await waitFor(() => expect(screen.getByTestId("judge-job")).toBeTruthy());
    expect(screen.getByTestId("judge-job").textContent).toContain("job-1");

    // 重复提交被幂等门禁挡住：按钮禁用，不再产生第二次付费请求
    expect((screen.getByTestId("judge-submit-button") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("judge-submit-reason").textContent).toContain("已提交");
    fireEvent.click(screen.getByTestId("judge-submit-button"));
    expect(clientMocks.submitJudgeJob).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "新建 Judge 请求" }));
    expect(screen.queryByTestId("judge-job")).toBeNull();
    expect(screen.getByTestId("judge-submit-reason").textContent).toContain("必须先读取只读预检");
    expect((screen.getByRole("switch", { name: "确认付费提交" }) as HTMLButtonElement).getAttribute("data-state")).toBe("unchecked");
  });

  it("未授权预检：提交保持禁用并说明零调用", async () => {
    clientMocks.preflightJudge.mockResolvedValue({
      ...PREFLIGHT_UNKNOWN_PRICE,
      authorised: false,
      budget_executable: false,
      reasons: ["no judge authorisation: zero calls are permitted"],
    });
    await openJudge();
    fireEvent.click(screen.getByRole("button", { name: "读取预检（只读，零调用）" }));
    await waitFor(() => expect(screen.getByTestId("judge-preflight-reasons")).toBeTruthy());
    fireEvent.click(screen.getByRole("switch", { name: "确认付费提交" }));
    expect((screen.getByTestId("judge-submit-button") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("judge-submit-reason").textContent).toContain("预算不可执行");
    expect(clientMocks.submitJudgeJob).not.toHaveBeenCalled();
  });

  it("提交失败保留表单内容并提示结果未知（不自动重复付费）", async () => {
    clientMocks.preflightJudge.mockResolvedValue(PREFLIGHT_UNKNOWN_PRICE);
    clientMocks.submitJudgeJob.mockRejectedValue(new ApiRequestError(503, { code: "JUDGE_UNAVAILABLE", message: "judge 暂时不可用" }));
    await openJudge();
    fireEvent.click(screen.getByRole("button", { name: "读取预检（只读，零调用）" }));
    await waitFor(() => expect(screen.getByTestId("judge-cost-coverage").textContent).toContain("未知"));
    fireEvent.click(screen.getByRole("switch", { name: "确认付费提交" }));
    await waitFor(() => expect((screen.getByTestId("judge-submit-button") as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByTestId("judge-submit-button"));
    await waitFor(() => expect(screen.getByTestId("judge-submit-error")).toBeTruthy());
    expect(screen.getByTestId("judge-submit-error").textContent).toContain("judge 暂时不可用");
    expect(screen.getByTestId("judge-submit-unknown").textContent).toContain("绝不自动重试");
    // 表单内容原样保留，但重新提交必须重新确认（不能沿用上一次勾选）
    expect((screen.getByLabelText("被评 Run") as HTMLInputElement).value).toBe("run-1");
    expect((screen.getByLabelText("用途") as HTMLSelectElement).value).toBe("single");
    expect((screen.getByTestId("judge-submit-button") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("judge-submit-reason").textContent).toContain("需要显式确认");
  });
});

describe("JudgesPage 只读历史", () => {
  it("读取历史、切换 pass 只走 GET；迟到响应不覆盖新 pass", async () => {
    const first = deferred<any>();
    const second = deferred<any>();
    clientMocks.getScoringPasses.mockResolvedValue({
      items: [
        { id: "pass-1", status: "completed", created_at: "2026-09-20T10:00:00Z" },
        { id: "pass-2", status: "completed", created_at: "2026-09-20T11:00:00Z" },
      ],
      total: 2,
    });
    clientMocks.getReport.mockImplementation((_runId: string, passId: string) =>
      passId === "pass-1" ? first.promise : second.promise);

    render(wrap(<JudgesPage />));
    await waitFor(() => expect(screen.getByTestId("judge-history-readonly")).toBeTruthy());
    expect(screen.getByTestId("judge-history-readonly").textContent).toContain("不会调用 Judge");

    fireEvent.change(screen.getByLabelText("Run"), { target: { value: "run-1" } });
    fireEvent.click(screen.getByRole("button", { name: "读取历史" }));
    await waitFor(() => expect(screen.getByText("pass-1")).toBeTruthy());

    fireEvent.click(screen.getAllByRole("button", { name: "查看（只读）" })[0]);
    fireEvent.click(screen.getAllByRole("button", { name: "查看（只读）" })[1]);
    second.resolve({ scores: [{ case_id: "case-b", metric_id: "judge.correctness", metric_status: "scored", passed: true, reason: "ok" }] });
    await waitFor(() => expect(screen.getByText("case-b")).toBeTruthy());
    first.resolve({ scores: [{ case_id: "case-a", metric_id: "judge.correctness", metric_status: "scored", passed: false, reason: "迟到" }] });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByText("case-a")).toBeNull();
    expect(screen.getByText("case-b")).toBeTruthy();

    // 读历史 / 切 pass 全程零 Judge 调用、零费用
    expect(clientMocks.preflightJudge).not.toHaveBeenCalled();
    expect(clientMocks.submitJudgeJob).not.toHaveBeenCalled();
  });
});
