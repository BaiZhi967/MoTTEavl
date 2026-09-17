import { describe, expect, it } from "vitest";
import { EVAL_SUITES, suiteForRun, suiteRoutes } from "../src/evalTypes/registry";

describe("evalTypes 注册表", () => {
  it("注册三个套件且 id 唯一", () => {
    expect(EVAL_SUITES.map((suite) => suite.id)).toEqual(["gsm8k", "direct-llm", "replay"]);
  });

  it("按 run 归属匹配套件", () => {
    expect(suiteForRun({ scenario_version: "gsm8k-test-smoke@1" })?.id).toBe("gsm8k");
    expect(suiteForRun({ scenario_version: "direct-llm@1" })?.id).toBe("direct-llm");
    expect(suiteForRun({ scenario_version: "replay@1" })?.id).toBe("replay");
    expect(suiteForRun({ scenario_version: "json_extract@1" })?.id).toBe("replay");
    expect(suiteForRun({ scenario_version: "unknown@9" })).toBeNull();
  });

  it("生成四类路由", () => {
    const routes = suiteRoutes("gsm8k");
    expect(routes.operate).toBe("/gsm8k");
    expect(routes.monitor(["run-1", "run-2"])).toBe("/gsm8k/monitor?runs=run-1,run-2");
    expect(routes.result("run-1")).toBe("/gsm8k/runs/run-1/result");
    expect(routes.compare(["run-1", "run-2"])).toBe("/gsm8k/compare?runs=run-1,run-2");
  });
});
