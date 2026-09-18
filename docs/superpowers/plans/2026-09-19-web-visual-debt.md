# Web 控制台视觉债清理实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按已核验的 17 项前端审计结论，分 5 批增量修复 `apps/web` 的全局 CSS 缺陷、主按钮语义、信息架构死胡同、页面布局与组件打磨，不重写任何页面。

**Architecture:** 修复分为三层——`src/index.css` 的全局规则与工具类治理（批 1）；`button.primary` 语义类替代 `type="submit"` 隐式主按钮（批 2）；组件层的信息架构与排版（批 3-5）。全程零新增颜色/字号 token，`:root` 不动。CSS 层回归用 `tests/stylesheet.test.ts`（`?raw` 导入断言）守护，组件行为用 @testing-library 断言。

**Tech Stack:** React 19 + react-router-dom 7 + Radix UI（Select/DropdownMenu/Dialog/Switch/Tabs）+ Phosphor Icons（Bold）+ 纯 `index.css` token 体系；测试 vitest 5 + @testing-library/react 16 + happy-dom。

## Global Constraints

- 样式取值只允许 `var(--…)`；禁止组件内写十六进制色值（`button.primary` 的 `color: #ffffff` 沿用现规则，属既有白字）。
- 状态中文标签与语气只从 `src/components/statusMeta.ts` 的 `STATUS_META` 取值。
- 图标只用 `@phosphor-icons/react` Bold 字重、带 `Icon` 后缀导出名；禁止 emoji。
- 禁止 Tailwind / Ant Design / MUI / shadcn / Lucide / Feather / Heroicons。
- CSS 只写在 `apps/web/src/index.css`；新类名需同步登记进 `apps/web/DESIGN.md`，文档与代码同一个提交。
- 每个任务收口：`pnpm --dir apps/web test` 与 `pnpm --dir apps/web build` 全绿才提交；每个批次最后一个任务额外跑 `make check`（仓库根）。
- 提交信息沿用仓库风格：`fix(web): …` / `feat(web): …`。
- 本计划文件（`docs/superpowers/plans/2026-09-19-web-visual-debt.md`）随 Task 1 一起提交。

---

### Task 1: 全局 box-sizing + 样式表回归测试基座

**Files:**
- Modify: `apps/web/src/index.css`
- Modify: `apps/web/DESIGN.md:127`（开关条目去掉冗余 box-sizing 表述）
- Create: `apps/web/tests/stylesheet.test.ts`

**Interfaces:**
- Produces: `tests/stylesheet.test.ts`（后续 CSS 任务在此追加断言）；全局 `border-box` 供全站继承。

- [ ] **Step 1: 写失败的样式表测试**

创建 `apps/web/tests/stylesheet.test.ts`：

```ts
import { describe, expect, it } from "vitest";
import css from "../src/index.css?raw";

/* 全局 CSS 完整性：这些断言对应审计报告里"一处代码坏，全站跟着坏"的缺陷，
 * 防止工具类重复定义、全局规则缺失等问题回归。 */
describe("全局样式完整性", () => {
  it("全局 box-sizing: border-box", () => {
    expect(css).toMatch(/\*,\s*\*::before,\s*\*::after\s*\{\s*box-sizing:\s*border-box;?\s*\}/);
  });

  it("冗余的局部 box-sizing 已收敛到全局一条", () => {
    expect(css.match(/box-sizing:\s*border-box/g)).toHaveLength(1);
  });
});
```

- [ ] **Step 2: 运行确认失败**

Run: `pnpm --dir apps/web test tests/stylesheet.test.ts`
Expected: FAIL（找不到全局规则；局部 box-sizing 出现 5 次）

- [ ] **Step 3: 改 index.css**

3a. 在 `:root { … }` 结束的 `}`（第 47 行）之后、`body {` 之前插入：

```css
/* 全局盒模型：宽度含 padding/border，杜绝 width:100% + padding 溢出容器 */
*,
*::before,
*::after {
  box-sizing: border-box;
}
```

3b. 删除四处冗余声明（保留规则块本身，只删这一行）：
- `.app-shell` 内的 `box-sizing: border-box;`（约 :77）
- `.model-config-group textarea` 内的 `box-sizing: border-box;`（约 :655）
- `.dialog-content` 内的 `box-sizing: border-box;`（约 :830）
- `.switch` 内的 `box-sizing: border-box;`（约 :960）

3c. `DESIGN.md` 第 4 节「开关（Switch）」条目，把

```
32×18 pill（`box-sizing: border-box` + `padding: 0`，并显式压掉全局 `button` 的 hover 底色
```

改为

```
32×18 pill（`padding: 0`，并显式压掉全局 `button` 的 hover 底色
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pnpm --dir apps/web test tests/stylesheet.test.ts`
Expected: PASS

- [ ] **Step 5: 构建与提交**

Run: `pnpm --dir apps/web build`
Expected: 构建成功

```bash
git add apps/web/src/index.css apps/web/DESIGN.md apps/web/tests/stylesheet.test.ts docs/superpowers/plans/2026-09-19-web-visual-debt.md
git commit -m "fix(web): enforce global border-box sizing"
```

---

### Task 2: 侧边导航去下划线 + [hidden] 守卫

**Files:**
- Modify: `apps/web/src/index.css`（`.tab` :121；表单控件段 :240 后）
- Modify: `apps/web/tests/stylesheet.test.ts`

**Interfaces:**
- Consumes: Task 1 的 stylesheet 测试文件。
- Produces: `[hidden]` 元素（如 DirectLlmOperate.tsx:437 的 file input）恢复隐藏；导航 `<a>` 无下划线。

- [ ] **Step 1: 追加失败的断言**

在 `tests/stylesheet.test.ts` 的 describe 内追加：

```ts
  it("导航链接无下划线", () => {
    const tabStart = css.indexOf(".tab {");
    const tabEnd = css.indexOf(".tab:hover");
    expect(css.slice(tabStart, tabEnd)).toContain("text-decoration: none");
  });

  it("[hidden] 不被作者样式（display:block）复活", () => {
    expect(css).toMatch(/\[hidden\]\s*\{\s*display:\s*none\s*!important;/);
  });
```

- [ ] **Step 2: 运行确认失败**

Run: `pnpm --dir apps/web test tests/stylesheet.test.ts`
Expected: FAIL（两条新断言）

- [ ] **Step 3: 改 index.css**

3a. `.tab` 规则内（`text-align: left;` 之后）加一行：

```css
  text-decoration: none;
```

3b. 在表单控件段（`form input, …, .control { … }` 规则块，约 :224-240）之后插入：

```css
/* 作者样式（如 form input 的 display:block）会覆盖 UA 对 [hidden] 的 display:none，显式守住 */
[hidden] {
  display: none !important;
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pnpm --dir apps/web test tests/stylesheet.test.ts`
Expected: PASS

- [ ] **Step 5: 构建与提交**

Run: `pnpm --dir apps/web build`
Expected: 构建成功

```bash
git add apps/web/src/index.css apps/web/tests/stylesheet.test.ts
git commit -m "fix(web): plain nav links and guard hidden inputs"
```

---

### Task 3: ModelPicker 特异性修复 + 工具类去重

**Files:**
- Modify: `apps/web/src/index.css`（ModelPicker 段 :1075 后；删 :575-583；合并 .actions）
- Modify: `apps/web/tests/stylesheet.test.ts`

