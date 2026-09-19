import { makeExternalPages, scopeLabel } from "../external/ExternalBenchmarkPages";

const pages = makeExternalPages("ceval", {
  title: "C-Eval（job-based 外部基准）",
  operateAria: "C-Eval 外部基准",
  preparePlaceholder: "每行一题 JSON（id/subject/question/A-D/answer；answer 缺省=unscored）",
});

/** C-Eval 五页：通用外部基准组件的 ceval 实例（review R16 共用骨架）。 */
export const CevalOperate = pages.Operate;
export const CevalCases = pages.Cases;
export const CevalMonitor = pages.Monitor;
export const CevalResult = pages.Result;
export const CevalCompare = pages.Compare;

export { scopeLabel };
