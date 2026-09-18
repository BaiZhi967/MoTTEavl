import type { ComponentType } from "react";
import { CalculatorIcon, ChatTextIcon, ClockCounterClockwiseIcon, type IconProps } from "@phosphor-icons/react";

export interface RunLike {
  scenario_version: string;
  manifest?: any;
}

export interface EvalTypeSuite {
  id: string;
  label: string;
  icon: ComponentType<IconProps>;
  matchRun(run: RunLike): boolean;
}

export function suiteRoutes(id: string) {
  return {
    operate: `/${id}`,
    monitor: (runIds: string[]) => `/${id}/monitor?runs=${runIds.join(",")}`,
    result: (runId: string) => `/${id}/runs/${runId}/result`,
    compare: (runIds: string[]) => `/${id}/compare?runs=${runIds.join(",")}`,
  };
}

const GSM8K_SUITE: EvalTypeSuite = {
  id: "gsm8k",
  label: "GSM8K 数学评测",
  icon: CalculatorIcon,
  matchRun: (run) =>
    /^gsm8k.*@\d+$/.test(run.scenario_version) || Boolean(run.manifest?.benchmark_provenance),
};

const DIRECT_LLM_SUITE: EvalTypeSuite = {
  id: "direct-llm",
  label: "Direct LLM 评测",
  icon: ChatTextIcon,
  matchRun: (run) => run.scenario_version.startsWith("direct-llm@"),
};

const REPLAY_SUITE: EvalTypeSuite = {
  id: "replay",
  label: "Replay 回放",
  icon: ClockCounterClockwiseIcon,
  matchRun: (run) =>
    run.scenario_version.startsWith("replay@") || run.scenario_version.startsWith("json_extract@"),
};

export const EVAL_SUITES: EvalTypeSuite[] = [GSM8K_SUITE, DIRECT_LLM_SUITE, REPLAY_SUITE];

export function suiteForRun(run: RunLike): EvalTypeSuite | null {
  return EVAL_SUITES.find((suite) => suite.matchRun(run)) ?? null;
}
