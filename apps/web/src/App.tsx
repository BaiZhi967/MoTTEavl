import { Navigate, NavLink, Route, Routes } from "react-router-dom";
import { ActivityIcon, PlugIcon, PuzzlePieceIcon, StackIcon } from "@phosphor-icons/react";
import { RunsOverviewPage } from "./pages/RunsOverviewPage";
import { BenchmarksPage } from "./pages/BenchmarksPage";
import { HarnessesPage, ProvidersPage } from "./pages/ResourcesPage";
import { EVAL_SUITES } from "./evalTypes/registry";
import { FallbackMonitorPage, FallbackResultPage } from "./evalTypes/fallback/FallbackPages";
import { Gsm8kOperate } from "./evalTypes/gsm8k/Gsm8kOperate";
import { Gsm8kMonitor } from "./evalTypes/gsm8k/Gsm8kMonitor";
import { Gsm8kResult } from "./evalTypes/gsm8k/Gsm8kResult";

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
          <Route path="/" element={<Navigate to="/runs" replace />} />
          <Route path="/runs" element={<RunsOverviewPage />} />
          <Route path="/benchmarks" element={<BenchmarksPage />} />
          <Route path="/providers" element={<ProvidersPage />} />
          <Route path="/harnesses" element={<HarnessesPage />} />
          <Route path="/gsm8k" element={<Gsm8kOperate />} />
          <Route path="/gsm8k/monitor" element={<Gsm8kMonitor />} />
          <Route path="/gsm8k/runs/:runId/result" element={<Gsm8kResult />} />
          <Route path="/runs/:runId/monitor" element={<FallbackMonitorPage />} />
          <Route path="/runs/:runId/result" element={<FallbackResultPage />} />
          <Route path="*" element={<Navigate to="/runs" replace />} />
        </Routes>
      </div>
    </div>
  );
}
