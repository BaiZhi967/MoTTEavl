import type { ComponentType } from "react";
import { CalculatorIcon, ChatTextIcon, ClockCounterClockwiseIcon, GraduationCapIcon, RobotIcon, TerminalWindowIcon, type IconProps } from "@phosphor-icons/react";

export interface RunLike {
  scenario_version: string;
  manifest?: any;
}

/** 套件顶栏分段页签（DESIGN.md 3.2 两级导航）。result 落在运行总览并按套件标签过滤。 */
export interface SuiteTab {
  key: string;
  label: string;
  to: string;
}

export interface EvalTypeSuite {
  id: string;
  label: string;
  icon: ComponentType<IconProps>;
  tabs: SuiteTab[];
  matchRun(run: RunLike): boolean;
}

/** 标准五段页签：操作 / 题目 / 监控 / 结果 / 对比 */
function standardTabs(id: string, label: string): SuiteTab[] {
  return [
    { key: "operate", label: "操作", to: `/${id}` },
    { key: "cases", label: "题目", to: `/${id}/cases` },
    { key: "monitor", label: "监控", to: `/${id}/monitor` },
    { key: "result", label: "结果", to: `/runs?type=${encodeURIComponent(label)}` },
    { key: "compare", label: "对比", to: `/${id}/compare` },
  ];
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
  tabs: standardTabs("gsm8k", "GSM8K 数学评测"),
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
  tabs: standardTabs("direct-llm", "Direct LLM 评测"),
  matchRun: (run) => provenanceSuite(run) === "direct-llm"
    || (provenanceSuite(run) === null && /^direct-llm([@-]).*/.test(run.scenario_version)),
};

const REPLAY_SUITE: EvalTypeSuite = {
  id: "replay",
  label: "Replay",
  icon: ClockCounterClockwiseIcon,
  tabs: [
    { key: "operate", label: "操作", to: "/replay" },
    { key: "monitor", label: "监控", to: "/replay/monitor" },
    { key: "result", label: "结果", to: `/runs?type=${encodeURIComponent("Replay")}` },
  ],
  matchRun: (run) =>
    run.scenario_version.startsWith("replay@") || run.scenario_version.startsWith("json_extract@"),
};

const AGENT_TASKS_SUITE: EvalTypeSuite = {
  id: "agent-tasks",
  label: "Agent 文件任务",
  icon: RobotIcon,
  tabs: [
    { key: "operate", label: "操作", to: "/agent-tasks" },
    { key: "monitor", label: "监控", to: "/agent-tasks/monitor" },
    { key: "result", label: "结果", to: `/runs?type=${encodeURIComponent("Agent 文件任务")}` },
    { key: "compare", label: "对比", to: "/agent-tasks/compare" },
  ],
  matchRun: (run) =>
    provenanceSuite(run) === "agent-tasks"
    || /^file-report.*@\d+$/.test(run.scenario_version) === false
      && run.manifest?.agent === "builtin-agent@1",
};

const CEVAL_SUITE: EvalTypeSuite = {
  id: "ceval",
  label: "C-Eval 外部基准",
  icon: GraduationCapIcon,
  tabs: standardTabs("ceval", "C-Eval 外部基准"),
  matchRun: (run) =>
    run.scenario_version.startsWith("ceval-external@")
    || run.manifest?.execution?.backend_id === "external-benchmark",
};

const CMMLU_SUITE: EvalTypeSuite = {
  id: "cmmlu",
  label: "CMMLU 外部基准",
  icon: GraduationCapIcon,
  tabs: standardTabs("cmmlu", "CMMLU 外部基准"),
  matchRun: (run) => run.scenario_version.startsWith("cmmlu-external@"),
};

/**
 * Terminal-Bench（Harbor）运行：场景名以 terminal-bench 开头，或执行后端是
 * harbor-external。必须排在 CEVAL 之前——ceval 的兜底条件（backend_id ===
 * "external-benchmark"）会吞掉同后端的 Harbor 运行，先判 Harbor 才能拿到正确归属。
 */
export const TERMINAL_BENCH_SUITE: EvalTypeSuite = {
  id: "terminal-bench",
  label: "Terminal-Bench（Harbor）",
  icon: TerminalWindowIcon,
  tabs: [
    { key: "operate", label: "操作", to: "/terminal-bench" },
    { key: "tasks", label: "任务", to: "/terminal-bench/tasks" },
    { key: "monitor", label: "监控", to: "/terminal-bench/monitor" },
    { key: "result", label: "结果", to: `/runs?type=${encodeURIComponent("Terminal-Bench（Harbor）")}` },
    { key: "compare", label: "对比", to: "/terminal-bench/compare" },
  ],
  matchRun: (run) =>
    run.scenario_version.startsWith("terminal-bench")
    || run.manifest?.execution?.backend_id === "harbor-external",
};

const RUNTIMES_SUITE: EvalTypeSuite = {
  id: "runtimes",
  label: "外部 Runtime",
  icon: TerminalWindowIcon,
  tabs: [
    { key: "operate", label: "操作", to: "/runtimes" },
    { key: "monitor", label: "监控", to: "/runtimes/monitor" },
  ],
  matchRun: (run) => typeof run.manifest?.runtime === "string" && run.manifest.runtime.length > 0,
};

export const EVAL_SUITES: EvalTypeSuite[] = [
  AGENT_TASKS_SUITE,
  CMMLU_SUITE,
  TERMINAL_BENCH_SUITE,
  CEVAL_SUITE,
  GSM8K_SUITE,
  DIRECT_LLM_SUITE,
  REPLAY_SUITE,
  RUNTIMES_SUITE,
];

export function suiteForRun(run: RunLike): EvalTypeSuite | null {
  return EVAL_SUITES.find((suite) => suite.matchRun(run)) ?? null;
}
