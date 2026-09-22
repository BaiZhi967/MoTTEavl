import { useState, type FormEvent } from "react";
import { Navigate, NavLink, Route, Routes, useLocation, Link } from "react-router-dom";
import * as Dialog from "@radix-ui/react-dialog";
import {
  ActivityIcon,
  ArrowsLeftRightIcon,
  FlowArrowIcon,
  FlagCheckeredIcon,
  FlaskIcon,
  MagicWandIcon,
  PlugIcon,
  PuzzlePieceIcon,
  PushPinIcon,
  ScalesIcon,
  StackIcon,
  XIcon,
} from "@phosphor-icons/react";
import { RunsOverviewPage } from "./pages/RunsOverviewPage";
import { HarnessesPage, ProvidersPage } from "./pages/ResourcesPage";
import { JudgesPage } from "./pages/judges/JudgesPage";
import { BaselinesPage } from "./pages/m6/BaselinesPage";
import { ComparePage } from "./pages/m6/ComparePage";
import { ExperimentsPage } from "./pages/m6/ExperimentsPage";
import { GatePage } from "./pages/m6/GatePage";
import { ScenarioRunStepsPage, ScenarioWorkflowsPage } from "./evalTypes/scenario/ScenarioPages";
import { SkillComparePage, SkillValidationPage } from "./evalTypes/skill/SkillPages";
import { EVAL_SUITES, type EvalTypeSuite } from "./evalTypes/registry";
import { FallbackMonitorPage, FallbackResultPage } from "./evalTypes/fallback/FallbackPages";
import { CevalCases } from "./evalTypes/ceval/CevalPages";
import { CevalCompare } from "./evalTypes/ceval/CevalPages";
import { CevalMonitor } from "./evalTypes/ceval/CevalPages";
import { CevalOperate } from "./evalTypes/ceval/CevalPages";
import { CevalResult } from "./evalTypes/ceval/CevalPages";
import { makeExternalPages } from "./evalTypes/external/ExternalBenchmarkPages";
import { Gsm8kCases } from "./evalTypes/gsm8k/Gsm8kCases";
import { Gsm8kCompare } from "./evalTypes/gsm8k/Gsm8kCompare";
import { Gsm8kOperate } from "./evalTypes/gsm8k/Gsm8kOperate";
import { Gsm8kMonitor } from "./evalTypes/gsm8k/Gsm8kMonitor";
import { Gsm8kResult } from "./evalTypes/gsm8k/Gsm8kResult";
import { DirectLlmCases } from "./evalTypes/directllm/DirectLlmCases";
import { DirectLlmCompare } from "./evalTypes/directllm/DirectLlmCompare";
import { DirectLlmMonitor } from "./evalTypes/directllm/DirectLlmMonitor";
import { DirectLlmOperate } from "./evalTypes/directllm/DirectLlmOperate";
import { DirectLlmResult } from "./evalTypes/directllm/DirectLlmResult";
import { AgentCompare, AgentMonitor, AgentOperate, AgentResult } from "./evalTypes/agent/AgentPages";
import { HarnessMonitor, HarnessOperate } from "./evalTypes/harness/HarnessPages";
import { ReplayOperate, ReplayMonitor, ReplayResult } from "./evalTypes/replay/ReplayPages";
import {
  TerminalBenchCompare,
  TerminalBenchMonitor,
  TerminalBenchOperate,
  TerminalBenchResult,
  TerminalBenchTasks,
} from "./evalTypes/terminalbench/TerminalBenchPages";
import { getApiToken, setApiToken } from "./api/client";

const CmmluPages = makeExternalPages("cmmlu", {
  title: "CMMLU（独立身份的外部基准）",
  operateAria: "CMMLU 外部基准",
  preparePlaceholder: "每行一题 JSON（id/subject/question/A-D/answer；subject 需在 CMMLU 67 学科清单）",
});

/* 两级导航（DESIGN.md 3.2）：侧栏只放分区入口，套件五段页签由顶栏承担 */

const OVERVIEW_NAV = [{ to: "/runs", label: "运行总览", icon: StackIcon }] as const;

const RESOURCE_NAV = [
  { to: "/providers", label: "Provider 与模型", icon: PlugIcon },
  { to: "/harnesses", label: "Agent / Harness", icon: PuzzlePieceIcon },
  { to: "/scenario", label: "场景 Workflow", icon: FlowArrowIcon },
  { to: "/skill", label: "Skill 校验", icon: MagicWandIcon },
  { to: "/judges", label: "Judge 校准", icon: ScalesIcon },
] as const;

