import { afterEach, describe, expect, it } from "vitest";
import {
  clearCaseSelection, loadCaseSelection, randomSeed, runSelectionLabel,
  saveCaseSelection, selectionRequest,
} from "../src/evalTypes/gsm8k/selection";

afterEach(() => {
  sessionStorage.clear();
});

describe("gsm8k selection helpers", () => {
  it("存储往返只保留字符串 id，空集合视为无选择", () => {
    saveCaseSelection({ dataset: "gsm8k-test@1", caseIds: ["gsm8k-test-0001", "gsm8k-test-0002"] });
    expect(loadCaseSelection()).toEqual({
      dataset: "gsm8k-test@1", caseIds: ["gsm8k-test-0001", "gsm8k-test-0002"] });
    saveCaseSelection({ dataset: "gsm8k-test@1", caseIds: [] });
    expect(loadCaseSelection()).toBeNull();
    sessionStorage.setItem("motte.gsm8k.case-selection", "{not json");
    expect(loadCaseSelection()).toBeNull();
    sessionStorage.setItem("motte.gsm8k.case-selection", JSON.stringify({ dataset: 7, caseIds: "x" }));
    expect(loadCaseSelection()).toBeNull();
    saveCaseSelection({ dataset: "gsm8k-test@1", caseIds: ["gsm8k-test-0001"] });
    clearCaseSelection();
    expect(loadCaseSelection()).toBeNull();
  });

  it("随机种子是 8-64 位十六进制且每次不同", () => {
    const seeds = new Set(Array.from({ length: 20 }, () => randomSeed()));
    expect(seeds.size).toBeGreaterThan(15);
    for (const seed of seeds) expect(seed).toMatch(/^[0-9a-f]{8,64}$/);
  });

  it("selectionRequest 空选择返回 null（调用方据此禁用发起）", () => {
    expect(selectionRequest("all")).toEqual({ mode: "all" });
    expect(selectionRequest("ids", { caseIds: [] })).toBeNull();
    expect(selectionRequest("ids", { caseIds: ["gsm8k-test-0001"] }))
      .toEqual({ mode: "ids", case_ids: ["gsm8k-test-0001"] });
    expect(selectionRequest("random", { count: 0 })).toBeNull();
    expect(selectionRequest("random", { count: 5 })).toEqual({ mode: "random", count: 5, seed: undefined });
    expect(selectionRequest("random", { count: 5, seed: "deadbeef" }))
      .toEqual({ mode: "random", count: 5, seed: "deadbeef" });
  });

  it("runSelectionLabel 描述运行快照里的选择，旧运行回落到数据集题数", () => {
    expect(runSelectionLabel({ run_selection: { mode: "all", count: 1319 } })).toBe("全部 1319 题");
    expect(runSelectionLabel({ run_selection: { mode: "ids", count: 3 } })).toBe("指定 3 题");
    expect(runSelectionLabel({ run_selection: { mode: "random", count: 100, seed: "deadbeefcafe" } }))
      .toBe("随机 100 题（seed deadbeef…，可复现）");
    expect(runSelectionLabel({ selected_count: 20 })).toBe("全部 20 题");
    expect(runSelectionLabel(undefined)).toBeNull();
    expect(runSelectionLabel({})).toBeNull();
  });
});
