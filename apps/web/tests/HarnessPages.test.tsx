import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
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
    expect(await screen.findByText("pi-agent@1")).toBeTruthy();
    expect(screen.getByText("claude-cli@1")).toBeTruthy();
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
    await screen.findByText("codex-app-server@1");
    expect(screen.getByText(/命令消费者接通前，消息入口保持关闭/)).toBeTruthy();
    expect(screen.getAllByText(/交互/).length).toBeGreaterThanOrEqual(1);
  });
});
