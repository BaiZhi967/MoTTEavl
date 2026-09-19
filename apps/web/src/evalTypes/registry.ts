import type { ComponentType } from "react";
import { CalculatorIcon, ChatTextIcon, ClockCounterClockwiseIcon, GraduationCapIcon, RobotIcon, type IconProps } from "@phosphor-icons/react";

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
    cases: `/${id}/cases`,
    monitor: (runIds: string[]) => `/${id}/monitor?runs=${runIds.join(",")}`,
    result: (runId: string) => `/${id}/runs/${runId}/result`,
    compare: (runIds: string[]) => `/${id}/compare?runs=${runIds.join(",")}`,
  };
}

/**
 * 运行快照里的套件判别字段。GSM8K 与 Direct LLM 的运行都带 benchmark_provenance，
 * 唯一的区分依据是 provenance.suite；该字段之前落库的旧运行没有，按 gsm8k 兼容。
 */
function provenanceSuite(run: RunLike): string | null {
  const suite = run.manifest?.benchmark_provenance?.suite;
  return typeof suite === "string" ? suite : null;
}

const GSM8K_SUITE: EvalTypeSuite = {
  id: "gsm8k",
  label: "GSM8K 数学评测",
  icon: CalculatorIcon,
  matchRun: (run) => {
    const suite = provenanceSuite(run);
    if (suite !== null) return suite === "gsm8k";
    // 旧运行（无 suite 字段）：场景名或 benchmark_provenance 存在即归 GSM8K
    return /^gsm8k.*@\d+$/.test(run.scenario_version) || Boolean(run.manifest?.benchmark_provenance);
  },
};

const DIRECT_LLM_SUITE: EvalTypeSuite = {
  id: "direct-llm",
  label: "Direct LLM 评测",
  icon: ChatTextIcon,
  matchRun: (run) => provenanceSuite(run) === "direct-llm"
    || (provenanceSuite(run) === null && /^direct-llm([@-]).*/.test(run.scenario_version)),
};

const REPLAY_SUITE: EvalTypeSuite = {
  id: "replay",
  label: "Replay",
  icon: ClockCounterClockwiseIcon,
  matchRun: (run) =>
    run.scenario_version.startsWith("replay@") || run.scenario_version.startsWith("json_extract@"),
};

const AGENT_TASKS_SUITE: EvalTypeSuite = {
  id: "agent-tasks",
  label: "Agent 文件任务",
  icon: RobotIcon,
  matchRun: (run) =>
    provenanceSuite(run) === "agent-tasks"
    || /^file-report.*@\d+$/.test(run.scenario_version) === false
      && run.manifest?.agent === "builtin-agent@1",
};

const CEVAL_SUITE: EvalTypeSuite = {
  id: "ceval",
  label: "C-Eval 外部基准",
  icon: GraduationCapIcon,
  matchRun: (run) =>
    run.scenario_version.startsWith("ceval-external@")
      || run.manifest?.execution?.backend_id === "external-benchmark",
};

export const EVAL_SUITES: EvalTypeSuite[] = [
  AGENT_TASKS_SUITE,
  CEVAL_SUITE,
  GSM8K_SUITE,
  DIRECT_LLM_SUITE,
  REPLAY_SUITE,
];

export function suiteForRun(run: RunLike): EvalTypeSuite | null {
  return EVAL_SUITES.find((suite) => suite.matchRun(run)) ?? null;
}
