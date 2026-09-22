import { lazy, Suspense, useState, type ComponentType, type FormEvent } from "react";
import { Navigate, NavLink, Route, Routes, useLocation, Link } from "react-router-dom";
import * as Dialog from "@radix-ui/react-dialog";
import {
  ActivityIcon,
  ArrowsLeftRightIcon,
  CaretDownIcon,
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
  type IconProps,
} from "@phosphor-icons/react";
import { RunsOverviewPage } from "./pages/RunsOverviewPage";
const ResourcesModule = () => import("./pages/ResourcesPage");
const ProvidersPage = lazyNamed(ResourcesModule, "ProvidersPage");
const HarnessesPage = lazyNamed(ResourcesModule, "HarnessesPage");
const JudgesPage = lazyNamed(() => import("./pages/judges/JudgesPage"), "JudgesPage");
import { BaselinesPage } from "./pages/m6/BaselinesPage";
import { ComparePage } from "./pages/m6/ComparePage";
import { ExperimentsPage } from "./pages/m6/ExperimentsPage";
import { GatePage } from "./pages/m6/GatePage";
const ScenarioModule = () => import("./evalTypes/scenario/ScenarioPages");
const ScenarioWorkflowsPage = lazyNamed(ScenarioModule, "ScenarioWorkflowsPage");
const ScenarioRunStepsPage = lazyNamed(ScenarioModule, "ScenarioRunStepsPage");
const SkillModule = () => import("./evalTypes/skill/SkillPages");
const SkillValidationPage = lazyNamed(SkillModule, "SkillValidationPage");
const SkillComparePage = lazyNamed(SkillModule, "SkillComparePage");
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
const DirectLlmOperate = lazyNamed(() => import("./evalTypes/directllm/DirectLlmOperate"), "DirectLlmOperate");
import { DirectLlmResult } from "./evalTypes/directllm/DirectLlmResult";
import { AgentCompare, AgentMonitor, AgentOperate, AgentResult } from "./evalTypes/agent/AgentPages";
import { HarnessMonitor, HarnessOperate } from "./evalTypes/harness/HarnessPages";
import { ReplayOperate, ReplayMonitor, ReplayResult } from "./evalTypes/replay/ReplayPages";
const TerminalBenchPagesModule = () => import("./evalTypes/terminalbench/TerminalBenchPages");
const TerminalBenchOperate = lazyNamed(TerminalBenchPagesModule, "TerminalBenchOperate");
const TerminalBenchTasks = lazyNamed(TerminalBenchPagesModule, "TerminalBenchTasks");
const TerminalBenchMonitor = lazyNamed(TerminalBenchPagesModule, "TerminalBenchMonitor");
const TerminalBenchResult = lazyNamed(TerminalBenchPagesModule, "TerminalBenchResult");
const TerminalBenchCompare = lazyNamed(TerminalBenchPagesModule, "TerminalBenchCompare");
import { getApiToken, setApiToken } from "./api/client";

/** 路由级代码分割：把重页面从首屏 chunk 里切出去。
 *  实测（pnpm build）：首屏 app chunk 90KB gzip 是单个最大块，其中大半是只被某个路由用到的页面模块。
 *  用具名导出需要这层映射——lazy 只接受 default。 */
function lazyNamed(loader: () => Promise<Record<string, unknown>>, name: string) {
  return lazy(async () => {
    const mod = await loader();
    return { default: mod[name] as ComponentType };
  });
}
import { useSessionActivity } from "./hooks/useSessionActivity";

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


/** 侧栏分组（DESIGN.md 3.2）：四个分区入口，套件从注册表生成。
 *  分组只声明一次——侧栏渲染与顶栏面包屑都从这里取，不留两份真相。 */
type NavItem = { to: string; label: string; icon: ComponentType<IconProps> };
const NAV_GROUPS: ReadonlyArray<{ label: string; items: ReadonlyArray<NavItem> }> = [
  { label: "总览", items: OVERVIEW_NAV },
  {
    label: "评测套件",
    items: EVAL_SUITES.map((suite) => ({ to: "/" + suite.id, label: suite.label, icon: suite.icon })),
  },
  { label: "资源", items: RESOURCE_NAV },
  { label: "实验体系", items: EXPERIMENT_NAV },
];

