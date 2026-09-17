import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { RunTimeline } from "../src/components/RunTimeline";
import { ScoreTable } from "../src/components/ScoreTable";
import { StatusBadge, statusLabel } from "../src/components/StatusBadge";
import { ProvidersPage } from "../src/pages/ResourcesPage";

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
  createModel: vi.fn(async (body: any) => body),
  deleteModel: vi.fn(),
  deleteProvider: vi.fn(),
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
  getAgents: vi.fn(),
  getHarnesses: vi.fn(),
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

describe("ProvidersPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  const fill = (label: string | RegExp, value: string) => {
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
  };

  it("输入 API Key 时先写凭据再建 Provider，密钥不进 provider 载荷", async () => {
    render(<ProvidersPage />);
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
    fill("名称", "local-vllm");
    fill("Base URL", "http://localhost:8001/v1");
    fireEvent.click(screen.getByRole("button", { name: "创建" }));

    await waitFor(() => expect(clientMocks.createProvider).toHaveBeenCalled());
    expect(clientMocks.setCredential).not.toHaveBeenCalled();
  });

  it("切换协议类型后按该 kind 创建并预填官方默认端点", async () => {
    render(<ProvidersPage />);
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
    await screen.findByText("OpenAI Responses");
    fill("名称", "gateway");
    fill("Base URL", "http://gateway.local/v1");
    fireEvent.change(screen.getByLabelText("协议类型"), { target: { value: "openai_responses" } });
    expect((screen.getByLabelText("Base URL") as HTMLInputElement).value).toBe("http://gateway.local/v1");
  });

  it("更新密钥走凭据 profile 并刷新", async () => {
    clientMocks.getProviders.mockResolvedValueOnce({
      items: [{ name: "claude-official", kind: "anthropic_messages", base_url: "https://api.anthropic.com/v1" }],
    });
    render(<ProvidersPage />);
    await screen.findByText("claude-official");
    fireEvent.click(screen.getByRole("button", { name: /更新密钥/ }));
    fireEvent.change(screen.getByLabelText(/新 API Key/), { target: { value: "sk-rotated-9876543210" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(clientMocks.setCredential).toHaveBeenCalledWith("claude-official", "sk-rotated-9876543210"));
  });

  it("编辑模型预填档案并可覆盖保存", async () => {
    clientMocks.getProviders.mockResolvedValueOnce({
      items: [{ name: "local-vllm", kind: "openai_compatible", base_url: "http://localhost:8001/v1" }],
    });
    clientMocks.getModels.mockResolvedValueOnce({
      items: [{
        id: "qwen2.5-7b",
        provider: "local-vllm",
        model: null,
        capabilities: { text: true },
        context_window: 32768,
        supports_tools: true,
        parameters: { temperature: 0.7 },
      }],
    });
    render(<ProvidersPage />);
    await screen.findAllByText("qwen2.5-7b");
    fireEvent.click(screen.getByRole("button", { name: /编辑/ }));

    const idInput = screen.getByLabelText(/模型 ID/) as HTMLInputElement;
    expect(idInput.value).toBe("qwen2.5-7b");
    expect(idInput.disabled).toBe(true);
    expect((screen.getByLabelText(/上下文窗口/) as HTMLInputElement).value).toBe("32768");
    expect((screen.getByLabelText(/temperature/) as HTMLInputElement).value).toBe("0.7");
    expect((screen.getByLabelText(/top_p/) as HTMLInputElement).value).toBe("");

    fill(/top_p/, "0.9");
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() => expect(clientMocks.createModel).toHaveBeenCalled());
    expect(clientMocks.createModel.mock.calls[0][0]).toEqual({
      id: "qwen2.5-7b",
      provider: "local-vllm",
      capabilities: { text: true },
      context_window: 32768,
      supports_tools: true,
      parameters: { temperature: 0.7, top_p: 0.9 },
    });
  });

  it("测试模型展示行内结果", async () => {
    clientMocks.getProviders.mockResolvedValueOnce({
      items: [{ name: "local-vllm", kind: "openai_compatible", base_url: "http://localhost:8001/v1" }],
    });
    clientMocks.getModels.mockResolvedValueOnce({
      items: [{ id: "qwen2.5-7b", provider: "local-vllm", capabilities: { text: true } }],
    });
    render(<ProvidersPage />);
    await screen.findAllByText("qwen2.5-7b");
    fireEvent.click(screen.getByRole("button", { name: /测试/ }));

    await waitFor(() => expect(clientMocks.testModel).toHaveBeenCalledWith("qwen2.5-7b"));
    expect(await screen.findByText(/通过/)).toBeTruthy();
    expect(screen.getByText(/812ms/)).toBeTruthy();
  });
});