**Interfaces:**
- Produces: `.operate-card .model-picker-item`（0-2-0，压过 `.operate-card label` 的 0-1-1）；唯一 `.actions` 定义（gap 12 / margin-top 16 / align-items center / flex-wrap）。

- [ ] **Step 1: 追加失败的断言**

```ts
  it("ModelPicker 行内布局压过 .operate-card label 的 display:block", () => {
    expect(css).toContain(".operate-card .model-picker-item {");
  });

  it("工具类只定义一次", () => {
    expect(css.match(/\.actions\s*\{/g)).toHaveLength(1);
    expect(css.match(/\.run-progress\s*\{/g)).toHaveLength(1);
  });
```

- [ ] **Step 2: 运行确认失败**

Run: `pnpm --dir apps/web test tests/stylesheet.test.ts`
Expected: FAIL

- [ ] **Step 3: 改 index.css**

3a. 在 `.model-picker-item:hover { … }` 规则之后插入：

```css
/* 勾选框 + 模型 ID + 徽章必须同行：0-2-0 压过 .operate-card label（0-1-1）的 display:block，
   并修正被强加的 12px label 下边距 */
.operate-card .model-picker-item {
  display: flex;
  align-items: center;
  margin-bottom: 2px;
}
```

3b. 删除死代码（表格版 run-progress，全站只有 `<span class="run-progress">` 一种用法）：

```css
.run-progress {
  margin: 0;
}

.run-progress td {
  border-top: none;
  padding: 2px 12px 2px 0;
  font-size: 12px;
}
```

3c. 把第一处 `.actions`（「结果与提示」段，约 :1029）整体替换为合并后的唯一定义：

```css
.actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 16px;
}
```

并删除「类型操作页三卡布局」段末尾的重复定义（约 :1107-1112）：

```css
.actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 16px;
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pnpm --dir apps/web test`（全量，确认无组件测试受影响）
Expected: PASS

- [ ] **Step 5: 构建与提交**

Run: `pnpm --dir apps/web build`

```bash
git add apps/web/src/index.css apps/web/tests/stylesheet.test.ts
git commit -m "fix(web): resolve model picker specificity and dedupe utility classes"
```

---

### Task 4: 操作卡顶对齐（批 1 收尾）

**Files:**
- Modify: `apps/web/src/index.css:1096`（`.operate-grid`）
- Modify: `apps/web/DESIGN.md:131`（类型操作页卡片条目）
- Modify: `apps/web/tests/stylesheet.test.ts`

- [ ] **Step 1: 追加失败的断言**

```ts
  it("操作卡不强行等高", () => {
    const start = css.indexOf(".operate-grid {");
    const end = css.indexOf(".operate-card {");
    expect(css.slice(start, end)).toContain("align-items: flex-start");
  });
```

- [ ] **Step 2: 运行确认失败**

Run: `pnpm --dir apps/web test tests/stylesheet.test.ts`
Expected: FAIL

- [ ] **Step 3: 改 index.css 与 DESIGN.md**

3a. `.operate-grid` 的 `align-items: stretch;` 改为：

```css
  align-items: flex-start;
```

3b. `DESIGN.md` 第 4 节「类型操作页卡片」条目末尾追加：

```
；卡片顶对齐、高度随内容，不强行等高
```

- [ ] **Step 4: 全量门禁（批 1 收尾）**

Run: `pnpm --dir apps/web test && pnpm --dir apps/web build`
Expected: 全绿

Run: `make check`（仓库根）
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add apps/web/src/index.css apps/web/DESIGN.md apps/web/tests/stylesheet.test.ts
git commit -m "fix(web): top-align operate cards"
```

---

### Task 5: 主按钮语义化 button.primary（批 2）

**Files:**
- Modify: `apps/web/src/index.css:288-297`
- Modify: `apps/web/src/evalTypes/gsm8k/Gsm8kOperate.tsx:353`
- Modify: `apps/web/src/evalTypes/directllm/DirectLlmOperate.tsx:509`
- Modify: `apps/web/src/evalTypes/replay/ReplayPages.tsx:61`
- Modify: `apps/web/src/pages/ResourcesPage.tsx:293,636,862`
- Modify: `apps/web/DESIGN.md:120`（按钮条目）
- Modify: `apps/web/tests/stylesheet.test.ts`

**Interfaces:**
- Produces: `button.primary` 类（`--ink` 实底白字）；`button[type="submit"]` 回归默认次级外观。Gsm8kCases:149 / DirectLlmCases:138 的「搜索」、Gsm8kOperate:302「下载」、DirectLlmOperate:415/:499「导入」无需改标记，自动降为次级。

- [ ] **Step 1: 追加失败的断言**

```ts
  it("主按钮由 .primary 语义类驱动", () => {
    expect(css).toContain("button.primary {");
    expect(css).not.toContain('button[type="submit"]');
  });
```

- [ ] **Step 2: 运行确认失败**

Run: `pnpm --dir apps/web test tests/stylesheet.test.ts`
Expected: FAIL

- [ ] **Step 3: 改 index.css**

把

```css
button[type="submit"] {
  background: var(--ink);
  border-color: var(--ink);
  color: #ffffff;
}

button[type="submit"]:hover {
  background: var(--ink-hover);
  border-color: var(--ink-hover);
}
```

替换为

```css
/* 主动作显式声明（语义驱动）；type=submit 不再自动获得主按钮外观 */
button.primary {
  background: var(--ink);
  border-color: var(--ink);
  color: #ffffff;
}

button.primary:hover {
  background: var(--ink-hover);
  border-color: var(--ink-hover);
}
```

- [ ] **Step 4: 给各页主动作补 .primary（共 7 处标记改动）**

4a. `Gsm8kOperate.tsx`（发起跑测，:353）：

```tsx
          <button
            type="button"
            className="primary"
            onClick={() => void doRun()}
            disabled={running || selected.length === 0 || !preset || runSize <= 0}
          >
```

4b. `DirectLlmOperate.tsx`（发起评测，:509）：

```tsx
          <button
            type="button"
            className="primary"
            onClick={() => void doRun()}
            disabled={running || selected.length === 0 || !preset || runSize <= 0}
          >
```

4c. `ReplayPages.tsx:61`：

```tsx
        <button type="button" className="primary" onClick={submit}>创建回放</button>
```

4d. `ResourcesPage.tsx:293`（创建 Provider）：

```tsx
            <button type="submit" className="primary">创建</button>
```

4e. `ResourcesPage.tsx:636`（保存连接）：

```tsx
          <button type="submit" className="primary" disabled={savingConnection}>
            保存连接
          </button>
```

4f. `ResourcesPage.tsx:862`（注册 / 保存修改模型）：

```tsx
          <button type="submit" className="primary" disabled={savingModel}>{savingModel ? "保存中…" : editing ? "保存修改" : "注册"}</button>
