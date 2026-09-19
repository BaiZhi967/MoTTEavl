import { describe, expect, it } from "vitest";
import { EVAL_SUITES, suiteForRun, suiteRoutes } from "../src/evalTypes/registry";

describe("evalTypes 注册表", () => {
  it("注册五个套件且 id 唯一", () => {
    expect(EVAL_SUITES.map((suite) => suite.id)).toEqual([
      "agent-tasks", "ceval", "gsm8k", "direct-llm", "replay",
    ]);
  });

  it("按 run 归属匹配套件", () => {
    expect(suiteForRun({ scenario_version: "gsm8k-test-smoke@1" })?.id).toBe("gsm8k");
    expect(suiteForRun({ scenario_version: "direct-llm@1" })?.id).toBe("direct-llm");
    expect(suiteForRun({ scenario_version: "direct-llm-classify@1" })?.id).toBe("direct-llm");
    expect(suiteForRun({ scenario_version: "replay@1" })?.id).toBe("replay");
    expect(suiteForRun({ scenario_version: "json_extract@1" })?.id).toBe("replay");
    expect(suiteForRun({ scenario_version: "unknown@9" })).toBeNull();
  });

  it("两个套件的运行都带 benchmark_provenance，靠 provenance.suite 区分", () => {
    const gsm8kRun = {
      scenario_version: "gsm8k-test-full@1",
      manifest: { benchmark_provenance: { suite: "gsm8k", selected_count: 20 } },
    };
    const directRun = {
      scenario_version: "direct-llm-classify@1",
      manifest: { benchmark_provenance: { suite: "direct-llm", scorer: "contains" } },
    };
    expect(suiteForRun(gsm8kRun)?.id).toBe("gsm8k");
    expect(suiteForRun(directRun)?.id).toBe("direct-llm");
    // 数据集名与场景名同形时，仍按快照字段而不是场景名前缀判定
    expect(suiteForRun({
      scenario_version: "direct-llm-classify@1",
      manifest: { benchmark_provenance: { suite: "gsm8k" } },
    })?.id).toBe("gsm8k");
    // 旧运行没有 suite 字段：带 benchmark_provenance 或 gsm8k 场景名的一律归 GSM8K
    expect(suiteForRun({ scenario_version: "gsm8k-test-full@1", manifest: {} })?.id).toBe("gsm8k");
    expect(suiteForRun({
      scenario_version: "legacy-benchmark@1",
      manifest: { benchmark_provenance: { selected_count: 20 } },
    })?.id).toBe("gsm8k");
    expect(suiteForRun({
      scenario_version: "direct-llm-legacy@1",
      manifest: { benchmark_provenance: { selected_count: 5 } },
    })?.id).toBe("gsm8k");
  });

  it("生成四类路由", () => {
    const routes = suiteRoutes("gsm8k");
    expect(routes.operate).toBe("/gsm8k");
    expect(routes.cases).toBe("/gsm8k/cases");
    expect(routes.monitor(["run-1", "run-2"])).toBe("/gsm8k/monitor?runs=run-1,run-2");
    expect(routes.result("run-1")).toBe("/gsm8k/runs/run-1/result");
    expect(routes.compare(["run-1", "run-2"])).toBe("/gsm8k/compare?runs=run-1,run-2");
    expect(suiteRoutes("direct-llm").cases).toBe("/direct-llm/cases");
  });
});
