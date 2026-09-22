# MoTTEavl Web 控制台设计规范（DESIGN.md · v4 信息板）

本文件是 `apps/web` 所有 UI 工作的唯一设计依据。改动界面之前先读本文。
改设计 = 改本文 = 改 `src/tailwind.css` / `src/theme.css` 的令牌，三者同一个提交。

v4 描述的是**已经建成的东西**（不是计划）：深色信息板世界 + 板面/签发台/转录面三种页面语法。
v3 的骨架与页面模式思路被继承，视觉世界整体替换；**迁移桥已在 v4.1 拆除**（第 8 节），
旧世界（浅色冷灰纸面 + 6/8/9999px 圆角 + `--bg-canvas` 一族令牌）在仓库里不再有任何残留。

## 0. 设计语言与基调

一句话：**深色搪瓷信息板**——并行的运行在一瞥之内成立，任一读数一步走到它的证据。

- 气质关键词：承诺过的深底、方角、2px 语气条、DIN 血统窄体大写标签、tabular 等宽数字、翻牌格
- 隐喻：机场/车站的信息板与导向系统。板面是被"扫"的，不是被"读"的
- **唯一主题：深色**。不提供亮色变体（未决，见第 11 节）
- **设计基准：桌面 16:9**（1440×900 与 1920×1080 是验收尺寸）

## 1. 设计原则

1. **状态色是一等公民**：五语气只表达状态与语气，禁止装饰性用色；全站同一信息只有一种表达。
2. **层级来自结构与间距**：全站无阴影（下拉浮层除外），靠 1px 发丝线与 16px 栅格区分。
3. **数据层用等宽字体**：ID / 模型名 / 数字 / 成本 / 时间戳 / 哈希 / 日志一律 `--font-data`，全程 tabular。
4. **方角 + 一层描边**：圆角只有 2px（格/控件）与 4px（面板）两档；**不存在 9999px 的圆 pill**。
5. **动效只指示状态变化**：唯一签名动效是"翻牌"（状态真变化时 240ms 换字），其余 120ms。
6. **诚实优先**：未知写未知、缺工件写缺工件，不填 0、不伪造、不静默隐藏。
7. **一屏一个工作面**：滚动发生在板面内部，顶栏与底部主动作永不随内容滚走。

## 2. 令牌

令牌定义在 `src/` 的两个文件里（`tailwind.css` 的 `@theme` 与 `theme.css` 的 `:root`），Tailwind 4 的 CSS-first 配置：

### 2.1 `tailwind.css` 的 `@theme`（唯一定义处）

| 组 | 令牌 | 值 |
|---|---|---|
| 板面底 | `--color-board-void` / `-panel` / `-band` / `-cell` | `#0b0f14` / `#131a22` / `#1b242e` / `#101820` |
| 发丝线 | `--color-board-rule` / `-rule-strong` | `#2a3641` / `#3c4b59` |
| 墨色 | `--color-ink-primary` / `-secondary` / `-faint` / `-inverse` | `#f2f5f7` / `#a9b6c2` / `#6f7e8c` / `#0b0f14` |
| 五语气 | `--color-tone-info` / `-success` / `-warning` / `-error` / `-neutral` | `#57b6d9` / `#4cc38a` / `#e3a63c` / `#e8695f` / `#7e8c9a` |
| 字体 | `--font-board`（Barlow Semi Condensed + 中文系统栈）/ `--font-data`（JetBrains Mono）/ `--font-sans` | 均自托管（`src/assets/fonts/`） |
| 圆角 | `--radius-cell` 2px / `--radius-panel` 4px | |
| 密度 | `--spacing-row` 40px / `--spacing-row-compact` 32px | |

`theme.css` 把它们别名成 `--board-*` / `--ink-*` / `--tone-*`，供手写 CSS（`ui.css`）使用，并补上三组 `@theme` 不适合放的令牌：

- **语气三件套**：`--tone-{info,success,warning,error,neutral}-bg` / `-border`（深色底上的半透明语气，成对使用）。
  语气前景就是 `--tone-*` 本身，不再有独立的 `-fg` 令牌。