const EXPERIMENT_NAV = [
  { to: "/experiments", label: "实验", icon: FlaskIcon },
  { to: "/compare", label: "比较", icon: ArrowsLeftRightIcon },
  { to: "/baselines", label: "基线", icon: PushPinIcon },
  { to: "/gate", label: "门禁", icon: FlagCheckeredIcon },
] as const;

const GENERAL_NAV = [...OVERVIEW_NAV, ...RESOURCE_NAV, ...EXPERIMENT_NAV] as const;

function suiteForPath(pathname: string): EvalTypeSuite | null {
  const segment = pathname.split("/")[1] ?? "";
  return EVAL_SUITES.find((suite) => suite.id === segment) ?? null;
}

/** 顶栏（DESIGN.md 3.1）：左面包屑，中段套件分段页签（全局页为页面标题），右留白给会话级操作 */
function TopBar() {
  const location = useLocation();
  const suite = suiteForPath(location.pathname);

  const isTabActive = (to: string) => {
    const [path, query] = to.split("?");
    if (location.pathname !== path) return false;
    if (!query) return true;
    const [key, value] = query.split("=");
    return new URLSearchParams(location.search).get(key) === decodeURIComponent(value);
  };

  if (suite) {
    return (
      <header className="topbar">
        <span className="crumb">
          评测套件 / <b>{suite.id}</b>
        </span>
        <nav className="tabs-list topbar-tabs" aria-label={`${suite.label}分段页签`}>
          {suite.tabs.map((tab) => (
            <Link
              key={tab.key}
              to={tab.to}
              aria-current={isTabActive(tab.to) ? "page" : undefined}
              className={`tabs-trigger${isTabActive(tab.to) ? " tabs-trigger-active" : ""}`}
            >
              {tab.label}
            </Link>
          ))}
        </nav>
      </header>
    );
  }

  const current = GENERAL_NAV.find((item) => location.pathname.startsWith(item.to));
  return (
    <header className="topbar">
      <span className="crumb">
        通用 / <b>{location.pathname.split("/")[1] || "runs"}</b>
      </span>
      {current ? <span className="topbar-title">{current.label}</span> : null}
    </header>
  );
}

