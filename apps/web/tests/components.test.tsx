import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { ModelPicker } from "../src/components/ModelPicker";
import { RunAuditSummary } from "../src/components/RunAuditSummary";
import { RunTimeline } from "../src/components/RunTimeline";
import { ScoreTable } from "../src/components/ScoreTable";
import { StatusBadge, statusLabel } from "../src/components/StatusBadge";
import { HarnessesPage, ProvidersPage } from "../src/pages/ResourcesPage";

const clientMocks = vi.hoisted(() => ({
  getProviders: vi.fn(async () => ({ items: [] })),
  getModels: vi.fn(async () => ({ items: [] })),
  getCredentials: vi.fn(async () => ({ items: [] })),
  getProviderKinds: vi.fn(async () => ({
    items: [
      { kind: "openai_compatible", label: "OpenAI 兼容", description: "", default_base_url: null, default_key_env: "OPENAI_API_KEY" },
      { kind: "anthropic_messages", label: "Anthropic Messages", description: "", default_base_url: "https://api.anthropic.com/v1", default_key_env: "ANTHROPIC_API_KEY" },
      { kind: "openai_responses", label: "OpenAI Responses", description: "", default_base_url: "https://api.openai.com/v1", default_key_env: "OPENAI_API_KEY" },
    ],
  })),
  setCredential: vi.fn(async (profile: string, apiKey: string) => ({ profile, key_hint: "sk-l...cdef" })),
  createProvider: vi.fn(async (body: any) => body),
  updateProvider: vi.fn(async (name: string, body: any) => ({ name, ...body })),
  deleteProvider: vi.fn(),
  createModel: vi.fn(async (body: any) => body),
  updateModel: vi.fn(async (id: string, body: any) => ({ id, ...body })),
  publishModel: vi.fn(async (id: string) => ({ id, lifecycle: "published" })),
  deleteModel: vi.fn(),
  testModel: vi.fn(async (id: string) => ({
    ok: true,
    provider: "openai_compatible",
    model: id,
    tested_at: "2026-09-17T00:00:00Z",
    latency_ms: 812.4,
    attempts: 1,
    retry_count: 0,
    usage: { prompt_tokens: 9, completion_tokens: 2, total_tokens: 11 },
  })),
  getAgents: vi.fn(async () => ({ items: [] })),
  getHarnesses: vi.fn(async () => ({ items: [] })),
}));

vi.mock("../src/api/client", () => clientMocks);

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
    expect(statusLabel("needs_review")).toBe("需人工复核");
    expect(statusLabel("whatever")).toBe("whatever");
    render(<StatusBadge status="running" />);
    expect(screen.getByText("运行中")).toBeTruthy();
  });
});

