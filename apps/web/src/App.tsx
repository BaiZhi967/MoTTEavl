import * as Tabs from "@radix-ui/react-tabs";
import { ActivityIcon, CubeIcon, PlayIcon, PlugIcon, PuzzlePieceIcon } from "@phosphor-icons/react";
import { RunsPage } from "./pages/RunsPage";
import { HarnessesPage, ModelsPage, ProvidersPage } from "./pages/ResourcesPage";

const TABS = [
  { id: "runs", label: "运行", icon: PlayIcon },
  { id: "providers", label: "Provider", icon: PlugIcon },
  { id: "models", label: "模型", icon: CubeIcon },
  { id: "harnesses", label: "Agent / Harness", icon: PuzzlePieceIcon },
] as const;

export default function App() {
  return (
    <Tabs.Root defaultValue="runs" orientation="vertical" className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <h1>
            <ActivityIcon size={18} weight="bold" aria-hidden />
            MoTTEavl
          </h1>
          <p>评测控制台</p>
        </div>
        <Tabs.List className="side-nav" aria-label="主导航">
          {TABS.map(({ id, label, icon: Icon }) => (
            <Tabs.Trigger key={id} value={id} className="tab">
              <Icon size={16} weight="bold" aria-hidden />
              <span>{label}</span>
            </Tabs.Trigger>
          ))}
        </Tabs.List>
      </aside>
      <div className="workbench">
        <Tabs.Content value="runs" className="tab-panel">
          <RunsPage />
        </Tabs.Content>
        <Tabs.Content value="providers" className="tab-panel">
          <ProvidersPage />
        </Tabs.Content>
        <Tabs.Content value="models" className="tab-panel">
          <ModelsPage />
        </Tabs.Content>
        <Tabs.Content value="harnesses" className="tab-panel">
          <HarnessesPage />
        </Tabs.Content>
      </div>
    </Tabs.Root>
  );
}