- **弹层表面**：`--scrim`（遮罩）、`--shadow-pop`（浮层阴影，全站唯一允许阴影处）。
- **骨架尺寸**：`--shell-sidebar-w` / `--shell-topbar-h` / `--pane-gap` / `--page-pad-x|y` / `--panel-pad-x|y` / `--measure` / `--rail-w`。

### 2.2 样式分层（三层，顺序即优先级）

| 文件 | 职责 | 允许写十六进制色值 |
|---|---|---|
| `tailwind.css` | preflight + `@theme` 令牌（唯一定义处） | ✅ |
| `ui.css` | 控制台类层：`.page` / `.panel` / `.control` / `.status-badge` … 全部定义成板面语法 | ❌（断言在 `tests/stylesheet.test.ts`） |
| `theme.css` | 令牌别名与骨架尺寸、字体 `@font-face`、Semi 语义 token 覆盖、浏览器表面、板选择器 | ✅ |
| `board/board.css` | 板面基础件（`Board` / `StatusFlap` / `FieldGrid` / 转录面）的语法 | ❌ |

`ui.css` **不是"旧世界的皮"，也不是迁移桥**：它是控制台类名的唯一定义处，取值只允许板面令牌。
页面级的一次性样式写在页面自己的 Tailwind 工具类里，不进 `ui.css`。

### 2.3 Semi Design 覆盖（运行期 CSS 变量，已实测）

Semi 2.103 的编译产物用 **2744 处 `var(--semi-*)` 消费 690 个变量**；深色主题就是
`body[theme-mode=dark]` 里重定义它们；语义 token **从不被 `rgba()` 二次包装**（实测 0 处）。
因此主题化走**运行期 CSS 变量**，不需要编译 SCSS 主题包：`theme.css` 覆盖约 30 个
`--semi-color-*` 语义 token 即完成全站换肤。

包路径注意：Semi 的 `exports` 没导出 `dist/css/*`，完整主题只能用 Vite 别名指到真实文件
（见 `vite.config.mts` 的 `semiThemeCss`）；`lib/es/_base/base.css` 只有 35KB 变量层，不含组件样式。

### 2.4 字号阶梯

26 板面大读数（data 600）/ 20 页面标题 / 15 面板标题（board 600）/ 13 正文与表单 /
12.5 数据 / 12 翻牌格 / 11 列名（board、大写、字距 .08–.1em）/ 10 构建标识与 seq。

## 3. 骨架与导航

```
┌──────────┬──────────────────────────────────────────────────────────────┐
│ 品牌条 52│ 顶栏 52：路径 · 板选择器 · 右：会话心跳（进行中 N + LED）      │
├──────────┼──────────────────────────────────────────────────────────────┤
│ 目录 240 │ 工作台（.page 是唯一滚动区，或滚动下沉进板面）                 │
│ 可折叠   │                                                              │
│ 状态条吸底│                                                              │
└──────────┴──────────────────────────────────────────────────────────────┘
```

- 侧栏齐窗边，品牌条与顶栏同高 → 顶部一条 1px 线贯穿全窗
- 导航分组可折叠（状态存 `localStorage`），导航区内部滚动，底部状态条**永远可见**
- 底部状态条右侧是**构建标识**（`__BUILD_ID__`，形如 `ac94801·09221429`）——截图自带的构建证据
- **板选择器**（原分段页签）：DIN 大写、激活项 = 填充翻牌格 + 2px info 底线；它是套件的主导航，权重不能低于右侧心跳
- 顶栏右区**会话心跳**：`进行中 N`，每 10s 轮询 `GET /runs?status=running`，页面不可见时暂停；读不到写「未知」，不填 0

## 4. 五种页面模式

| 模式 | 结构 | 归入 |
|---|---|---|
| **P1 板面** | 吸附表头带 + 固定列栅格 + 状态列固定列位 + 行就地展开 + 底部读数带 | 运行总览、题目清单（GSM8K / Direct LLM）、评分历史、TB 任务清单、资源清单、批次过程、逐题下钻、评分结果 |
| **P2 闸口页** | 单单元聚焦：标识铭牌 → 状态格 + 进度 → 阶段/事件/工件/评分分节 | 各套件监控、各套件结果 |
| **P3 签发台** | `.field-grid` 字段栅格 + 后果行 + 吸底单一主动作（`.issue-bar`） | 各套件操作页、Judge 校准、各创建表单 |
| **P4 转录面** | 连续、可搜索、键盘优先的文本面（`.transcript`）；层级靠缩进与整宽横线 | 运行时间线、终端日志、草案编辑 |
| **P5 对照页** | 同板语法并排多个单元 + 密度即读数的逐题格 | 比较、基线、门禁求值 |

