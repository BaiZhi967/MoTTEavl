import { Navigate, NavLink, Route, Routes } from "react-router-dom";
import { ActivityIcon, PlugIcon, PuzzlePieceIcon, StackIcon } from "@phosphor-icons/react";
import { RunsOverviewPage } from "./pages/RunsOverviewPage";
import { HarnessesPage, ProvidersPage } from "./pages/ResourcesPage";
import { EVAL_SUITES } from "./evalTypes/registry";
import { FallbackMonitorPage, FallbackResultPage } from "./evalTypes/fallback/FallbackPages";
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
import { ReplayOperate, ReplayMonitor, ReplayResult } from "./evalTypes/replay/ReplayPages";

const GENERAL_NAV = [
  { to: "/runs", label: "运行", icon: StackIcon },
  { to: "/providers", label: "Provider 与模型", icon: PlugIcon },
  { to: "/harnesses", label: "Agent / Harness", icon: PuzzlePieceIcon },
] as const;

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
          <p className="nav-group-label">评测类型</p>
          {EVAL_SUITES.map(({ id, label, icon: Icon }) => (
            <NavLink key={id} to={`/${id}`} className="tab">
              <Icon size={16} weight="bold" aria-hidden />
              <span>{label}</span>
            </NavLink>
          ))}
          <p className="nav-group-label">通用</p>
          {GENERAL_NAV.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} className="tab">
              <Icon size={16} weight="bold" aria-hidden />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>
      </aside>
      <div className="workbench">
        <Routes>
          <Route path="/" element={<Navigate to="/gsm8k" replace />} />
          <Route path="/runs" element={<RunsOverviewPage />} />
          <Route path="/providers" element={<ProvidersPage />} />
          <Route path="/harnesses" element={<HarnessesPage />} />
          <Route path="/agent-tasks" element={<AgentOperate />} />
          <Route path="/agent-tasks/monitor" element={<AgentMonitor />} />
          <Route path="/agent-tasks/runs/:runId/result" element={<AgentResult />} />
          <Route path="/agent-tasks/compare" element={<AgentCompare />} />
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
          <Route path="/runs/:runId/monitor" element={<FallbackMonitorPage />} />
          <Route path="/runs/:runId/result" element={<FallbackResultPage />} />
          <Route path="*" element={<Navigate to="/gsm8k" replace />} />
        </Routes>
      </div>
    </div>
  );
}
