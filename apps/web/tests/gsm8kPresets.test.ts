import { describe, expect, it } from "vitest";
import type { BenchmarkPreset } from "../src/api/client";
import { scopeLabel, sortPresets } from "../src/evalTypes/gsm8k/presets";

const preset = (scenario: string, dataset: string, scope: string, cases: number): BenchmarkPreset =>
  ({ scenario, dataset, scope, cases, runs: [] });

describe("gsm8k presets", () => {
  it("scopeLabel 只认已登记的两种范围", () => {
    expect(scopeLabel("smoke")).toBe("冒烟");
    expect(scopeLabel("full")).toBe("全量");
    expect(scopeLabel(null)).toBe("未知范围");
  });

  it("sortPresets 让题数最多的数据集在前（默认跑最完整的数据集）", () => {
    const items = [
      preset("gsm8k-test-smoke@1", "gsm8k-test@1", "smoke", 20),
      preset("gsm8k-test-full@2", "gsm8k-test@2", "full", 1319),
    ];
    expect(sortPresets(items).map((item) => item.scenario)).toEqual([
      "gsm8k-test-full@2", "gsm8k-test-smoke@1"]);
    expect(items[0].scenario).toBe("gsm8k-test-smoke@1"); // 不改动入参
  });

  it("sortPresets 同题数时冒烟在前，其余按场景名稳定排序", () => {
    const items = [
      preset("b-full@3", "b@3", "full", 20),
      preset("a-smoke@1", "a@1", "smoke", 20),
      preset("a-full@2", "a@2", "full", 20),
    ];
    expect(sortPresets(items).map((item) => item.scenario)).toEqual([
      "a-smoke@1", "a-full@2", "b-full@3"]);
  });
});
