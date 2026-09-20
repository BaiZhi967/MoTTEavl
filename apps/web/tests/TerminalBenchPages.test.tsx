import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { MemoryRouter } from "react-router-dom";
import {
  TerminalBenchCompare,
  TerminalBenchMonitor,
  TerminalBenchOperate,
  TerminalBenchResult,
  TerminalBenchTasks,
} from "../src/evalTypes/terminalbench/TerminalBenchPages";
import type { TerminalBenchTrialDetail } from "../src/api/client";

const clientMocks = vi.hoisted(() => ({
  getTerminalBenchOverview: vi.fn(),
  getTerminalBenchTasks: vi.fn(),
  getTerminalBenchPreflight: vi.fn(),
  createTerminalBenchRun: vi.fn(),
  getRunTasks: vi.fn(),
  getRunTaskTrials: vi.fn(),
  getRunTrial: vi.fn(),
  getRuns: vi.fn(),
  getRun: vi.fn(),
  compareRuns: vi.fn(),
  evaluateRunGate: vi.fn(),
}));

vi.mock("../src/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/api/client")>()),
  ...clientMocks,
}));

function wrap(node: React.ReactNode, route = "/terminal-bench") {
  return <MemoryRouter initialEntries={[route]}>{node}</MemoryRouter>;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const RUN = {
  id: "run-tb-1",
  status: "completed",
  scenario_version: "terminal-bench-harbor@1",
  manifest: {
    model: "provider/model-a",
    benchmark_provenance: { aggregation: "first-trial", dataset_revision: "tb-rev-1" },
  },
};

const TASK_ROW = {
  task_key: "task-a",
  normalized_relative_path: "tasks/task-a",
  planned_trials: 3,
  observed_trials: 3,
  valid_trials: 2,
  invalid_trials: 1,
  valid_trial_pass_rate: 0.5,
  task_pass: true,
  task_pass_reason: "first-trial",
  aggregate: {
    selected_tasks: 1,
    scored_tasks: 1,
    selected_trials: 3,
    observed_trials: 3,
    valid_trials: 2,
    invalid_trials: 1,
    valid_trial_pass_rate: 0.5,
  },
};

const TRIAL_ROWS = [
  {
    trial_id: "trial-a", repeat_index: 0, disposition: "failed", verifier_status: "scored",
    reward: 0, valid: true, coverage: { missing: [], partial: [] }, source_trial_id: "src-a",
  },
  {
    trial_id: "trial-b", repeat_index: 1, disposition: "succeeded", verifier_status: "scored",
    reward: 1, valid: true, coverage: { missing: ["trajectory"], partial: [] }, source_trial_id: "src-b",
  },
  {
    trial_id: "trial-c", repeat_index: 2, disposition: "indeterminate", verifier_status: "verifier_protocol_error",
    reward: null, valid: false, coverage: { missing: ["reward"], partial: [] }, source_trial_id: null,
  },
];

function makeDetail(
  trialId: string,
  overrides: Partial<TerminalBenchTrialDetail> = {},
): TerminalBenchTrialDetail {
  return {
    run_id: "run-tb-1",
    trial_id: trialId,
    task_key: "task-a",
    repeat_index: 0,
    disposition: "failed",
    termination: {
      reason: "completed",
      agent_started: true,
      failure_phase: null,
      exception_type: null,
      exception_message: null,
      timings: {
        environment_setup_sec: 1.25,
        agent_setup_sec: 0.5,
        agent_execution_sec: 12.5,
        verifier_sec: 2.75,
        total_sec: 17,
      },
    },
    verifier_observation: {
      status: "scored",
      rewards: { reward: 0 },
      error: null,
      evidence_refs: [{ artifact_id: "ev-1" }],
    },
    usage: { cost_usd: 0.125, tokens: { input: 10, output: 5 }, coverage: "observed" },
    coverage: {
      items: {
        instruction: "complete", config: "complete", terminal: "complete", trajectory: "complete",
        workspace: "complete", verifier_output: "complete", reward: "complete", usage: "complete",
      },
      missing: [],
      partial: [],
    },
    artifacts: [{
      artifact_id: "artifact-a", kind: "harbor-trial-log", sha256: "sha256:aaa",
      size_bytes: 2048, complete: true, truncated: false, note: null,
    }],
    terminal_text: "$ solved task-a\n",
    terminal_truncated: false,
    evidence_complete: true,
    ...overrides,
  };
}

beforeEach(() => {
  for (const mock of Object.values(clientMocks)) mock.mockReset();
  clientMocks.getTerminalBenchOverview.mockResolvedValue({
    items: [{
      scenario: "terminal-bench-harbor@1", dataset_revision: "tb-rev-1", tasks: 2,
      runner_connected: true,
      runs: [{ id: "run-tb-1", status: "completed", created_at: "2026-09-20T00:00:00Z", valid_trial_pass_rate: 0.5 }],
    }],
    total: 1,
    runner: { adapter_id: "terminal-bench-harbor", harbor_version: "0.23.0", connected: true },
    benchmark: "terminal-bench",
  });
  clientMocks.getTerminalBenchTasks.mockResolvedValue({
    items: [
      {
        task_key: "task-a", normalized_relative_path: "tasks/task-a", display_name: "Task A",
        source_id: "local-root", dataset_revision: "tb-rev-1", file_count: 4, total_bytes: 2048,
        has_tests: true, has_solution: true, declared_license: "MIT",
      },
      {
        task_key: "task-b", normalized_relative_path: "tasks/task-b", display_name: "Task B",
        source_id: "local-root", dataset_revision: "tb-rev-1", file_count: 2, total_bytes: 512,
        has_tests: false, has_solution: false, declared_license: null,
      },
    ],
    total: 2,
  });
  clientMocks.getRun.mockResolvedValue(RUN);
  clientMocks.getRuns.mockResolvedValue({ items: [], total: 0 });
  clientMocks.getRunTasks.mockResolvedValue({ run_id: "run-tb-1", items: [TASK_ROW], total: 1 });
  clientMocks.getRunTaskTrials.mockResolvedValue({
    run_id: "run-tb-1", task_key: "task-a", items: TRIAL_ROWS, total: 3,
  });
  clientMocks.getRunTrial.mockImplementation(
    (_runId: string, trialId: string) => Promise.resolve(makeDetail(trialId)),
  );
  clientMocks.createTerminalBenchRun.mockResolvedValue({ id: "run-new", status: "queued" });
});

afterEach(cleanup);

describe("Terminal-Bench 页面", () => {
  it("操作页：预检阻塞原因带可执行说明，创建按钮保持禁用", async () => {
    clientMocks.getTerminalBenchPreflight.mockResolvedValue({
      ok: false,
      reasons: ["DOCKER_UNAVAILABLE", "TASK_WITHOUT_VERIFIER"],
      messages: {
        DOCKER_UNAVAILABLE: "Runner 上无法访问 Docker daemon，无法创建任务环境。",
        TASK_WITHOUT_VERIFIER: "所选任务没有 Verifier（tests/），无法产生评分证据。",
      },
      checks: {
        environment_type: "docker",
        docker: { available: false },
        network_policy: "allowed",
        task_count: 2,
      },
      profile_fingerprint: "sha256:profile-1",
      platform_custom_profile: false,
    });
    render(wrap(<TerminalBenchOperate />));
    await waitFor(() => expect(screen.getByTestId("tb-dataset-revision").textContent).toContain("tb-rev-1"));
    // 数据集版本与 Runner 状态常显
    expect(screen.getByTestId("tb-runner").textContent).toContain("harbor 0.23.0");
    expect(screen.getByTestId("tb-runner").textContent).toContain("已连接");
    expect(screen.getByTestId("tb-task-list").textContent).toContain("tasks/task-a");
    // 预检前 fail-closed：不能创建
    expect((screen.getByTestId("tb-create-run") as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: /预检/ }));
    await waitFor(() => expect(screen.getByTestId("tb-preflight")).toBeTruthy());
    const reasons = screen.getByTestId("tb-preflight-reasons");
    expect(reasons.textContent).toContain("DOCKER_UNAVAILABLE");
    expect(reasons.textContent).toContain("无法访问 Docker daemon");
    expect(reasons.textContent).toContain("TASK_WITHOUT_VERIFIER");
    expect(screen.getByTestId("tb-preflight-verdict").textContent).toContain("2 项阻塞");
    expect(screen.getByTestId("tb-preflight").textContent).toContain("sha256:profile-1");
    expect(screen.getByTestId("tb-preflight").textContent).toContain("docker");
    expect((screen.getByTestId("tb-create-run") as HTMLButtonElement).disabled).toBe(true);

    // 预检通过后放行；输入改动使旧结论作废（必须重新预检）
    clientMocks.getTerminalBenchPreflight.mockResolvedValue({
      ok: true, reasons: [], messages: {},
      checks: { environment_type: "docker", task_count: 2 },
      profile_fingerprint: "sha256:profile-2", platform_custom_profile: false,
    });
    fireEvent.click(screen.getByRole("button", { name: /预检/ }));
    await waitFor(() => expect(screen.getByTestId("tb-preflight-verdict").textContent).toContain("预检通过"));
    expect((screen.getByTestId("tb-create-run") as HTMLButtonElement).disabled).toBe(false);
    fireEvent.change(screen.getByLabelText("重复次数"), { target: { value: "3" } });
    expect((screen.getByTestId("tb-create-run") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: /预检/ }));
    await waitFor(() => expect((screen.getByTestId("tb-create-run") as HTMLButtonElement).disabled).toBe(false));

    fireEvent.click(screen.getByTestId("tb-create-run"));
    await waitFor(() => expect(clientMocks.createTerminalBenchRun).toHaveBeenCalledTimes(1));
    expect(clientMocks.createTerminalBenchRun.mock.calls[0][0]).toMatchObject({
      agent_id: "oracle",
      agent_version: "1.0.0",
      n_trials: 3,
      aggregation: "first-trial",
    });
    expect(clientMocks.getTerminalBenchPreflight).toHaveBeenLastCalledWith(
      expect.objectContaining({ n_trials: 3 }),
    );
  });

  it("任务清单页：身份、文件数、tests/solution 与许可证说明", async () => {
    render(wrap(<TerminalBenchTasks />, "/terminal-bench/tasks"));
    await waitFor(() => expect(screen.getByTestId("tb-task-task-a")).toBeTruthy());
    const row = screen.getByTestId("tb-task-task-a");
    expect(row.textContent).toContain("tasks/task-a");
    expect(row.textContent).toContain("4");
    expect(row.textContent).toContain("有");
    expect(row.textContent).toContain("MIT");
    expect(screen.getByTestId("tb-task-task-b").textContent).toContain("未声明");
    expect(screen.getByTestId("tb-license-note").textContent).toContain("未声明 1 个");
    expect(screen.getByTestId("tb-tasks-meta").textContent).toContain("tb-rev-1");

    fireEvent.change(screen.getByLabelText("任务搜索"), { target: { value: "task-b" } });
    expect(screen.queryByTestId("tb-task-task-a")).toBeNull();
    expect(screen.getByTestId("tb-task-task-b")).toBeTruthy();
  });

  it("监控页：queued 运行没有 Trial 时给出显式空态", async () => {
    clientMocks.getRuns.mockResolvedValue({
      items: [{
        id: "run-q", status: "queued", scenario_version: "terminal-bench-harbor@1", manifest: {},
      }],
      total: 1,
    });
    clientMocks.getRunTasks.mockResolvedValue({ run_id: "run-q", items: [], total: 0 });
    render(wrap(<TerminalBenchMonitor />, "/terminal-bench/monitor"));
    await waitFor(() => expect(screen.getByTestId("tb-queued-empty")).toBeTruthy());
    // 状态标签来自 STATUS_META，不在页面里手写
    expect(screen.getByText("排队中")).toBeTruthy();
    expect(screen.getByTestId("tb-queued-empty").textContent).toContain("还没有 Trial");
    expect(screen.getByTestId("tb-queued-empty").textContent).toContain("等待 Worker");
    expect(screen.getByTestId("tb-runner-status").textContent).toContain("Runner 已连接");
  });

  it("监控页：Job → Task → Trial 树按需展开，未产出的 Trial 不隐藏", async () => {
    const trials = [
      ...TRIAL_ROWS,
      {
        trial_id: "trial-d", repeat_index: 3, disposition: "not_attempted",
        verifier_status: "missing_verifier_evidence", reward: null, valid: false,
        coverage: { missing: ["reward"], partial: [] }, source_trial_id: null,
      },
    ];
    clientMocks.getRuns.mockResolvedValue({
      items: [{ id: "run-tb-1", status: "running", scenario_version: "terminal-bench-harbor@1", manifest: { model: "provider/model-a" } }],
      total: 1,
    });
    clientMocks.getRunTasks.mockResolvedValue({
      run_id: "run-tb-1",
      items: [{ ...TASK_ROW, planned_trials: 4, observed_trials: 3, valid_trials: 2, invalid_trials: 1 }],
      total: 1,
    });
    clientMocks.getRunTaskTrials.mockResolvedValue({
      run_id: "run-tb-1", task_key: "task-a", items: trials, total: 4,
    });
    render(wrap(<TerminalBenchMonitor />, "/terminal-bench/monitor"));
    await waitFor(() => expect(screen.getByTestId("tb-task-progress-task-a")).toBeTruthy());
    expect(screen.getByTestId("tb-task-progress-task-a").textContent).toContain("计划 4");
    expect(screen.getByTestId("tb-task-progress-task-a").textContent).toContain("无效 1");
    expect(screen.getByText(/模型|provider\/model-a/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /展开任务/ }));
    await waitFor(() => expect(screen.getByTestId("tb-trial-node-trial-a")).toBeTruthy());
    const notAttempted = screen.getByTestId("tb-trial-node-trial-d");
    expect(notAttempted.textContent).toContain("未尝试");
    expect(notAttempted.textContent).toContain("缺评分证据");
    expect(notAttempted.textContent).toContain("未知");
  });

  it("结果页：切换 Trial 重置工件视图，晚到的旧 Trial 响应不覆盖新选择", async () => {
    const slowB = deferred<TerminalBenchTrialDetail>();
    clientMocks.getRunTrial.mockImplementation((_runId: string, trialId: string) => {
      if (trialId === "trial-b") return slowB.promise;
      if (trialId === "trial-c") {
        return Promise.resolve(makeDetail("trial-c", {
          verifier_observation: { status: "scored", rewards: { reward: 1 }, error: null, evidence_refs: [] },
          artifacts: [{
            artifact_id: "artifact-c", kind: "harbor-artifact", sha256: "sha256:ccc",
            size_bytes: 10, complete: true, truncated: false, note: null,
          }],
        }));
      }
      return Promise.resolve(makeDetail("trial-a"));
    });
    render(wrap(<TerminalBenchResult />, "/terminal-bench/runs/run-tb-1/result"));
    await waitFor(() => expect(screen.getByTestId("tb-artifact-artifact-a")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "查看" }));
    await waitFor(() => expect(screen.getByTestId("tb-artifact-detail").textContent).toContain("sha256:aaa"));

    fireEvent.click(screen.getAllByRole("button", { name: "钻取" })[1]);
    fireEvent.click(screen.getAllByRole("button", { name: "钻取" })[2]);
    await waitFor(() => expect(screen.getByTestId("tb-artifact-artifact-c")).toBeTruthy());
    // 工件视图已随 Trial 切换重置：旧 Trial 的工件与展开项都不再出现
    expect(screen.queryByTestId("tb-artifact-detail")).toBeNull();
    expect(screen.queryByTestId("tb-artifact-artifact-a")).toBeNull();

    // trial-b 的响应此刻才回来：不得覆盖已选中的 trial-c
    await act(async () => {
      slowB.resolve(makeDetail("trial-b", {
        verifier_observation: { status: "scored", rewards: { reward: 1 }, error: null, evidence_refs: [] },
        artifacts: [{
          artifact_id: "artifact-b", kind: "harbor-artifact", sha256: "sha256:bbb",
          size_bytes: 1, complete: true, truncated: false, note: null,
        }],
      }));
    });
    expect(screen.getByTestId("tb-drill-trial-id").textContent).toBe("trial-c");
    expect(screen.getByTestId("tb-artifact-artifact-c")).toBeTruthy();
    expect(screen.queryByTestId("tb-artifact-artifact-b")).toBeNull();
  });

  it("结果页：Verifier 协议错误、缺失 reward 与 reward=0 明确区分", async () => {
    clientMocks.getRunTaskTrials.mockResolvedValue({
      run_id: "run-tb-1",
      task_key: "task-a",
      items: [
        { ...TRIAL_ROWS[0], trial_id: "trial-a", disposition: "failed", verifier_status: "scored", reward: 0, valid: true },
        { ...TRIAL_ROWS[1], trial_id: "trial-b", disposition: "not_attempted", verifier_status: "missing_verifier_evidence", reward: null, valid: false },
        { ...TRIAL_ROWS[2], trial_id: "trial-c", disposition: "indeterminate", verifier_status: "verifier_protocol_error", reward: null, valid: false },
        // 覆盖登记整体缺失：显示未知，不是「缺失 0 项」
        { ...TRIAL_ROWS[2], trial_id: "trial-d", verifier_status: "missing_verifier_evidence", reward: null, valid: false, coverage: {} },
      ],
      total: 4,
    });
    clientMocks.getRunTrial.mockImplementation((_runId: string, trialId: string) => {
      if (trialId === "trial-c") {
        return Promise.resolve(makeDetail("trial-c", {
          disposition: "indeterminate",
          verifier_observation: {
            status: "verifier_protocol_error",
            rewards: {},
            error: { code: "VERIFIER_PROTOCOL_INVALID", message: "reward payload is not valid JSON" },
            evidence_refs: [],
          },
          coverage: { items: { reward: "unavailable", terminal: "unavailable" }, missing: ["reward", "terminal"], partial: [] },
          terminal_text: null,
          evidence_complete: false,
        }));
      }
      return Promise.resolve(makeDetail(trialId));
    });
    render(wrap(<TerminalBenchResult />, "/terminal-bench/runs/run-tb-1/result"));
    await waitFor(() => expect(screen.getByTestId("tb-reward-trial-c")).toBeTruthy());

    // reward=0 是有效失败，必须显示 0；缺失 reward 显示未知，两者不同
    expect(screen.getByTestId("tb-reward-trial-a").textContent).toBe("0");
    expect(screen.getByTestId("tb-reward-trial-b").textContent).toBe("未知");
    expect(screen.getByTestId("tb-reward-trial-c").textContent).toBe("未知");
    expect(screen.getByTestId("tb-trial-trial-a").textContent).toContain("已评分");
    expect(screen.getByTestId("tb-trial-trial-b").textContent).toContain("缺评分证据");
    expect(screen.getByTestId("tb-trial-trial-c").textContent).toContain("Verifier 协议错误");
    // 覆盖登记缺失 → 未知；登记为空集合 → 0（两者不同）
    expect(screen.getByTestId("tb-coverage-trial-a").textContent).toBe("0");
    expect(screen.getByTestId("tb-coverage-trial-d").textContent).toBe("未知");

    // 协议错误：错误码可见，判定是「无质量判定」，不是 0 分
    fireEvent.click(screen.getAllByRole("button", { name: "钻取" })[2]);
    await waitFor(() => expect(screen.getByTestId("tb-drill-verifier-error").textContent).toContain("VERIFIER_PROTOCOL_INVALID"));
    expect(screen.getByTestId("tb-drill-verifier-error").textContent).toContain("not valid JSON");
    expect(screen.getByTestId("tb-drill-reward").textContent).toContain("未知");
    expect(screen.getByTestId("tb-drill-reward").textContent).toContain("不等于 0");
    expect(screen.getByTestId("tb-drill-verdict").textContent).toContain("无质量判定");
    expect(screen.getByTestId("tb-coverage-missing").textContent).toContain("Reward");

    // reward=0：明确表述为有效失败（不是缺证据、也不是平台错误）
    fireEvent.click(screen.getAllByRole("button", { name: "钻取" })[0]);
    await waitFor(() => expect(screen.getByTestId("tb-drill-verdict").textContent).toContain("有效失败"));
    expect(screen.getByTestId("tb-drill-reward").textContent).toBe("reward=0");
    expect(screen.getByTestId("tb-drill-verdict").textContent).toContain("reward=0");
  });

  it("结果页：截断的终端文本被标注为截断，完整文本不标注", async () => {
    clientMocks.getRunTrial.mockImplementation((_runId: string, trialId: string) => (
      trialId === "trial-a"
        ? Promise.resolve(makeDetail("trial-a", {
          terminal_text: "line-1\nline-2 截断处",
          terminal_truncated: true,
          coverage: {
            items: { terminal: "truncated", trajectory: "partial" },
            missing: [],
            partial: ["terminal", "trajectory"],
          },
        }))
        : Promise.resolve(makeDetail(trialId, { terminal_text: "complete log\n", terminal_truncated: false }))
    ));
    render(wrap(<TerminalBenchResult />, "/terminal-bench/runs/run-tb-1/result"));
    await waitFor(() => expect(screen.getByTestId("tb-terminal-truncated")).toBeTruthy());
    expect(screen.getByTestId("tb-terminal-truncated").textContent).toContain("已截断");
    expect(screen.getByTestId("tb-terminal-log").textContent).toContain("line-2 截断处");
    expect(screen.getByTestId("tb-coverage").textContent).toContain("已截断");
    expect(screen.getByTestId("tb-trajectory").textContent).toContain("部分");

    fireEvent.click(screen.getAllByRole("button", { name: "钻取" })[1]);
    await waitFor(() => expect(screen.getByTestId("tb-terminal-log").textContent).toContain("complete log"));
    expect(screen.queryByTestId("tb-terminal-truncated")).toBeNull();
  });

  it("结果页：未知模型身份与未知成本显示为未知，绝不填 0", async () => {
    clientMocks.getRun.mockResolvedValue({
      id: "run-tb-1",
      status: "completed",
      scenario_version: "terminal-bench-harbor@1",
      manifest: { benchmark_provenance: { aggregation: "first-trial" } },
    });
    clientMocks.getRunTrial.mockImplementation((_runId: string, trialId: string) => Promise.resolve(
      makeDetail(trialId, { usage: { cost_usd: null, tokens: null, coverage: "unavailable" } }),
    ));
    render(wrap(<TerminalBenchResult />, "/terminal-bench/runs/run-tb-1/result"));
    await waitFor(() => expect(screen.getByTestId("tb-model")).toBeTruthy());
    expect(screen.getByTestId("tb-model").textContent).toContain("未知");

    await waitFor(() => expect(screen.getByTestId("tb-cost-note").textContent).toContain("成本未知 3 个"));
    const knownCost = screen.getByText("已知成本（USD）").closest(".metric-card") as HTMLElement;
    expect(knownCost.textContent).toContain("未知");
    expect(knownCost.textContent).not.toContain("$0");
    const perSuccess = screen.getByText("每成功成本（USD）").closest(".metric-card") as HTMLElement;
    expect(perSuccess.textContent).toContain("未知");
    expect(perSuccess.textContent).not.toContain("$0");

    fireEvent.click(screen.getAllByRole("button", { name: "钻取" })[0]);
    await waitFor(() => expect(screen.getByTestId("tb-usage-coverage").textContent).toContain("未观测"));
    expect(screen.getByTestId("tb-usage-coverage").textContent).toContain("成本 未知");
    expect(screen.getByTestId("tb-usage-coverage").textContent).not.toContain("$0.000000");
  });

  it("结果页：没有成功 Trial 时每成功成本显示不适用（分母为零不编数）", async () => {
    clientMocks.getRunTaskTrials.mockResolvedValue({
      run_id: "run-tb-1",
      task_key: "task-a",
      items: [
        { ...TRIAL_ROWS[0], trial_id: "trial-a", reward: 0, valid: true },
        { ...TRIAL_ROWS[1], trial_id: "trial-b", reward: 0, valid: true },
      ],
      total: 2,
    });
    clientMocks.getRunTrial.mockImplementation((_runId: string, trialId: string) => Promise.resolve(
      makeDetail(trialId, {
        verifier_observation: { status: "scored", rewards: { reward: 0 }, error: null, evidence_refs: [] },
        usage: { cost_usd: 0.5, tokens: null, coverage: "observed" },
      }),
    ));
    render(wrap(<TerminalBenchResult />, "/terminal-bench/runs/run-tb-1/result"));
    await waitFor(() => expect(screen.getByTestId("tb-cost-note").textContent).toContain("成本已知的 Trial 2 个"));
    const perSuccess = screen.getByText("每成功成本（USD）").closest(".metric-card") as HTMLElement;
    expect(perSuccess.textContent).toContain("不适用");
    const knownCost = screen.getByText("已知成本（USD）").closest(".metric-card") as HTMLElement;
    expect(knownCost.textContent).toContain("$1.000000");
  });

  it("结果页：深链落到指定 Trial；切 Task 清空旧 Task 的钻取", async () => {
    clientMocks.getRunTasks.mockResolvedValue({
      run_id: "run-tb-1",
      items: [
        TASK_ROW,
        { ...TASK_ROW, task_key: "task-b", normalized_relative_path: "tasks/task-b", aggregate: undefined },
      ],
      total: 2,
    });
    clientMocks.getRunTaskTrials.mockImplementation((_runId: string, taskKey: string) => Promise.resolve({
      run_id: "run-tb-1",
      task_key: taskKey,
      items: taskKey === "task-b"
        ? [{ ...TRIAL_ROWS[1], trial_id: "trial-z", disposition: "succeeded", reward: 1, valid: true }]
        : TRIAL_ROWS,
      total: 1,
    }));
    render(wrap(<TerminalBenchResult />, "/terminal-bench/runs/run-tb-1/result?task=task-a&trial=trial-c"));
    // 深链（监控页「钻取」按钮）直接落到指定 Trial
    await waitFor(() => expect(screen.getByTestId("tb-drill-trial-id").textContent).toBe("trial-c"));
    expect((screen.getByLabelText("Task") as HTMLSelectElement).value).toBe("task-a");

    fireEvent.change(screen.getByLabelText("Task"), { target: { value: "task-b" } });
    await waitFor(() => expect(screen.getByTestId("tb-drill-trial-id").textContent).toBe("trial-z"));
    expect(screen.queryByTestId("tb-trial-trial-c")).toBeNull();
  });

  it("比较页：数据不足时只给原因，不给放行结论", async () => {
    clientMocks.compareRuns.mockResolvedValue({
      eligible: false,
      reasons: ["TASK_SET_CHANGED:task sets differ"],
      metric_eligibility: { valid_trial_pass_rate: false },
      case_diff: { added: ["task-x"], removed: [], changed: [] },
    });
    render(wrap(<TerminalBenchCompare />, "/terminal-bench/compare?runs=run-a,run-b"));
    fireEvent.click(screen.getByRole("button", { name: "比较" }));
    await waitFor(() => expect(screen.getByTestId("tb-compare-insufficient")).toBeTruthy());
    expect(screen.getByTestId("tb-compare-insufficient").textContent).toContain("TASK_SET_CHANGED");
    expect(screen.getByTestId("tb-case-diff").textContent).toContain("+1");
    expect(screen.queryByTestId("tb-gate")).toBeNull();
    expect(clientMocks.evaluateRunGate).not.toHaveBeenCalled();
  });

  it("比较页：可比时逐条列出 API 返回的固定比较条件", async () => {
    clientMocks.compareRuns.mockResolvedValue({
      eligible: true,
      reasons: [],
      metric_eligibility: { valid_trial_pass_rate: true },
      case_diff: { added: [], removed: [], changed: [] },
    });
    clientMocks.evaluateRunGate.mockResolvedValue({
      schema: "gate-lite@2",
      passed: false,
      rules: [
        { id: "coverage", passed: false, reason: "valid_trial_coverage=0.75<1.0" },
        { id: "comparable", passed: true, reason: "same task set and environment digest" },
      ],
    });
    render(wrap(<TerminalBenchCompare />, "/terminal-bench/compare?runs=run-a,run-b"));
    fireEvent.click(screen.getByRole("button", { name: "比较" }));
    await waitFor(() => expect(screen.getByTestId("tb-gate")).toBeTruthy());
    expect(screen.getByTestId("tb-comparison").textContent).toContain("两份报告可比");
    expect(screen.getByTestId("tb-gate").textContent).toContain("coverage");
    expect(screen.getByTestId("tb-gate").textContent).toContain("valid_trial_coverage=0.75<1.0");
    expect(screen.getByTestId("tb-gate").textContent).toContain("comparable");
    expect(screen.getByTestId("tb-gate-blocked")).toBeTruthy();
    expect(screen.queryByTestId("tb-gate-pass")).toBeNull();
    expect(clientMocks.evaluateRunGate.mock.calls[0][0].policy).toMatchObject({
      metric: "valid_trial_pass_rate",
      required_coverage: 1.0,
    });
  });
});
