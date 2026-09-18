# 评测类型专区控制台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Web 控制台重构为按评测类型（GSM8K / Direct LLM / Replay）注册专属「操作 / 过程 / 结果」三页的类型专区平台，含多模型批量发起与对比、运行总览，后端仅补 runs 列表模型摘要。

**Architecture:** react-router 多页路由替代 Radix Tabs 骨架；`evalTypes/registry.ts` 类型套件注册表驱动侧边栏与 run 归属路由；公共底座（ModelPicker、useRunEvents、批次监控、钻取表）与类型专属页面分层；多模型 = 前端循环创建 run，批次只是 URL query。

**Tech Stack:** React 19 + Vite 8 + react-router-dom 7（新增）+ Radix UI（现有）+ Phosphor Icons + vitest + happy-dom；后端 FastAPI（仅 `list_runs` 增量）。

**Spec:** `docs/superpowers/specs/2026-09-18-eval-type-suite-console-design.md`

## Global Constraints

- 样式取值只允许 `var(--…)` token（`apps/web/src/index.css` `:root`）；禁止组件/CSS 里写十六进制色值。
- 运行状态中文标签与语气唯一来源 `apps/web/src/components/statusMeta.ts`；禁止组件里手写状态颜色。
- 图标用 `@phosphor-icons/react` Bold 字重，导入名带 `Icon` 后缀；禁止 emoji 图标、Lucide/Feather/Heroicons。
- 交互原语只用 Radix UI 与原生元素；禁止 Ant Design / MUI / shadcn 等带视觉主见的库（react-router 是路由库，允许）。
- UI 文案中文；术语（Provider、Case、Manifest）保留英文。文案禁止 em-dash。
- 完工门禁：`pnpm --dir apps/web test` 与 `pnpm --dir apps/web build` 全绿；改后端时 `uv run pytest -q -m "not live"` 相关文件通过。
- 提交信息 conventional 风格（`feat(web):` / `feat(api):` / `docs:`）；文档与对应代码同一提交。
- API 契约：`POST /api/v1/runs` 请求体 `{scenario_version, manifest?: {model?: string, provider?: object|string, parameters?: object}, case_ids?: string[]}`；GSM8K 走 `POST /api/v1/benchmarks/gsm8k/runs` `{model: string, scenario?: string, parameters?: object}`；`manifest.model` 由后端在创建期展开为 provider 快照（`packages/sdk-python/motte_sdk/resolve.py`）。
- GSM8K 分数行形状（`getRun().scores[]`）：`{case_id, outcome: "correct"|"wrong"|"not_attempted"|…, passed: boolean, attempted, responded, scorer_version}`；期望答案在 `run.manifest.benchmark_snapshot.dataset.cases[].expected`，题面在同处 `.input`；模型信封在 `run.cases[].result`（`content` / `error` / `usage`）。

## File Structure

```
apps/web/src/
  main.tsx                        # 改：挂 BrowserRouter
  App.tsx                         # 改：路由表 + 侧边栏（registry 驱动）
  api/client.ts                   # 改：RunRecord 补 model 可选字段
  index.css                       # 改：逐步新增组件类（各任务内提交）
  hooks/useRunEvents.ts           # 新：SSE hook + 进度聚合纯函数
  evalTypes/registry.ts           # 新：套件接口、注册表、suiteRoutes
  evalTypes/gsm8k/Gsm8kOperate.tsx       # 新
  evalTypes/gsm8k/Gsm8kMonitor.tsx       # 新
  evalTypes/gsm8k/Gsm8kResult.tsx        # 新
  evalTypes/gsm8k/Gsm8kCompare.tsx       # 新
  evalTypes/gsm8k/grid.ts                # 新：格子状态纯函数
  evalTypes/directllm/DirectLlmPages.tsx # 新：三页同文件（页面小）
  evalTypes/replay/ReplayPages.tsx       # 新：三页同文件
  evalTypes/fallback/FallbackPages.tsx   # 新：通用兜底 Monitor/Result
  components/ModelPicker.tsx      # 新：多选模型选择器
  components/BatchMonitor.tsx     # 新：批次过程页通用骨架
  components/MetricCards.tsx      # 新：指标卡行
  components/CaseDrillTable.tsx   # 新：钻取表
  components/RunProgress.tsx      # 新：x/N 进度条
  pages/RunsOverviewPage.tsx      # 新：运行总览（替代 RunsPage）
  pages/RunsPage.tsx              # 删（Task 13）
  pages/BenchmarksPage.tsx        # 删（Task 7 迁移后，Task 13 删文件）
  routes/index.ts                 # 删（Task 2，未被引用）
apps/web/tests/
  app.test.tsx                    # 新：骨架与路由
  hooks.test.ts                   # 新：useRunEvents / countDone
  registry.test.ts                # 新：match 规则
  evalTypes.test.tsx              # 新：套件页面
  components.test.tsx             # 改：ModelPicker / 钻取表 / 对比等
apps/api/app/main.py              # 改：list_runs 模型摘要
tests/api/test_runs.py            # 改：新增断言
apps/web/DESIGN.md                # 改：Task 13 与代码同提交
```

---

### Task 1: API 运行列表补模型摘要

**Files:**
- Modify: `apps/api/app/main.py`（`list_runs`，当前 `apps/api/app/main.py:157-162`）
- Test: `tests/api/test_runs.py`

**Interfaces:**
- Produces: `GET /api/v1/runs` 与 `GET /api/v1/runs?status=…` 的每个列表项新增 `"model": string | null` 字段（后续 Task 6 运行总览与 Task 10 对比页消费；单 run 端点 `GET /api/v1/runs/{id}` 不加，避免改 `_view`）。

- [ ] **Step 1: Write the failing test**

追加到 `tests/api/test_runs.py`：

```python
def test_list_runs_includes_model_summary():
    from motte_storage.resource_store import InMemoryResourceStore

    resources = InMemoryResourceStore()
    resources.providers.put({
        "name": "local", "kind": "openai_compatible",
        "base_url": "http://localhost:8001/v1",
    })
    resources.models.put({
        "id": "qwen2.5-7b", "provider": "local", "capabilities": {},
    })
    client = TestClient(create_app(InMemoryRunStore(), resource_store=resources))
    model_run = client.post("/api/v1/runs", json={
        "scenario_version": "direct-llm@1",
        "manifest": {"model": "qwen2.5-7b"},
        "case_ids": ["case-1"],
    }).json()
    inline_run = client.post("/api/v1/runs", json={
        "scenario_version": "replay@1",
        "manifest": {"provider": {"kind": "replay", "model": "fixture-model"}},
        "case_ids": ["case-1"],
    }).json()
    bare_run = client.post("/api/v1/runs", json={"scenario_version": "replay@1"}).json()

    items = {run["id"]: run for run in client.get("/api/v1/runs").json()["items"]}
    assert items[model_run["id"]]["model"] == "qwen2.5-7b"
    assert items[inline_run["id"]]["model"] == "fixture-model"
    assert items[bare_run["id"]]["model"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/api/test_runs.py::test_list_runs_includes_model_summary -q`
Expected: FAIL with `KeyError: 'model'`

- [ ] **Step 3: Write minimal implementation**

`apps/api/app/main.py` 中替换 `list_runs`：

```python
    @application.get("/api/v1/runs")
    def list_runs(status: str | None = None):
        runs = service.store.runs.list()
        if status is not None:
            runs = [run for run in runs if run.get("status") == status]
        items = [{**run, "model": _run_model_label(run)} for run in runs]
        return {"items": items, "total": len(items)}
```

模块底部（`_gsm8k_accuracy` 附近）新增：

```python
def _run_model_label(run: dict[str, Any]) -> str | None:
    """列表项模型摘要：模型档案引用 > 展开快照 > inline provider 字段。"""
    manifest = run.get("manifest") or {}
    if isinstance(manifest.get("model"), str):
        return manifest["model"]
    provider = manifest.get("provider")
    if isinstance(provider, dict) and isinstance(provider.get("model"), str):
        return provider["model"]
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/api/test_runs.py -q`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api/app/main.py tests/api/test_runs.py
git commit -m "feat(api): include model summary on run list items"
```

---

### Task 2: 路由底座迁移（react-router + 侧边栏）

**Files:**
- Modify: `apps/web/package.json`（新增依赖，用 pnpm add）
- Modify: `apps/web/src/main.tsx`
- Modify: `apps/web/src/App.tsx`（整体重写为路由 + 侧边栏）
- Modify: `apps/web/src/index.css`（`.tab` active 态兼容 NavLink）
- Delete: `apps/web/src/routes/index.ts`
- Test: `apps/web/tests/app.test.tsx`

**Interfaces:**
- Produces: 路由表挂载点（后续任务向 `App.tsx` 的 `<Routes>` 追加路由）与侧边栏结构：品牌区 + 分组「评测类型」（本任务为空占位注释，Task 5 填充）+「通用」（运行 `/runs`、Provider `/providers`、Agent/Harness `/harnesses`）。现有 `RunsPage` 临时挂 `/runs`，`BenchmarksPage` 临时挂 `/benchmarks`（Task 6 / Task 7 替换）。`/` 重定向 `/runs`（Task 13 改为 `/gsm8k`）。

- [ ] **Step 1: Install react-router-dom**

```bash
pnpm --dir apps/web add react-router-dom
```

Expected: `apps/web/package.json` dependencies 出现 `react-router-dom`，根 lockfile 更新（CI frozen install 依赖此步）。

- [ ] **Step 2: Write the failing test**

创建 `apps/web/tests/app.test.tsx`：

```tsx
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import App from "../src/App";

vi.mock("../src/api/client", () => ({
  getRuns: vi.fn(async () => ({ items: [], total: 0 })),
  createRun: vi.fn(),
  getRun: vi.fn(),
  cancelRun: vi.fn(),
  retryRun: vi.fn(),
  rescoreRun: vi.fn(),
  getReport: vi.fn(),
  getModels: vi.fn(async () => ({ items: [] })),
  getProviders: vi.fn(async () => ({ items: [] })),
  getCredentials: vi.fn(async () => ({ items: [] })),
  getProviderKinds: vi.fn(async () => ({ items: [] })),
  getAgents: vi.fn(async () => ({ items: [] })),
  getHarnesses: vi.fn(async () => ({ items: [] })),
  getBenchmarkOverview: vi.fn(async () => ({ items: [], total: 0 })),
  importBenchmark: vi.fn(),
  createBenchmarkRun: vi.fn(),
  subscribeRunEvents: vi.fn(() => () => undefined),
}));

/** 渲染 App 并附带当前 location 探针，供跳转断言（后续任务复用）。 */
export function renderWithLocation(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
      <Routes>
        <Route path="*" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>
  );
}

function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location">{location.pathname + location.search}</span>;
}

afterEach(cleanup);

