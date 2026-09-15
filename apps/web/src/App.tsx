import { useState } from "react";
import { RunsPage } from "./pages/RunsPage";
import { HarnessesPage, ModelsPage, ProvidersPage } from "./pages/ResourcesPage";

const TABS = [
  { id: "runs", label: "运行" },
  { id: "providers", label: "Provider" },
  { id: "models", label: "模型" },
  { id: "harnesses", label: "Agent / Harness" },
] as const;

export default function App() {
  const [tab, setTab] = useState<(typeof TABS)[number]["id"]>("runs");
  return (
    <main>
      <header className="topbar">
        <h1>MoTTEavl 评测控制台</h1>
        <nav>
          {TABS.map((item) => (
            <button key={item.id} className={tab === item.id ? "tab active" : "tab"} onClick={() => setTab(item.id)}>
              {item.label}
            </button>
          ))}
        </nav>
      </header>
      {tab === "runs" && <RunsPage />}
      {tab === "providers" && <ProvidersPage />}
      {tab === "models" && <ModelsPage />}
      {tab === "harnesses" && <HarnessesPage />}
    </main>
  );
}