通则：主动作每屏一个；过滤永远在表头带内；空态必须写原因与下一步；未知不渲染成 0 或绿色。

## 5. 组件

板面件（`src/board/`，世界语法靠它们钉住）：

| 组件 | 规格 |
|---|---|
| `Board` | 板面容器：`table-layout: fixed` + 吸附 `.board-band`（DIN 大写列名 + 2px 下横线）；列宽由页面用 Tailwind 工具类写在 `th` 上；支持 `testId` 让旧表带着 data-testid 搬进来 |
| `StatusFlap` | 状态格：2px 左侧语气条 + 2px 圆角格 + **封闭词表**（只从 `STATUS_META` 取）；进行中呼吸；状态**真变化**时播一次翻牌 |
| `OutcomeFlap` | 结果格：评分判定（通过/未通过/未判定）。**与 StatusFlap 共用语法但不共用词表**——状态语义的唯一来源不能被评分判定污染 |
| `IdentityPlate` | 标识铭牌：主标识（data 字体、不截断到不可区分、可复制）+ 固定顺序的证据行（revision / scenario / pass） |
| `EmptyBoard` | 空态：必须写原因与下一步 |
| `.ready` | 就绪格：二值能力（是/否）用点 + 词，**不借五种语气** |
| `.transcript` | 转录面行：`seq / time / type / detail` 四列栅格；只设左边框的宽度与样式，颜色留给 `.event-tone-*` |

签发台件（`src/board/FieldGrid.tsx`）：

| 组件 | 规格 |
|---|---|
| `FieldGrid` | 字段栅格：默认两列、`columns={1\|2\|3}`、单列 measure ≤420px。**控件样式由这个显式原语按上下文给定**——所以"忘了写 className 就掉回浏览器默认样式"在新系统里不可能发生 |
| `Field` | 字段：DIN 大写标签在上、控件在中、hint 在下；`wide` 跨满整行 |
| `IssueBar` | 主动作条：吸底、左侧唯一 primary、右侧后果行；`margin-top: auto` 吃掉余高，内容不足一屏也落位在工作台底部 |

表单控件优先 Semi（`Input` / `Select` / `Button`，深路径引入）；原生控件放进 `FieldGrid` 也会被原语统一到板面规格。

## 6. 状态语义（唯一来源：`src/components/statusMeta.ts`）

五语气（neutral / info / success / warning / error）是全站唯一信号系统，映射表在 `STATUS_META`。
规则：新增状态先登记语气、中文标签与 `scope`；**禁止在组件里手写状态颜色**；运行总览的状态过滤只列 `scope: "run"`。
进行中语气呼吸；终态恒亮；`unknown` 视为需人工处理，不得当作成功展示。
**评分判定是独立词表**（通过 / 未通过 / 未判定），不进 `STATUS_META`。

## 7. 数据呈现（`src/components/runFormat.ts` 是唯一实现）

| 类型 | 规则 |
|---|---|
| 绝对时间 | `MM-DD HH:mm`（`formatTimestamp`）/ 带秒用 `formatClock`；**禁止 ISO 原样输出** |
| 时长 | `25s` / `1m22s` / `2h05m`（`formatDuration`） |
| ID | 等宽；列宽按最长形态给足，不截断到不可区分；确需缩略时保留**尾部**（`shortRunId`）+ `title` 全量 |
| 哈希 | 前 8 后 8 + 点击复制全量 |
| 成本 | 永远带币种与统计范围；未知写「未知」 |
| 空值 | 统一 `—`；永不空白、永不 0 |

## 8. 迁移（已完成，桥已拆除）

v4 的第一版用两段**迁移桥**把尚未重写的页面拉进新世界：token 层（旧 token 名 → 板面调色板）与
组件层（旧类名整体改写到板面语法）。桥的价值是让"深色外壳 + 浅色页面"的半成品期不存在。