```

4g. `DESIGN.md` 第 4 节「按钮」条目的 primary 规格后追加：

```
；主动作用 `button.primary` 类显式声明，`type="submit"` 不再隐式获得主按钮外观
```

- [ ] **Step 5: 全量门禁（批 2 收尾）**

Run: `pnpm --dir apps/web test`
Expected: PASS（现有按钮断言全部按 accessible name 查找，不受 className 影响）

Run: `pnpm --dir apps/web build && make check`
Expected: 全绿

- [ ] **Step 6: 提交**

```bash
git add apps/web/src/index.css apps/web/DESIGN.md apps/web/tests/stylesheet.test.ts apps/web/src/evalTypes/gsm8k/Gsm8kOperate.tsx apps/web/src/evalTypes/directllm/DirectLlmOperate.tsx apps/web/src/evalTypes/replay/ReplayPages.tsx apps/web/src/pages/ResourcesPage.tsx
git commit -m "fix(web): drive primary buttons by semantic class"
```

---

### Task 6: runFormat 工具函数（批 3 起）

**Files:**
- Create: `apps/web/src/components/runFormat.ts`
- Create: `apps/web/tests/runFormat.test.ts`

**Interfaces:**
- Produces（Task 7/10/12 消费，签名如下）:
  - `shortRunId(id: string): string` — ≤22 字符原样；否则前 13 + `…` + 后 4。
  - `formatTimestamp(iso?: string | null): string | null` — `MM-DD HH:mm`；空/非法返回 null。
  - `formatClock(iso?: string | null): string | null` — `MM-DD HH:mm:ss`。
  - `formatDuration(from?: string | null, to?: string | null): string | null` — `45s` / `3m12s` / `1h04m`。

- [ ] **Step 1: 写失败的测试**

创建 `apps/web/tests/runFormat.test.ts`：

```ts
import { describe, expect, it } from "vitest";
import { formatClock, formatDuration, formatTimestamp, shortRunId } from "../src/components/runFormat";

describe("shortRunId", () => {
  it("短 ID 原样返回", () => {
    expect(shortRunId("run-42")).toBe("run-42");
  });

  it("长 ID 截断为首 13 + … + 尾 4", () => {
    const id = `run-${"a".repeat(36)}`;
    expect(shortRunId(id)).toBe(`run-${"a".repeat(9)}…${"a".repeat(4)}`);
  });
});

describe("formatTimestamp", () => {
  it("输出 MM-DD HH:mm", () => {
    expect(formatTimestamp("2026-09-19T08:30:00")).toBe("09-19 08:30");
  });

  it("空值与非法值返回 null", () => {
    expect(formatTimestamp(null)).toBeNull();
    expect(formatTimestamp("not-a-date")).toBeNull();
  });
});

describe("formatClock", () => {
  it("输出 MM-DD HH:mm:ss", () => {
    expect(formatClock("2026-09-19T08:30:05")).toBe("09-19 08:30:05");
  });
});

describe("formatDuration", () => {
  it("按秒/分/时分级格式化", () => {
    expect(formatDuration("2026-09-19T08:00:00", "2026-09-19T08:00:45")).toBe("45s");
    expect(formatDuration("2026-09-19T08:00:00", "2026-09-19T08:03:12")).toBe("3m12s");
    expect(formatDuration("2026-09-19T07:00:00", "2026-09-19T08:04:00")).toBe("1h04m");
  });

  it("缺起止时间返回 null（进行中）", () => {
    expect(formatDuration("2026-09-19T08:00:00", null)).toBeNull();
  });
});
```

- [ ] **Step 2: 运行确认失败**

Run: `pnpm --dir apps/web test tests/runFormat.test.ts`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `apps/web/src/components/runFormat.ts`**

```ts
/** run 展示格式化：ID 截断与时间列（时间戳为本地时区，控制台自用不做时区切换）。 */

export function shortRunId(id: string): string {
  return id.length <= 22 ? id : `${id.slice(0, 13)}…${id.slice(-4)}`;
}