/** 侧栏底部连接状态条：LED + mono 文案，点击打开 API 认证设置 */
function ApiTokenBar() {
  const [open, setOpen] = useState(false);
  const [token, setToken] = useState("");
  const configured = getApiToken() !== "";

  const save = (event: FormEvent) => {
    event.preventDefault();
    setApiToken(token);
    setOpen(false);
    window.location.reload();
  };

  return (
    <Dialog.Root open={open} onOpenChange={(next) => {
      setOpen(next);
      if (next) setToken(getApiToken());
    }}>
      <Dialog.Trigger asChild>
        <button type="button" className="sidebar-foot">
          <span className={`led${configured ? " led-on" : ""}`} aria-hidden />
          <span>{configured ? "API 已认证" : "API 未认证"}</span>
        </button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="dialog-content" aria-describedby="api-token-description">
          <div className="dialog-header">
            <Dialog.Title className="dialog-title">API Bearer token</Dialog.Title>
            <Dialog.Close asChild>
              <button type="button" className="icon-button" aria-label="关闭 API 认证设置">
                <XIcon size={16} weight="bold" aria-hidden />
              </button>
            </Dialog.Close>
          </div>
          <Dialog.Description id="api-token-description" className="hint">
            令牌仅保存在当前标签页会话中，并用于普通请求和事件流。留空可清除。
          </Dialog.Description>
          <form onSubmit={save}>
            <label htmlFor="api-token">
              Bearer token
              <input
                id="api-token"
                type="password"
                autoComplete="off"
                value={token}
                onChange={(event) => setToken(event.target.value)}
              />
            </label>
            <div className="actions">
              <button type="submit" className="primary">应用并重新连接</button>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export default function App() {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <h1>
            <ActivityIcon size={18} weight="bold" aria-hidden />
            MoTTEavl
          </h1>
          <p>评测控制台</p>
        </div>
        <nav className="side-nav" aria-label="主导航">
          <p className="nav-group-label">总览</p>
          {OVERVIEW_NAV.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} className="tab">
              <Icon size={16} weight="bold" aria-hidden />
              <span>{label}</span>
            </NavLink>
          ))}
          <p className="nav-group-label">评测套件</p>
          {EVAL_SUITES.map(({ id, label, icon: Icon }) => (
            <NavLink key={id} to={`/${id}`} className="tab">
              <Icon size={16} weight="bold" aria-hidden />
              <span>{label}</span>
            </NavLink>
          ))}
          <p className="nav-group-label">资源</p>
          {RESOURCE_NAV.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} className="tab">
              <Icon size={16} weight="bold" aria-hidden />
              <span>{label}</span>
            </NavLink>
          ))}
          <p className="nav-group-label">实验体系</p>
          {EXPERIMENT_NAV.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} className="tab">
              <Icon size={16} weight="bold" aria-hidden />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <ApiTokenBar />
      </aside>
      <div className="workbench">
        <TopBar />
        <Routes>
          <Route path="/" element={<Navigate to="/gsm8k" replace />} />
          <Route path="/runs" element={<RunsOverviewPage />} />
          <Route path="/providers" element={<ProvidersPage />} />
          <Route path="/harnesses" element={<HarnessesPage />} />
          <Route path="/scenario" element={<ScenarioWorkflowsPage />} />
          <Route path="/scenario/runs/:runId" element={<ScenarioRunStepsPage />} />
          <Route path="/skill" element={<SkillValidationPage />} />
          <Route path="/skill/compare" element={<SkillComparePage />} />
          <Route path="/judges" element={<JudgesPage />} />
          <Route path="/experiments" element={<ExperimentsPage />} />
          <Route path="/compare" element={<ComparePage />} />
          <Route path="/baselines" element={<BaselinesPage />} />
          <Route path="/gate" element={<GatePage />} />
          <Route path="/agent-tasks" element={<AgentOperate />} />
          <Route path="/runtimes" element={<HarnessOperate />} />
          <Route path="/runtimes/monitor" element={<HarnessMonitor />} />
          <Route path="/agent-tasks/monitor" element={<AgentMonitor />} />
          <Route path="/agent-tasks/runs/:runId/result" element={<AgentResult />} />
          <Route path="/agent-tasks/compare" element={<AgentCompare />} />
          <Route path="/ceval" element={<CevalOperate />} />
          <Route path="/ceval/cases" element={<CevalCases />} />
          <Route path="/ceval/monitor" element={<CevalMonitor />} />
          <Route path="/ceval/runs/:runId/result" element={<CevalResult />} />
          <Route path="/ceval/compare" element={<CevalCompare />} />
          <Route path="/cmmlu" element={<CmmluPages.Operate />} />
          <Route path="/cmmlu/cases" element={<CmmluPages.Cases />} />
          <Route path="/cmmlu/monitor" element={<CmmluPages.Monitor />} />
          <Route path="/cmmlu/runs/:runId/result" element={<CmmluPages.Result />} />
          <Route path="/cmmlu/compare" element={<CmmluPages.Compare />} />
          <Route path="/gsm8k" element={<Gsm8kOperate />} />
          <Route path="/gsm8k/cases" element={<Gsm8kCases />} />
          <Route path="/gsm8k/monitor" element={<Gsm8kMonitor />} />
          <Route path="/gsm8k/runs/:runId/result" element={<Gsm8kResult />} />
          <Route path="/gsm8k/compare" element={<Gsm8kCompare />} />
          <Route path="/direct-llm" element={<DirectLlmOperate />} />
          <Route path="/direct-llm/cases" element={<DirectLlmCases />} />
          <Route path="/direct-llm/monitor" element={<DirectLlmMonitor />} />
          <Route path="/direct-llm/runs/:runId/result" element={<DirectLlmResult />} />
          <Route path="/direct-llm/compare" element={<DirectLlmCompare />} />
          <Route path="/replay" element={<ReplayOperate />} />
          <Route path="/replay/monitor" element={<ReplayMonitor />} />
          <Route path="/replay/runs/:runId/result" element={<ReplayResult />} />
          <Route path="/terminal-bench" element={<TerminalBenchOperate />} />
          <Route path="/terminal-bench/tasks" element={<TerminalBenchTasks />} />
          <Route path="/terminal-bench/monitor" element={<TerminalBenchMonitor />} />
          <Route path="/terminal-bench/runs/:runId/result" element={<TerminalBenchResult />} />
          <Route path="/terminal-bench/compare" element={<TerminalBenchCompare />} />
          <Route path="/runs/:runId/monitor" element={<FallbackMonitorPage />} />
          <Route path="/runs/:runId/result" element={<FallbackResultPage />} />
          <Route path="*" element={<Navigate to="/gsm8k" replace />} />
        </Routes>
      </div>
    </div>
  );
}