describe("App 骨架", () => {
  it("侧边栏含通用导航组与品牌", async () => {
    renderWithLocation("/providers");
    expect(await screen.findByText("MoTTEavl")).toBeTruthy();
    expect(screen.getByRole("link", { name: /Provider 与模型/ })).toBeTruthy();
    expect(screen.getByRole("link", { name: /运行/ })).toBeTruthy();
    expect(screen.getByRole("link", { name: /Agent \/ Harness/ })).toBeTruthy();
  });

  it("按路径渲染对应页面", async () => {
    renderWithLocation("/harnesses");
    await waitFor(() => expect(screen.getByText("Harness 安装情况")).toBeTruthy());
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pnpm --dir apps/web test`
Expected: FAIL（App 仍是 Tabs，无 `link` role）

- [ ] **Step 4: Implement**

`apps/web/src/main.tsx` 全量替换：

```tsx
import React from 'react';
import {createRoot} from 'react-dom/client';
import {BrowserRouter} from 'react-router-dom';
import App from './App';
import './index.css';

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App/>
    </BrowserRouter>
  </React.StrictMode>
);
```

`apps/web/src/App.tsx` 全量替换：

```tsx
import { Navigate, NavLink, Route, Routes } from "react-router";
import { ActivityIcon, PlugIcon, PuzzlePieceIcon, StackIcon } from "@phosphor-icons/react";
import { RunsPage } from "./pages/RunsPage";
import { BenchmarksPage } from "./pages/BenchmarksPage";
import { HarnessesPage, ProvidersPage } from "./pages/ResourcesPage";

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
          {/* 类型入口由 evalTypes 注册表驱动，Task 5 填充 */}
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
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/benchmarks" element={<BenchmarksPage />} />
          <Route path="/providers" element={<ProvidersPage />} />
          <Route path="/harnesses" element={<HarnessesPage />} />
          <Route path="*" element={<Navigate to="/runs" replace />} />
        </Routes>
      </div>
    </div>
  );
}
```

`apps/web/src/index.css` 在 `.tab[data-state="active"]` 规则后追加：

```css
.tab[aria-current="page"] {
  background: var(--tone-neutral-bg);
  color: var(--text-primary);
  font-weight: 500;
}

.nav-group-label {
  margin: 12px 8px 4px;
  font-size: 11px;
  color: var(--text-faint);
}

.nav-group-label:first-child {
  margin-top: 0;
}
```

删除未使用文件：`git rm apps/web/src/routes/index.ts`

- [ ] **Step 5: Run tests + build**

Run: `pnpm --dir apps/web test && pnpm --dir apps/web build`
Expected: 全部 PASS、build 成功

- [ ] **Step 6: Commit**

```bash
git add apps/web
git commit -m "feat(web): route-based app shell with sidebar navigation"
```

---

### Task 3: useRunEvents hook 与进度聚合

**Files:**
- Create: `apps/web/src/hooks/useRunEvents.ts`
- Modify: `apps/web/src/api/client.ts`（无改动需要，`subscribeRunEvents` 已存在；仅确认导出）
- Test: `apps/web/tests/hooks.test.ts`

**Interfaces:**
- Consumes: `subscribeRunEvents(runId, { onEvent })` from `../api/client`（现有签名）。
- Produces:
  - `useRunEvents(runId: string): { events: TraceEvent[]; status: string | null }`（Task 5/8/11/12 消费）
  - `countDone(events: TraceEvent[]): number`（去重统计 `model_response` + `case_call_failed` 的 case 数）
  - `isTerminal(status: string | null): boolean`（终态判断：completed/failed/cancelled/unsupported/profile_stale）

- [ ] **Step 1: Write the failing test**

创建 `apps/web/tests/hooks.test.ts`：

```ts
import { describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { countDone, isTerminal, useRunEvents } from "../src/hooks/useRunEvents";
import type { TraceEvent } from "../src/api/client";

const handlers = vi.hoisted(() => ({ onEvent: vi.fn() }));
vi.mock("../src/api/client", () => ({
  subscribeRunEvents: vi.fn((runId: string, h: { onEvent: (e: TraceEvent) => void }) => {
    handlers.onEvent.mockImplementation(h.onEvent);
    return () => undefined;
  }),
}));

describe("countDone", () => {
  it("按 case_id 去重统计完成事件", () => {
    const events = [
      { run_id: "r", seq: 1, type: "running", status: "running" },
      { run_id: "r", seq: 2, type: "model_response", case_id: "case-1" },
      { run_id: "r", seq: 3, type: "score", case_id: "case-1", passed: true },
      { run_id: "r", seq: 4, type: "case_call_failed", case_id: "case-2" },
      { run_id: "r", seq: 5, type: "model_response", case_id: "case-2" },
      { run_id: "r", seq: 6, type: "model_response", case_id: "case-3" },
    ] as TraceEvent[];
    expect(countDone(events)).toBe(3);
  });
});

describe("isTerminal", () => {
  it("识别终态", () => {
    expect(isTerminal("completed")).toBe(true);
    expect(isTerminal("failed")).toBe(true);
    expect(isTerminal("running")).toBe(false);
    expect(isTerminal(null)).toBe(false);
  });
});

describe("useRunEvents", () => {
  it("订阅事件并回写状态", async () => {
    const { result } = renderHook(() => useRunEvents("run-1"));
    await act(async () => {
      handlers.onEvent({ run_id: "run-1", seq: 1, type: "running", status: "running" });
      handlers.onEvent({ run_id: "run-1", seq: 2, type: "model_response", case_id: "case-1", result: {} });
    });
    expect(result.current.events).toHaveLength(2);
    expect(result.current.status).toBe("running");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --dir apps/web test -- tests/hooks.test.ts`
Expected: FAIL（模块不存在）

- [ ] **Step 3: Implement**

创建 `apps/web/src/hooks/useRunEvents.ts`：

```ts
import { useEffect, useState } from "react";
import { subscribeRunEvents, type TraceEvent } from "../api/client";

const TERMINAL_STATUSES = ["completed", "failed", "cancelled", "unsupported", "profile_stale"];

export function isTerminal(status: string | null): boolean {
  return status != null && TERMINAL_STATUSES.includes(status);
}

/** 去重统计已完成的 case 数（成功响应与失败调用都算已完成）。 */
export function countDone(events: TraceEvent[]): number {
  const done = new Set<string>();
  for (const event of events) {
    if ((event.type === "model_response" || event.type === "case_call_failed") && typeof event.case_id === "string") {
      done.add(event.case_id);
    }
  }
  return done.size;
}

export function useRunEvents(runId: string): { events: TraceEvent[]; status: string | null } {
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [status, setStatus] = useState<string | null>(null);

  useEffect(() => {
    setEvents([]);
    setStatus(null);
    const unsubscribe = subscribeRunEvents(runId, {
      onEvent: (event) => {
        setEvents((current) => (current.some((item) => item.seq === event.seq) ? current : [...current, event]));
        if (typeof event.status === "string") {
          setStatus(event.status);
        }
      },
    });
    return unsubscribe;
  }, [runId]);

  return { events, status };
}
```

- [ ] **Step 4: Run tests**

Run: `pnpm --dir apps/web test -- tests/hooks.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/hooks apps/web/tests/hooks.test.ts
git commit -m "feat(web): useRunEvents hook with progress aggregation"
```

---

### Task 4: ModelPicker 多选组件

**Files:**
- Create: `apps/web/src/components/ModelPicker.tsx`
- Modify: `apps/web/src/pages/ResourcesPage.tsx`（`formatContext` 移入 ModelPicker 并从该处导入，删除本地定义）
- Modify: `apps/web/src/index.css`（新增 `.model-picker*` 类）
- Test: `apps/web/tests/components.test.tsx`（追加 describe 块）

**Interfaces:**
- Consumes: `ModelRecord` from `../api/client`。
- Produces:
  - `formatContext(contextWindow?: number | null): string | null`（256K/1M 格式化，ResourcesPage 与套件页共用）
  - `ModelPicker({ models, selected, onToggle }: { models: ModelRecord[]; selected: string[]; onToggle: (id: string) => void })`（Task 7/11 消费；按 Provider 分组渲染复选行，停用项禁用）

- [ ] **Step 1: Write the failing test**

在 `apps/web/tests/components.test.tsx` 追加（文件顶部 import 增加 `ModelPicker`；此 describe 不依赖 client mock，直接传入 models prop）：

```tsx
import { ModelPicker } from "../src/components/ModelPicker";

describe("ModelPicker", () => {
  const MODELS = [
    { id: "glm-4.7", provider: "zhipu", capabilities: {}, context_window: 128000 },
    { id: "qwen-max", provider: "zhipu", capabilities: {}, context_window: 32000 },
    { id: "qwen2.5-7b", provider: "local-vllm", capabilities: {}, context_window: 32768 },
    { id: "glm-4-air", provider: "zhipu", capabilities: {}, enabled: false },
  ];

  it("按 Provider 分组渲染并标注上下文与停用态", () => {
    const onToggle = vi.fn();
    render(<ModelPicker models={MODELS as any} selected={["qwen-max"]} onToggle={onToggle} />);
    expect(screen.getByText("zhipu")).toBeTruthy();
    expect(screen.getByText("local-vllm")).toBeTruthy();
    expect(screen.getByText("128K")).toBeTruthy();
    expect(screen.getByText("已停用")).toBeTruthy();
    const disabled = screen.getByLabelText("选择模型 glm-4-air") as HTMLInputElement;
    expect(disabled.disabled).toBe(true);
  });

  it("点击勾选触发 onToggle", () => {
    const onToggle = vi.fn();
    render(<ModelPicker models={MODELS as any} selected={[]} onToggle={onToggle} />);
    fireEvent.click(screen.getByLabelText("选择模型 qwen-max"));
    expect(onToggle).toHaveBeenCalledWith("qwen-max");
  });

  it("空模型列表显示空状态", () => {
    render(<ModelPicker models={[]} selected={[]} onToggle={vi.fn()} />);
    expect(screen.getByText("暂无可用模型，先在 Provider 页注册")).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --dir apps/web test -- tests/components.test.tsx`
Expected: FAIL（组件不存在）

- [ ] **Step 3: Implement**

创建 `apps/web/src/components/ModelPicker.tsx`：

```tsx
import type { ModelRecord } from "../api/client";

/** 上下文窗口徽章格式：256000 → 256K、1000000 → 1M（自 ResourcesPage 移入，全站共用）。 */
export function formatContext(contextWindow?: number | null): string | null {
  if (contextWindow == null) return null;
  if (contextWindow >= 1_000_000) {
    const millions = contextWindow / 1_000_000;
    return `${Number.isInteger(millions) ? millions : millions.toFixed(1)}M`;
  }
  if (contextWindow >= 1_000) {
    const thousands = contextWindow / 1_000;
    return `${Number.isInteger(thousands) ? thousands : thousands.toFixed(1)}K`;
  }
  return String(contextWindow);
}

export function ModelPicker({ models, selected, onToggle }: {
  models: ModelRecord[];
  selected: string[];
  onToggle: (id: string) => void;
}) {
  const groups = new Map<string, ModelRecord[]>();
  for (const model of models) {
    const key = model.provider ?? "（未关联 Provider）";
    groups.set(key, [...(groups.get(key) ?? []), model]);
  }
  return (
    <div className="model-picker" role="group" aria-label="模型多选">
      {[...groups.entries()].map(([provider, group]) => (
        <div key={provider} className="model-picker-group">
          <p className="field-label">{provider}</p>
          {group.map((model) => {
            const enabled = model.enabled !== false;
            const context = formatContext(model.context_window);
            return (
              <label key={model.id} className="model-picker-item" data-enabled={enabled ? undefined : "false"}>
                <input
                  type="checkbox"
                  checked={selected.includes(model.id)}
                  disabled={!enabled}
                  onChange={() => onToggle(model.id)}
                  aria-label={`选择模型 ${model.id}`}
                />
                <span className="mono">{model.id}</span>
                {context && <span className="status-badge status-tone-neutral model-badge">{context}</span>}
                {model.supports_tools && <span className="status-badge status-tone-neutral model-badge">工具</span>}
                {!enabled && <span className="muted">已停用</span>}
              </label>
            );
          })}
        </div>
      ))}
      {models.length === 0 && <p className="empty">暂无可用模型，先在 Provider 页注册</p>}
    </div>
  );
}
```

`apps/web/src/pages/ResourcesPage.tsx`：删除本地 `formatContext`（第 48-60 行），改为：

```tsx
import { formatContext } from "../components/ModelPicker";
```

（原使用处签名一致，无需其它改动。）

`apps/web/src/index.css` 追加：

```css
/* ---------- ModelPicker（按 Provider 分组的多选清单） ---------- */

.model-picker-group {
  margin-bottom: 8px;
}

.model-picker-group .field-label {
  margin-bottom: 4px;
}

.model-picker-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 6px;
  font-size: 13px;
  color: var(--text-primary);
}

.model-picker-item:hover {
  background: var(--bg-subtle);
}

.model-picker-item[data-enabled="false"] .mono {
  color: var(--text-faint);
  text-decoration: line-through;
}

.model-picker-item input[type="checkbox"] {
  width: auto;
  margin: 0;
  accent-color: var(--ink);
}
```

注意：`.model-picker-item` 是裸 label 不在 form 内，不受 `form label` 规则影响，需显式设 display。

- [ ] **Step 4: Run tests**

Run: `pnpm --dir apps/web test`
Expected: 全部 PASS（含 ResourcesPage 既有用例，`formatContext` 行为不变）

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/components/ModelPicker.tsx apps/web/src/pages/ResourcesPage.tsx apps/web/src/index.css apps/web/tests/components.test.tsx
git commit -m "feat(web): grouped multi-select ModelPicker component"
```

---

### Task 5: 类型注册表与通用兜底页

**Files:**
- Create: `apps/web/src/evalTypes/registry.ts`
- Create: `apps/web/src/evalTypes/fallback/FallbackPages.tsx`
- Modify: `apps/web/src/App.tsx`（侧边栏「评测类型」组由注册表渲染 + 兜底路由）
- Test: `apps/web/tests/registry.test.ts`、`apps/web/tests/evalTypes.test.tsx`

**Interfaces:**
- Produces:
  - `EvalTypeSuite { id: string; label: string; icon: ComponentType; matchRun(run: { scenario_version: string; manifest?: any }): boolean }`
  - `EVAL_SUITES: EvalTypeSuite[]`（gsm8k / direct-llm / replay 顺序）
  - `suiteForRun(run): EvalTypeSuite | null`
  - `suiteRoutes(id) => { operate: string; monitor: (runIds: string[]) => string; result: (runId: string) => string; compare: (runIds: string[]) => string }`（URL 规则：`/{id}`、`/{id}/monitor?runs=a,b`、`/{id}/runs/:runId/result`、`/{id}/compare?runs=a,b`）
  - `FallbackMonitorPage` / `FallbackResultPage`（路由组件，props `{ runId: string }`，从 `useParams` 取）
- Consumes: Task 3 的 `useRunEvents`、`isTerminal`。

- [ ] **Step 1: Write the failing tests**

创建 `apps/web/tests/registry.test.ts`：

```ts
import { describe, expect, it } from "vitest";
import { EVAL_SUITES, suiteForRun, suiteRoutes } from "../src/evalTypes/registry";

describe("evalTypes 注册表", () => {
  it("注册三个套件且 id 唯一", () => {
    expect(EVAL_SUITES.map((suite) => suite.id)).toEqual(["gsm8k", "direct-llm", "replay"]);
  });

  it("按 run 归属匹配套件", () => {
    expect(suiteForRun({ scenario_version: "gsm8k-test-smoke@1" })?.id).toBe("gsm8k");
    expect(suiteForRun({ scenario_version: "direct-llm@1" })?.id).toBe("direct-llm");
    expect(suiteForRun({ scenario_version: "replay@1" })?.id).toBe("replay");
    expect(suiteForRun({ scenario_version: "json_extract@1" })?.id).toBe("replay");
    expect(suiteForRun({ scenario_version: "unknown@9" })).toBeNull();
  });

  it("生成四类路由", () => {
    const routes = suiteRoutes("gsm8k");
    expect(routes.operate).toBe("/gsm8k");
    expect(routes.monitor(["run-1", "run-2"])).toBe("/gsm8k/monitor?runs=run-1,run-2");
    expect(routes.result("run-1")).toBe("/gsm8k/runs/run-1/result");
    expect(routes.compare(["run-1", "run-2"])).toBe("/gsm8k/compare?runs=run-1,run-2");
  });
});
```

创建 `apps/web/tests/evalTypes.test.tsx`（兜底页部分；client mock 顶部集中声明，后续任务在同一文件追加套件页用例）：

```tsx
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { FallbackMonitorPage, FallbackResultPage } from "../src/evalTypes/fallback/FallbackPages";

const clientMocks = vi.hoisted(() => ({
  getRun: vi.fn(),
  cancelRun: vi.fn(),
  retryRun: vi.fn(),
  rescoreRun: vi.fn(),
  getReport: vi.fn(),
  subscribeRunEvents: vi.fn(() => () => undefined),
}));
vi.mock("../src/api/client", () => clientMocks);

/** 当前 location 探针，供跳转断言；本文件后续套件用例复用。 */
function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location">{location.pathname + location.search}</span>;
}

afterEach(cleanup);

const RUN = {
  id: "run-9", scenario_version: "unknown@9", status: "running",
  case_ids: ["case-1"], cases: [], scores: [],
};

describe("FallbackMonitorPage", () => {
  it("渲染运行概要与时间线，未知类型不 404", async () => {
    clientMocks.getRun.mockResolvedValue(RUN);
    render(
      <MemoryRouter initialEntries={["/runs/run-9/monitor"]}>
        <FallbackMonitorPage />
      </MemoryRouter>
    );
    expect(await screen.findByText("运行 run-9")).toBeTruthy();
  });
});

describe("FallbackResultPage", () => {
  it("渲染场景与评分表", async () => {
    clientMocks.getRun.mockResolvedValue({ ...RUN, status: "completed", scores: [{ case_id: "case-1", passed: true }] });
    render(
      <MemoryRouter initialEntries={["/runs/run-9/result"]}>
        <FallbackResultPage />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText("unknown@9")).toBeTruthy());
    expect(screen.getByText(/通过率/)).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pnpm --dir apps/web test -- tests/registry.test.ts tests/evalTypes.test.tsx`
Expected: FAIL（模块不存在）

- [ ] **Step 3: Implement**

创建 `apps/web/src/evalTypes/registry.ts`：

```ts
import type { ComponentType } from "react";
import { CalculatorIcon, ChatTextIcon, ClockCounterClockwiseIcon } from "@phosphor-icons/react";

export interface RunLike {
  scenario_version: string;
  manifest?: any;
}

export interface EvalTypeSuite {
  id: string;
  label: string;
  icon: ComponentType<{ size?: number; weight?: string; "aria-hidden"?: boolean }>;
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
```

match 说明：GSM8K 场景名规则是 `{dataset}-smoke@{version}`（如 `gsm8k-test-smoke@1`），正则 `/^gsm8k.*@\d+$/` 同时覆盖；展开后的 benchmark run 以 `manifest.benchmark_provenance` 双重兜底。

创建 `apps/web/src/evalTypes/fallback/FallbackPages.tsx`：

```tsx
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getRun, cancelRun, retryRun, rescoreRun, getReport, type RunRecord } from "../../api/client";
import { isTerminal, useRunEvents } from "../../hooks/useRunEvents";
import { StatusBadge } from "../../components/StatusBadge";
import { RunTimeline } from "../../components/RunTimeline";
import { ScoreTable } from "../../components/ScoreTable";

/** 未匹配类型 run 的通用过程页：状态 + 时间线 + 取消/重试。 */
export function FallbackMonitorPage() {
  const { runId = "" } = useParams();
  const [run, setRun] = useState<RunRecord | null>(null);
  const [error, setError] = useState("");
  const { events, status } = useRunEvents(runId);
  const current = status ?? run?.status ?? null;
  const terminal = isTerminal(current);

  useEffect(() => {
    let alive = true;
    const load = () => getRun(runId).then((record) => alive && setRun(record)).catch((e) => alive && setError(String(e)));
    void load();
    if (!terminal) {
      const timer = setInterval(load, 3000);
      return () => { alive = false; clearInterval(timer); };
    }
    return () => { alive = false; };
  }, [runId, terminal]);

  const act = async (action: () => Promise<unknown>) => {
    try {
      await action();
      setRun(await getRun(runId));
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="page">
      <section className="panel detail">
        <div className="panel-head">
          <h2 className="mono">运行 {runId}</h2>
          <div className="panel-head-actions">
            {terminal ? (
              <Link className="link" to={`/runs/${runId}/result`}>查看结果</Link>
            ) : (
              <button type="button" onClick={() => act(() => cancelRun(runId, "web 控制台取消"))}>取消</button>
            )}
            {terminal && run && ["failed", "cancelled", "unsupported", "profile_stale"].includes(current ?? "") && (
              <button type="button" onClick={() => act(() => retryRun(runId))}>重试</button>
            )}
          </div>
        </div>
        {error && <p className="error">{error}</p>}
        {run && (
          <dl className="kv">
            <dt>场景</dt><dd className="mono">{run.scenario_version}</dd>
            <dt>状态</dt><dd><StatusBadge status={current ?? "queued"} /></dd>
          </dl>
        )}
        {current === "queued" && (
          <p className="hint">等待 Worker 领取；若长期排队，请在服务端启动 Worker（make worker）。</p>
        )}
        <RunTimeline events={events} embedded />
      </section>
    </div>
  );
}

function downloadJson(name: string, payload: unknown) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  URL.revokeObjectURL(url);
}

/** 未匹配类型 run 的通用结果页：概要 + 评分表 + 重评分/导出。 */
export function FallbackResultPage() {
  const { runId = "" } = useParams();
  const [run, setRun] = useState<RunRecord | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    getRun(runId).then(setRun).catch((e) => setError(String(e)));
  }, [runId]);

  const act = async (action: () => Promise<unknown>) => {
    try {
      await action();
      setRun(await getRun(runId));
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="page">
      <section className="panel detail">
        <div className="panel-head">
          <h2 className="mono">运行 {runId}</h2>
          <div className="panel-head-actions">
            <button type="button" onClick={() => act(() => rescoreRun(runId))} disabled={run?.status !== "completed"}>
              重新评分
            </button>
            <button
              type="button"
              onClick={async () => {
                try {
                  downloadJson(`${runId}-report.json`, await getReport(runId));
                } catch (e) {
                  setError(String(e));
                }
              }}
            >
              导出报告
            </button>
          </div>
        </div>
        {error && <p className="error">{error}</p>}
        {run && (
          <dl className="kv">
            <dt>场景</dt><dd className="mono">{run.scenario_version}</dd>
            <dt>状态</dt><dd><StatusBadge status={run.status} /></dd>
            {run.parent_run_id && (<><dt>父运行</dt><dd className="mono">{run.parent_run_id}</dd></>)}
            {run.cancellation?.reason && (<><dt>取消原因</dt><dd>{run.cancellation.reason}</dd></>)}
          </dl>
        )}
        {run?.scores && <ScoreTable scores={run.scores} embedded />}
      </section>
    </div>
  );
}
```

`apps/web/src/App.tsx`：侧边栏「评测类型」注释替换为：

```tsx
import { EVAL_SUITES } from "./evalTypes/registry";
// ...
          <p className="nav-group-label">评测类型</p>
          {EVAL_SUITES.map(({ id, label, icon: Icon }) => (
            <NavLink key={id} to={`/${id}`} className="tab">
              <Icon size={16} weight="bold" aria-hidden />
              <span>{label}</span>
            </NavLink>
          ))}
```

`<Routes>` 追加兜底路由（放在通用路由之后、`*` 之前）：

```tsx
          <Route path="/runs/:runId/monitor" element={<FallbackMonitorPage />} />
          <Route path="/runs/:runId/result" element={<FallbackResultPage />} />
```

- [ ] **Step 4: Run tests + build**

Run: `pnpm --dir apps/web test && pnpm --dir apps/web build`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/evalTypes apps/web/src/App.tsx apps/web/tests
git commit -m "feat(web): eval type suite registry with fallback run pages"
```

---

### Task 6: 运行总览页（替代旧运行页）

**Files:**
- Create: `apps/web/src/pages/RunsOverviewPage.tsx`
- Modify: `apps/web/src/api/client.ts`（`RunRecord` 增加可选 `model` 字段）
- Modify: `apps/web/src/App.tsx`（`/runs` 指向新页；`RunsPage` import 删除）
- Test: `apps/web/tests/evalTypes.test.tsx`（追加 describe）

**Interfaces:**
- Consumes: `getRuns`（列表项含 Task 1 的 `model` 字段）、`suiteForRun`、`suiteRoutes`、`StatusBadge`、`cancelRun` / `retryRun`。
- Produces: 路由 `/runs` 页面；行点击跳 `suiteRoutes(suite.id).result(run.id)`，未匹配跳 `/runs/{id}/result`。

- [ ] **Step 1: Write the failing test**

`apps/web/tests/evalTypes.test.tsx` 追加（clientMocks 增加 `getRuns`）：

```tsx
import { RunsOverviewPage } from "../src/pages/RunsOverviewPage";
// fireEvent 已在文件顶部 import；LocationProbe 复用本文件已有的定义

describe("RunsOverviewPage", () => {
  it("展示类型与模型列，点击行跳转所属套件结果页", async () => {
    clientMocks.getRuns.mockResolvedValue({
      items: [
        { id: "run-42", scenario_version: "gsm8k-test-smoke@1", status: "completed", model: "glm-4.7", case_ids: [], cases: [], scores: [] },
        { id: "run-38", scenario_version: "direct-llm@1", status: "running", model: "qwen-max", case_ids: ["c1"], cases: [], scores: [] },
        { id: "run-31", scenario_version: "mystery@2", status: "failed", model: null, case_ids: [], cases: [], scores: [] },
      ],
      total: 3,
    });
    render(
      <MemoryRouter initialEntries={["/runs"]}>
        <RunsOverviewPage />
        <LocationProbe />
      </MemoryRouter>
    );
    expect(await screen.findByText("GSM8K")).toBeTruthy();
    expect(screen.getByText("Direct LLM")).toBeTruthy();
    expect(screen.getByText("通用")).toBeTruthy();
    expect(screen.getByText("glm-4.7")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "run-42" }));
    expect(screen.getByTestId("location").textContent).toBe("/gsm8k/runs/run-42/result");

    fireEvent.click(screen.getByRole("button", { name: "run-31" }));
    expect(screen.getByTestId("location").textContent).toBe("/runs/run-31/result");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --dir apps/web test -- tests/evalTypes.test.tsx`
Expected: FAIL（页面不存在）

- [ ] **Step 3: Implement**

先改 `apps/web/src/api/client.ts` 的 `RunRecord`（第 12-23 行），在 `parent_run_id` 之前加一行：

```ts
  model?: string | null;
```

创建 `apps/web/src/pages/RunsOverviewPage.tsx`：

```tsx
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import * as Select from "@radix-ui/react-select";
import { CaretDownIcon, CheckIcon, DotsThreeIcon } from "@phosphor-icons/react";
import { cancelRun, getRuns, retryRun, type RunRecord } from "../api/client";
import { StatusBadge } from "../components/StatusBadge";
import { STATUS_ORDER, statusLabel } from "../components/statusMeta";
import { EVAL_SUITES, suiteForRun, suiteRoutes } from "../evalTypes/registry";

const TERMINAL_STATUSES = ["completed", "failed", "cancelled", "unsupported", "profile_stale"];
const RETRYABLE_STATUSES = ["failed", "cancelled", "unsupported", "profile_stale"];

function typeLabel(run: RunRecord): string {
  return suiteForRun(run)?.label ?? "通用";
}

export function RunsOverviewPage() {
  const navigate = useNavigate();
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [statusFilter, setStatusFilter] = useState("all");
  const [typeFilter, setTypeFilter] = useState("all");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const payload = await getRuns(statusFilter === "all" ? undefined : statusFilter);
      setRuns(payload.items);
      setError("");
    } catch (e) {
      setError(String(e));
    }
  }, [statusFilter]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 3000);
    return () => clearInterval(timer);
  }, [refresh]);

  const visible = runs.filter((run) => typeFilter === "all" || typeLabel(run) === typeFilter);

  const openRun = (run: RunRecord) => {
    const suite = suiteForRun(run);
    navigate(suite ? suiteRoutes(suite.id).result(run.id) : `/runs/${run.id}/result`);
  };

  const act = async (action: () => Promise<unknown>) => {
    try {
      await action();
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="page">
      <section className="panel detail">
        <div className="panel-head">
          <h2>运行总览</h2>
          <div className="inline-field">
            <span className="field-label">类型</span>
            <select className="control" value={typeFilter} onChange={(change) => setTypeFilter(change.target.value)} aria-label="类型过滤">
              <option value="all">全部类型</option>
              {[...EVAL_SUITES.map((suite) => suite.label), "通用"].map((label) => (
                <option key={label} value={label}>{label}</option>
              ))}
            </select>
          </div>
          <div className="inline-field">
            <span className="field-label">状态</span>
            <Select.Root value={statusFilter} onValueChange={setStatusFilter}>
              <Select.Trigger className="select-trigger" aria-label="状态过滤">
                <Select.Value />
                <CaretDownIcon size={14} weight="bold" aria-hidden />
              </Select.Trigger>
              <Select.Portal>
                <Select.Content className="select-content" position="popper" sideOffset={4}>
                  <Select.Viewport>
                    <Select.Item value="all" className="select-item">
                      <Select.ItemText>全部</Select.ItemText>
                      <Select.ItemIndicator className="select-item-indicator">
                        <CheckIcon size={14} weight="bold" aria-hidden />
                      </Select.ItemIndicator>
                    </Select.Item>
                    {STATUS_ORDER.map((status) => (
                      <Select.Item key={status} value={status} className="select-item">
                        <Select.ItemText>{statusLabel(status)}</Select.ItemText>
                        <Select.ItemIndicator className="select-item-indicator">
                          <CheckIcon size={14} weight="bold" aria-hidden />
                        </Select.ItemIndicator>
                      </Select.Item>
                    ))}
                  </Select.Viewport>
                </Select.Content>
              </Select.Portal>
            </Select.Root>
          </div>
        </div>
        {error && <p className="error">{error}</p>}
        <table>
          <thead>
            <tr>
              <th>ID</th><th>类型</th><th>场景</th><th>模型</th><th>状态</th><th>进度</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((run) => {
              const cancellable = !TERMINAL_STATUSES.includes(run.status);
              const retryable = RETRYABLE_STATUSES.includes(run.status);
              const total = run.case_ids?.length ?? 0;
              const done = run.cases?.length ?? 0;
              return (
                <tr key={run.id}>
                  <td><button className="link" onClick={() => openRun(run)}>{run.id}</button></td>
                  <td>{typeLabel(run)}</td>
                  <td className="mono">{run.scenario_version}</td>
                  <td className="mono">{run.model ?? "—"}</td>
                  <td><StatusBadge status={run.status} /></td>
                  <td className="mono">{total ? `${done}/${total}` : "—"}</td>
                  <td className="row-actions">
                    {cancellable || retryable ? (
                      <DropdownMenu.Root>
                        <DropdownMenu.Trigger asChild>
                          <button className="icon-btn" aria-label={`运行 ${run.id} 操作`}>
                            <DotsThreeIcon size={16} weight="bold" aria-hidden />
                          </button>
                        </DropdownMenu.Trigger>
                        <DropdownMenu.Portal>
                          <DropdownMenu.Content className="menu-content" align="end" sideOffset={4}>
                            {cancellable && (
                              <DropdownMenu.Item className="menu-item danger" onSelect={() => act(() => cancelRun(run.id, "web 控制台取消"))}>
                                取消
                              </DropdownMenu.Item>
                            )}
                            {retryable && (
                              <DropdownMenu.Item className="menu-item" onSelect={() => act(() => retryRun(run.id))}>
                                重试
                              </DropdownMenu.Item>
                            )}
                          </DropdownMenu.Content>
                        </DropdownMenu.Portal>
                      </DropdownMenu.Root>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                </tr>
              );
            })}
            {visible.length === 0 && (
              <tr><td colSpan={7} className="empty">暂无运行</td></tr>
            )}
          </tbody>
        </table>
      </section>
    </div>
  );
}
```

`apps/web/src/App.tsx`：`/runs` 路由 element 换成 `<RunsOverviewPage />`，删除 `RunsPage` import。

- [ ] **Step 4: Run tests**

Run: `pnpm --dir apps/web test`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/pages/RunsOverviewPage.tsx apps/web/src/App.tsx apps/web/tests/evalTypes.test.tsx
git commit -m "feat(web): cross-type runs overview with suite routing"
```

---

### Task 7: GSM8K 操作页（数据集导入 + 多模型发起）

**Files:**
- Create: `apps/web/src/evalTypes/gsm8k/Gsm8kOperate.tsx`
- Modify: `apps/web/src/App.tsx`（`/gsm8k` 路由）
- Test: `apps/web/tests/evalTypes.test.tsx`（追加）

**Interfaces:**
- Consumes: `getBenchmarkOverview` / `importBenchmark` / `createBenchmarkRun` / `getModels`、`ModelPicker`、`suiteRoutes("gsm8k").monitor(ids)`。
- Produces: `/gsm8k` 页面。发起 = 循环 `createBenchmarkRun({ model: id })`，成功收集 run id 跳批次过程页；失败就地列出（模型 + 错误），不阻塞成功者。

- [ ] **Step 1: Write the failing test**

`apps/web/tests/evalTypes.test.tsx` 追加（clientMocks 增加 `getBenchmarkOverview` / `importBenchmark` / `createBenchmarkRun` / `getModels`）：

```tsx
import { Gsm8kOperate } from "../src/evalTypes/gsm8k/Gsm8kOperate";

describe("Gsm8kOperate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clientMocks.getBenchmarkOverview.mockResolvedValue({
      items: [{ scenario: "gsm8k-test-smoke@1", dataset: "gsm8k-test@1", cases: 20, runs: [] }],
      total: 1,
    });
    clientMocks.getModels.mockResolvedValue({
      items: [
        { id: "glm-4.7", provider: "zhipu", capabilities: {}, context_window: 128000 },
        { id: "qwen-max", provider: "zhipu", capabilities: {}, context_window: 32000 },
      ],
    });
  });

  it("勾选多个模型发起后跳转批次过程页", async () => {
    clientMocks.createBenchmarkRun
      .mockResolvedValueOnce({ id: "run-42", status: "queued" })
      .mockResolvedValueOnce({ id: "run-43", status: "queued" });
    render(
      <MemoryRouter initialEntries={["/gsm8k"]}>
        <Gsm8kOperate />
        <LocationProbe />
      </MemoryRouter>
    );
    await screen.findByText("gsm8k-test-smoke@1");
    fireEvent.click(screen.getByLabelText("选择模型 glm-4.7"));
    fireEvent.click(screen.getByLabelText("选择模型 qwen-max"));
    fireEvent.click(screen.getByRole("button", { name: /发起跑测/ }));
    await waitFor(() => expect(clientMocks.createBenchmarkRun).toHaveBeenCalledTimes(2));
    expect(clientMocks.createBenchmarkRun).toHaveBeenCalledWith({ model: "glm-4.7" });
    expect(screen.getByTestId("location").textContent).toBe("/gsm8k/monitor?runs=run-42,run-43");
  });

  it("个别模型失败不阻塞整批，错误就地显示", async () => {
    clientMocks.createBenchmarkRun
      .mockRejectedValueOnce(new Error("MODEL_DISABLED"))
      .mockResolvedValueOnce({ id: "run-44", status: "queued" });
    render(
      <MemoryRouter initialEntries={["/gsm8k"]}>
        <Gsm8kOperate />
        <LocationProbe />
      </MemoryRouter>
    );
    await screen.findByText("gsm8k-test-smoke@1");
    fireEvent.click(screen.getByLabelText("选择模型 glm-4.7"));
    fireEvent.click(screen.getByLabelText("选择模型 qwen-max"));
    fireEvent.click(screen.getByRole("button", { name: /发起跑测/ }));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/gsm8k/monitor?runs=run-44"));
    expect(screen.getByText(/MODEL_DISABLED/)).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --dir apps/web test -- tests/evalTypes.test.tsx`
Expected: FAIL

- [ ] **Step 3: Implement**

创建 `apps/web/src/evalTypes/gsm8k/Gsm8kOperate.tsx`：

```tsx
import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { DownloadSimpleIcon } from "@phosphor-icons/react";
import {
  createBenchmarkRun, getBenchmarkOverview, getModels, importBenchmark,
  type BenchmarkOverview, type ModelRecord,
} from "../../api/client";
import { ModelPicker } from "../../components/ModelPicker";
import { suiteRoutes } from "../registry";

const ROUTES = suiteRoutes("gsm8k");

export function Gsm8kOperate() {
  const navigate = useNavigate();
  const [overview, setOverview] = useState<BenchmarkOverview | null>(null);
  const [models, setModels] = useState<ModelRecord[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [revision, setRevision] = useState("");
  const [license, setLicense] = useState("MIT");
  const [importing, setImporting] = useState(false);
  const [running, setRunning] = useState(false);
  const [failures, setFailures] = useState<{ model: string; error: string }[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      setOverview(await getBenchmarkOverview());
      setError("");
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
    getModels().then((payload) => setModels(payload.items)).catch(() => undefined);
  }, [refresh]);

  const doImport = async (form: FormEvent<HTMLFormElement>) => {
    form.preventDefault();
    setImporting(true);
    setError("");
    setMessage("");
    try {
      const payload = await importBenchmark({ revision: revision.trim(), license: license.trim() });
      setMessage(`已导入 ${payload.imported}（${payload.cases} 题）`);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setImporting(false);
    }
  };

  const doRun = async () => {
    setRunning(true);
    setFailures([]);
    setError("");
    try {
      const created: string[] = [];
      const failed: { model: string; error: string }[] = [];
      for (const model of selected) {
        try {
          const run = await createBenchmarkRun({ model });
          created.push(run.id);
        } catch (e) {
          failed.push({ model, error: String(e) });
        }
      }
      setFailures(failed);
      if (created.length > 0) {
        navigate(ROUTES.monitor(created));
        return;
      }
      if (failed.length === 0) {
        setError("请先选择至少一个模型");
      }
    } finally {
      setRunning(false);
    }
  };

  const preset = overview?.items[0];

  return (
    <div className="page">
      <section className="panel detail" aria-label="GSM8K 操作页">
        <div className="panel-head">
          <h2>GSM8K 数学评测 · 操作</h2>
        </div>
        {error && <p className="error">{error}</p>}

        {!preset ? (
          <form onSubmit={doImport} aria-label="下载并导入测试集">
            <h3 className="embed-title">导入测试集</h3>
            <p className="hint">从官方 openai/grade-school-math 仓库按 pinned commit 下载 test.jsonl，取前 20 题生成冒烟数据集。</p>
            <label>
              官方仓库 commit（40 位）
              <input value={revision} onChange={(change) => setRevision(change.target.value)} placeholder="完整 40 位 commit hash" required />
            </label>
            <label>
              License
              <input value={license} onChange={(change) => setLicense(change.target.value)} />
            </label>
            <button type="submit" disabled={importing}>
              <DownloadSimpleIcon size={14} weight="bold" aria-hidden />
              {importing ? "下载中…" : "下载并导入"}
            </button>
            {message && <p className="import-feedback pass">{message}</p>}
          </form>
        ) : (
          <div className="operate-grid">
            <div className="operate-card">
              <h3 className="embed-title">数据集（pinned）</h3>
              <p className="mono">{preset.scenario}</p>
              <p className="hint mono">
                {preset.dataset} · {preset.cases} 题
                {preset.provenance?.revision ? ` · revision ${String(preset.provenance.revision).slice(0, 7)}` : ""}
              </p>
            </div>
            <div className="operate-card">
              <h3 className="embed-title">模型（可多选对比）</h3>
              <ModelPicker
                models={models}
                selected={selected}
                onToggle={(id) => setSelected((current) =>
                  current.includes(id) ? current.filter((item) => item !== id) : [...current, id])}
              />
            </div>
            <div className="operate-card">
              <h3 className="embed-title">跑测参数（preset 固定）</h3>
              <dl className="kv">
                <dt>题数</dt><dd className="mono">{preset.cases}</dd>
                <dt>输出上限</dt><dd className="mono">1024</dd>
                <dt>重试</dt><dd className="mono">0</dd>
              </dl>
            </div>
          </div>
        )}

        {preset && (
          <div className="actions">
            <button type="button" onClick={() => void doRun()} disabled={running || selected.length === 0}>
              {running ? "创建中…" : `发起跑测（${selected.length} 个模型 × ${preset.cases} 题）`}
            </button>
            <span className="hint">真实调用 · 产生费用 · 发起后自动进入过程页</span>
          </div>
        )}
        {failures.length > 0 && (
          <ul>
            {failures.map((failure) => (
              <li key={failure.model} className="error">{failure.model}：{failure.error}</li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
```

（`useCallback` 需加入顶部 import。）

`apps/web/src/index.css` 追加：

```css
/* ---------- 类型操作页三卡布局 ---------- */

.operate-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  align-items: stretch;
}

.operate-card {
  flex: 1 1 280px;
  min-width: 260px;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
}

.actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 16px;
}
```

`apps/web/src/App.tsx`：`/gsm8k` 路由 element `<Gsm8kOperate />`。

- [ ] **Step 4: Run tests**

Run: `pnpm --dir apps/web test`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/evalTypes/gsm8k apps/web/src/App.tsx apps/web/src/index.css apps/web/tests/evalTypes.test.tsx
git commit -m "feat(web): gsm8k operate page with dataset import and multi-model launch"
```

---

### Task 8: GSM8K 过程页（批次进度 + 逐题格子）

**Files:**
- Create: `apps/web/src/components/RunProgress.tsx`
- Create: `apps/web/src/components/BatchMonitor.tsx`
- Create: `apps/web/src/evalTypes/gsm8k/grid.ts`
- Create: `apps/web/src/evalTypes/gsm8k/Gsm8kMonitor.tsx`
- Modify: `apps/web/src/App.tsx`（`/gsm8k/monitor` 路由）
- Modify: `apps/web/src/index.css`（进度条 / 格子网格）
- Test: `apps/web/tests/evalTypes.test.tsx`（追加）、`apps/web/tests/registry.test.ts` 不变

**Interfaces:**
- Consumes: `useRunEvents` / `countDone` / `isTerminal`、`getRun`、`cancelRun`。
- Produces:
  - `RunProgress({ done, total }: { done: number; total: number })`（x/N 文本 + 进度条）
  - `BatchMonitor({ runIds, renderDetail }: { runIds: string[]; renderDetail?: (runId: string) => ReactNode })`（批次骨架：每 run 一行 + 展开插槽；全终态渲染 compare 链接的回调由页面自绘——见下）
  - `gridCells(run: RunRecord): { caseId: string; state: "pass" | "fail" | "pending" }[]`（GSM8K 格子状态纯函数）
  - `/gsm8k/monitor` 页面：URL query `runs=a,b`

BatchMonitor 具体 props（锁定）：

```ts
export function BatchMonitor(props: {
  runIds: string[];
  comparePath?: string;                    // 全部终态后显示「查看对比结果」链接
  renderDetail?: (runId: string) => ReactNode;  // 展开区插槽（格子 / 卡片 / 时间线）
})
```

- [ ] **Step 1: Write the failing tests**

`apps/web/tests/evalTypes.test.tsx` 追加：

```tsx
import { gridCells } from "../src/evalTypes/gsm8k/grid";
import { BatchMonitor } from "../src/components/BatchMonitor";

describe("gridCells", () => {
  it("由 scores 推导格子状态，无分为未跑", () => {
    const run = {
      case_ids: ["c1", "c2", "c3"],
      scores: [
        { case_id: "c1", outcome: "correct", passed: true },
        { case_id: "c2", outcome: "wrong", passed: false },
      ],
    } as any;
    expect(gridCells(run)).toEqual([
      { caseId: "c1", state: "pass" },
      { caseId: "c2", state: "fail" },
      { caseId: "c3", state: "pending" },
    ]);
  });
});

describe("BatchMonitor", () => {
  it("全部终态后显示对比入口与单个结果链接", async () => {
    clientMocks.getRun.mockImplementation(async (id: string) => ({
      id, scenario_version: "gsm8k-test-smoke@1", status: "completed", model: id,
      case_ids: ["c1"], cases: [{ case_id: "c1", result: {} }], scores: [],
    }));
    render(
      <MemoryRouter initialEntries={["/gsm8k/monitor"]}>
        <BatchMonitor
          runIds={["run-42", "run-43"]}
          resultPath={(id) => `/gsm8k/runs/${id}/result`}
          comparePath="/gsm8k/compare?runs=run-42,run-43"
        />
        <LocationProbe />
      </MemoryRouter>
    );
    const compare = await screen.findByRole("link", { name: /查看对比结果/ });
    expect(compare.getAttribute("href")).toBe("/gsm8k/compare?runs=run-42,run-43");
    fireEvent.click(screen.getByRole("link", { name: "run-42" }));
    expect(screen.getByTestId("location").textContent).toBe("/gsm8k/runs/run-42/result");
  });

  it("排队中显示 Worker 提示", async () => {
    clientMocks.getRun.mockResolvedValue({
      id: "run-45", scenario_version: "gsm8k-test-smoke@1", status: "queued", model: "m",
      case_ids: ["c1"], cases: [], scores: [],
    });
    render(
      <MemoryRouter initialEntries={["/gsm8k/monitor"]}>
        <BatchMonitor runIds={["run-45"]} resultPath={(id) => `/gsm8k/runs/${id}/result`} />
      </MemoryRouter>
    );
    expect(await screen.findByText(/等待 Worker 领取/)).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --dir apps/web test -- tests/evalTypes.test.tsx`
Expected: FAIL

- [ ] **Step 3: Implement**

创建 `apps/web/src/components/RunProgress.tsx`：

```tsx
export function RunProgress({ done, total }: { done: number; total: number }) {
  const ratio = total > 0 ? Math.min(1, done / total) : 0;
  return (
    <span className="run-progress">
      <span className="mono">{done}/{total}</span>
      <span className="progress-bar" role="progressbar" aria-valuenow={done} aria-valuemin={0} aria-valuemax={total}>
        <span className="progress-fill" style={{ width: `${Math.round(ratio * 100)}%` }} />
      </span>
    </span>
  );
}
```

创建 `apps/web/src/components/BatchMonitor.tsx`：

```tsx
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { cancelRun, getRun, retryRun, type RunRecord } from "../api/client";
import { StatusBadge } from "./StatusBadge";
import { RunProgress } from "./RunProgress";
import { countDone, isTerminal, useRunEvents } from "../hooks/useRunEvents";

const RETRYABLE = ["failed", "cancelled", "unsupported", "profile_stale"];

function BatchRow({ runId, resultPath, renderDetail }: {
  runId: string;
  resultPath: (runId: string) => string;
  renderDetail?: (runId: string) => ReactNode;
}) {
  const [run, setRun] = useState<RunRecord | null>(null);
  const { events, status } = useRunEvents(runId);
  const [expanded, setExpanded] = useState(false);
  const current = status ?? run?.status ?? "queued";
  const terminal = isTerminal(current);

  useEffect(() => {
    let alive = true;
    const load = () => getRun(runId).then((record) => alive && setRun(record)).catch(() => undefined);
    void load();
    if (!terminal) {
      const timer = setInterval(load, 3000);
      return () => { alive = false; clearInterval(timer); };
    }
    return () => { alive = false; };
  }, [runId, terminal]);

  const total = run?.case_ids?.length ?? 0;
  const done = terminal ? (run?.cases?.length ?? total) : Math.max(countDone(events), run?.cases?.length ?? 0);

  const refresh = () => getRun(runId).then(setRun).catch(() => undefined);

  return (
    <li className="batch-row">
      <div className="batch-row-head">
        <button type="button" className="link" onClick={() => setExpanded((open) => !open)} aria-expanded={expanded}>
          {runId}
        </button>
        <span className="mono">{run?.model ?? "—"}</span>
        <StatusBadge status={current} />
        <RunProgress done={done} total={total} />
        {terminal ? (
          <>
            <Link className="link" to={resultPath(runId)}>结果</Link>
            {RETRYABLE.includes(current) && (
              <button type="button" onClick={() => void retryRun(runId).then(refresh)}>重试</button>
            )}
          </>
        ) : (
          <button type="button" onClick={() => void cancelRun(runId, "web 控制台取消")}>取消</button>
        )}
      </div>
      {current === "queued" && <p className="hint">等待 Worker 领取；若长期排队，请在服务端启动 Worker（make worker）。</p>}
      {terminal && run?.error && (
        <p className="error">
          {run.error.code ?? run.error.type ?? ""} {run.error.message ?? ""}
        </p>
      )}
      {expanded && renderDetail?.(runId)}
    </li>
  );
}

export function BatchMonitor({ runIds, resultPath, comparePath, renderDetail }: {
  runIds: string[];
  resultPath: (runId: string) => string;
  comparePath?: string;
  renderDetail?: (runId: string) => ReactNode;
}) {
  return (
    <section className="panel detail" aria-label="批次过程">
      <div className="panel-head">
        <h2>运行过程</h2>
      </div>
      <ul className="batch-list">
        {runIds.map((runId) => (
          <BatchRow key={runId} runId={runId} resultPath={resultPath} renderDetail={renderDetail} />
        ))}
      </ul>
      {comparePath && <Link className="link" to={comparePath}>查看对比结果</Link>}
    </section>
  );
}
```

`resultPath` 为必填 prop：GSM8K 页传 `suiteRoutes("gsm8k").result`，其余套件同理；`comparePath` 只在页面确认全部 run 终态后传入（`Gsm8kMonitor` 轮询判定）。

创建 `apps/web/src/evalTypes/gsm8k/grid.ts`：

```ts
import type { RunRecord } from "../../api/client";

export interface GridCell {
  caseId: string;
  state: "pass" | "fail" | "pending";
}

/** 由 scores 推导逐题格子状态：有 outcome 按 passed，无分数未跑。 */
export function gridCells(run: RunRecord): GridCell[] {
  const scoreByCase = new Map((run.scores ?? []).map((score) => [score.case_id, score]));
  return (run.case_ids ?? []).map((caseId) => {
    const score = scoreByCase.get(caseId);
    if (!score) return { caseId, state: "pending" };
    return { caseId, state: score.passed ? "pass" : "fail" };
  });
}
```

创建 `apps/web/src/evalTypes/gsm8k/Gsm8kMonitor.tsx`：

```tsx
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { getRun, type RunRecord } from "../../api/client";
import { BatchMonitor } from "../../components/BatchMonitor";
import { isTerminal } from "../../hooks/useRunEvents";
import { gridCells } from "./grid";
import { suiteRoutes } from "../registry";

const ROUTES = suiteRoutes("gsm8k");

function GridDetail({ run }: { run: RunRecord }) {
  return (
    <div className="progress-grid" role="list" aria-label="逐题进度">
      {gridCells(run).map((cell) => (
        <span
          key={cell.caseId}
          className={`grid-cell grid-cell-${cell.state}`}
          role="listitem"
          title={`${cell.caseId}：${cell.state === "pass" ? "答对" : cell.state === "fail" ? "答错" : "未跑"}`}
        >
          {cell.caseId.replace(/^.*?(\d+)$/, "$1")}
        </span>
      ))}
    </div>
  );
}

function Gsm8kRowDetail({ runId }: { runId: string }) {
  const [run, setRun] = useState<RunRecord | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => getRun(runId).then((record) => alive && setRun(record)).catch(() => undefined);
    void load();
    const timer = setInterval(load, 3000);
    return () => { alive = false; clearInterval(timer); };
  }, [runId]);
  return run ? <GridDetail run={run} /> : <p className="hint">加载逐题进度…</p>;
}

export function Gsm8kMonitor() {
  const [params] = useSearchParams();
  const runIds = (params.get("runs") ?? "").split(",").filter(Boolean);
  const [allTerminal, setAllTerminal] = useState(false);

  useEffect(() => {
    if (runIds.length === 0) return;
    const timer = setInterval(async () => {
      const runs = await Promise.all(runIds.map((id) => getRun(id).catch(() => null)));
      setAllTerminal(runs.every((run) => run != null && isTerminal(run.status)));
    }, 3000);
    return () => clearInterval(timer);
  }, [runIds.join(",")]);

  return (
    <div className="page">
      <BatchMonitor
        runIds={runIds}
        resultPath={ROUTES.result}
        comparePath={allTerminal ? ROUTES.compare(runIds) : undefined}
        renderDetail={(runId) => <Gsm8kRowDetail runId={runId} />}
      />
    </div>
  );
}
```

`apps/web/src/index.css` 追加：

```css
/* ---------- 批次过程与逐题格子 ---------- */

.run-progress {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}

.progress-bar {
  display: inline-block;
  width: 120px;
  height: 6px;
  border-radius: 9999px;
  background: var(--tone-neutral-bg);
  overflow: hidden;
}

.progress-fill {
  display: block;
  height: 6px;
  border-radius: 9999px;
  background: var(--tone-info-fg);
}

.batch-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.batch-row {
  padding: 8px 0;
  border-bottom: 1px solid var(--border);
}

.batch-row:last-child {
  border-bottom: none;
}

.batch-row-head {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.progress-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(28px, 1fr));
  gap: 4px;
  margin-top: 8px;
}

.grid-cell {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 24px;
  border-radius: 4px;
  font-size: 11px;
  font-family: var(--font-mono);
}

.grid-cell-pass {
  background: var(--tone-success-bg);
  color: var(--tone-success-fg);
  border: 1px solid var(--tone-success-fg);
}

.grid-cell-fail {
  background: var(--tone-error-bg);
  color: var(--tone-error-fg);
  border: 1px solid var(--tone-error-fg);
}

.grid-cell-pending {
  background: var(--tone-neutral-bg);
  color: var(--text-faint);
}
```

`apps/web/src/App.tsx`：`/gsm8k/monitor` 路由 element `<Gsm8kMonitor />`。

- [ ] **Step 4: Run tests**

Run: `pnpm --dir apps/web test`
Expected: PASS（含 BatchMonitor 终态对比入口与 queued 提示用例）

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/components apps/web/src/evalTypes/gsm8k apps/web/src/App.tsx apps/web/src/index.css apps/web/tests/evalTypes.test.tsx
git commit -m "feat(web): gsm8k batch monitor with per-case grid"
```

---

### Task 9: GSM8K 结果页（指标卡 + 逐题钻取）

**Files:**
- Create: `apps/web/src/components/MetricCards.tsx`
- Create: `apps/web/src/components/CaseDrillTable.tsx`
- Create: `apps/web/src/evalTypes/gsm8k/Gsm8kResult.tsx`
- Modify: `apps/web/src/App.tsx`（`/gsm8k/runs/:runId/result`）
- Modify: `apps/web/src/index.css`（指标卡 / 钻取行）
- Test: `apps/web/tests/evalTypes.test.tsx`（追加）

**Interfaces:**
- Consumes: `getRun` / `getReport` / `rescoreRun`、`run.manifest.benchmark_snapshot.dataset.cases`（题面 `.input`、期望 `.expected`）、`run.cases[].result`（`content` / `error` / `usage`）、`run.scores[]`（`outcome` / `passed`）。
- Produces:
  - `MetricCards({ items }: { items: { label: string; value: string; tone?: "success" | "error" | "neutral" }[] })`
  - `CaseDrillTable({ rows }: { rows: DrillRow[] })`，`DrillRow = { caseId: string; outcomeLabel: string; outcomeTone: "success" | "error" | "neutral"; summary: string; detail: ReactNode }`（Task 11/12 复用）
  - `/gsm8k/runs/:runId/result` 页面

- [ ] **Step 1: Write the failing test**

`apps/web/tests/evalTypes.test.tsx` 追加：

```tsx
import { Gsm8kResult } from "../src/evalTypes/gsm8k/Gsm8kResult";

const GSM8K_RUN = {
  id: "run-42",
  scenario_version: "gsm8k-test-smoke@1",
  status: "completed",
  model: "glm-4.7",
  case_ids: ["case-1", "case-2"],
  cases: [
    { case_id: "case-1", result: { content: "72", usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 } } },
    { case_id: "case-2", result: { content: "答非所问", error: { class: "extraction", message: "无法解析数字" }, usage: { prompt_tokens: 90, completion_tokens: 30, total_tokens: 120 } } },
  ],
  scores: [
    { case_id: "case-1", outcome: "correct", passed: true },
    { case_id: "case-2", outcome: "wrong", passed: false },
  ],
  manifest: {
    benchmark_snapshot: {
      dataset: {
        cases: [
          { case_id: "case-1", input: { question: "Natalia 四月卖了 48 个夹子，五月卖了一半。共多少？" }, expected: "72" },
          { case_id: "case-2", input: { question: "每周存 18 元，四周共多少？" }, expected: "72" },
        ],
      },
    },
  },
};

describe("Gsm8kResult", () => {
  it("渲染指标卡与钻取表，展开失败行看输出与期望", async () => {
    clientMocks.getRun.mockResolvedValue(GSM8K_RUN);
    clientMocks.getReport.mockResolvedValue({
      run_id: "run-42", scenario_version: "gsm8k-test-smoke@1", status: "completed", generated_at: "",
      summary: { cases: 2, scored: 2, passed: 1, failed: 1, pass_rate: 0.5 },
      cost: { total: 0.42, price_table_versions: ["v3"] }, scores: [],
    });
    render(
      <MemoryRouter initialEntries={["/gsm8k/runs/run-42/result"]}>
        <Gsm8kResult />
      </MemoryRouter>
    );
    expect(await screen.findByText("50%")).toBeTruthy();
    expect(screen.getByText("¥0.42")).toBeTruthy();
    expect(screen.getByText("case-2").textContent).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "case-2" }));
    expect(screen.getByText(/无法解析数字/)).toBeTruthy();
    expect(screen.getByText(/Natalia|每周存/)).toBeTruthy();
    expect(screen.getByText(/期望/)).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --dir apps/web test -- tests/evalTypes.test.tsx`
Expected: FAIL

- [ ] **Step 3: Implement**

创建 `apps/web/src/components/MetricCards.tsx`：

```tsx
export interface MetricItem {
  label: string;
  value: string;
  tone?: "success" | "error" | "neutral";
}

export function MetricCards({ items }: { items: MetricItem[] }) {
  return (
    <div className="metric-cards">
      {items.map((item) => (
        <div key={item.label} className="metric-card" data-tone={item.tone ?? "neutral"}>
          <p className="metric-value mono">{item.value}</p>
          <p className="metric-label">{item.label}</p>
        </div>
      ))}
    </div>
  );
}
```

创建 `apps/web/src/components/CaseDrillTable.tsx`：

```tsx
import { useState, type ReactNode } from "react";

export interface DrillRow {
  caseId: string;
  outcomeLabel: string;
  outcomeTone: "success" | "error" | "neutral";
  summary: string;
  detail: ReactNode;
}

export function CaseDrillTable({ rows }: { rows: DrillRow[] }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <table className="drill-table">
      <thead>
        <tr>
          <th>Case</th>
          <th>判定</th>
          <th>摘要</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <>
            <tr key={row.caseId}>
              <td>
                <button type="button" className="link" onClick={() => setOpen(open === row.caseId ? null : row.caseId)} aria-expanded={open === row.caseId}>
                  {row.caseId}
                </button>
              </td>
              <td><span className={`status-badge status-tone-${row.outcomeTone}`}>{row.outcomeLabel}</span></td>
              <td className="muted">{row.summary}</td>
            </tr>
            {open === row.caseId && (
              <tr key={`${row.caseId}-detail`} className="drill-detail-row">
                <td colSpan={3}><div className="drill-detail">{row.detail}</div></td>
              </tr>
            )}
          </>
        ))}
        {rows.length === 0 && (
          <tr><td colSpan={3} className="empty">暂无记录</td></tr>
        )}
      </tbody>
    </table>
  );
}
```

注意：`<>…</>` 片段里两个 `<tr>` 各自带 key 的写法要改成 `<Fragment key={row.caseId}>`（从 react 导入 `Fragment`），避免 React key 警告。

创建 `apps/web/src/evalTypes/gsm8k/Gsm8kResult.tsx`：

```tsx
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { getReport, getRun, rescoreRun, type RunRecord } from "../../api/client";
import { MetricCards } from "../../components/MetricCards";
import { CaseDrillTable, type DrillRow } from "../../components/CaseDrillTable";
import { StatusBadge } from "../../components/StatusBadge";

const OUTCOME_LABELS: Record<string, { label: string; tone: "success" | "error" | "neutral" }> = {
  correct: { label: "答对", tone: "success" },
  wrong: { label: "答错", tone: "error" },
  not_attempted: { label: "未尝试", tone: "neutral" },
};

function outputText(result: any): string {
  if (result == null) return "（无结果）";
  if (typeof result.content === "string" && result.content) return result.content.slice(0, 120);
  if (result.error?.message) return `错误：${result.error.message}`;
  return "（空输出）";
}

export function Gsm8kResult() {
  const { runId = "" } = useParams();
  const [run, setRun] = useState<RunRecord | null>(null);
  const [cost, setCost] = useState<number | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    getRun(runId).then(setRun).catch((e) => setError(String(e)));
    getReport(runId).then((report) => setCost(report.cost?.total ?? null)).catch(() => undefined);
  }, [runId]);

  if (error) return <div className="page"><section className="panel detail"><p className="error">{error}</p></section></div>;
  if (!run) return <div className="page"><section className="panel detail"><p className="empty">加载中</p></section></div>;

  const snapshotCases = run.manifest?.benchmark_snapshot?.dataset?.cases as
    | { case_id: string; input: any; expected: any }[] | undefined;
  const scores = run.scores ?? [];
  const passed = scores.filter((score) => score.passed).length;
  const usage = (run.cases ?? []).reduce(
    (sum, row) => ({
      prompt: sum.prompt + (row.result?.usage?.prompt_tokens ?? 0),
      completion: sum.completion + (row.result?.usage?.completion_tokens ?? 0),
    }),
    { prompt: 0, completion: 0 },
  );
  const accuracy = scores.length > 0 ? Math.round((passed / scores.length) * 100) : null;
  const attempted = scores.filter((score) => (score as any).attempted !== false).length;

  const rows: DrillRow[] = (run.case_ids ?? []).map((caseId) => {
    const score = scores.find((item) => item.case_id === caseId);
    const outcome = OUTCOME_LABELS[(score as any)?.outcome ?? ""] ?? { label: score?.passed ? "通过" : "未通过", tone: score?.passed ? "success" : "error" };
    const datasetCase = snapshotCases?.find((item) => item.case_id === caseId);
    const result = (run.cases ?? []).find((item) => item.case_id === caseId)?.result;
    const question = datasetCase ? (datasetCase.input?.question ?? JSON.stringify(datasetCase.input)) : "（题面缺失）";
    const expected = datasetCase?.expected ?? "（期望缺失）";
    return {
      caseId,
      outcomeLabel: outcome.tone === "neutral" ? outcome.label : outcome.label,
      outcomeTone: outcome.tone,
      summary: outputText(result),
      detail: (
        <div className="drill-detail">
          <p><span className="field-label">题目</span>{question}</p>
          <p><span className="field-label">模型输出</span><span className="mono">{typeof result?.content === "string" ? result.content : JSON.stringify(result?.content ?? result)}</span></p>
          {result?.error && (
            <p><span className="field-label">失败原因</span><span className="fail">{result.error.class ? `${result.error.class}：` : ""}{result.error.message}</span></p>
          )}
          <p><span className="field-label">期望</span><span className="mono">{typeof expected === "string" ? expected : JSON.stringify(expected)}</span></p>
        </div>
      ),
    };
  });

  return (
    <div className="page">
      <section className="panel detail">
        <div className="panel-head">
          <h2 className="mono">运行 {runId} · 结果</h2>
          <div className="panel-head-actions">
            <StatusBadge status={run.status} />
            <button type="button" onClick={async () => { try { await rescoreRun(runId); setRun(await getRun(runId)); } catch (e) { setError(String(e)); } }}>
              重新评分
            </button>
          </div>
        </div>
        {run.error && (
          <p className="error">
            {run.error.code ?? run.error.type ?? ""} {run.error.message ?? ""}
          </p>
        )}
        <MetricCards items={[
          { label: `accuracy · ${passed}/${scores.length}`, value: accuracy == null ? "—" : `${accuracy}%`, tone: "success" },
          { label: `tokens（输入 ${usage.prompt} + 输出 ${usage.completion}）`, value: String(usage.prompt + usage.completion), tone: "neutral" },
          { label: "成本", value: cost == null ? "—" : `¥${cost}`, tone: "neutral" },
          { label: `口径（选中 ${run.case_ids?.length ?? 0} · 应答 ${attempted} · 正确 ${passed}）`, value: "", tone: "neutral" },
        ]} />
        <CaseDrillTable rows={rows} />
      </section>
    </div>
  );
}
```

（`run.error` 为后端失败信封 `{type?, code?, message?}`，已脱敏；batch 行的错误展示见 Task 8 的 `BatchRow`。）

`apps/web/src/index.css` 追加：

```css
/* ---------- 指标卡与钻取行 ---------- */

.metric-cards {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 16px;
}

.metric-card {
  flex: 1 1 140px;
  min-width: 130px;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  text-align: center;
}

.metric-card[data-tone="success"] .metric-value {
  color: var(--tone-success-fg);
}

.metric-card[data-tone="error"] .metric-value {
  color: var(--tone-error-fg);
}

.metric-value {
  margin: 0;
  font-size: 26px;
  font-weight: 600;
}

.metric-label {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--text-secondary);
}

.drill-detail {
  border-left: 3px solid var(--tone-error-fg);
  background: var(--bg-subtle);
  border-radius: 0 6px 6px 0;
  padding: 8px 12px;
  font-size: 13px;
}

.drill-detail p {
  margin: 4px 0;
}

.drill-detail .field-label {
  margin-right: 8px;
}
```

`apps/web/src/App.tsx`：`/gsm8k/runs/:runId/result` element `<Gsm8kResult />`。

- [ ] **Step 4: Run tests**

Run: `pnpm --dir apps/web test`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/components apps/web/src/evalTypes/gsm8k apps/web/src/App.tsx apps/web/src/index.css apps/web/tests/evalTypes.test.tsx
git commit -m "feat(web): gsm8k result page with metric cards and case drilldown"
```

---

### Task 10: GSM8K 对比页

**Files:**
- Create: `apps/web/src/evalTypes/gsm8k/Gsm8kCompare.tsx`
- Modify: `apps/web/src/App.tsx`（`/gsm8k/compare`）
- Test: `apps/web/tests/evalTypes.test.tsx`（追加）

**Interfaces:**
- Consumes: `getRun`（每 run）、`suiteRoutes("gsm8k").result`。
- Produces: `/gsm8k/compare?runs=a,b`：模型列 × 指标行（accuracy / tokens / 成本）+ 答错题重合列表（交集），每项可展开看各模型输出与期望。

- [ ] **Step 1: Write the failing test**

```tsx
import { Gsm8kCompare } from "../src/evalTypes/gsm8k/Gsm8kCompare";

describe("Gsm8kCompare", () => {
  it("并排指标与答错重合", async () => {
    clientMocks.getRun.mockImplementation(async (id: string) => ({
      id,
      scenario_version: "gsm8k-test-smoke@1",
      status: "completed",
      model: `model-${id}`,
      case_ids: ["case-1", "case-2", "case-3"],
      cases: [],
      scores: id === "run-41"
        ? [{ case_id: "case-1", passed: true }, { case_id: "case-2", passed: false }, { case_id: "case-3", passed: true }]
        : [{ case_id: "case-1", passed: true }, { case_id: "case-2", passed: false }, { case_id: "case-3", passed: false }],
      manifest: { benchmark_snapshot: { dataset: { cases: [
        { case_id: "case-1", input: { question: "Q1" }, expected: "1" },
        { case_id: "case-2", input: { question: "Q2" }, expected: "2" },
        { case_id: "case-3", input: { question: "Q3" }, expected: "3" },
      ] } } },
    }));
    render(
      <MemoryRouter initialEntries={["/gsm8k/compare?runs=run-41,run-42"]}>
        <Gsm8kCompare />
      </MemoryRouter>
    );
    expect(await screen.findByText("model-run-41")).toBeTruthy();
    expect(screen.getByText("model-run-42")).toBeTruthy();
    expect(screen.getByText("67%")).toBeTruthy();
    expect(screen.getByText("33%")).toBeTruthy();
    expect(screen.getByText(/case-2/)).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --dir apps/web test -- tests/evalTypes.test.tsx`
Expected: FAIL

- [ ] **Step 3: Implement**

创建 `apps/web/src/evalTypes/gsm8k/Gsm8kCompare.tsx`：

```tsx
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { getRun, type RunRecord } from "../../api/client";
import { suiteRoutes } from "../registry";

const ROUTES = suiteRoutes("gsm8k");

interface Column {
  runId: string;
  model: string;
  accuracy: number | null;
  tokens: number;
  failedCases: string[];
  run: RunRecord;
}

function collect(run: RunRecord): Column {
  const scores = run.scores ?? [];
  const passed = scores.filter((score) => score.passed);
  const tokens = (run.cases ?? []).reduce((sum, row) => sum + (row.result?.usage?.total_tokens ?? 0), 0);
  return {
    runId: run.id,
    model: run.model ?? run.id,
    accuracy: scores.length > 0 ? Math.round((passed.length / scores.length) * 100) : null,
    tokens,
    failedCases: scores.filter((score) => !score.passed).map((score) => score.case_id),
    run,
  };
}

export function Gsm8kCompare() {
  const [params] = useSearchParams();
  const runIds = (params.get("runs") ?? "").split(",").filter(Boolean);
  const [columns, setColumns] = useState<Column[] | null>(null);
  const [error, setError] = useState("");
  const [openCase, setOpenCase] = useState<string | null>(null);

  useEffect(() => {
    Promise.all(runIds.map((id) => getRun(id)))
      .then((runs) => setColumns(runs.map(collect)))
      .catch((e) => setError(String(e)));
  }, [runIds.join(",")]);

  if (error) return <div className="page"><section className="panel detail"><p className="error">{error}</p></section></div>;
  if (!columns) return <div className="page"><section className="panel detail"><p className="empty">加载中</p></section></div>;

  const sharedFailed = columns
    .map((column) => new Set(column.failedCases))
    .reduce((acc, set) => new Set([...acc].filter((id) => set.has(id))));

  const snapshotCases = (columns[0]?.run.manifest?.benchmark_snapshot?.dataset?.cases ?? []) as
    { case_id: string; input: any; expected: any }[];

  return (
    <div className="page">
      <section className="panel detail">
        <div className="panel-head">
          <h2>GSM8K · 多模型对比</h2>
        </div>
        <table>
          <thead>
            <tr>
              <th>指标</th>
              {columns.map((column) => (
                <th key={column.runId}>
                  {column.model}
                  <span className="muted mono"> {column.runId}</span>
                  <Link className="link" to={ROUTES.result(column.runId)}>详情</Link>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr><td>accuracy</td>{columns.map((c) => <td key={c.runId} className="mono">{c.accuracy == null ? "—" : `${c.accuracy}%`}</td>)}</tr>
            <tr><td>tokens</td>{columns.map((c) => <td key={c.runId} className="mono">{c.tokens}</td>)}</tr>
            <tr><td>答错题重合</td><td colSpan={columns.length} className="mono">{[...sharedFailed].join(", ") || "无"}</td></tr>
          </tbody>
        </table>

        <h3 className="embed-title">逐题下钻</h3>
        <ul className="compare-case-list">
          {(columns[0]?.run.case_ids ?? []).map((caseId) => (
            <li key={caseId}>
              <button type="button" className="link" onClick={() => setOpenCase(openCase === caseId ? null : caseId)}>
                {caseId}
              </button>
              {openCase === caseId && (
                <div className="drill-detail">
                  <p><span className="field-label">题目</span>{snapshotCases.find((c) => c.case_id === caseId)?.input?.question ?? "（题面缺失）"}</p>
                  <p><span className="field-label">期望</span><span className="mono">{String(snapshotCases.find((c) => c.case_id === caseId)?.expected ?? "")}</span></p>
                  {columns.map((column) => {
                    const result = column.run.cases?.find((item) => item.case_id === caseId)?.result;
                    return (
                      <p key={column.runId}>
                        <span className="field-label">{column.model}</span>
                        <span className="mono">{typeof result?.content === "string" ? result.content : JSON.stringify(result?.content ?? "（无结果）")}</span>
                      </p>
                    );
                  })}
                </div>
              )}
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
```

`apps/web/src/index.css` 追加：

```css
.compare-case-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.compare-case-list li {
  display: inline-block;
}
```

`apps/web/src/App.tsx`：`/gsm8k/compare` element `<Gsm8kCompare />`。

- [ ] **Step 4: Run tests**

Run: `pnpm --dir apps/web test`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/evalTypes/gsm8k apps/web/src/App.tsx apps/web/src/index.css apps/web/tests/evalTypes.test.tsx
git commit -m "feat(web): gsm8k multi-model compare page"
```

---

### Task 11: Direct LLM 三页

**Files:**
- Create: `apps/web/src/evalTypes/directllm/DirectLlmPages.tsx`（Operate / Monitor / Result 同文件）
- Modify: `apps/web/src/App.tsx`（`/direct-llm`、`/direct-llm/monitor`、`/direct-llm/runs/:runId/result`）
- Test: `apps/web/tests/evalTypes.test.tsx`（追加）

**Interfaces:**
- Consumes: `createRun`（`{ scenario_version, manifest: { model, parameters? }, case_ids }`）、`ModelPicker`、`BatchMonitor`、`CaseDrillTable`、`RunTimeline`、`suiteRoutes("direct-llm")`。
- Produces: Direct LLM 三页。Operate 参数：temperature / max_output_tokens（≤ 模型 `max_output_tokens` 上限，前端仅提示后端 422 兜底）/ reasoning_level（选了带 `reasoning.levels` 的模型时下拉）。

- [ ] **Step 1: Write the failing test**

```tsx
import { DirectLlmOperate, DirectLlmResult } from "../src/evalTypes/directllm/DirectLlmPages";

describe("DirectLlmOperate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clientMocks.getModels.mockResolvedValue({
      items: [
        { id: "glm-4.7", provider: "zhipu", capabilities: {}, reasoning: { supported: true, levels: ["low", "high"], default_level: "high", control: "{}" } },
      ],
    });
    clientMocks.getScenarios.mockResolvedValue({ items: [{ name: "direct-llm", version: "1" }] });
  });

  it("选模型填 case 后按 manifest.model 创建并跳批次过程页", async () => {
    clientMocks.createRun.mockResolvedValue({ id: "run-51", status: "queued" });
    render(
      <MemoryRouter initialEntries={["/direct-llm"]}>
        <DirectLlmOperate />
        <LocationProbe />
      </MemoryRouter>
    );
    await screen.findByLabelText("选择模型 glm-4.7");
    fireEvent.click(screen.getByLabelText("选择模型 glm-4.7"));
    fireEvent.change(screen.getByLabelText("Case 列表（逗号分隔）"), { target: { value: "case-1, case-2" } });
    fireEvent.click(screen.getByRole("button", { name: /发起评测/ }));
    await waitFor(() => expect(clientMocks.createRun).toHaveBeenCalledWith({
      scenario_version: "direct-llm@1",
      manifest: { model: "glm-4.7" },
      case_ids: ["case-1", "case-2"],
    }));
    expect(screen.getByTestId("location").textContent).toBe("/direct-llm/monitor?runs=run-51");
  });
});

describe("DirectLlmResult", () => {
  it("期望对比钻取", async () => {
    clientMocks.getRun.mockResolvedValue({
      id: "run-51", scenario_version: "direct-llm@1", status: "completed", model: "glm-4.7",
      case_ids: ["case-1"],
      cases: [{ case_id: "case-1", result: { content: "42" }, expected: 42 }],
      scores: [{ case_id: "case-1", passed: false }],
    });
    render(
      <MemoryRouter initialEntries={["/direct-llm/runs/run-51/result"]}>
        <DirectLlmResult />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText("case-1")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "case-1" }));
    expect(screen.getByText(/期望/)).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --dir apps/web test -- tests/evalTypes.test.tsx`
Expected: FAIL

- [ ] **Step 3: Implement**

创建 `apps/web/src/evalTypes/directllm/DirectLlmPages.tsx`：

```tsx
import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { createRun, getModels, getRun, type ModelRecord, type RunRecord } from "../../api/client";
import { ModelPicker } from "../../components/ModelPicker";
import { BatchMonitor } from "../../components/BatchMonitor";
import { MetricCards } from "../../components/MetricCards";
import { CaseDrillTable, type DrillRow } from "../../components/CaseDrillTable";
import { RunTimeline } from "../../components/RunTimeline";
import { useRunEvents } from "../../hooks/useRunEvents";
import { suiteRoutes } from "../registry";

const ROUTES = suiteRoutes("direct-llm");
const SCENARIO = "direct-llm@1";

export function DirectLlmOperate() {
  const navigate = useNavigate();
  const [models, setModels] = useState<ModelRecord[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [caseIds, setCaseIds] = useState("");
  const [temperature, setTemperature] = useState("");
  const [maxTokens, setMaxTokens] = useState("");
  const [reasoningLevel, setReasoningLevel] = useState("");
  const [failures, setFailures] = useState<{ model: string; error: string }[]>([]);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);

  useEffect(() => {
    getModels().then((payload) => setModels(payload.items)).catch(() => undefined);
  }, []);

  const selectedModel = models.find((model) => selected.length === 1 && model.id === selected[0]);
  const ceiling = selectedModel?.max_output_tokens ?? null;

  const doRun = async () => {
    setRunning(true);
    setFailures([]);
    const ids = caseIds.split(/[,，\s]+/).filter(Boolean);
    const parameters: Record<string, number> = {};
    if (temperature.trim()) parameters.temperature = Number(temperature);
    if (maxTokens.trim()) parameters.max_output_tokens = Number(maxTokens);
    const manifest: Record<string, unknown> = { model: selected[0] };
    if (Object.keys(parameters).length > 0) manifest.parameters = parameters;
    if (reasoningLevel) manifest.reasoning_level = reasoningLevel;
    try {
      const created: string[] = [];
      const failed: { model: string; error: string }[] = [];
      for (const model of selected) {
        try {
          const run = await createRun({
            scenario_version: SCENARIO,
            manifest: { ...manifest, model },
            case_ids: ids,
          });
          created.push(run.id);
        } catch (e) {
          failed.push({ model, error: String(e) });
        }
      }
      setFailures(failed);
      if (created.length > 0) {
        navigate(ROUTES.monitor(created));
        return;
      }
      if (failed.length === 0) setError("请先选择至少一个模型并填写 Case 列表");
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="page">
      <section className="panel detail">
        <div className="panel-head"><h2>Direct LLM 评测 · 操作</h2></div>
        {error && <p className="error">{error}</p>}
        <div className="operate-grid">
          <div className="operate-card">
            <h3 className="embed-title">模型（可多选对比）</h3>
            <ModelPicker models={models} selected={selected} onToggle={(id) =>
              setSelected((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id])} />
          </div>
          <div className="operate-card">
            <h3 className="embed-title">场景与样本</h3>
            <label>
              场景
              <input value={SCENARIO} readOnly className="mono" />
            </label>
            <label>
              Case 列表（逗号分隔）
              <input value={caseIds} onChange={(change) => setCaseIds(change.target.value)} placeholder="case-1, case-2" />
            </label>
          </div>
          <div className="operate-card">
            <h3 className="embed-title">参数（可选）</h3>
            <label>
              temperature
              <input type="number" step="0.1" min="0" value={temperature} onChange={(change) => setTemperature(change.target.value)} />
            </label>
            <label>
              max_output_tokens{ceiling != null ? `（≤ ${ceiling}）` : ""}
              <input type="number" min="1" value={maxTokens} onChange={(change) => setMaxTokens(change.target.value)} />
            </label>
            {selectedModel?.reasoning?.supported && (
              <label>
                推理等级（默认 {selectedModel.reasoning.default_level ?? "无"}）
                <select className="mono" value={reasoningLevel} onChange={(change) => setReasoningLevel(change.target.value)}>
                  <option value="">默认</option>
                  {selectedModel.reasoning.levels.map((level) => <option key={level} value={level}>{level}</option>)}
                </select>
              </label>
            )}
          </div>
        </div>
        <div className="actions">
          <button type="button" onClick={() => void doRun()} disabled={running || selected.length === 0 || !caseIds.trim()}>
            {running ? "创建中…" : `发起评测（${selected.length} 个模型 × ${caseIds.split(/[,，\s]+/).filter(Boolean).length} case）`}
          </button>
          <span className="hint">真实调用 · 产生费用</span>
        </div>
        {failures.length > 0 && (
          <ul>{failures.map((f) => <li key={f.model} className="error">{f.model}：{f.error}</li>)}</ul>
        )}
      </section>
    </div>
  );
}

function CaseStream({ runId }: { runId: string }) {
  const { events } = useRunEvents(runId);
  const responses = events.filter((event) => event.type === "model_response");
  if (responses.length === 0) return <p className="hint">等待首个 case 响应…</p>;
  return (
    <div className="case-stream">
      {responses.map((event) => (
        <div key={event.seq} className="case-card">
          <p className="field-label mono">{event.case_id}</p>
          <p className="mono">{typeof event.result?.content === "string" ? event.result.content : JSON.stringify(event.result ?? "")}</p>
        </div>
      ))}
    </div>
  );
}

export function DirectLlmMonitor() {
  const [params] = useSearchParams();
  const runIds = (params.get("runs") ?? "").split(",").filter(Boolean);
  return (
    <div className="page">
      <BatchMonitor runIds={runIds} resultPath={ROUTES.result} renderDetail={(runId) => <CaseStream runId={runId} />} />
    </div>
  );
}

export function DirectLlmResult() {
  const { runId = "" } = useParams();
  const [run, setRun] = useState<RunRecord | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    getRun(runId).then(setRun).catch((e) => setError(String(e)));
  }, [runId]);

  if (error) return <div className="page"><section className="panel detail"><p className="error">{error}</p></section></div>;
  if (!run) return <div className="page"><section className="panel detail"><p className="empty">加载中</p></section></div>;

  const scores = run.scores ?? [];
  const passed = scores.filter((score) => score.passed).length;
  const rows: DrillRow[] = (run.cases ?? []).map((entry) => ({
    caseId: entry.case_id,
    outcomeLabel: scores.find((score) => score.case_id === entry.case_id)?.passed ? "一致" : "不一致",
    outcomeTone: scores.find((score) => score.case_id === entry.case_id)?.passed ? "success" : "error",
    summary: typeof entry.result?.content === "string" ? entry.result.content.slice(0, 120) : "（无输出）",
    detail: (
      <div className="drill-detail">
        <p><span className="field-label">实际输出</span><span className="mono">{typeof entry.result?.content === "string" ? entry.result.content : JSON.stringify(entry.result?.content ?? "")}</span></p>
        <p><span className="field-label">期望</span><span className="mono">{typeof entry.expected === "string" ? entry.expected : JSON.stringify(entry.expected ?? "（无）")}</span></p>
      </div>
    ),
  }));

  return (
    <div className="page">
      <section className="panel detail">
        <div className="panel-head"><h2 className="mono">运行 {runId} · 结果</h2></div>
        <MetricCards items={[
          { label: `通过 · ${passed}/${scores.length}`, value: scores.length > 0 ? `${Math.round((passed / scores.length) * 100)}%` : "—", tone: "success" },
          { label: "模型", value: run.model ?? "—", tone: "neutral" },
        ]} />
        <CaseDrillTable rows={rows} />
      </section>
    </div>
  );
}
```

`apps/web/src/index.css` 追加：

```css
/* ---------- Direct LLM case 流式卡片 ---------- */

.case-stream {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 8px;
}

.case-card {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 12px;
  background: var(--bg-subtle);
  font-size: 13px;
}

.case-card p {
  margin: 2px 0;
}
```

`apps/web/src/App.tsx`：三条路由。

- [ ] **Step 4: Run tests**

Run: `pnpm --dir apps/web test`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/evalTypes/directllm apps/web/src/App.tsx apps/web/src/index.css apps/web/tests/evalTypes.test.tsx
git commit -m "feat(web): direct-llm suite pages"
```

---

### Task 12: Replay 三页

**Files:**
- Create: `apps/web/src/evalTypes/replay/ReplayPages.tsx`
- Modify: `apps/web/src/App.tsx`（`/replay`、`/replay/monitor`、`/replay/runs/:runId/result`）
- Test: `apps/web/tests/evalTypes.test.tsx`（追加）

**Interfaces:**
- Consumes: `createRun`（`{ scenario_version, manifest（inline provider 正式入口）, case_ids }`）、`BatchMonitor`、`CaseDrillTable`、`RunTimeline`、`useRunEvents`。
- Produces: Replay 三页。Operate：场景下拉（replay@1 / json_extract@1）+ Case 列表 + Manifest JSON textarea（专家模式为正式形态）。

- [ ] **Step 1: Write the failing test**

```tsx
import { ReplayOperate } from "../src/evalTypes/replay/ReplayPages";

describe("ReplayOperate", () => {
  it("手写 Manifest 作为 inline provider 创建运行", async () => {
    clientMocks.createRun.mockResolvedValue({ id: "run-61", status: "queued" });
    render(
      <MemoryRouter initialEntries={["/replay"]}>
        <ReplayOperate />
        <LocationProbe />
      </MemoryRouter>
    );
    fireEvent.change(screen.getByLabelText("Case 列表（逗号分隔）"), { target: { value: "case-1" } });
    fireEvent.change(screen.getByLabelText(/Manifest JSON/), {
      target: { value: '{"provider":{"kind":"replay","model":"fixture-model"}}' },
    });
    fireEvent.click(screen.getByRole("button", { name: /创建回放/ }));
    await waitFor(() => expect(clientMocks.createRun).toHaveBeenCalledWith({
      scenario_version: "replay@1",
      manifest: { provider: { kind: "replay", model: "fixture-model" } },
      case_ids: ["case-1"],
    }));
    expect(screen.getByTestId("location").textContent).toBe("/replay/monitor?runs=run-61");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --dir apps/web test -- tests/evalTypes.test.tsx`
Expected: FAIL

- [ ] **Step 3: Implement**

创建 `apps/web/src/evalTypes/replay/ReplayPages.tsx`：

```tsx
import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { createRun, getRun, type RunRecord } from "../../api/client";
import { BatchMonitor } from "../../components/BatchMonitor";
import { CaseDrillTable, type DrillRow } from "../../components/CaseDrillTable";
import { RunTimeline } from "../../components/RunTimeline";
import { useRunEvents } from "../../hooks/useRunEvents";
import { suiteRoutes } from "../registry";

const ROUTES = suiteRoutes("replay");
const SCENARIOS = ["replay@1", "json_extract@1"];

export function ReplayOperate() {
  const navigate = useNavigate();
  const [scenario, setScenario] = useState(SCENARIOS[0]);
  const [caseIds, setCaseIds] = useState("case-1");
  const [manifest, setManifest] = useState("");
  const [error, setError] = useState("");

  const submit = () => {
    setError("");
    let parsed: any = {};
    if (manifest.trim()) {
      try {
        parsed = JSON.parse(manifest);
      } catch (e) {
        setError(`Manifest JSON 无法解析：${e}`);
        return;
      }
    }
    createRun({
      scenario_version: scenario,
      manifest: parsed,
      case_ids: caseIds.split(/[,，\s]+/).filter(Boolean),
    })
      .then((run) => navigate(ROUTES.monitor([run.id])))
      .catch((e) => setError(String(e)));
  };

  return (
    <div className="page">
      <section className="panel form-panel">
        <h2>Replay 回放 · 操作</h2>
        {error && <p className="error">{error}</p>}
        <label>
          场景
          <select className="mono" value={scenario} onChange={(change) => setScenario(change.target.value)}>
            {SCENARIOS.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label>
          Case 列表（逗号分隔）
          <input value={caseIds} onChange={(change) => setCaseIds(change.target.value)} />
        </label>
        <label>
          Manifest JSON（inline provider 或 replay fixture）
          <textarea className="mono" rows={6} value={manifest} onChange={(change) => setManifest(change.target.value)}
            placeholder='{"provider":{"kind":"replay","fixture":{…}}}' />
        </label>
        <button type="button" onClick={submit}>创建回放</button>
        <p className="hint">回放不产生模型费用；fixture 与期望在 Manifest 或 replay 接口中提供。</p>
      </section>
    </div>
  );
}

function ReplayRowDetail({ runId }: { runId: string }) {
  const { events } = useRunEvents(runId);
  return <RunTimeline events={events} embedded />;
}

export function ReplayMonitor() {
  const [params] = useSearchParams();
  const runIds = (params.get("runs") ?? "").split(",").filter(Boolean);
  return (
    <div className="page">
      <BatchMonitor runIds={runIds} resultPath={ROUTES.result} renderDetail={(runId) => <ReplayRowDetail runId={runId} />} />
    </div>
  );
}

export function ReplayResult() {
  const { runId = "" } = useParams();
  const [run, setRun] = useState<RunRecord | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    getRun(runId).then(setRun).catch((e) => setError(String(e)));
  }, [runId]);

  if (error) return <div className="page"><section className="panel detail"><p className="error">{error}</p></section></div>;
  if (!run) return <div className="page"><section className="panel detail"><p className="empty">加载中</p></section></div>;

  const scores = run.scores ?? [];
  const passed = scores.filter((score) => score.passed).length;
  const rows: DrillRow[] = (run.cases ?? []).map((entry) => ({
    caseId: entry.case_id,
    outcomeLabel: scores.find((score) => score.case_id === entry.case_id)?.passed ? "一致" : "不一致",
    outcomeTone: scores.find((score) => score.case_id === entry.case_id)?.passed ? "success" : "error",
    summary: typeof entry.result?.content === "string" ? entry.result.content.slice(0, 120) : JSON.stringify(entry.result ?? ""),
    detail: (
      <div className="drill-detail">
        <p><span className="field-label">实际</span><span className="mono">{typeof entry.result?.content === "string" ? entry.result.content : JSON.stringify(entry.result ?? "")}</span></p>
        <p><span className="field-label">期望</span><span className="mono">{typeof entry.expected === "string" ? entry.expected : JSON.stringify(entry.expected ?? "（无）")}</span></p>
      </div>
    ),
  }));

  return (
    <div className="page">
      <section className="panel detail">
        <div className="panel-head"><h2 className="mono">运行 {runId} · 结果</h2></div>
        <p className="summary">共 {scores.length} 项，一致 {passed}，不一致 {scores.length - passed}</p>
        <CaseDrillTable rows={rows} />
      </section>
    </div>
  );
}
```

`apps/web/src/App.tsx`：三条路由。

- [ ] **Step 4: Run tests**

Run: `pnpm --dir apps/web test`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/evalTypes/replay apps/web/src/App.tsx apps/web/tests/evalTypes.test.tsx
git commit -m "feat(web): replay suite pages with manifest expert mode"
```

---

### Task 13: 收尾：删旧页、重定向、DESIGN.md 同步、门禁

**Files:**
- Delete: `apps/web/src/pages/RunsPage.tsx`、`apps/web/src/pages/BenchmarksPage.tsx`
- Modify: `apps/web/src/App.tsx`（`/` 与 `*` 重定向改 `/gsm8k`；删除 `/benchmarks` 路由与旧页 import）
- Modify: `apps/web/DESIGN.md`（应用骨架与新增组件规范，与代码同一提交）
- Test: `apps/web/tests/app.test.tsx`（更新断言）

**Interfaces:**
- Consumes: 前序全部任务的页面。
- Produces: 最终信息架构：`/` → `/gsm8k`；侧边栏「评测类型」三入口 + 「通用」三项（运行总览 / Provider / Harness）；`/benchmarks` 不再存在。

- [ ] **Step 1: Update the failing test**

`apps/web/tests/app.test.tsx` 追加 / 调整：

```tsx
describe("应用路由（收尾）", () => {
  it("根路径重定向到 GSM8K 专区", async () => {
    renderWithLocation("/");
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/gsm8k"));
  });

  it("侧边栏含三个类型入口", () => {
    renderWithLocation("/gsm8k");
    expect(screen.getByRole("link", { name: /GSM8K 数学评测/ })).toBeTruthy();
    expect(screen.getByRole("link", { name: /Direct LLM 评测/ })).toBeTruthy();
    expect(screen.getByRole("link", { name: /Replay 回放/ })).toBeTruthy();
  });
});
```

（`renderWithLocation` 用 Task 2 的 MemoryRouter + LocationProbe 结构正式化为文件内公共 helper。）

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --dir apps/web test -- tests/app.test.tsx`
Expected: FAIL（`/` 仍指向 `/runs`）

- [ ] **Step 3: Implement**

`apps/web/src/App.tsx`：

1. `<Route path="/" element={<Navigate to="/gsm8k" replace />} />` 与 `*` 路由同样改 `/gsm8k`；
2. 删除 `/runs` 旧 `RunsPage`、`/benchmarks` 路由与两个 import。

```bash
git rm apps/web/src/pages/RunsPage.tsx apps/web/src/pages/BenchmarksPage.tsx
```

`apps/web/DESIGN.md` 变更（同一提交）：

1. 第 4 节「应用骨架」行改为：`.app-shell` 使用 fixed 定位与 `inset: var(--space-none)` 固定在视口内，外框不滚动；左侧悬浮导航栏（216px 白卡、1px 边框、8px 圆角、品牌区 + 路由侧边栏，图标 + 文字，导航项由 react-router `NavLink` 驱动、active 态 `aria-current="page"` 同 Radix active 观感），分组标签「评测类型」「通用」用 `.nav-group-label`（11px `--text-faint`）；右侧全屏工作台独立滚动（`.page` 通栏铺满、padding 16/32）。
2. 第 4 节表格追加组件行：
   - 类型操作页三卡：`.operate-grid` / `.operate-card`（flex 换行、1px 边框 8px 圆角、`.embed-title` 分节）
   - 步进与批次行：`.batch-row-head`（run ID link + mono 模型 + 状态徽章 + `RunProgress` x/N 进度条 + 操作）；排队提示用 `.hint`
   - 逐题格子：`.progress-grid`（auto-fill 28px 格）+ `.grid-cell-pass/fail/pending`（语义粉彩底 + 对应前景/描边，禁止新色）
   - 指标卡：`.metric-cards` / `.metric-card`（flex 换行、居中、26px mono 数值、12px 次色标签；success/error 语气只染数值色）
   - 钻取行：`.drill-detail`（3px 左语气条 + `--bg-subtle` 底、6px 圆角右侧、`.field-label` 前缀）
   - 对比页：模型列 × 指标行表格沿用通用表格规范；`.compare-case-list` 逐题下钻用 link 按钮 + `.drill-detail`
3. 第 7 节禁止清单不变；新增依赖说明：路由用 react-router-dom（无视觉），不违反「禁带视觉主见组件库」。

- [ ] **Step 4: Full gates**

Run: `pnpm --dir apps/web test && pnpm --dir apps/web build`
Expected: 全绿

Run: `make check`（仓库根）
Expected: lint + test + web build + compose config 全绿

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(web): finalize type suite console navigation and design doc"
```

---

## 执行顺序与依赖

Task 1（API）独立先行；Task 2（路由底座）是所有前端任务的地基；Task 3/4（hook、ModelPicker）相互独立可并行；Task 5 依赖 2/3；Task 6 依赖 1/5；Task 7 依赖 4/5；Task 8 依赖 3/5；Task 9 依赖 8（钻取组件其实仅依赖 5，可与其并行但页面挂路由在 8 后更顺）；Task 10 依赖 9；Task 11/12 依赖 5/8/9 的公共组件；Task 13 收尾必须最后。

## 回滚策略

每个任务独立提交且应用始终可构建：Task 2-12 期间旧页面与新页面并存（旧 `/runs` 表单在 Task 6 被总览替代、旧 `/benchmarks` 在 Task 13 删除），任一任务出问题 revert 单个提交即可。
