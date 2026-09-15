import { afterEach, describe, expect, it, vi } from "vitest";
import { cancelRun, createRun, getRuns } from "../src/api/client";

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
});