function parseDate(iso?: string | null): Date | null {
  if (!iso) return null;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

const pad = (value: number) => String(value).padStart(2, "0");

/** 总览表「开始」列：MM-DD HH:mm。 */
export function formatTimestamp(iso?: string | null): string | null {
  const date = parseDate(iso);
  if (!date) return null;
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 时间线事件列：MM-DD HH:mm:ss（长运行跨天也能对上日历）。 */
export function formatClock(iso?: string | null): string | null {
  const date = parseDate(iso);
  if (!date) return null;
  return `${formatTimestamp(iso)}:${pad(date.getSeconds())}`;
}

/** 耗时列：finished_at - created_at，分级到 h/m/s。 */
export function formatDuration(from?: string | null, to?: string | null): string | null {
  const start = parseDate(from);
  const end = parseDate(to);
  if (!start || !end || end < start) return null;
  const totalSeconds = Math.round((end.getTime() - start.getTime()) / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h${pad(minutes)}m`;
  if (minutes > 0) return `${minutes}m${pad(seconds)}s`;
  return `${seconds}s`;
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pnpm --dir apps/web test tests/runFormat.test.ts`
Expected: PASS

- [ ] **Step 5: 构建与提交**

Run: `pnpm --dir apps/web build`

```bash
git add apps/web/src/components/runFormat.ts apps/web/tests/runFormat.test.ts
git commit -m "feat(web): add run formatting helpers"
```

---

### Task 7: 运行总览改造（状态感知跳转 / 过程入口 / ID 截断 / 时间列 / Radix 类型过滤）

**Files:**
- Modify: `apps/web/src/pages/RunsOverviewPage.tsx`（整文件重写，见 Step 3）
- Modify: `apps/web/tests/evalTypes.test.tsx`（RunsOverviewPage describe 内追加用例）

**Interfaces:**
- Consumes: Task 6 的 `shortRunId/formatTimestamp/formatDuration`；`useRunEvents` 的 `isTerminal`。
- Produces: 行点击规则——非终态 → `/{suite}/monitor?runs={id}`（无套件 → `/runs/{id}/monitor`），终态 → 结果页；每行常显「过程」link 按钮。

- [ ] **Step 1: 先追加失败的测试**

在 `tests/evalTypes.test.tsx` 的 `describe("RunsOverviewPage", …)` 内（现有用例之后）追加：

```tsx
  it("运行中的 run 点击进过程页；长 ID 截断显示", async () => {
    const longId = `run-${"a".repeat(36)}`;
    clientMocks.getRuns.mockResolvedValue({
      items: [
        { id: longId, scenario_version: "direct-llm@1", status: "running", model: "m", case_ids: [], cases: [], scores: [] },
      ],
      total: 1,
    });
    render(
      <MemoryRouter initialEntries={["/runs"]}>
        <RunsOverviewPage />
        <LocationProbe />
      </MemoryRouter>
    );
    const shortName = `run-${"a".repeat(9)}…${"a".repeat(4)}`;
    fireEvent.click(await screen.findByRole("button", { name: shortName }));
    expect(screen.getByTestId("location").textContent).toBe(`/direct-llm/monitor?runs=${longId}`);
  });

  it("「过程」入口对未知套件落到通用监控路由", async () => {
    clientMocks.getRuns.mockResolvedValue({
      items: [
        { id: "run-31", scenario_version: "mystery@2", status: "completed", model: null, case_ids: [], cases: [], scores: [] },
      ],
      total: 1,
    });
    render(
      <MemoryRouter initialEntries={["/runs"]}>
        <RunsOverviewPage />
        <LocationProbe />
      </MemoryRouter>
    );
    fireEvent.click(await screen.findByRole("button", { name: "过程" }));
    expect(screen.getByTestId("location").textContent).toBe("/runs/run-31/monitor");
  });
```

- [ ] **Step 2: 运行确认失败**

Run: `pnpm --dir apps/web test tests/evalTypes.test.tsx`
Expected: 新用例 FAIL（当前点击一律去结果页；无「过程」按钮；ID 不截断）

- [ ] **Step 3: 重写 `apps/web/src/pages/RunsOverviewPage.tsx`**

```tsx
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import * as Select from "@radix-ui/react-select";
import { CaretDownIcon, CheckIcon, DotsThreeIcon } from "@phosphor-icons/react";
import { cancelRun, getRuns, retryRun, type RunRecord } from "../api/client";
import { StatusBadge } from "../components/StatusBadge";
import { STATUS_ORDER, statusLabel } from "../components/statusMeta";
import { formatDuration, formatTimestamp, shortRunId } from "../components/runFormat";
import { isTerminal } from "../hooks/useRunEvents";
import { EVAL_SUITES, suiteForRun, suiteRoutes } from "../evalTypes/registry";

const RETRYABLE_STATUSES = ["failed", "cancelled", "unsupported", "profile_stale", "needs_review"];

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

  const monitorPath = (run: RunRecord) => {
    const suite = suiteForRun(run);
    return suite ? suiteRoutes(suite.id).monitor([run.id]) : `/runs/${run.id}/monitor`;
  };

  /* 非终态优先看过程，终态直达结果 */
  const openRun = (run: RunRecord) => {
    if (!isTerminal(run.status)) {
      navigate(monitorPath(run));
      return;
    }
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
            <Select.Root value={typeFilter} onValueChange={setTypeFilter}>
              <Select.Trigger className="select-trigger" aria-label="类型过滤">
                <Select.Value />
                <CaretDownIcon size={14} weight="bold" aria-hidden />
              </Select.Trigger>
              <Select.Portal>
                <Select.Content className="select-content" position="popper" sideOffset={4}>
                  <Select.Viewport>
                    <Select.Item value="all" className="select-item">
                      <Select.ItemText>全部类型</Select.ItemText>
                      <Select.ItemIndicator className="select-item-indicator">
                        <CheckIcon size={14} weight="bold" aria-hidden />
                      </Select.ItemIndicator>
                    </Select.Item>
                    {[...EVAL_SUITES.map((suite) => suite.label), "通用"].map((label) => (
                      <Select.Item key={label} value={label} className="select-item">
                        <Select.ItemText>{label}</Select.ItemText>
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
              <th>ID</th><th>类型</th><th>场景</th><th>模型</th><th>状态</th><th>进度</th><th>开始</th><th>耗时</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((run) => {
              const cancellable = !isTerminal(run.status);
              const retryable = RETRYABLE_STATUSES.includes(run.status);
              const total = run.case_ids?.length ?? 0;
              const done = run.cases?.length ?? 0;
              return (
                <tr key={run.id}>
                  <td className="mono" title={run.id}>
                    <button className="link" onClick={() => openRun(run)}>{shortRunId(run.id)}</button>
                  </td>
                  <td>{typeLabel(run)}</td>
                  <td className="mono">{run.scenario_version}</td>
                  <td className="mono">{run.model ?? "—"}</td>
                  <td><StatusBadge status={run.status} /></td>
                  <td className="mono">{total ? `${done}/${total}` : "—"}</td>
                  <td className="mono">{formatTimestamp(run.created_at) ?? "—"}</td>
                  <td className="mono">{formatDuration(run.created_at, run.finished_at) ?? "—"}</td>
                  <td className="row-actions">
                    <button type="button" className="link" onClick={() => navigate(monitorPath(run))}>过程</button>
                    {(cancellable || retryable) ? (
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
                    ) : null}
                  </td>
                </tr>
              );
            })}
            {visible.length === 0 && (
              <tr><td colSpan={9} className="empty">暂无运行</td></tr>
            )}
          </tbody>
        </table>
      </section>
    </div>
  );
}
```

（较原文件：删掉本地 `TERMINAL_STATUSES`，改用 `isTerminal`；类型过滤换 Radix Select；新增「开始/耗时」两列与「过程」链接；ID 截断 + title 全量。）

- [ ] **Step 4: 运行测试确认通过**

Run: `pnpm --dir apps/web test tests/evalTypes.test.tsx`
Expected: PASS（现有用例 run-42 completed→result、run-31 failed→result 与状态感知规则一致）

- [ ] **Step 5: 构建与提交**

Run: `pnpm --dir apps/web build`

```bash
git add apps/web/src/pages/RunsOverviewPage.tsx apps/web/tests/evalTypes.test.tsx
git commit -m "feat(web): overview links to live monitor and shows timing"
```

---

### Task 8: BatchMonitor 空状态

**Files:**
- Modify: `apps/web/src/components/BatchMonitor.tsx:84-88`
- Modify: `apps/web/tests/evalTypes.test.tsx`（BatchMonitor describe 追加用例）

- [ ] **Step 1: 追加失败的测试**

在 `describe("BatchMonitor", …)` 内追加：

```tsx
  it("未指定运行时给出引导空状态", async () => {
    render(
      <MemoryRouter initialEntries={["/gsm8k/monitor"]}>
        <BatchMonitor runIds={[]} resultPath={(id) => `/gsm8k/runs/${id}/result`} />
      </MemoryRouter>
    );
    expect(await screen.findByText(/未指定运行/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "运行总览" })).toBeTruthy();
  });
```

- [ ] **Step 2: 运行确认失败**

Run: `pnpm --dir apps/web test tests/evalTypes.test.tsx`
Expected: FAIL

- [ ] **Step 3: 改 BatchMonitor.tsx**

`<ul className="batch-list">…</ul>` 之后、`{comparePath && …}` 之前插入：

```tsx
      {runIds.length === 0 && (
        <p className="empty">
          未指定运行。从各类型操作页发起跑测后自动进入，或到
          <Link className="link" to="/runs">运行总览</Link>
          查看历史运行。
        </p>
      )}
```

（`Link` 已在该文件导入。）

- [ ] **Step 4: 运行测试确认通过**

Run: `pnpm --dir apps/web test tests/evalTypes.test.tsx`
Expected: PASS

- [ ] **Step 5: 构建与提交**

Run: `pnpm --dir apps/web build`

```bash
git add apps/web/src/components/BatchMonitor.tsx apps/web/tests/evalTypes.test.tsx
git commit -m "fix(web): monitor empty state guidance"
```

---

### Task 9: 对比页空状态 + 结果页返回链接（批 3 收尾）

**Files:**
- Modify: `apps/web/src/evalTypes/gsm8k/Gsm8kCompare.tsx`
- Modify: `apps/web/src/evalTypes/directllm/DirectLlmCompare.tsx`
- Modify: `apps/web/src/evalTypes/gsm8k/Gsm8kResult.tsx:91`
- Modify: `apps/web/src/evalTypes/directllm/DirectLlmResult.tsx:102`
- Modify: `apps/web/src/evalTypes/replay/ReplayPages.tsx`（ReplayResult 面板头）
- Modify: `apps/web/src/evalTypes/fallback/FallbackPages.tsx:115`
- Modify: `apps/web/tests/evalTypes.test.tsx`（两个「缺少 runs 参数」用例）

- [ ] **Step 1: 先改测试（TDD）**

`Gsm8kCompare` 用例中，把

```tsx
    expect(await screen.findByText("GSM8K · 多模型对比")).toBeTruthy();
    expect(screen.getByText("无")).toBeTruthy();
    /* 空列时 colSpan 至少为 1，避免渲染 colspan="0" */
    expect(screen.getByText("无").getAttribute("colspan")).toBe("1");
```

改为

```tsx
    expect(await screen.findByText("GSM8K · 多模型对比")).toBeTruthy();
    expect(await screen.findByText(/暂无可对比运行/)).toBeTruthy();
```

`DirectLlmCompare` 用例中，把

```tsx
    expect(await screen.findByText("Direct LLM · 多模型对比")).toBeTruthy();
    expect(screen.getByText("无")).toBeTruthy();
    expect(screen.getByText("无").getAttribute("colspan")).toBe("1");
```

改为

```tsx
    expect(await screen.findByText("Direct LLM · 多模型对比")).toBeTruthy();
    expect(await screen.findByText(/暂无可对比运行/)).toBeTruthy();
```

- [ ] **Step 2: 运行确认失败**

Run: `pnpm --dir apps/web test tests/evalTypes.test.tsx`
Expected: FAIL（空态文案不存在）

- [ ] **Step 3: 组件改动**

3a. `Gsm8kCompare.tsx`：在 `useEffect(…)` 之后、`if (error) …` 之前插入提前返回：

```tsx
  if (runIds.length === 0) {
    return (
      <div className="page">
        <section className="panel detail">
          <div className="panel-head"><h2>GSM8K · 多模型对比</h2></div>
          <p className="empty">
            暂无可对比运行。对比入口在批次过程页（全部运行到终态后出现「查看对比结果」），
            也可从 <Link className="link" to="/runs">运行总览</Link> 回看单次结果。
          </p>
        </section>
      </div>
    );
  }
```

3b. `DirectLlmCompare.tsx`：同样位置插入同构块，仅标题换成 `Direct LLM · 多模型对比`。

3c. 四个结果页的 `panel-head-actions` 首位加「返回总览」链接（未导入 `Link` 的文件补导入）：

- `Gsm8kResult.tsx`（第 1 行 import 增加 `Link`；:91）：

```tsx
          <div className="panel-head-actions">
            <Link className="link" to="/runs">返回总览</Link>
            <StatusBadge status={run.status} />
```

- `DirectLlmResult.tsx`（import 增加 `Link`；:102）：

```tsx
          <div className="panel-head-actions">
            <Link className="link" to="/runs">返回总览</Link>
            <StatusBadge status={run.status} />
```

- `ReplayPages.tsx` ReplayResult（:113，import 增加 `Link`）：

```tsx
        <div className="panel-head">
          <h2 className="mono">运行 {runId} · 结果</h2>
          <div className="panel-head-actions"><Link className="link" to="/runs">返回总览</Link></div>
        </div>
```

- `FallbackPages.tsx` FallbackResultPage（:115，`Link` 已导入）：

```tsx
          <div className="panel-head-actions">
            <Link className="link" to="/runs">返回总览</Link>
            <button type="button" onClick={() => act(() => rescoreRun(runId))} disabled={run?.status !== "completed"}>
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pnpm --dir apps/web test`
Expected: PASS（全量，确认四个结果页的返回链接不与现有断言冲突）

- [ ] **Step 5: 批次门禁与提交（批 3 收尾）**

Run: `pnpm --dir apps/web build && make check`
Expected: 全绿

```bash
git add apps/web/src/evalTypes/gsm8k/Gsm8kCompare.tsx apps/web/src/evalTypes/directllm/DirectLlmCompare.tsx apps/web/src/evalTypes/gsm8k/Gsm8kResult.tsx apps/web/src/evalTypes/directllm/DirectLlmResult.tsx apps/web/src/evalTypes/replay/ReplayPages.tsx apps/web/src/evalTypes/fallback/FallbackPages.tsx apps/web/tests/evalTypes.test.tsx
git commit -m "fix(web): compare empty states and result back links"
```

---

### Task 10: Replay 操作页重排 + 导航标签改名（批 4 起）

**Files:**
- Modify: `apps/web/src/evalTypes/replay/ReplayPages.tsx`（ReplayOperate 整体重写 + ReplayResult 面板头已在 Task 9 改）
- Modify: `apps/web/src/evalTypes/registry.ts:57`
- Modify: `apps/web/tests/app.test.tsx:72`
- Modify: `apps/web/tests/evalTypes.test.tsx`（ReplayOperate describe）

**Interfaces:**
- Consumes: Task 6 的 `shortRunId/formatTimestamp`；`useRunEvents` 的 `isTerminal`；`registry` 的 `suiteForRun`。
- Produces: 导航标签「Replay」（原「Replay 回放」）；`ReplayOperate` 挂载时调用 `getRuns()` 过滤 replay 套件渲染「最近 Replay 运行」面板（**测试必须 mock `getRuns`，否则 `undefined.then` 直接抛错**）。

- [ ] **Step 1: 先改测试（TDD）**

1a. `tests/app.test.tsx:72`：

```tsx
    expect(screen.getByRole("link", { name: /Replay 回放/ })).toBeTruthy();
```

改为

```tsx
    expect(screen.getByRole("link", { name: /^Replay$/ })).toBeTruthy();
```

1b. `tests/evalTypes.test.tsx` 的 ReplayOperate 现有用例（"手写 Manifest 作为 inline provider 创建运行"）里，`render(…)` 之前加一行 mock：

```tsx
    clientMocks.getRuns.mockResolvedValue({ items: [], total: 0 });
```

1c. 同 describe 内追加新用例：

```tsx
  it("最近回放运行面板可进入过程页", async () => {
    clientMocks.getRuns.mockResolvedValue({
      items: [{ id: "run-70", scenario_version: "replay@1", status: "running", case_ids: [], cases: [], scores: [] }],
      total: 1,
    });
    render(
      <MemoryRouter initialEntries={["/replay"]}>
        <ReplayOperate />
        <LocationProbe />
      </MemoryRouter>
    );
    fireEvent.click(await screen.findByRole("button", { name: "run-70" }));
    expect(screen.getByTestId("location").textContent).toBe("/replay/monitor?runs=run-70");
  });
```

- [ ] **Step 2: 运行确认失败**

Run: `pnpm --dir apps/web test tests/app.test.tsx tests/evalTypes.test.tsx`
Expected: FAIL（标签仍是「Replay 回放」；无最近运行面板）

- [ ] **Step 3: 改组件与注册表**

3a. `registry.ts` 的 REPLAY_SUITE：

```ts
  label: "Replay",
```

（原 `"Replay 回放"`。导航、总览类型列、类型过滤自动继承。）

3b. `ReplayPages.tsx` 头部 import 改为：

```tsx
import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { createRun, getRun, getRuns, type RunRecord } from "../../api/client";
import { BatchMonitor } from "../../components/BatchMonitor";
import { CaseDrillTable, type DrillRow } from "../../components/CaseDrillTable";
import { RunTimeline } from "../../components/RunTimeline";
import { RunAuditSummary } from "../../components/RunAuditSummary";
import { StatusBadge } from "../../components/StatusBadge";
import { formatTimestamp, shortRunId } from "../../components/runFormat";
import { isTerminal, useRunEvents } from "../../hooks/useRunEvents";
import { suiteForRun, suiteRoutes } from "../registry";
```

3c. `ReplayOperate` 整体替换为：

```tsx
export function ReplayOperate() {
  const navigate = useNavigate();
  const [scenario, setScenario] = useState(SCENARIOS[0]);
  const [caseIds, setCaseIds] = useState("case-1");
  const [manifest, setManifest] = useState("");
  const [error, setError] = useState("");
  const [recent, setRecent] = useState<RunRecord[]>([]);

  useEffect(() => {
    getRuns()
      .then((payload) => setRecent(payload.items.filter((run) => suiteForRun(run)?.id === "replay")))
      .catch(() => undefined);
  }, []);

  const openRecent = (run: RunRecord) => {
    navigate(isTerminal(run.status) ? ROUTES.result(run.id) : ROUTES.monitor([run.id]));
  };

  const submit = (form: FormEvent<HTMLFormElement>) => {
    form.preventDefault();
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
        <h2>Replay · 操作</h2>
        {error && <p className="error">{error}</p>}
        <form onSubmit={submit} aria-label="创建回放">
          <label>
            场景
            <select className="control mono" value={scenario} onChange={(change) => setScenario(change.target.value)}>
              {SCENARIOS.map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
          </label>
          <label>
            Case 列表（逗号分隔）
            <input className="control mono" value={caseIds} onChange={(change) => setCaseIds(change.target.value)} />
          </label>
          <label>
            Manifest JSON（inline Provider 或 replay fixture）
            <textarea className="control mono" rows={6} value={manifest} onChange={(change) => setManifest(change.target.value)}
              placeholder='{"provider":{"kind":"replay","fixture":{…}}}' />
          </label>
          <button type="submit" className="primary">创建回放</button>
        </form>
        <p className="hint">回放不产生模型费用；fixture 与期望在 Manifest 或 replay 接口中提供。</p>
      </section>
      <section className="panel" aria-label="最近 Replay 运行">
        <div className="panel-head"><h2>最近 Replay 运行</h2></div>
        {recent.length === 0 ? (
          <p className="empty">暂无回放运行</p>
        ) : (
          <table>
            <thead>
              <tr><th>ID</th><th>状态</th><th>开始</th></tr>
            </thead>
            <tbody>
              {recent.slice(0, 10).map((run) => (
                <tr key={run.id}>
                  <td className="mono" title={run.id}>
                    <button type="button" className="link" onClick={() => openRecent(run)}>{shortRunId(run.id)}</button>
                  </td>
                  <td><StatusBadge status={run.status} /></td>
                  <td className="mono">{formatTimestamp(run.created_at) ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
```

（表单包进 `<form>`、控件全部走 `.control`、提交按钮 submit+primary；右侧新增最近运行面板填补孤儿卡留下的空白。）

- [ ] **Step 4: 运行测试确认通过**

Run: `pnpm --dir apps/web test`
Expected: PASS

- [ ] **Step 5: 构建与提交**

Run: `pnpm --dir apps/web build`

```bash
git add apps/web/src/evalTypes/replay/ReplayPages.tsx apps/web/src/evalTypes/registry.ts apps/web/tests/app.test.tsx apps/web/tests/evalTypes.test.tsx
git commit -m "feat(web): rebuild replay operate page layout"
```

---

### Task 11: Harness 页布局归位（批 4 收尾）

**Files:**
- Modify: `apps/web/src/pages/ResourcesPage.tsx`（HarnessesPage :879-940）
- Modify: `apps/web/src/index.css`（`.panel.wide-panel` / `.panel.narrow-panel` / `th` nowrap）
- Modify: `apps/web/DESIGN.md:106,113`
- Modify: `apps/web/tests/components.test.tsx`（HarnessesPage describe 追加用例）

- [ ] **Step 1: 追加失败的测试**

在 `describe("HarnessesPage", …)` 内追加：

```tsx
  it("接口失败时错误在面板内并显示空状态行", async () => {
    clientMocks.getHarnesses.mockRejectedValueOnce(new Error("HTTP 500"));
    clientMocks.getAgents.mockResolvedValueOnce({ items: [] });
    render(<HarnessesPage />);
    expect(await screen.findByText(/HTTP 500/)).toBeTruthy();
    const harnessPanel = document.querySelector("section[aria-label='Harness 安装情况']") as HTMLElement;
    expect(harnessPanel.textContent).toContain("暂无 Harness 数据");
    expect(await screen.findByText("暂无 Agent")).toBeTruthy();
  });
```

- [ ] **Step 2: 运行确认失败**

Run: `pnpm --dir apps/web test tests/components.test.tsx`
Expected: FAIL（错误当前渲染在 `.page` 直下，面板内无空状态行）

- [ ] **Step 3: 改组件与样式**

3a. `HarnessesPage` 的 return 整体替换为：

```tsx
  return (
    <div className="page">
      <section className="panel wide-panel" aria-label="Harness 安装情况">
        <h2>Harness 安装情况</h2>
        {error && <p className="error">{error}</p>}
        <table>
          <thead>
            <tr>
              <th>Harness</th>
              <th>已安装</th>
              <th>版本</th>
              <th>本机可运行</th>
              <th>协议就绪</th>
              <th>评测执行就绪</th>
            </tr>
          </thead>
          <tbody>
            {harnesses.map((harness) => (
              <tr key={harness.name}>
                <td>{harness.name}</td>
                <td className={harness.installed ? "pass" : "fail"}>{harness.installed ? "是" : "否"}</td>
                <td className="mono">{harness.version ?? "—"}</td>
                <td className={harness.runnable ? "pass" : "fail"}>{harness.runnable ? "是" : "否"}</td>
                <td className={harness.protocol_ready ? "pass" : "fail"}>{harness.protocol_ready ? "是" : "否"}</td>
                <td className={harness.execution_ready ? "pass" : "fail"}>{harness.execution_ready ? "是" : "否"}</td>
              </tr>
            ))}
            {harnesses.length === 0 && (
              <tr><td colSpan={6} className="empty">暂无 Harness 数据</td></tr>
            )}
          </tbody>
        </table>
      </section>
      <section className="panel narrow-panel" aria-label="Agent 运行时">
        <h2>Agent 运行时</h2>
        <table>
          <thead>
            <tr><th>Agent</th><th>类型</th><th>协议就绪</th><th>评测执行就绪</th><th>说明</th></tr>
          </thead>
          <tbody>
            {agents.map((agent) => (
              <tr key={agent.id}>
                <td className="mono">{agent.id}</td>
                <td className="mono">{agent.kind}</td>
                <td className={agent.protocol_ready ? "pass" : "fail"}>{agent.protocol_ready ? "是" : "否"}</td>
                <td className={agent.execution_ready ? "pass" : "fail"}>{agent.execution_ready ? "是" : "否"}</td>
                <td>{agent.description}</td>
              </tr>
            ))}
            {agents.length === 0 && (
              <tr><td colSpan={5} className="empty">暂无 Agent</td></tr>
            )}
          </tbody>
        </table>
      </section>
    </div>
  );
```

3b. `index.css`「页面与面板」段 `.panel.detail` 规则之后追加：

```css
/* 不对称双表布局：宽表占大头，禁止两个内容面板 50/50 平分（DESIGN.md 第 4 节） */
.panel.wide-panel {
  flex: 2 1 0;
}

.panel.narrow-panel {
  flex: 1 1 0;
}
```

3c. `index.css` 表格段 `th { … }` 规则内追加一行：

```css
  white-space: nowrap;
```

3d. `DESIGN.md` 第 4 节「面板宽度分级」条目末尾追加：

```
；不对称双表布局用 `.panel.wide-panel`（flex 2）/ `.panel.narrow-panel`（flex 1）
```

「表格」条目的「表头 12-13px 次色 weight 500」后追加「、表头不换行」。

- [ ] **Step 4: 运行测试确认通过**

Run: `pnpm --dir apps/web test tests/components.test.tsx`
Expected: PASS（现有用例 `getAllByText("评测执行就绪")` 仍为 2）

- [ ] **Step 5: 批次门禁与提交（批 4 收尾）**

Run: `pnpm --dir apps/web build && make check`
Expected: 全绿

```bash
git add apps/web/src/pages/ResourcesPage.tsx apps/web/src/index.css apps/web/DESIGN.md apps/web/tests/components.test.tsx
git commit -m "fix(web): harness report layout and empty states"
```

---

### Task 12: 时间线事件时间戳（批 5 起）

**Files:**
- Modify: `apps/web/src/components/RunTimeline.tsx:53-59`
- Modify: `apps/web/src/index.css`（时间线段）
- Modify: `apps/web/DESIGN.md:116`（时间线条目）
- Modify: `apps/web/tests/components.test.tsx`（RunTimeline describe 追加用例）

**Interfaces:**
- Consumes: Task 6 的 `formatClock`；`TraceEvent.recorded_at`（client.ts:387，字段已存在）。

- [ ] **Step 1: 追加失败的测试**

在 `describe("RunTimeline", …)` 内追加：

```tsx
  it("渲染事件时间戳（recorded_at）", () => {
    render(<RunTimeline events={[
      { run_id: "run-1", seq: 1, type: "queued", recorded_at: "2026-09-19T08:30:05" },
    ] as any} />);
    expect(document.querySelector(".event .time")?.textContent).toBe("09-19 08:30:05");
  });
```

- [ ] **Step 2: 运行确认失败**

Run: `pnpm --dir apps/web test tests/components.test.tsx`
Expected: FAIL（无 `.time` 节点）

- [ ] **Step 3: 改组件与样式**

3a. `RunTimeline.tsx` 头部增加导入：

```tsx
import { formatClock } from "./runFormat";
```

事件行 `<span className="seq">#{event.seq}</span>` 之后插入：

```tsx
            {event.recorded_at && <span className="time">{formatClock(event.recorded_at)}</span>}
```

3b. `index.css` 时间线段 `.event .seq { … }` 之后追加：

```css
.event .time {
  min-width: 110px;
  font-family: var(--font-mono);
  color: var(--text-faint);
}
```

3c. `DESIGN.md` 第 4 节「时间线」条目「seq 用 `--text-faint` mono」后追加「，事件时间戳列（recorded_at，`MM-DD HH:mm:ss` mono `--text-faint`）」。

- [ ] **Step 4: 运行测试确认通过**

Run: `pnpm --dir apps/web test tests/components.test.tsx`
Expected: PASS（现有 EVENTS 无 recorded_at，时间列不渲染，原断言不受影响）

- [ ] **Step 5: 构建与提交**

Run: `pnpm --dir apps/web build`

```bash
git add apps/web/src/components/RunTimeline.tsx apps/web/src/index.css apps/web/DESIGN.md apps/web/tests/components.test.tsx
git commit -m "feat(web): timeline event timestamps"
```

---

### Task 13: 指标卡语气跟随运行状态

**Files:**
- Modify: `apps/web/src/evalTypes/gsm8k/Gsm8kResult.tsx:105`
- Modify: `apps/web/src/evalTypes/directllm/DirectLlmResult.tsx:117`
- Modify: `apps/web/DESIGN.md:139`（指标卡条目）
- Modify: `apps/web/tests/evalTypes.test.tsx`（Gsm8kResult / DirectLlmResult describe 各追加用例）

- [ ] **Step 1: 追加失败的测试**

Gsm8kResult describe 内：

```tsx
  it("失败运行的通过率指标降为中性语气", async () => {
    clientMocks.getRun.mockResolvedValue({ ...GSM8K_RUN, status: "failed" });
    clientMocks.getReport.mockResolvedValue({ cost: null, scores: [] });
    render(
      <MemoryRouter initialEntries={["/gsm8k/runs/run-42/result"]}>
        <Gsm8kResult />
      </MemoryRouter>
    );
    const value = await screen.findByText("33%");
    expect(value.closest(".metric-card")!.getAttribute("data-tone")).toBe("neutral");
  });

  it("完成运行的通过率保持 success 语气", async () => {
    clientMocks.getRun.mockResolvedValue(GSM8K_RUN);
    clientMocks.getReport.mockResolvedValue({ cost: null, scores: [] });
    render(
      <MemoryRouter initialEntries={["/gsm8k/runs/run-42/result"]}>
        <Gsm8kResult />
      </MemoryRouter>
    );
    const value = await screen.findByText("33%");
    expect(value.closest(".metric-card")!.getAttribute("data-tone")).toBe("success");
  });
```

DirectLlmResult describe 内（`directRun` 与 `getReport` beforeEach 已存在）：

```tsx
  it("失败运行的通过率指标降为中性语气", async () => {
    clientMocks.getRun.mockResolvedValue(directRun({ status: "failed" }));
    render(
      <MemoryRouter initialEntries={["/direct-llm/runs/run-51/result"]}>
        <DirectLlmResult />
      </MemoryRouter>
    );
    const value = await screen.findByText("50%", { selector: ".metric-value" });
    expect(value.closest(".metric-card")!.getAttribute("data-tone")).toBe("neutral");
  });
```

- [ ] **Step 2: 运行确认失败**

Run: `pnpm --dir apps/web test tests/evalTypes.test.tsx`
Expected: 新用例 FAIL（tone 硬编码 success）

- [ ] **Step 3: 改两个结果页**

Gsm8kResult.tsx 指标数组第一项：

```tsx
          { label: `accuracy · ${passed}/${scores.length}`, value: accuracy == null ? "—" : `${accuracy}%`, tone: run.status === "completed" ? "success" : "neutral" },
```

DirectLlmResult.tsx 指标数组第一项：

```tsx
          { label: `通过率 · ${passed}/${judged}`, value: rate == null ? "—" : `${rate}%`, tone: run.status === "completed" ? "success" : "neutral" },
```

`DESIGN.md` 第 4 节「指标卡」条目末尾追加：

```
；指标语气跟随运行整体状态，仅 completed 用 success，失败/取消等终态降为 neutral
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pnpm --dir apps/web test tests/evalTypes.test.tsx`
Expected: PASS（现有 "33%"/"50%" 文本断言不受影响）

- [ ] **Step 5: 构建与提交**

Run: `pnpm --dir apps/web build`

```bash
git add apps/web/src/evalTypes/gsm8k/Gsm8kResult.tsx apps/web/src/evalTypes/directllm/DirectLlmResult.tsx apps/web/DESIGN.md apps/web/tests/evalTypes.test.tsx
git commit -m "fix(web): metric tone follows run status"
```

---

### Task 14: 审计快照排版

**Files:**
- Modify: `apps/web/src/components/RunAuditSummary.tsx`
- Modify: `apps/web/tests/components.test.tsx`（RunAuditSummary describe）

- [ ] **Step 1: 先改测试（TDD）**

RunAuditSummary 用例中，把

```tsx
    expect(screen.getByText(/requested → reported/)).toBeTruthy();
```

改为

```tsx
    expect(screen.getByText(/请求 requested · 报告 reported · 实际 reported/)).toBeTruthy();
```

并在该 describe 内追加：

```tsx
  it("长哈希截断为 12 位并以 title 保留全量", () => {
    const hash = "b".repeat(64);
    render(<RunAuditSummary run={{
      id: "run-2",
      schema_version: 2,
      revision: 1,
      scenario_version: "direct-llm@1",
      status: "completed",
      manifest: {
        resource_snapshots: { model_profile: { id: "m", generation: 2, lifecycle: "published", content_hash: hash } },
      },
      cases: [],
    } as any} />);
    const cell = screen.getByTitle(hash);
    expect(cell.textContent).toContain(`${"b".repeat(12)}…`);
  });
```

- [ ] **Step 2: 运行确认失败**

Run: `pnpm --dir apps/web test tests/components.test.tsx`
Expected: FAIL

- [ ] **Step 3: 改 RunAuditSummary.tsx**

3a. 删除 `identityLabel` 函数，替换为按字段去重汇总：

```tsx
function fieldSummary(identities: Record<string, any>[], key: string): string {
  const values = [...new Set(identities
    .map((identity) => identity[key])
    .filter((value): value is string => typeof value === "string" && Boolean(value)))];
  if (values.length === 0) return "未报告";
  if (values.length === 1) return values[0];
  return `${values.length} 种取值`;
}

function shortHash(hash: unknown): string {
  if (typeof hash !== "string" || !hash) return "—";
  return hash.length > 16 ? `${hash.slice(0, 12)}…` : hash;
}
```

3b. 快照两条 dd 改为（title 保留全量哈希）：

```tsx
        {modelSnapshot.id && (
          <>
            <dt>ModelProfile 快照</dt>
            <dd className="mono" title={typeof (modelSnapshot.profile_hash ?? modelSnapshot.content_hash) === "string"
              ? String(modelSnapshot.profile_hash ?? modelSnapshot.content_hash) : undefined}>
              {modelSnapshot.id} · g{modelSnapshot.generation ?? 1} · {modelSnapshot.lifecycle ?? "legacy"} · {shortHash(modelSnapshot.profile_hash ?? modelSnapshot.content_hash)}
            </dd>
          </>
        )}
        {providerSnapshot.name && (
          <>
            <dt>Provider 快照</dt>
            <dd className="mono" title={typeof providerSnapshot.content_hash === "string" ? providerSnapshot.content_hash : undefined}>
              {providerSnapshot.name} · g{providerSnapshot.generation ?? 1} · {shortHash(providerSnapshot.content_hash)}
            </dd>
          </>
        )}
```

3c. 「模型身份」dd 替换为结构化三段（去掉箭头链）：

```tsx
        {identities.length > 0 && (
          <>
            <dt>模型身份</dt>
            <dd className="mono">
              请求 {fieldSummary(identities, "requested_model")} · 报告 {fieldSummary(identities, "reported_model")} · 实际 {fieldSummary(identities, "resolved_model_identity")}
              {identities.length > 1 ? ` · ${identities.length} cases` : ""}
            </dd>
            <dt>身份策略</dt>
            <dd className="mono">
              {new Set(identities.map((item) => item.identity_policy ?? "report_only")).size === 1
                ? identities[0].identity_policy ?? "report_only"
                : "多种策略"}
              {identities.every((item) => item.policy_passed === true)
                ? " · 通过"
                : identities.some((item) => item.policy_passed === false)
                  ? " · 未通过"
                  : " · 未判定"}
            </dd>
          </>
        )}
```

（「身份策略」段维持原逻辑不变，只是从原 JSX 平移。）

- [ ] **Step 4: 运行测试确认通过**

Run: `pnpm --dir apps/web test tests/components.test.tsx`
Expected: PASS（`require_match · 未通过` 断言不受影响）

- [ ] **Step 5: 构建与提交**

Run: `pnpm --dir apps/web build`

```bash
git add apps/web/src/components/RunAuditSummary.tsx apps/web/tests/components.test.tsx
git commit -m "fix(web): readable audit snapshot formatting"
```

---

### Task 15: 失败列表 / 题目页 nowrap / 文案统一（批 5 收尾）

**Files:**
- Modify: `apps/web/src/index.css`（`.failure-list` / `.nowrap`）
- Modify: `apps/web/src/evalTypes/gsm8k/Gsm8kOperate.tsx:158,362`
- Modify: `apps/web/src/evalTypes/directllm/DirectLlmOperate.tsx:231,518`
- Modify: `apps/web/src/evalTypes/gsm8k/Gsm8kCases.tsx:197`
- Modify: `apps/web/src/evalTypes/directllm/DirectLlmCases.tsx:187`
- Modify: `apps/web/DESIGN.md:113,122`

**Interfaces:**
- Produces: `.failure-list`（无原生列表样式的逐模型失败列表）；`.nowrap`（单值 ID 列不折行）。

- [ ] **Step 1: 改标记（样式类先于 CSS 落地不影响测试）**

1a. `Gsm8kOperate.tsx:362` 与 `DirectLlmOperate.tsx:518` 的失败列表开标签：

```tsx
        {failures.length > 0 && (
          <ul className="failure-list">
```

1b. `Gsm8kCases.tsx:197`：

```tsx
                <td className="mono nowrap">{item.case_id}</td>
```

`DirectLlmCases.tsx:187`：

```tsx
                <td className="mono nowrap">{item.case_id}</td>
```

1c. 文案统一：
- `Gsm8kOperate.tsx:158` 与 `DirectLlmOperate.tsx:231` 的卡片标题 `数据集（pinned）` 改为 `数据集（版本固定）`。

- [ ] **Step 2: 改 index.css 与 DESIGN.md**

2a. `index.css`「结果与提示」段 `.error { … }` 之后追加：

```css
/* 逐模型失败列表：贴近主动作行，清掉原生列表样式 */
.failure-list {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
```

2b. 表格段 `td.row-actions { … }` 之后追加：

```css
/* 单值 ID 列不折行（case id 的连字符不是断行点） */
.nowrap {
  white-space: nowrap;
}
```

2c. `DESIGN.md` 第 4 节「表格」条目「ID / 数字列加 `.mono`」后追加「，单值 ID 列加 `.nowrap` 防连字符折行」；「错误提示」条目末尾追加「；逐模型失败清单用 `.failure-list`（无原生列表样式）」。

- [ ] **Step 3: 全量测试**

Run: `pnpm --dir apps/web test`
Expected: PASS（`MODEL_DISABLED` 等断言按文本查找，不受 ul 类名影响）

- [ ] **Step 4: 批次门禁与提交（批 5 收尾）**

Run: `pnpm --dir apps/web build && make check`
Expected: 全绿

```bash
git add apps/web/src/index.css apps/web/DESIGN.md apps/web/src/evalTypes/gsm8k/Gsm8kOperate.tsx apps/web/src/evalTypes/directllm/DirectLlmOperate.tsx apps/web/src/evalTypes/gsm8k/Gsm8kCases.tsx apps/web/src/evalTypes/directllm/DirectLlmCases.tsx
git commit -m "fix(web): failure lists, nowrap ids, copy polish"
```

---

## 完成后人工验收清单（浏览器，1440px 与 860px）

1. 侧边栏无横向滚动条、导航项无下划线（批 1）。
2. GSM8K / Direct LLM 操作页：模型行「勾选框 + ID + 徽章」一行；file 控件不可见；每屏至多一个黑按钮且是「发起跑测/发起评测」（批 1-2）。
3. 运行总览：长 ID 截断、开始/耗时列、「过程」入口可进监控页；类型/状态两个下拉同款外观（批 3）。
4. `/gsm8k/monitor`、`/gsm8k/compare` 直接访问均有引导空状态（批 3）。
5. Replay 操作页：左表单右「最近 Replay 运行」、无裸奔原生控件（批 4）。
6. Harness 页：错误在面板内、宽窄表 2:1、表头横排（批 4）。
7. 时间线有时间列；失败 run 的 accuracy 无绿色大数字；审计快照无 70 字符长哈希与箭头链（批 5）。

## 明确不做（backlog）

- 窄屏（<900px）侧栏收窄为图标栏：需新增侧栏 token 与骨架改动，单独成批。
- 时间线事件间相对时差（距上个事件 N 秒）：时间列落地后再看必要性。
