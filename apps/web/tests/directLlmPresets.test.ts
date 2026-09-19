import { describe, expect, it } from "vitest";
import {
  SCORER_OPTIONS, datasetScorer, scorerLabel, scorerShort,
} from "../src/evalTypes/directllm/presets";

describe("Direct LLM scorer display helpers", () => {
  it("保留 v1 导入评分器的三种固定选项", () => {
    expect(SCORER_OPTIONS.map((option) => option.value)).toEqual(["exact", "contains", "regex"]);
    expect(scorerLabel("exact")).toBe("exact（逐字精确匹配）");
  });

  it("v2 scorer spec 显示 id@version，不回落为未知或对象字符串", () => {
    const scorer = { id: "numeric", version: "1", config: { tolerance: "0.01" } };
    const preset = { eval: { scorer } } as any;
    expect(datasetScorer(preset)).toBe(scorer);
    expect(scorerLabel(datasetScorer(preset))).toBe("numeric@1");
    expect(scorerShort(datasetScorer(preset))).toBe("numeric@1");
    expect(scorerShort({ id: "choice", version: null })).toBe("choice");
    expect(scorerLabel({ version: "1" })).toBe("未知评分器");
  });
});