describe("RunAuditSummary", () => {
  it("显示固定的执行、评测、身份和评分批次", () => {
    render(<RunAuditSummary run={{
      id: "run-1",
      schema_version: 2,
      revision: 9,
      scenario_version: "direct-llm@1",
      status: "completed",
      current_scoring_pass_id: "pass-1",
      scoring_pass: { id: "pass-1", scorer_id: "exact", scorer_version: "2", source: "rescore" },
      manifest: {
        execution: { backend_id: "direct-llm", backend_version: "1" },
        evaluation: { adapter_id: "direct-llm", adapter_version: "1", scorer_id: "exact", scorer_version: "2" },
        provider: { adapter_id: "openai_responses", adapter_version: "1" },
      },
      cases: [{ case_id: "case-1", result: {
        requested_model: "requested", reported_model: "reported", resolved_model_identity: "reported",
        identity_policy: "require_match", identity_policy_result: "mismatch", policy_passed: false,
      } }],
    }} />);
    expect(screen.getAllByText("direct-llm@1")).toHaveLength(2);
    expect(screen.getByText("pass-1 · rescore")).toBeTruthy();
    expect(screen.getByText(/requested → reported/)).toBeTruthy();
    expect(screen.getByText("require_match · 未通过")).toBeTruthy();
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

describe("HarnessesPage", () => {
  it("区分本机可运行、协议就绪与评测执行就绪", async () => {
    clientMocks.getHarnesses.mockResolvedValueOnce({ items: [{
      name: "codex-cli", binary: "codex", installed: true, version: "1", source: "path",
      path: "/bin/codex", runnable: true, protocol_ready: true, execution_ready: false,
    }] });
    clientMocks.getAgents.mockResolvedValueOnce({ items: [{
      id: "pi", kind: "bridge", description: "protocol v1",
      protocol_ready: true, execution_ready: false,
    }] });
    render(<HarnessesPage />);
    expect(await screen.findByText("codex-cli")).toBeTruthy();
    expect(screen.getAllByText("评测执行就绪")).toHaveLength(2);
    expect(screen.getByText("pi")).toBeTruthy();
    expect(screen.getAllByText("否").length).toBeGreaterThanOrEqual(2);
  });

  it("接口失败时错误在面板内并显示空状态行", async () => {
    clientMocks.getHarnesses.mockRejectedValueOnce(new Error("HTTP 500"));
    clientMocks.getAgents.mockResolvedValueOnce({ items: [] });
    render(<HarnessesPage />);
    expect(await screen.findByText(/HTTP 500/)).toBeTruthy();
    const harnessPanel = document.querySelector("section[aria-label='Harness 安装情况']") as HTMLElement;
    expect(harnessPanel.textContent).toContain("暂无 Harness 数据");
    expect(await screen.findByText("暂无 Agent")).toBeTruthy();
  });
});

describe("ProvidersPage", () => {
  const PROVIDERS = [{ name: "local-vllm", kind: "openai_compatible", base_url: "http://localhost:8001/v1" }];

  beforeEach(() => {
    vi.clearAllMocks();
    clientMocks.getProviders.mockImplementation(async () => ({ items: [] }));
    clientMocks.getModels.mockImplementation(async () => ({ items: [] }));
  });

  const fill = (label: string | RegExp, value: string) => {
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
  };

  const openCreateDialog = async () => {
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    await screen.findByText("创建 Provider");
  };

  it("创建 Provider 时先写凭据再建连接，密钥不进 provider 载荷", async () => {
    render(<ProvidersPage />);
    await openCreateDialog();
    fill("名称", "my-provider");
    fill("Base URL", "http://localhost:8001/v1");
    fill(/API Key/, "sk-live-0123456789abcdef");
    fireEvent.click(screen.getByRole("button", { name: "创建" }));

    await waitFor(() => expect(clientMocks.createProvider).toHaveBeenCalled());
    expect(clientMocks.setCredential).toHaveBeenCalledWith("my-provider", "sk-live-0123456789abcdef");
    expect(
      clientMocks.setCredential.mock.invocationCallOrder[0],
    ).toBeLessThan(clientMocks.createProvider.mock.invocationCallOrder[0]);
    expect(clientMocks.createProvider.mock.calls[0][0]).toEqual({
      name: "my-provider",
      kind: "openai_compatible",
      base_url: "http://localhost:8001/v1",
    });
  });

  it("未输入 API Key 时不调用凭据接口", async () => {
    render(<ProvidersPage />);
    await openCreateDialog();
    fill("名称", "local-vllm");
    fill("Base URL", "http://localhost:8001/v1");
    fireEvent.click(screen.getByRole("button", { name: "创建" }));

    await waitFor(() => expect(clientMocks.createProvider).toHaveBeenCalled());
    expect(clientMocks.setCredential).not.toHaveBeenCalled();
  });

  it("创建面板里切换协议类型后按该 kind 创建并预填官方默认端点", async () => {
    render(<ProvidersPage />);
    await openCreateDialog();
    await screen.findByText("Anthropic Messages");
    fill("名称", "claude-official");
    fireEvent.change(screen.getByLabelText("协议类型"), { target: { value: "anthropic_messages" } });
    expect((screen.getByLabelText("Base URL") as HTMLInputElement).value).toBe("https://api.anthropic.com/v1");
    fireEvent.click(screen.getByRole("button", { name: "创建" }));

    await waitFor(() => expect(clientMocks.createProvider).toHaveBeenCalled());
    expect(clientMocks.createProvider.mock.calls[0][0]).toEqual({
      name: "claude-official",
      kind: "anthropic_messages",
      base_url: "https://api.anthropic.com/v1",
    });
  });

  it("手输端点不被切换 kind 的默认值覆盖", async () => {
    render(<ProvidersPage />);
    await openCreateDialog();
    await screen.findByText("OpenAI Responses");
    fill("名称", "gateway");
    fill("Base URL", "http://gateway.local/v1");
    fireEvent.change(screen.getByLabelText("协议类型"), { target: { value: "openai_responses" } });
    expect((screen.getByLabelText("Base URL") as HTMLInputElement).value).toBe("http://gateway.local/v1");
  });

  const renderWithProvider = async (providers = PROVIDERS, models: any[] = []) => {
    clientMocks.getProviders.mockResolvedValue({ items: providers });
    clientMocks.getModels.mockResolvedValue({ items: models });
    render(<ProvidersPage />);
    await screen.findAllByText(providers[0].name);
  };

  it("清单选中切换右侧详情", async () => {
    await renderWithProvider([
      { name: "local-vllm", kind: "openai_compatible", base_url: "http://localhost:8001/v1" },
      { name: "claude-official", kind: "anthropic_messages", base_url: "https://api.anthropic.com/v1" },
    ]);
    fireEvent.click(screen.getByRole("button", { name: /claude-official/ }));
    expect(screen.getByRole("heading", { name: "claude-official" })).toBeTruthy();
  });

  it("详情头开关切换 Provider 启用状态", async () => {
    await renderWithProvider();
    fireEvent.click(screen.getByRole("switch", { name: "启用 local-vllm" }));

    await waitFor(() => expect(clientMocks.updateProvider).toHaveBeenCalledWith("local-vllm", { enabled: false }));
  });

  it("连接分节编辑 Base URL 后保存走更新接口", async () => {
    await renderWithProvider();
    fill("Base URL", "http://gateway:9000/v1");
    fireEvent.click(screen.getByRole("button", { name: "保存连接" }));

    await waitFor(() =>
      expect(clientMocks.updateProvider).toHaveBeenCalledWith("local-vllm", {
        kind: "openai_compatible",
        base_url: "http://gateway:9000/v1",
      }),
    );
  });

  it("未编辑连接时不出现保存按钮", async () => {
    await renderWithProvider();
    expect(screen.queryByRole("button", { name: "保存连接" })).toBeNull();
  });

  it("模型行徽章格式化上下文与工具能力", async () => {
    await renderWithProvider(PROVIDERS, [
      { id: "big-context", provider: "local-vllm", capabilities: { text: true }, context_window: 1000000 },
      { id: "tool-model", provider: "local-vllm", capabilities: { text: true }, context_window: 256000, supports_tools: true },
    ]);
    expect(screen.getByText("1M")).toBeTruthy();
    expect(screen.getByText("256K")).toBeTruthy();
    expect(screen.getByText("工具")).toBeTruthy();
  });

  it("更新密钥走凭据 profile 并刷新", async () => {
    await renderWithProvider();
    fireEvent.click(screen.getByRole("button", { name: /更新密钥/ }));
    fireEvent.change(screen.getByLabelText(/新 API Key/), { target: { value: "sk-rotated-9876543210" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(clientMocks.setCredential).toHaveBeenCalledWith("local-vllm", "sk-rotated-9876543210"));
  });

  it("编辑模型预填档案并走合并式更新接口", async () => {
    await renderWithProvider(PROVIDERS, [{
      id: "qwen2.5-7b",
      provider: "local-vllm",
      model: null,
      capabilities: { text: true },
      context_window: 32768,
      supports_tools: true,
      parameters: { temperature: 0.7 },
    }]);
    fireEvent.click(screen.getByRole("button", { name: /编辑/ }));

    const idInput = screen.getByLabelText(/模型 ID/) as HTMLInputElement;
    expect(idInput.value).toBe("qwen2.5-7b");
    expect(idInput.disabled).toBe(true);
    expect((screen.getByLabelText(/上下文窗口/) as HTMLInputElement).value).toBe("32768");
    expect((screen.getByLabelText(/temperature/) as HTMLInputElement).value).toBe("0.7");
    expect((screen.getByLabelText(/top_p/) as HTMLInputElement).value).toBe("");

    fill(/top_p/, "0.9");
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() => expect(clientMocks.updateModel).toHaveBeenCalled());
    expect(clientMocks.updateModel.mock.calls[0][0]).toBe("qwen2.5-7b");
    expect(clientMocks.updateModel.mock.calls[0][1]).toEqual({
      model: null,
      capabilities: { text: true },
      input_modalities: ["text"],
      context_window: 32768,
      max_output_tokens: null,
      supports_tools: true,
      parameters: { temperature: 0.7, top_p: 0.9 },
      reasoning: { supported: false, levels: [], default_level: null, control: null },
    });
  });

  it("最大输出 Token 兼容旧值并统一保存到顶层", async () => {
    await renderWithProvider(PROVIDERS, [{
      id: "legacy", provider: "local-vllm", capabilities: { text: true },
      parameters: { max_output_tokens: 4096, seed: 7 },
    }]);
    fireEvent.click(screen.getByRole("button", { name: /编辑/ }));
    expect((screen.getByLabelText("最大输出 Token") as HTMLInputElement).value).toBe("4096");
    fill("最大输出 Token", "8192");
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
    await waitFor(() => expect(clientMocks.updateModel).toHaveBeenCalled());
    expect(clientMocks.updateModel.mock.calls[0][1]).toMatchObject({ max_output_tokens: 8192, parameters: { seed: 7 } });
    expect(clientMocks.updateModel.mock.calls[0][1].parameters).not.toHaveProperty("max_output_tokens");
  });

  it("清空限制和采样参数删除旧值但保留其他配置", async () => {
    await renderWithProvider(PROVIDERS, [{
      id: "clear", provider: "local-vllm", model: "old-api-name", capabilities: { custom: true },
      context_window: 32000, max_output_tokens: 8192,
      parameters: { max_output_tokens: 4096, temperature: 0.7, top_p: 0.9, seed: 7 },
    }]);
    fireEvent.click(screen.getByRole("button", { name: /编辑/ }));
    expect((screen.getByLabelText("最大输出 Token") as HTMLInputElement).value).toBe("8192");
    for (const label of [/API 模型名/, "上下文窗口", "最大输出 Token", /temperature/, /top_p/]) fill(label, "");
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
    await waitFor(() => expect(clientMocks.updateModel).toHaveBeenCalled());
    expect(clientMocks.updateModel.mock.calls[0][1]).toMatchObject({
      model: null, context_window: null, max_output_tokens: null, parameters: { seed: 7 }, capabilities: { custom: true },
    });
    expect(clientMocks.updateModel.mock.calls[0][1].parameters).toEqual({ seed: 7 });
  });

  it("创建模型保存多模态、能力和 CEL 推理映射", async () => {
    await renderWithProvider();
    fireEvent.click(screen.getByRole("button", { name: "添加模型" }));
    fill(/模型 ID/, "modern-model");
    const text = screen.getByRole("switch", { name: "输入类型：文本" });
    expect(text.getAttribute("aria-checked")).toBe("true");
    expect(text.hasAttribute("disabled")).toBe(true);
    for (const name of ["输入类型：图片", "输入类型：视频", "输入类型：PDF", "结构化输出", "原生联网搜索", "对话中系统消息", "启用推理等级映射"]) {
      fireEvent.click(screen.getByRole("switch", { name }));
    }
    fill("可用推理等级", "low, medium, high");
    fill("默认推理等级", "medium");
    fill("CEL 表达式", '{"reasoning_effort": reasoningLevel}');
    fill("最大输出 Token", "16384");
    fireEvent.click(screen.getByRole("button", { name: "注册" }));
    await waitFor(() => expect(clientMocks.createModel).toHaveBeenCalled());
    expect(clientMocks.createModel.mock.calls[0][0]).toMatchObject({
      id: "modern-model", provider: "local-vllm", max_output_tokens: 16384,
      input_modalities: ["text", "image", "video", "pdf"],
      capabilities: { text: true, structured_output: true, native_search: true, system_messages: true },
      reasoning: { supported: true, levels: ["low", "medium", "high"], default_level: "medium", control: '{"reasoning_effort": reasoningLevel}' },
    });
  });

  it("推理配置回填，保存错误保留用户输入", async () => {
    await renderWithProvider(PROVIDERS, [{
      id: "reasoner", provider: "local-vllm", capabilities: { structured_output: true }, input_modalities: ["text", "image"],
      reasoning: { supported: true, levels: ["low", "high"], default_level: "high", control: '{"reasoning_effort": reasoningLevel}' },
    }]);
    fireEvent.click(screen.getByRole("button", { name: /编辑/ }));
    expect((screen.getByLabelText("默认推理等级") as HTMLSelectElement).value).toBe("high");
    expect(screen.getByRole("switch", { name: "输入类型：图片" }).getAttribute("aria-checked")).toBe("true");
    expect(screen.getByRole("switch", { name: "结构化输出" }).getAttribute("aria-checked")).toBe("true");
    clientMocks.updateModel.mockRejectedValueOnce(new Error("CEL 表达式无效"));
    fill("CEL 表达式", "invalid(");
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
    expect(await screen.findByText("Error: CEL 表达式无效")).toBeTruthy();
    expect((screen.getByLabelText("CEL 表达式") as HTMLTextAreaElement).value).toBe("invalid(");
    expect((screen.getByRole("button", { name: "保存修改" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("关闭推理映射清除默认等级以通过契约校验", async () => {
    await renderWithProvider(PROVIDERS, [{
      id: "reasoner", provider: "local-vllm", capabilities: {},
      reasoning: { supported: true, levels: ["high"], default_level: "high", control: '{"reasoning_effort": reasoningLevel}' },
    }]);
    fireEvent.click(screen.getByRole("button", { name: /编辑/ }));
    fireEvent.click(screen.getByRole("switch", { name: "启用推理等级映射" }));
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
    await waitFor(() => expect(clientMocks.updateModel).toHaveBeenCalled());
    expect(clientMocks.updateModel.mock.calls[0][1].reasoning).toMatchObject({ supported: false, default_level: null });
  });

  it("草稿模型显式发布", async () => {
    await renderWithProvider(PROVIDERS, [
      { id: "qwen2.5-7b", provider: "local-vllm", lifecycle: "draft", generation: 1, capabilities: { text: true } },
    ]);
    expect(screen.getByText("草稿 · g1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "发布" }));
    await waitFor(() => expect(clientMocks.publishModel).toHaveBeenCalledWith("qwen2.5-7b"));
  });

  it("模型行开关切换启用状态", async () => {
    await renderWithProvider(PROVIDERS, [
      { id: "qwen2.5-7b", provider: "local-vllm", capabilities: { text: true } },
    ]);
    fireEvent.click(screen.getByRole("switch", { name: "启用 qwen2.5-7b" }));

    await waitFor(() => expect(clientMocks.updateModel).toHaveBeenCalledWith("qwen2.5-7b", { enabled: false }));
  });

  it("测试模型展示行内结果并在清单点亮状态点", async () => {
    await renderWithProvider(PROVIDERS, [
      { id: "qwen2.5-7b", provider: "local-vllm", capabilities: { text: true } },
    ]);
    fireEvent.click(screen.getByRole("button", { name: /测试/ }));

    await waitFor(() => expect(clientMocks.testModel).toHaveBeenCalledWith("qwen2.5-7b"));
    expect(await screen.findByText(/通过/)).toBeTruthy();
    expect(screen.getByText(/812ms/)).toBeTruthy();
    expect(await screen.findByLabelText("最近测试通过")).toBeTruthy();
  });
});

describe("ModelPicker", () => {
  const MODELS = [
    { id: "glm-4.7", provider: "zhipu", capabilities: {}, context_window: 128000 },
    { id: "qwen-max", provider: "zhipu", capabilities: {}, context_window: 32000 },
    { id: "qwen2.5-7b", provider: "local-vllm", capabilities: {}, context_window: 32768 },
    { id: "glm-4-air", provider: "zhipu", capabilities: {}, enabled: false },
    { id: "draft-model", provider: "zhipu", capabilities: {}, lifecycle: "draft" },
  ];

  it("按 Provider 分组渲染并标注上下文与停用态", () => {
    const onToggle = vi.fn();
    render(<ModelPicker models={MODELS as any} selected={["qwen-max"]} onToggle={onToggle} />);
    expect(screen.getByText("zhipu")).toBeTruthy();
    expect(screen.getByText("local-vllm")).toBeTruthy();
    expect(screen.getByText("128K")).toBeTruthy();
    expect(screen.getByText("已停用")).toBeTruthy();
    const disabled = screen.getByLabelText("选择模型 glm-4-air") as HTMLInputElement;
    expect(disabled.disabled).toBe(true);
    expect((screen.getByLabelText("选择模型 draft-model") as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText("草稿")).toBeTruthy();
  });

  it("点击勾选触发 onToggle", () => {
    const onToggle = vi.fn();
    render(<ModelPicker models={MODELS as any} selected={[]} onToggle={onToggle} />);
    fireEvent.click(screen.getByLabelText("选择模型 qwen-max"));
    expect(onToggle).toHaveBeenCalledWith("qwen-max");
  });

  it("空模型列表显示空状态", () => {
    render(<ModelPicker models={[]} selected={[]} onToggle={vi.fn()} />);
    expect(screen.getByText("暂无可用模型，先在 Provider 页注册")).toBeTruthy();
  });
});
