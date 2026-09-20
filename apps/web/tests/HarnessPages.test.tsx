import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import {
  HarnessMonitor,
  HarnessOperate,
} from "../src/evalTypes/harness/HarnessPages";

function stubFetch(responses: Record<string, unknown>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    for (const [prefix, payload] of Object.entries(responses)) {
      if (path.includes(prefix)) {
        return {
          ok: true,
          status: 200,
          json: async () => payload,
        } as Response;
      }
    }
    return { ok: false, status: 404, json: async () => ({}) } as unknown as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("HarnessPages（外部 Runtime）", () => {
  beforeEach(() => {
    vi.resetModules();
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("目录页展示分层就绪与未就绪原因（installed/protocol/execution 三层）", async () => {
    stubFetch({
      "/api/v1/runtimes": {
        total: 2,
        items: [
          {
            name: "pi-agent", version: "1", kind: "pi-bridge",
            transport: "bridge-stdio-jsonl",
            upstream_version: "@mariozechner/pi-agent-core@0.73.1",
            model_control: "runner-configured", interactive: false, published: true,
            readiness: {
              backend: "pi-agent", pinned_version: "0.73.1", installed: true,
              installed_version: "0.73.1", protocol_ready: false, execution_ready: false,
              reasons: { protocol_ready: "bridge probe result not provided" },
            },
          },
          {
            name: "claude-cli", version: "1", kind: "claude-cli",
            transport: "cli-batch-json", upstream_version: "claude-code@2.1.278",
            model_control: "runner-configured", interactive: false, published: false,
            readiness: {
              backend: "claude-cli", pinned_version: "2.1.278", installed: true,
              installed_version: "2.1.268", protocol_ready: false, execution_ready: false,
              reasons: {
                protocol_ready: "claude version drift: installed 2.1.268, pinned 2.1.278",
              },
            },
          },
        ],
      },
    });
    render(
      <MemoryRouter>
        <HarnessOperate />
      </MemoryRouter>,
    );
    expect((await screen.findAllByText("pi-agent@1")).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("claude-cli@1").length).toBeGreaterThanOrEqual(1);
    // R20：创建链路面板存在（场景/runtime/profile + 内联配置）。
    expect(screen.getByText("创建 Runtime Run")).toBeTruthy();
    expect(screen.getByText("Runtime Profile")).toBeTruthy();
    expect(screen.getByLabelText("场景（agent-tasks）")).toBeTruthy();
    // 三层就绪标记渲染
    const flags = screen.getAllByText("已安装");
    expect(flags.length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText("协议就绪").length).toBeGreaterThanOrEqual(2);
    // 原因区块存在
    expect(screen.getByText(/各层未就绪原因/)).toBeTruthy();
  });

  it("运行页只列 manifest.runtime 驱动的 Run", async () => {
    stubFetch({
      "/api/v1/runs": {
        total: 2,
        items: [
          { id: "run-rt-1", status: "completed", manifest: { runtime: "pi-agent@1" }, created_at: "2026-09-20" },
          { id: "run-plain-2", status: "completed", manifest: { provider: {} }, created_at: "2026-09-20" },
        ],
      },
    });
    render(
      <MemoryRouter>
        <HarnessMonitor />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByText("run-rt-1")).toBeTruthy();
    });
    expect(screen.queryByText("run-plain-2")).toBeNull();
    expect(screen.getByText("pi-agent@1")).toBeTruthy();
  });

  it("交互通道未接消费者时目录页明确说明消息入口关闭", async () => {
    stubFetch({
      "/api/v1/runtimes": {
        total: 1,
        items: [{
          name: "codex-app-server", version: "1", kind: "codex-app-server",
          transport: "app-server-jsonrpc", upstream_version: "codex@0.155.1",
          model_control: "runner-configured", interactive: true, published: true,
          readiness: {
            backend: "codex-app-server", pinned_version: "0.155.1", installed: true,
            installed_version: "0.148.0", protocol_ready: false, execution_ready: false,
            reasons: { protocol_ready: "version drift" },
          },
        }],
      },
    });
    render(
      <MemoryRouter>
        <HarnessOperate />
      </MemoryRouter>,
    );
    await screen.findAllByText("codex-app-server@1");
    expect(screen.getByText(/命令消费者接通前，消息入口保持关闭/)).toBeTruthy();
    expect(screen.getAllByText(/交互/).length).toBeGreaterThanOrEqual(1);
  });

  it("R20：创建表单选择已发布 runtime 并提交后调用创建接口", async () => {
    const fetchMock = stubFetch({
      "/api/v1/runtimes": {
        total: 1,
        items: [{
          name: "pi-agent", version: "1", kind: "pi-bridge",
          transport: "bridge-stdio-jsonl", upstream_version: "pi@0.73.1",
          model_control: "runner-configured", interactive: false, published: true,
          readiness: {
            backend: "pi-agent", pinned_version: "0.73.1", installed: true,
            protocol_ready: true, execution_ready: false, reasons: {},
          },
        }],
      },
      "/api/v1/scenarios": {
        items: [{ name: "agent-suite", version: "1", suite: "agent-tasks" }],
      },
      "/api/v1/runtime_profiles": { items: [], total: 0 },
      "/api/v1/runs": {
        id: "run-new-1", status: "queued",
        manifest: { runtime: "pi-agent@1" }, created_at: "2026-09-20",
      },
    });
    render(
      <MemoryRouter>
        <HarnessOperate />
      </MemoryRouter>,
    );
    const scenarioSelect = await screen.findByLabelText("场景（agent-tasks）");
    fireEvent.change(scenarioSelect, { target: { value: "agent-suite@1" } });
    const runtimeSelect = screen.getByLabelText("Runtime（已发布）");
    fireEvent.change(runtimeSelect, { target: { value: "pi-agent" } });
    const settingsBox = screen.getByLabelText("原生设置 JSON（runner-configured；至少 model 字段）");
    fireEvent.change(settingsBox, {
      target: { value: '{"model":"scripted-1"}' },
    });
    fireEvent.click(screen.getByRole("button", { name: /预检并创建/ }));
    await waitFor(() => {
      const calls = fetchMock.mock.calls.map((call) => String(call[0]));
      expect(calls).toContain("/api/v1/runs");
    });
    const createCall = fetchMock.mock.calls.find(
      (call) => String(call[0]) === "/api/v1/runs",
    );
    const body = JSON.parse(String((createCall?.[1] as RequestInit)?.body));
    expect(body.scenario_version).toBe("agent-suite@1");
    expect(body.manifest.runtime).toBe("pi-agent@1");
    expect(body.manifest.runtime_profile.native_settings.model).toBe("scripted-1");
  });

  it("R21：运行页详情链接指向已注册的 monitor 路由", async () => {
    stubFetch({
      "/api/v1/runs": {
        total: 1,
        items: [{
          id: "run-link-1", status: "completed",
          manifest: { runtime: "pi-agent@1" }, created_at: "2026-09-20",
        }],
      },
    });
    render(
      <MemoryRouter>
        <HarnessMonitor />
      </MemoryRouter>,
    );
    const link = await screen.findByRole("link", { name: "run-link-1" });
    expect(link.getAttribute("href")).toBe("/runs/run-link-1/monitor");
  });
});
