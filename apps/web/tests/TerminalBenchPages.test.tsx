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
import {
  ApiRequestError,
  type TerminalBenchArtifactContent,
  type TerminalBenchTrialDetail,
} from "../src/api/client";

const clientMocks = vi.hoisted(() => ({
  getTerminalBenchOverview: vi.fn(),
  getTerminalBenchTasks: vi.fn(),
  getTerminalBenchPreflight: vi.fn(),
  createTerminalBenchRun: vi.fn(),
  getRunTasks: vi.fn(),
  getRunTaskTrials: vi.fn(),
  getRunTrial: vi.fn(),
  getRunTrialArtifact: vi.fn(),
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

/** Run 级聚合（GET /runs/{id}/tasks 每行都带同一份；含 R15 的成本字段）。 */
function runCost(overrides: Record<string, any> = {}) {
  return {
    known_cost_usd: 3.0,
    known_trials: 4,
    unknown_trials: 0,
    unknown_cost: false,
    currency: "USD",
    price_table_version: null,
    metering_source: "harbor-agent-result",
    per_success_usd: 1.5,
    per_success_usd_basis: "complete",
    known_cost_subtotal_per_success_usd: 1.5,
    successes: 2,
    includes_failed_trials_in_numerator: true,
    note: "total covers every trial that reported cost, including failed trials",
    ...overrides,
  };
}

const RUN_AGGREGATE = {
  aggregation: "first-trial",
  unit: "trial",
  denominator: "planned_trials",
  selected_tasks: 2,
  scored_tasks: 2,
  passed_tasks: 1,
  selected_trials: 4,
  observed_trials: 4,
  valid_trials: 3,
  valid_pass_trials: 2,
  valid_fail_trials: 1,
  invalid_trials: 1,
  task_pass_rate: 0.5,
  valid_trial_pass_rate: 0.666667,
  valid_trial_coverage: 0.75,
  observed_trial_coverage: 1.0,
  trial_completeness: 0.75,
  cost: runCost(),
};

const TASK_ROW = {
  task_key: "task-a",
  display_name: "task-a",
  normalized_relative_path: "tasks/task-a",
  planned_trials: 3,
  observed_trials: 3,
  valid_trials: 2,
  passed_trials: 1,
  failed_trials: 1,
  invalid_trials: 1,
  task_pass: true,
  task_pass_reason: "first-trial",
  valid_trial_pass_rate: 0.5,
  aggregate: RUN_AGGREGATE,
  gate: { allowed: false, reason_codes: [], messages: {} },
};

const TASK_ROW_B = {
  ...TASK_ROW,
  task_key: "task-b",
  display_name: "task-b",
  normalized_relative_path: "tasks/task-b",
  planned_trials: 1,
  observed_trials: 1,
  valid_trials: 1,
  passed_trials: 1,
  failed_trials: 0,
  invalid_trials: 0,
  valid_trial_pass_rate: 1.0,
};

const TRIAL_ROWS = [
  {
    trial_id: "trial-a", repeat_index: 0, disposition: "failed", verifier_status: "scored",
    reward: 0, valid: true, coverage: { missing: [], partial: [] }, source_trial_id: "src-a",
    planned: true, status: "completed", seed: "seed-a",
  },
  {
    trial_id: "trial-b", repeat_index: 1, disposition: "succeeded", verifier_status: "scored",
    reward: 1, valid: true, coverage: { missing: ["trajectory"], partial: [] }, source_trial_id: "src-b",
    planned: true, status: "completed", seed: "seed-b",
  },
  {
    trial_id: "trial-c", repeat_index: 2, disposition: "indeterminate", verifier_status: "verifier_protocol_error",
    reward: null, valid: false, coverage: { missing: ["reward"], partial: [] }, source_trial_id: null,
    planned: true, status: "completed", seed: "seed-c",
  },
];

const TERMINAL_ID = "harbor/trials/task-a-0/trial.log";
const RESULT_ID = "harbor/trials/task-a-0/result.json";

/** 终端/工件内容对象：字段来自真实 DTO（GET .../trials/{id} 的 terminal 与 artifacts/{id}）。 */
function artifactContent(
  overrides: Partial<TerminalBenchArtifactContent> = {},
): TerminalBenchArtifactContent {
  return {
    artifact_id: TERMINAL_ID,
    kind: "harbor-trial-log",
    sha256: "sha256:terminal",
    size_bytes: 128,
    media_type: "text/plain",
    source_path: TERMINAL_ID,
    encoding: "utf-8",
    text: "$ solved task-a\n",
    truncated: false,
    verified: true,
    note: null,
    ...overrides,
  };
}

/** 证据引用：字段与 parser 产出的真实 artifact_refs 一致（无 media_type）。 */
function artifactRef(artifactId: string, overrides: Record<string, any> = {}) {
  return {
    artifact_id: artifactId,
    kind: artifactId.endsWith("trial.log") ? "harbor-trial-log" : "harbor-trial-result",
    sha256: artifactId.endsWith("trial.log") ? "sha256:terminal" : "sha256:aaa",
    size_bytes: 2048,
    source_path: artifactId,
    complete: true,
    truncated: false,
    note: null,
    ...overrides,
  };
}

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
      evidence_refs: [{ artifact_id: "harbor/trials/task-a-0/result.json" }],
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
    artifacts: [artifactRef(RESULT_ID), artifactRef(TERMINAL_ID)],
    terminal_ref: artifactRef(TERMINAL_ID),
    terminal: artifactContent(),
    evidence_complete: true,
    source_hash: "sha256:source",
    parser_version: "harbor-terminal-bench-parser@1",
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
  clientMocks.getRunTrialArtifact.mockImplementation(
    (_runId: string, _trialId: string, artifactId: string) => Promise.resolve(artifactContent({
      artifact_id: artifactId,
      kind: artifactId.endsWith("trial.log") ? "harbor-trial-log" : "harbor-trial-result",
      sha256: artifactId.endsWith("trial.log") ? "sha256:terminal" : "sha256:aaa",
      text: `content of ${artifactId}\n`,
    })),
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
    // 预检与创建走同一组身份参数（review R20：预检放行才算数）
    expect(clientMocks.getTerminalBenchPreflight).toHaveBeenLastCalledWith(
      expect.objectContaining({ agent_id: "oracle", agent_version: "1.0.0", dataset_revision: "tb-rev-1" }),
    );

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

  it("R16：创建请求只发公共 DTO 字段（timeouts/resources），不再发被静默忽略的 timeout_sec", async () => {
    clientMocks.getTerminalBenchPreflight.mockResolvedValue({
      ok: true, reasons: [], messages: {}, checks: {},
      profile_fingerprint: "sha256:profile-3", platform_custom_profile: false,
    });
    render(wrap(<TerminalBenchOperate />));
    await waitFor(() => expect(screen.getByTestId("tb-dataset-revision").textContent).toContain("tb-rev-1"));

    fireEvent.change(screen.getByLabelText("模型档案"), { target: { value: "provider/model-a" } });
    fireEvent.change(screen.getByLabelText("重复次数"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("Agent 超时"), { target: { value: "7" } });
    fireEvent.change(screen.getByLabelText("Verifier 超时"), { target: { value: "13" } });
    fireEvent.change(screen.getByLabelText("Agent 准备超时"), { target: { value: "11" } });
    fireEvent.change(screen.getByLabelText("Job 超时"), { target: { value: "600" } });
    fireEvent.change(screen.getByLabelText("CPU 数"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("内存（MB）"), { target: { value: "1024" } });
    fireEvent.change(screen.getByLabelText("存储（MB）"), { target: { value: "2048" } });
    fireEvent.change(screen.getByLabelText("GPU 数"), { target: { value: "1" } });

    fireEvent.click(screen.getByRole("button", { name: /预检/ }));
    await waitFor(() => expect((screen.getByTestId("tb-create-run") as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByTestId("tb-create-run"));
    await waitFor(() => expect(clientMocks.createTerminalBenchRun).toHaveBeenCalledTimes(1));

    // 逐字断言：字段名与 API 的公共 DTO 完全一致，没有任何多余字段。
    expect(clientMocks.createTerminalBenchRun.mock.calls[0][0]).toEqual({
      model: "provider/model-a",
      agent_id: "oracle",
      agent_version: "1.0.0",
      n_trials: 2,
      dataset_revision: "tb-rev-1",
      aggregation: "first-trial",
      timeouts: { agent_sec: 7, verifier_sec: 13, agent_setup_sec: 11, job_sec: 600 },
      resources: { cpus: 2, memory_mb: 1024, storage_mb: 2048, gpus: 1 },
    });
    // 表单不提供 environment_build_sec（平台无法强制，API 会 422 拒绝）
    const body = clientMocks.createTerminalBenchRun.mock.calls[0][0];
    expect(body.timeouts).not.toHaveProperty("environment_build_sec");
    expect(body).not.toHaveProperty("timeout_sec");
    expect(body).not.toHaveProperty("job_timeout_sec");
  });

  it("R16：oracle 可省略模型；非整数 n_trials 在本地被拦下，不发无效请求", async () => {
    clientMocks.getTerminalBenchPreflight.mockResolvedValue({
      ok: true, reasons: [], messages: {}, checks: {},
      profile_fingerprint: "sha256:profile-4", platform_custom_profile: false,
    });
    render(wrap(<TerminalBenchOperate />));
    await waitFor(() => expect(screen.getByTestId("tb-dataset-revision").textContent).toContain("tb-rev-1"));

    fireEvent.change(screen.getByLabelText("重复次数"), { target: { value: "oops" } });
    // 非法输入在本地就暴露（不靠一次 422 才发现），预检与创建都 fail-closed
    expect((screen.getByTestId("tb-create-run") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("tb-input-error").textContent).toContain("1–32 的整数");
    expect((screen.getByRole("button", { name: /预检/ }) as HTMLButtonElement).disabled).toBe(true);
    expect(clientMocks.getTerminalBenchPreflight).not.toHaveBeenCalled();

    // 上限与 API 一致（1..32），不是旧页面的 1..10
    fireEvent.change(screen.getByLabelText("重复次数"), { target: { value: "33" } });
    expect((screen.getByTestId("tb-create-run") as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("重复次数"), { target: { value: "32" } });
    fireEvent.click(screen.getByRole("button", { name: /预检/ }));
    await waitFor(() => expect((screen.getByTestId("tb-create-run") as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByTestId("tb-create-run"));
    await waitFor(() => expect(clientMocks.createTerminalBenchRun).toHaveBeenCalledTimes(1));
    const body = clientMocks.createTerminalBenchRun.mock.calls[0][0];
    expect(body.n_trials).toBe(32);
    // oracle 的模型可以为空：不发明一个模型 id
    expect(body).not.toHaveProperty("model");
  });

  it("R16：API 结构化拒绝原样显示错误码、消息与允许字段", async () => {
    clientMocks.getTerminalBenchPreflight.mockResolvedValue({
      ok: true, reasons: [], messages: {}, checks: {},
      profile_fingerprint: "sha256:profile-5", platform_custom_profile: false,
    });
    clientMocks.createTerminalBenchRun.mockRejectedValue(new ApiRequestError(422, {
      code: "REQUEST_FIELD_UNKNOWN",
      message: "unknown request field(s): timeout_sec",
      details: { allowed: ["agent_id", "aggregation", "n_trials", "timeouts"] },
    }));
    render(wrap(<TerminalBenchOperate />));
    await waitFor(() => expect(screen.getByTestId("tb-dataset-revision").textContent).toContain("tb-rev-1"));
    fireEvent.click(screen.getByRole("button", { name: /预检/ }));
    await waitFor(() => expect((screen.getByTestId("tb-create-run") as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByTestId("tb-create-run"));

    await waitFor(() => expect(screen.getByTestId("tb-run-error")).toBeTruthy());
    const error = screen.getByTestId("tb-run-error");
    expect(error.textContent).toContain("REQUEST_FIELD_UNKNOWN");
    expect(error.textContent).toContain("unknown request field(s): timeout_sec");
    expect(screen.getByTestId("tb-run-error-allowed").textContent).toContain("timeouts");
  });

  it("操作页：预检拒绝时同样显示服务端错误码（不吞掉结构）", async () => {
    clientMocks.getTerminalBenchPreflight.mockRejectedValue(new ApiRequestError(422, {
      code: "MODEL_NOT_PUBLISHED",
      message: "Terminal-Bench runs require a published model profile",
    }));
    render(wrap(<TerminalBenchOperate />));
    await waitFor(() => expect(screen.getByTestId("tb-dataset-revision").textContent).toContain("tb-rev-1"));
    fireEvent.click(screen.getByRole("button", { name: /预检/ }));
    await waitFor(() => expect(screen.getByTestId("tb-preflight-error")).toBeTruthy());
    expect(screen.getByTestId("tb-preflight-error").textContent).toContain("MODEL_NOT_PUBLISHED");
    expect(screen.getByTestId("tb-preflight-error").textContent)
      .toContain("require a published model profile");
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
          artifacts: [artifactRef("harbor/trials/task-a-2/result.json", { sha256: "sha256:ccc", size_bytes: 10 })],
          terminal_ref: null,
          terminal: null,
        }));
      }
      return Promise.resolve(makeDetail("trial-a"));
    });
    render(wrap(<TerminalBenchResult />, "/terminal-bench/runs/run-tb-1/result"));
    await waitFor(() => expect(screen.getByTestId(`tb-artifact-${RESULT_ID}`)).toBeTruthy());
    fireEvent.click(screen.getAllByRole("button", { name: "查看" })[0]);
    await waitFor(() => expect(screen.getByTestId("tb-artifact-detail").textContent).toContain(RESULT_ID));

    fireEvent.click(screen.getAllByRole("button", { name: "钻取" })[1]);
    fireEvent.click(screen.getAllByRole("button", { name: "钻取" })[2]);
    await waitFor(() => expect(screen.getByTestId("tb-artifact-harbor/trials/task-a-2/result.json")).toBeTruthy());
    // 工件视图已随 Trial 切换重置：旧 Trial 的展开项不再出现
    expect(screen.queryByTestId("tb-artifact-detail")).toBeNull();
    expect(screen.queryByTestId(`tb-artifact-${RESULT_ID}`)).toBeNull();

    // trial-b 的响应此刻才回来：不得覆盖已选中的 trial-c
    await act(async () => {
      slowB.resolve(makeDetail("trial-b", {
        verifier_observation: { status: "scored", rewards: { reward: 1 }, error: null, evidence_refs: [] },
        artifacts: [artifactRef("harbor/trials/task-a-1/result.json", { sha256: "sha256:bbb", size_bytes: 1 })],
      }));
    });
    expect(screen.getByTestId("tb-drill-trial-id").textContent).toBe("trial-c");
    expect(screen.queryByTestId("tb-artifact-harbor/trials/task-a-1/result.json")).toBeNull();
  });

  it("R19：Terminal 按真实 DTO 渲染（terminal 内容 + 工件引用元数据）", async () => {
    render(wrap(<TerminalBenchResult />, "/terminal-bench/runs/run-tb-1/result"));
    await waitFor(() => expect(screen.getByTestId("tb-terminal-log")).toBeTruthy());
    // 真实 API 只给 terminal_ref + terminal 内容对象；页面不得依赖不存在的 terminal_text
    expect(screen.getByTestId("tb-terminal-log").textContent).toContain("$ solved task-a");
    const meta = screen.getByTestId("tb-terminal-meta");
    expect(meta.textContent).toContain(TERMINAL_ID);
    expect(meta.textContent).toContain("sha256:terminal");
    expect(meta.textContent).toContain("utf-8");
    expect(meta.textContent).toContain("已校验");
    expect(screen.queryByTestId("tb-terminal-truncated")).toBeNull();
    expect(screen.queryByTestId("tb-terminal-unreadable")).toBeNull();
  });

  it("R19：终端文本截断时给出截断提示，不推断被截去的部分", async () => {
    clientMocks.getRunTrial.mockImplementation((_runId: string, trialId: string) => (
      trialId === "trial-a"
        ? Promise.resolve(makeDetail("trial-a", {
          terminal: artifactContent({
            text: "line-1\nline-2 截断处",
            truncated: true,
            size_bytes: 70000,
          }),
          terminal_ref: artifactRef(TERMINAL_ID, { truncated: true, complete: false }),
          coverage: {
            items: { terminal: "truncated", trajectory: "partial" },
            missing: [],
            partial: ["terminal", "trajectory"],
          },
        }))
        : Promise.resolve(makeDetail(trialId, {
          terminal: artifactContent({ text: "complete log\n" }),
        }))
    ));
    render(wrap(<TerminalBenchResult />, "/terminal-bench/runs/run-tb-1/result"));
    await waitFor(() => expect(screen.getByTestId("tb-terminal-truncated")).toBeTruthy());
    expect(screen.getByTestId("tb-terminal-truncated").textContent).toContain("已截断");
    expect(screen.getByTestId("tb-terminal-log").textContent).toContain("line-2 截断处");
    expect(screen.getByTestId("tb-terminal-meta").textContent).toContain("68.4 KiB");
    expect(screen.getByTestId("tb-coverage").textContent).toContain("已截断");
    expect(screen.getByTestId("tb-trajectory").textContent).toContain("部分");

    fireEvent.click(screen.getAllByRole("button", { name: "钻取" })[1]);
    await waitFor(() => expect(screen.getByTestId("tb-terminal-log").textContent).toContain("complete log"));
    expect(screen.queryByTestId("tb-terminal-truncated")).toBeNull();
  });

  it("R19：verified=false 的终端内容只显示不可读原因，不显示假日志", async () => {
    clientMocks.getRunTrial.mockImplementation((_runId: string, trialId: string) => Promise.resolve(
      makeDetail(trialId, {
        terminal: artifactContent({
          text: null,
          verified: false,
          note: "artifact content hash differs from the frozen reference",
        }),
      }),
    ));
    render(wrap(<TerminalBenchResult />, "/terminal-bench/runs/run-tb-1/result"));
    await waitFor(() => expect(screen.getByTestId("tb-terminal-unreadable")).toBeTruthy());
    expect(screen.getByTestId("tb-terminal-unreadable").textContent)
      .toContain("artifact content hash differs from the frozen reference");
    expect(screen.queryByTestId("tb-terminal-log")).toBeNull();
    // 引用与 hash 仍然可见（身份不被吞掉，方便人工按引用去冻结证据里核对）
    expect(screen.getByTestId("tb-terminal-meta").textContent).toContain(TERMINAL_ID);
    expect(screen.getByTestId("tb-terminal-meta").textContent).toContain("未校验");
  });

  it("R19：二进制终端内容不渲染文本，只说明需要下载原始工件", async () => {
    clientMocks.getRunTrial.mockImplementation((_runId: string, trialId: string) => Promise.resolve(
      makeDetail(trialId, {
        terminal: artifactContent({
          text: null,
          encoding: "binary",
          verified: true,
          note: "content is not UTF-8 text; download the frozen artifact for the raw bytes",
        }),
      }),
    ));
    render(wrap(<TerminalBenchResult />, "/terminal-bench/runs/run-tb-1/result"));
    await waitFor(() => expect(screen.getByTestId("tb-terminal-binary")).toBeTruthy());
    expect(screen.getByTestId("tb-terminal-binary").textContent).toContain("binary");
    expect(screen.queryByTestId("tb-terminal-log")).toBeNull();
  });

  it("R19：工件内容按引用读取；同 Trial 内切换工件时晚到的旧内容不覆盖", async () => {
    const slowResult = deferred<TerminalBenchArtifactContent>();
    clientMocks.getRunTrialArtifact.mockImplementation(
      (_runId: string, _trialId: string, artifactId: string) => (
        artifactId === RESULT_ID
          ? slowResult.promise
          : Promise.resolve(artifactContent({ artifact_id: artifactId, text: `content of ${artifactId}\n` }))
      ),
    );
    render(wrap(<TerminalBenchResult />, "/terminal-bench/runs/run-tb-1/result"));
    await waitFor(() => expect(screen.getByTestId(`tb-artifact-${RESULT_ID}`)).toBeTruthy());

    // 先点开 result.json（慢），再点开 trial.log（快）
    fireEvent.click(screen.getAllByRole("button", { name: "查看" })[0]);
    fireEvent.click(screen.getAllByRole("button", { name: "查看" })[1]);
    await waitFor(() => expect(screen.getByTestId("tb-artifact-content").textContent).toContain(TERMINAL_ID));

    // result.json 的内容此刻才回来：不得覆盖已打开的 trial.log
    await act(async () => {
      slowResult.resolve(artifactContent({ artifact_id: RESULT_ID, text: "stale result json\n" }));
    });
    expect(screen.getByTestId("tb-artifact-detail").textContent).toContain(TERMINAL_ID);
    expect(screen.getByTestId("tb-artifact-content").textContent).toContain(TERMINAL_ID);
    expect(screen.queryByText(/stale result json/)).toBeNull();
  });

  it("R19：切换 Trial 后晚到的工件内容不出现在新 Trial 下", async () => {
    const slowResult = deferred<TerminalBenchArtifactContent>();
    clientMocks.getRunTrialArtifact.mockImplementation(
      (_runId: string, _trialId: string, artifactId: string) => (
        artifactId === RESULT_ID
          ? slowResult.promise
          : Promise.resolve(artifactContent({ artifact_id: artifactId, text: "trial-c log\n" }))
      ),
    );
    render(wrap(<TerminalBenchResult />, "/terminal-bench/runs/run-tb-1/result"));
    await waitFor(() => expect(screen.getByTestId(`tb-artifact-${RESULT_ID}`)).toBeTruthy());
    fireEvent.click(screen.getAllByRole("button", { name: "查看" })[0]);

    fireEvent.click(screen.getAllByRole("button", { name: "钻取" })[2]);
    await waitFor(() => expect(screen.getByTestId("tb-drill-trial-id").textContent).toBe("trial-c"));

    await act(async () => {
      slowResult.resolve(artifactContent({ artifact_id: RESULT_ID, text: "stale trial-a result\n" }));
    });
    expect(screen.queryByText(/stale trial-a result/)).toBeNull();
    expect(screen.queryByTestId("tb-artifact-content")).toBeNull();
  });

  it("R19：工件内容不可读时如实显示原因，不显示伪造内容", async () => {
    clientMocks.getRunTrialArtifact.mockImplementation(
      (_runId: string, _trialId: string, artifactId: string) => Promise.resolve(artifactContent({
        artifact_id: artifactId,
        text: null,
        verified: false,
        note: "artifact content hash differs from the frozen reference",
      })),
    );
    render(wrap(<TerminalBenchResult />, "/terminal-bench/runs/run-tb-1/result"));
    await waitFor(() => expect(screen.getByTestId(`tb-artifact-${RESULT_ID}`)).toBeTruthy());
    fireEvent.click(screen.getAllByRole("button", { name: "查看" })[0]);
    await waitFor(() => expect(screen.getByTestId("tb-artifact-unreadable")).toBeTruthy());
    expect(screen.getByTestId("tb-artifact-unreadable").textContent)
      .toContain("hash differs from the frozen reference");
    expect(screen.queryByTestId("tb-artifact-content")).toBeNull();
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
          terminal_ref: null,
          terminal: null,
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
    expect(screen.getByTestId("tb-terminal-missing")).toBeTruthy();

    // reward=0：明确表述为有效失败（不是缺证据、也不是平台错误）
    fireEvent.click(screen.getAllByRole("button", { name: "钻取" })[0]);
    await waitFor(() => expect(screen.getByTestId("tb-drill-verdict").textContent).toContain("有效失败"));
    expect(screen.getByTestId("tb-drill-reward").textContent).toBe("reward=0");
    expect(screen.getByTestId("tb-drill-verdict").textContent).toContain("reward=0");
  });

  it("R15：全部成本未知时已知成本与每成功成本都显示未知，绝不填 0", async () => {
    clientMocks.getRun.mockResolvedValue({
      id: "run-tb-1",
      status: "completed",
      scenario_version: "terminal-bench-harbor@1",
      manifest: { benchmark_provenance: { aggregation: "first-trial" } },
    });
    clientMocks.getRunTasks.mockResolvedValue({
      run_id: "run-tb-1",
      items: [{
        ...TASK_ROW,
        aggregate: {
          ...RUN_AGGREGATE,
          cost: runCost({
            known_cost_usd: null, known_trials: 0, unknown_trials: 3, unknown_cost: true,
            currency: null, per_success_usd: null, per_success_usd_basis: "no_known_cost",
            known_cost_subtotal_per_success_usd: null, successes: 1,
          }),
        },
      }],
      total: 1,
    });
    clientMocks.getRunTrial.mockImplementation((_runId: string, trialId: string) => Promise.resolve(
      makeDetail(trialId, { usage: { cost_usd: null, tokens: null, coverage: "unavailable" } }),
    ));
    render(wrap(<TerminalBenchResult />, "/terminal-bench/runs/run-tb-1/result"));
    await waitFor(() => expect(screen.getByTestId("tb-model")).toBeTruthy());
    expect(screen.getByTestId("tb-model").textContent).toContain("未知");

    await waitFor(() => expect(screen.getByTestId("tb-cost-note").textContent).toContain("成本未知 3 个"));
    const knownCost = screen.getByTestId("tb-run-cost-card");
    expect(knownCost.textContent).toContain("未知");
    expect(knownCost.textContent).not.toContain("$0");
    const perSuccess = screen.getByTestId("tb-run-per-success-card");
    expect(perSuccess.textContent).toContain("未知");
    expect(perSuccess.textContent).not.toContain("$0");
    expect(screen.getByTestId("tb-cost-basis").textContent).toContain("no_known_cost");
    expect(screen.queryByTestId("tb-cost-subtotal")).toBeNull();

    fireEvent.click(screen.getAllByRole("button", { name: "钻取" })[0]);
    await waitFor(() => expect(screen.getByTestId("tb-usage-coverage").textContent).toContain("未观测"));
    expect(screen.getByTestId("tb-usage-coverage").textContent).toContain("成本 未知");
    expect(screen.getByTestId("tb-usage-coverage").textContent).not.toContain("$0.000000");
  });

  it("R15：成本不完整时 per_success_usd=null 不渲染为 0，小计必须标注为小计", async () => {
    clientMocks.getRunTasks.mockResolvedValue({
      run_id: "run-tb-1",
      items: [{
        ...TASK_ROW,
        aggregate: {
          ...RUN_AGGREGATE,
          cost: runCost({
            known_cost_usd: 2.0, known_trials: 2, unknown_trials: 1, unknown_cost: true,
            per_success_usd: null, per_success_usd_basis: "unknown_cost",
            known_cost_subtotal_per_success_usd: 1.0, successes: 2,
          }),
        },
      }],
      total: 1,
    });
    render(wrap(<TerminalBenchResult />, "/terminal-bench/runs/run-tb-1/result"));
    await waitFor(() => expect(screen.getByTestId("tb-run-cost-card").textContent).toContain("$2.000000"));
    const perSuccess = screen.getByTestId("tb-run-per-success-card");
    expect(perSuccess.textContent).toContain("未知");
    expect(perSuccess.textContent).not.toContain("$");
    expect(screen.getByTestId("tb-cost-basis").textContent).toContain("unknown_cost");
    const subtotal = screen.getByTestId("tb-cost-subtotal");
    expect(subtotal.textContent).toContain("小计");
    expect(subtotal.textContent).toContain("$1.000000");
  });

  it("R15：没有成功 Trial 时每成功成本显示不适用（分母为零不编数）", async () => {
    clientMocks.getRunTaskTrials.mockResolvedValue({
      run_id: "run-tb-1",
      task_key: "task-a",
      items: [
        { ...TRIAL_ROWS[0], trial_id: "trial-a", reward: 0, valid: true },
        { ...TRIAL_ROWS[1], trial_id: "trial-b", reward: 0, valid: true },
      ],
      total: 2,
    });
    clientMocks.getRunTasks.mockResolvedValue({
      run_id: "run-tb-1",
      items: [{
        ...TASK_ROW,
        aggregate: {
          ...RUN_AGGREGATE,
          cost: runCost({
            known_cost_usd: 1.0, known_trials: 2, unknown_trials: 0, unknown_cost: false,
            per_success_usd: null, per_success_usd_basis: "no_success",
            known_cost_subtotal_per_success_usd: null, successes: 0,
          }),
        },
      }],
      total: 1,
    });
    clientMocks.getRunTrial.mockImplementation((_runId: string, trialId: string) => Promise.resolve(
      makeDetail(trialId, {
        verifier_observation: { status: "scored", rewards: { reward: 0 }, error: null, evidence_refs: [] },
        usage: { cost_usd: 0.5, tokens: null, coverage: "observed" },
      }),
    ));
    render(wrap(<TerminalBenchResult />, "/terminal-bench/runs/run-tb-1/result"));
    await waitFor(() => expect(screen.getByTestId("tb-cost-note").textContent).toContain("成功 Trial 0 个"));
    const perSuccess = screen.getByTestId("tb-run-per-success-card");
    expect(perSuccess.textContent).toContain("不适用");
    expect(perSuccess.textContent).not.toContain("$");
    expect(screen.getByTestId("tb-run-cost-card").textContent).toContain("$1.000000");
    expect(screen.getByTestId("tb-cost-basis").textContent).toContain("no_success");
  });

  it("R21：切换 Task 不改变 Run 级成本卡（Run 卡消费全 Run aggregate.cost）", async () => {
    clientMocks.getRunTasks.mockResolvedValue({
      run_id: "run-tb-1",
      items: [TASK_ROW, TASK_ROW_B],
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
    // 两个 Task 的 Trial 成本明显不同：Task 范围小计会变，Run 总额不得变。
    clientMocks.getRunTrial.mockImplementation((_runId: string, trialId: string) => Promise.resolve(
      makeDetail(trialId, trialId === "trial-z"
        ? { usage: { cost_usd: 2.0, tokens: null, coverage: "observed" } }
        : { usage: { cost_usd: 0.1, tokens: null, coverage: "observed" } }),
    ));
    render(wrap(<TerminalBenchResult />, "/terminal-bench/runs/run-tb-1/result"));
    await waitFor(() => expect(screen.getByTestId("tb-task-summary")).toBeTruthy());
    const runCostBefore = screen.getByTestId("tb-run-cost-card").textContent;
    const runPerSuccessBefore = screen.getByTestId("tb-run-per-success-card").textContent;
    expect(runCostBefore).toContain("$3.000000");
    expect(runPerSuccessBefore).toContain("$1.500000");
    await waitFor(() => expect(screen.getByTestId("tb-task-cost").textContent).toContain("$0.300000"));

    fireEvent.change(screen.getByLabelText("Task"), { target: { value: "task-b" } });
    await waitFor(() => expect(screen.getByTestId("tb-task-cost").textContent).toContain("$2.000000"));
    expect(screen.getByTestId("tb-run-cost-card").textContent).toBe(runCostBefore);
    expect(screen.getByTestId("tb-run-per-success-card").textContent).toBe(runPerSuccessBefore);
    // Task 范围小计必须自带范围说明（不能冒充 Run 总额）
    expect(screen.getByTestId("tb-task-cost").textContent).toContain("Task 范围");
  });

  it("结果页：深链落到指定 Trial；切 Task 清空旧 Task 的钻取", async () => {
    clientMocks.getRunTasks.mockResolvedValue({
      run_id: "run-tb-1",
      items: [TASK_ROW, TASK_ROW_B],
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
      metric_eligibility: { quality: false, cost: false },
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

  it("R18：Gate 渲染 Trial 口径指标与 planned_trials 覆盖单位，不出现 unsupported 回退", async () => {
    clientMocks.compareRuns.mockResolvedValue({
      eligible: true,
      reasons: [],
      metric_eligibility: { quality: true, cost: false },
      case_diff: { added: [], removed: [], changed: [] },
    });
    clientMocks.evaluateRunGate.mockResolvedValue({
      schema: "gate-lite@2",
      policy: {
        metric: "valid_trial_pass_rate", op: "gte", threshold: 0.5,
        required_coverage: 1.0, require_cost_known: false, require_comparable: true,
      },
      metric_id: "valid_trial_pass_rate",
      passed: false,
      rules: [
        {
          id: "metric_threshold", passed: true,
          reason: "valid_trial_pass_rate=1.0 (ratio) gte 0.5",
        },
        {
          id: "coverage", passed: false,
          reason: "coverage 0.75 < required 1.0 (insufficient evidence)",
        },
        { id: "comparable", passed: true, reason: "reports comparable" },
      ],
      conclusion_hash: "sha256:gate",
      evaluated_at: null,
    });
    render(wrap(<TerminalBenchCompare />, "/terminal-bench/compare?runs=run-a,run-b"));
    fireEvent.click(screen.getByRole("button", { name: "比较" }));
    await waitFor(() => expect(screen.getByTestId("tb-gate")).toBeTruthy());

    // 后端已注册这两个指标：页面必须照实渲染 Gate 结论，不能出现 unsupported 回退
    expect(screen.getByTestId("tb-gate").textContent).toContain("valid_trial_pass_rate=1.0 (ratio) gte 0.5");
    expect(screen.getByTestId("tb-gate").textContent).not.toContain("unsupported metric");
    expect(screen.getByTestId("tb-gate-metric").textContent).toContain("valid_trial_pass_rate");
    // 覆盖口径必须写明分母是计划 Trial（valid_trial_coverage / planned_trials）
    const units = screen.getByTestId("tb-compare-coverage-unit");
    expect(units.textContent).toContain("valid_trial_coverage");
    expect(units.textContent).toContain("planned_trials");
    expect(clientMocks.evaluateRunGate.mock.calls[0][0].policy).toEqual({
      metric: "valid_trial_pass_rate",
      op: "gte",
      threshold: 0.5,
      required_coverage: 1.0,
      require_cost_known: false,
      require_comparable: true,
    });
  });

  it("比较页：可比时逐条列出 API 返回的固定比较条件", async () => {
    clientMocks.compareRuns.mockResolvedValue({
      eligible: true,
      reasons: [],
      metric_eligibility: { quality: true, cost: false },
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