/** 当前路径属于哪个分区、哪个入口；顶栏左侧据此渲染「分区 / 页面」 */
function sectionForPath(pathname: string): { group: string; item?: NavItem } {
  for (const group of NAV_GROUPS) {
    for (const item of group.items) {
      if (pathname === item.to || pathname.startsWith(item.to + "/")) {
        return { group: group.label, item };
      }
    }
  }
  return { group: "控制台" };
}

const NAV_COLLAPSE_KEY = "motteavl.nav.collapsed";

function readCollapsed(): string[] {
  try {
    const raw = window.localStorage.getItem(NAV_COLLAPSE_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return [];
  }
}

function suiteForPath(pathname: string): EvalTypeSuite | null {
  const segment = pathname.split("/")[1] ?? "";
  return EVAL_SUITES.find((suite) => suite.id === segment) ?? null;
}

/** 路由级懒加载的落位：写清在等什么，不用无意义转圈（诚实优先） */
function BoardLoading() {
  return (
    <div className="page">
      <div className="board-loading" role="status" aria-live="polite">
        <span className="led led-live" aria-hidden />
        正在装载板面…
      </div>
    </div>
  );
}

/** 会话心跳（顶栏右区）：进行中的运行数；读不到写「未知」，不填 0（DESIGN.md 9） */
function SessionStatus({ running }: { running: number | null }) {
  const live = running !== null && running > 0;
  return (
    <span
      className={"session-status" + (live ? " is-live" : "")}
      title="进行中的运行数 · 每 10 秒刷新，页面不可见时暂停"
    >
      <span className={"led" + (live ? " led-live" : "")} aria-hidden />
      <span>进行中</span>
      <b>{running === null ? "未知" : running}</b>
    </span>
  );
}

/** 顶栏（DESIGN.md 3.1）：左身份（分区 / 套件 + 页面标题），中段分段页签，右会话心跳 */
function TopBar() {
  const location = useLocation();
  const suite = suiteForPath(location.pathname);
  const running = useSessionActivity();

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
        <div className="topbar-right">
          <SessionStatus running={running} />
        </div>
      </header>
    );
  }

  const section = sectionForPath(location.pathname);
  return (
    <header className="topbar">
      <span className="crumb">{section.group}</span>
      {section.item ? <span className="topbar-title">{section.item.label}</span> : null}
      <div className="topbar-right">
        <SessionStatus running={running} />
      </div>
    </header>
  );
}

/** 侧栏（DESIGN.md 3.2）：分组可折叠、导航区内部滚动，入口再多也挤不走底部状态条 */
function SideNav() {
  const [collapsed, setCollapsed] = useState<string[]>(readCollapsed);

  const toggle = (label: string) => {
    setCollapsed((current) => {
      const next = current.includes(label)
        ? current.filter((item) => item !== label)
        : [...current, label];
      try {
        window.localStorage.setItem(NAV_COLLAPSE_KEY, JSON.stringify(next));
      } catch {
        // 隐私模式写不进去就只在本次会话内生效，不阻断导航
      }
      return next;
    });
  };

  return (
    <nav className="side-nav" aria-label="主导航">
      {NAV_GROUPS.map((group) => {
        const expanded = !collapsed.includes(group.label);
        return (
          <div className="nav-group" key={group.label}>
            <button
              type="button"
              className="nav-group-label"
              aria-expanded={expanded}
              onClick={() => toggle(group.label)}
            >
              <span>{group.label}</span>
              <CaretDownIcon className="nav-group-caret" size={11} weight="bold" aria-hidden />
            </button>
            {expanded ? (
              <div className="nav-group-items">
                {group.items.map(({ to, label, icon: ItemIcon }) => (
                  <NavLink key={to} to={to} className="tab">
                    <ItemIcon size={16} weight="bold" aria-hidden />
                    <span>{label}</span>
                  </NavLink>
                ))}
              </div>
            ) : null}
          </div>
        );
      })}
    </nav>
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
          {/* 构建标识：截图自带的"这是哪个构建"证据（见 vite.config.mts） */}
          <span className="build-stamp" title="当前前端构建">{__BUILD_ID__}</span>
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
        <SideNav />
        <ApiTokenBar />
      </aside>
      <div className="workbench">
        <TopBar />
        <Suspense fallback={<BoardLoading />}>
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
        </Suspense>
      </div>
    </div>
  );
}
