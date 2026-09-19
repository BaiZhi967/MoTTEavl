import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cancelRun, createBenchmarkRun, createRun, dryRunDirectLlm, getBenchmarkCases,
  getDirectLlmSources, getRuns, getSourceDetail, importBenchmark, publishModel, setCredential,
  updateModel, updateProvider,
} from "../src/api/client";

const mockFetch = (status: number, payload: unknown) => {
  const spy = vi.fn(async () => ({
    ok: status < 400,
    status,
    json: async () => payload,
  }));
  vi.stubGlobal("fetch", spy);
  return spy;
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api client", () => {
  it("GET runs 返回列表", async () => {
    const fetchMock = mockFetch(200, { items: [{ id: "run-1", status: "queued" }], total: 1 });
    const payload = await getRuns("queued");
    expect(payload.total).toBe(1);
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/runs?status=queued", expect.anything());
  });

  it("POST createRun 携带 JSON body", async () => {
    const fetchMock = mockFetch(202, { id: "run-9", status: "queued" });
    const created = await createRun({ scenario_version: "replay@1", case_ids: ["case-1"] });
    expect(created.id).toBe("run-9");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/runs");
    expect(JSON.parse((init as any).body)).toEqual({ scenario_version: "replay@1", case_ids: ["case-1"] });
  });

  it("非 2xx 抛出服务端错误信息", async () => {
    mockFetch(422, { error: { code: "CREDENTIALS_REJECTED", message: "凭据字段不接受明文存储" } });
    await expect(cancelRun("run-1", "原因")).rejects.toThrow("凭据字段不接受明文存储");
  });

  it("PUT setCredential 携带掩码路径与 api_key body", async () => {
    const fetchMock = mockFetch(200, { profile: "local-vllm", key_hint: "sk-l...cdef" });
    const saved = await setCredential("local-vllm", "sk-live-0123456789abcdef");
    expect(saved.key_hint).toBe("sk-l...cdef");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/credentials/local-vllm");
    expect((init as any).method).toBe("PUT");
    expect(JSON.parse((init as any).body)).toEqual({ api_key: "sk-live-0123456789abcdef" });
  });

  // 更新端点服务端只注册 PUT：错发 POST 会被 405 挡下（启用/停用开关就走这条路径）
  it("PUT updateProvider 走 set 语义而不是注册用 POST", async () => {
    const fetchMock = mockFetch(200, { name: "local-vllm", enabled: false });
    const updated = await updateProvider("local-vllm", { enabled: false });
    expect(updated.enabled).toBe(false);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/providers/local-vllm");
    expect((init as any).method).toBe("PUT");
    expect(JSON.parse((init as any).body)).toEqual({ enabled: false });
  });

  it("PUT updateModel 走 set 语义而不是注册用 POST", async () => {
    const fetchMock = mockFetch(200, { id: "qwen2.5-7b", enabled: true });
    const updated = await updateModel("qwen2.5-7b", { enabled: true });
    expect(updated.enabled).toBe(true);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/models/qwen2.5-7b");
    expect((init as any).method).toBe("PUT");
    expect(JSON.parse((init as any).body)).toEqual({ enabled: true });
  });

  it("POST publishModel 显式发布草稿", async () => {
    const fetchMock = mockFetch(200, { id: "qwen2.5-7b", lifecycle: "published" });
    const published = await publishModel("qwen2.5-7b");
    expect(published.lifecycle).toBe("published");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/models/qwen2.5-7b/publish");
    expect((init as any).method).toBe("POST");
  });

  it("POST importBenchmark 带 scope 与 pinned revision", async () => {
    const fetchMock = mockFetch(201, {
      imported: "gsm8k-test@2", scenario: "gsm8k-test-full@2", scope: "full",
      benchmark: "gsm8k-full", cases: 1319, source_sha256: "s", cases_sha256: "c",
    });
    const receipt = await importBenchmark({
      revision: "5d0b5c9a1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b", license: "MIT", version: "2", scope: "full",
    });
    expect(receipt.scenario).toBe("gsm8k-test-full@2");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/benchmarks/gsm8k/import");
    expect(JSON.parse((init as any).body)).toEqual({
      revision: "5d0b5c9a1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b", license: "MIT", version: "2", scope: "full",
    });
  });

  it("GET getBenchmarkCases 拼分页与搜索参数", async () => {
    const fetchMock = mockFetch(200, { dataset: "gsm8k-test@1", total: 0, items: [] });
    await getBenchmarkCases({ dataset: "gsm8k-test@1", offset: 25, limit: 25, query: "ducks" });
    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/benchmarks/gsm8k/cases?dataset=gsm8k-test%401&offset=25&limit=25&query=ducks");
  });

  it("GET Direct LLM 受管来源目录使用 typed 列表端点", async () => {
    const fetchMock = mockFetch(200, { items: [{ id: "mmlu-pro", label: "MMLU-Pro" }], total: 1 });
    const payload = await getDirectLlmSources();
    expect(payload.total).toBe(1);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/benchmarks/direct-llm/sources");
  });

  it("GET Direct LLM 来源详情编码 source id", async () => {
    const fetchMock = mockFetch(200, { id: "managed/source", label: "Managed Source" });
    const payload = await getSourceDetail("managed/source");
    expect(payload.id).toBe("managed/source");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/benchmarks/direct-llm/sources/managed%2Fsource");
  });

  it("POST Direct LLM dry-run 与 run 共用 strict request body", async () => {
    const fetchMock = mockFetch(200, {
      dataset: "direct-v2@1", scenario: "direct-v2@1", contract_version: 2,
      plugin_version: "direct-llm@2", selected_count: 2, case_ids_sha256: "a".repeat(64),
      max_output_tokens: 1024, estimated: true,
    });
    const response = await dryRunDirectLlm({
      model: "glm-4.7", scenario: "direct-v2@1",
      case_selection: { mode: "profile", profile: "smoke" },
    });
    expect(response.selected_count).toBe(2);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/benchmarks/direct-llm/dry-run");
    expect((init as any).method).toBe("POST");
    expect(JSON.parse((init as any).body)).toEqual({
      model: "glm-4.7", scenario: "direct-v2@1",
      case_selection: { mode: "profile", profile: "smoke" },
    });
  });

  it("POST createBenchmarkRun 携带题目子集与思考强度", async () => {
    const fetchMock = mockFetch(202, { id: "run-9", status: "queued" });
    await createBenchmarkRun({
      model: "reasoner", scenario: "gsm8k-test-full@1", reasoning_level: "high",
      case_selection: { mode: "random", count: 100, seed: "deadbeef" },
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/benchmarks/gsm8k/runs");
    expect(JSON.parse((init as any).body)).toEqual({
      model: "reasoner", scenario: "gsm8k-test-full@1", reasoning_level: "high",
      case_selection: { mode: "random", count: 100, seed: "deadbeef" },
    });
  });
});