**v4.1 把桥拆了。** 拆法不是删掉控制台类层（那会让 30 个页面的 markup 全部作废），
而是**把类层本身改写到板面令牌上**——于是它不再是"临时补丁"，而是类名的唯一定义处：

1. `index.css`（浅色"冷灰纸面"，2100 行）→ `ui.css`（板面类层，2047 行）。
   45 个旧 token 名、16 处十六进制、13 处 rgba、6/8/9999px 圆角**全部清零**，断言在 `tests/stylesheet.test.ts`。
2. `theme.css` 的 token 桥与组件桥整块删除；`theme.css` 只剩令牌别名、字体、Semi 覆盖、浏览器表面、板选择器。
3. 结构性遗留一并消灭：`.operate-section` / `.operate-grid` / `.operate-card` 整族删除
   （操作页改用 `FieldGrid` + `IssueBar`）；`.status-badge` 改成翻牌格语法；
   `.metric-card` 改成板面读数格墙；`.table-scroll` / `.pane-scroll` / `.action-bar` / `.runs-table` / `.skeleton` 等 28 个死类名删除。
4. 旧组件 `StatusBadge` / `MetricCards` 保留**类名与语义契约**（`.status-badge` / `.metric-card[data-tone]`），
   实现换成板面语法——测试按语义断言（tone），不按外观断言，所以它们不是"遗留组件"，是板面原语的薄封装。

### 迁移台账（诚实记录）

**已在板面语法上（全部页面）**：应用骨架、运行总览、GSM8K 操作页/题目页、Direct LLM 操作页/题目页、
Terminal-Bench 操作页/任务清单、Agent 文件任务、CMMLU / C-Eval、评分历史、批次过程、运行时间线、
逐题下钻、评分结果、资源清单、Provider 与模型、实验 / 比较 / 基线 / 门禁、场景 Workflow、Skill 校验、
Replay / Fallback / 外部基准、Judge 校准。

**仍然欠着（质量工作，不是一致性问题）**：
- Skill / Scenario 的 Schema 字段编辑仍是手写 `<label>` 序列（Workflow 文本页已迁到 `FieldGrid` + `IssueBar`）
- `.runs-table .col-*` 之类的旧列宽钩子已随迁移删除，若将来需要列宽请写在 `<th>` 的 Tailwind 工具类上

## 9. 技术栈与治理

React 19 + Vite 8 + TypeScript；**Tailwind CSS 4** + **Semi Design 2.103**（含 React 19 官方适配器
`@douyinfe/semi-ui/react19-adapter`，必须在任何 Semi 组件之前引入）；交互原语 Radix；路由 react-router-dom。

样式载入顺序（`src/main.tsx`，**顺序即优先级，别改**）：
`semi.css` → `tailwind.css`（preflight + 令牌）→ `ui.css`（控制台类层）→ `theme.css`（令牌别名 + 字体 + Semi 覆盖 + 浏览器表面）。

- preflight 必须排在类层之前：类层只定义自己的类名，不接管 UA 默认样式
- 依赖分包 + 路由级懒加载：首屏 app chunk 90KB → 52KB gzip；重页面按路由切分
- 治理规则见 `AGENTS.md`（含"截图即证据"三条）

## 10. 门禁与证据

1. `pnpm --dir apps/web test` 与 `pnpm --dir apps/web build` 全绿
2. 按 1440×900 与 1920×1080 截图，对照第 4 节逐条核验
3. `impeccable detect --json --scope layout` 无新增 findings
4. **截图必须自带构建证据**（第 3 节的构建标识）：截图晚于构建、服务端资产名与 `dist/index.html` 一致、图里读得到标识

## 11. 未决

- **亮色主题**：当前是深色单主题。若需要在强光下使用，需要另做一版亮色令牌（用户已同意"先完成深色再决定"）。
- **Semi CSS 体积**：单文件全量主题 75KB gzip，摇不掉；要压下去得走 SCSS 按组件编译主题。
- **字段编辑器的 Schema 分支**：Scenario / Skill 的 Schema 字段仍是手写 label 序列，未走 `FieldGrid`。
- **`ui.css` 的体量**：2047 行仍是"控制台类层"而非"组件库"；若将来类名继续增长，应拆成按域的多个文件。
