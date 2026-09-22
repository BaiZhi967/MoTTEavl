# MoTTEavl Web 控制台设计规范（DESIGN.md · v2 任务控制）

本文件是 `apps/web` 所有 UI 工作的唯一设计依据。任何 UI 改动前先读本文。
修改任何设计决策时，必须同步修改 `src/index.css` 中的对应 token，并放在同一个提交里。
token 与本文冲突时，以本文为准并立即修正 token。

本文取代 v1（暖灰文档式体系）。v1 中仍然有效的遗产——`STATUS_META` 唯一状态来源、
Radix 无头原语 + token 手写 CSS、证据诚实性规则——原样保留并在第 4、7、9 节登记。

## 0. 设计语言与基调

一句话：**亮色的任务控制终端**——数据密集、状态优先、仪表读数式的评测工作台。

- 气质关键词：冷灰纸面、发丝线分区、语义色即信号、等宽数据层、呼吸 LED
- 隐喻：评测操作员盯着的一块飞行仪表板。颜色不装饰，只报告；动效不表演，只指示存活
- 单一亮色主题，不提供暗色变体；所有组件只维护一套取值

## 1. 设计原则

1. **状态色是一等公民。** 语义五语气（info / success / warning / error / neutral）只表达状态与语气，禁止装饰性用色；其中 info 语气使用 accent 色。同一信息的状态表达全站唯一：徽章、LED、进度、信号格、时间线条、通知条共用同一组语气 token。
2. **层级来自结构与间距，不来自阴影。** 全站无阴影（下拉浮层除外，见 5.16）；面板之间靠 12px 网格留白与 1px 发丝线区分。
3. **数据层用等宽字体。** Run ID、模型名、数字、成本、时间戳、JSON、代码、徽章文字一律 mono；正文用中文黑体栈。数字全程 `tabular-nums`。
4. **扁平 + 一层描边。** 控件与面板只有「默认底 + 1px 边框」和「强调边框」两档，禁止渐变填充（品牌方块除外）、禁止投影按钮。
5. **动效只指示状态变化。** 只允许 200ms 内的 transform / opacity / 背景色 / 边框色过渡，外加两类环境动效：LED 呼吸（进行中）、进度流光（进行中）。`prefers-reduced-motion` 下全部降级为静态。
6. **诚实优先。** 未知显示「未知」，缺数据显示「缺工件」，不填 0、不伪造、不静默隐藏（第 9 节）。

## 2. 设计 token

唯一取值来源是 `src/index.css` 的 `:root`。写样式时只能引用 `var(--…)`；
需要新值时，先在本文登记，再加进 `:root`，然后才能使用。

### 2.1 色彩（冷灰纸面 + 语义信号色）

| Token | 值 | 用途 |
|---|---|---|
| `--bg-canvas` | `#F4F6FA` | 页面画布（冷灰纸面） |
| `--bg-surface` | `#FFFFFF` | 卡片 / 面板 / 侧栏 / 顶栏 |
| `--bg-subtle` | `#F0F3F8` | 行 hover、次级面、tab 容器底、徽章 dim 底 |
| `--bg-inset` | `#E9EDF4` | 输入控件底、终端底、进度条轨道、tab 未选中底 |
| `--border` | `#DCE2EC` | 一切默认边框与分隔线，永远 1px |
| `--border-strong` | `#C3CCDC` | 控件边框、hover 提亮、虚线空态框 |
| `--text-primary` | `#1B2331` | 正文与标题 |
| `--text-secondary` | `#5A6580` | 标签、说明、表头 |
| `--text-faint` | `#98A2B5` | 序号、占位、空状态、次级时间戳 |
| `--accent` | `#0E9BB8` | 主操作、链接、进行中（live）、焦点 |
| `--accent-hover` | `#0B7E96` | 主按钮 hover、链接 hover |
| `--on-accent` | `#FFFFFF` | accent 实底上的文字 |
| `--success` | `#15875A` | 通过、完成 |
| `--warning` | `#A96F00` | 需人工处理、证据不足 |
| `--error` | `#C6403C` | 失败、安全阻断、破坏性操作 |
| `--neutral` | `#667085` | 排队、取消、未运行、禁用 |

### 2.2 语气底（bg + 描边 + 前景三件套，成对使用）

| 语气 | dim 底 | 35% 透明描边 | 前景 |
|---|---|---|---|
| info（accent 色） | `rgba(14,155,184,.10)` | `rgba(14,155,184,.35)` | `--accent` |
| success | `rgba(21,135,90,.10)` | `rgba(21,135,90,.35)` | `--success` |
| warning | `rgba(169,111,0,.10)` | `rgba(169,111,0,.35)` | `--warning` |
| error | `rgba(198,64,60,.10)` | `rgba(198,64,60,.35)` | `--error` |
| neutral | `rgba(102,112,133,.10)` | `rgba(102,112,133,.35)` | `--neutral` |

徽章、信号格、通知条、时间线条、钻取条统一从这张表取三件套，**禁止单独发明搭配**。
token 命名为 `--tone-<语气>-bg` / `--tone-<语气>-border` / `--tone-<语气>-fg`。

### 2.3 字体

| Token | 值 | 说明 |
|---|---|---|
| `--font-sans` | `"PingFang SC","Microsoft YaHei",system-ui,sans-serif` | 中文与阅读文字；系统栈，不引入网络中文字体 |
| `--font-mono` | `"JetBrains Mono","PingFang SC","Microsoft YaHei",ui-monospace,monospace` | 数据层：ID、模型名、数字、JSON、徽章、表头标签、终端 |

- JetBrains Mono 自托管（woff2，400/500/600/700 置于 `apps/web/src/assets/fonts/`，
  `@font-face` 声明在 `index.css`，`font-display: swap`）。中文落入栈内中文字体，禁止用纯拉丁 display 字体渲染中文。
- 中文禁止斜体；强调用字重、颜色或字号。
- 字号阶梯：26 指标数值（mono 600）/ 18 品牌与页面大标题 / 15 面板标题（600）/ 14 正文 / 13 表格与表单 / 12.5 数据 mono / 12 徽章与辅助 / 11 大写分节标签（mono、字距 .14–.18em、uppercase）/ 10 表头与 seq（mono、字距 .14em、uppercase）。
- 行高：正文 1.6，数据行 1.5。全局 `font-variant-numeric: tabular-nums`。

### 2.4 间距、圆角、边框

- 基础网格 12px；间距刻度：`4 / 8 / 12 / 16 / 20 / 24 / 32`（侧栏与页边距用 20/24）
- 响应式辅助 token：`--space-none: 0`、`--space-sm: 12px`、`--size-full: 100%`；移动端单列与横向导航统一使用
- 圆角：`6px` 控件（按钮、输入、tab）/ `8px` 卡片与面板 / `9999px` 徽章 pill、进度条、开关
- 边框一律 `1px solid var(--border)`；控件与 hover 强调用 `var(--border-strong)`。全站不存在第二种边框宽度，不存在投影
- 焦点环：双层 `box-shadow: 0 0 0 2px var(--bg-canvas), 0 0 0 4px var(--accent)`，替代原生 outline

### 2.5 动效

| 场景 | 参数 |
|---|---|
| hover / 状态过渡 | `200ms ease`，只动 transform / opacity / 背景色 / 边框色 |
| 按钮 active | `scale(.98)`，100ms |
| LED 呼吸（仅进行中语气） | 1.5s ease-in-out 无限，外晕 0.35 不透明度缩放 |
| 进度流光（仅进行中进度条） | 1.8s linear 无限，一道 35% 白高光扫过 |
| 折叠展开 | `max-height + opacity`，0.42s cubic-bezier(.4,0,.2,1) |

`@media (prefers-reduced-motion: reduce)` 下：LED 恒亮、流光静止、过渡时长归零。

## 3. 界面结构与布局规则

### 3.1 应用骨架

```
┌──────────┬──────────────────────────────────────────────┐
│ 侧栏 236 │ 顶栏 56（面包屑 · 分段页签 · 右状态/操作区） │
│ sticky   ├──────────────────────────────────────────────┤
│ 全高     │ 工作台（独立滚动，padding 20/24，底部 48）    │
└──────────┴──────────────────────────────────────────────┘
```

- `.app-shell`：fixed 定位 `inset: 0`，外框不滚动；flex 行布局，gap 20，padding 20
- 侧栏 `sticky top:0 height:100vh` 独立滚动；工作台 `flex:1 min-width:0` 独立滚动
- 顶栏 `sticky top:0 z-index:10`，毛玻璃提亮（`backdrop-filter: blur(8px)` + 80% 不透明 `--bg-surface`），底 1px `--border`

### 3.2 两级导航

侧栏只放**分区入口**，共四区；套件的五段页面**不在侧栏平铺**，选中套件后由顶栏分段页签承担。全站约 45 条路由收敛为「分区 → 页签 / 页面」两层。

| 分区 | 侧栏项 |
|---|---|
| 总览 | 运行总览 |
| 评测套件 | Agent 文件任务 / CMMLU / Terminal-Bench / C-Eval / GSM8K / Direct LLM / Replay / 外部 Runtime（来自 `EVAL_SUITES` 注册表） |
| 资源 | Provider 与模型 / Agent·Harness / 场景 Workflow / Skill 校验 / Judge 校准 |
| 实验体系 | 实验 / 比较 / 基线 / 门禁 |

- 分区标签：11px mono、uppercase、字距 .18em、`--text-faint`，上下留白 14/6
- 导航项：16px 图标 + 13px 文字，padding 8/10，圆角 6；hover `--bg-subtle`；active **accent 语气三件套**（dim 底 + 35% 描边 + accent 前景 + 500 字重）
- 侧栏底部：连接状态条（LED + 11px mono 文案，如 `API 已连接 · v0.9.2`），永远可见
- 顶栏三段：左 = 面包屑（12px mono faint，如 `评测套件 / gsm8k / monitor`）；中 = 分段页签（套件页）或页面标题（全局页）；右 = 会话级状态（进行中计数 + LED）与操作（刷新、主题无关的全局动作）
- 分段页签：`.tabs-list` inset 底 + 1px 边框 + 6px 圆角，trigger 12.5px，active `--bg-surface` + accent 前景 + 1px `--border-strong` 描边

### 3.3 面板系统（`.page` 内的宽度分级）

| 类 | 宽度 | 用途 |
|---|---|---|
| `.panel` | 弹性占满剩余 | 默认内容面板 |
| `.panel.form-panel` | 340px 固定，max 340 | 付费提交、创建表单等辅助列；窄屏自动换行单列 |
| `.panel.list-panel` | 240px 固定 | 导航型清单列（Provider / Judge / Workflow 版本） |
| `.panel.wide-panel` + `.panel.narrow-panel` | flex 2 : 1 | 不对称双表；**禁止两个内容面板 50/50 平分** |
| `.panel.detail` / 通栏节 | flex-basis 100% | 时间线、终端、指标卡等横向整块 |

- 面板：白底 + 1px `--border` + 8px 圆角 + padding 16/18；**卡片只做分组，禁止卡片套卡片**；卡内分节用 `.embed-title`（14px 600，无壳）
- `.page`：flex wrap、gap 16、顶对齐（`align-items: flex-start`）、高度随内容不强制等高

### 3.4 四种页面模式

| 模式 | 结构 | 适用 |
|---|---|---|
| A 操作页 | 卡片栅格（`operate-grid` flex wrap，顶对齐不等高）：数据集 / 题目 / 模型 / 参数 / 导入等卡；底部通栏主动作条（primary 按钮 + 后果说明） | 各套件 operate |
| B 主从分栏 | `list-panel`（清单 + 选中态）+ 弹性详情区；详情空态给引导文案 | Provider、Judge、Workflow、实验 |
| C 全宽表格页 | 面板头内联过滤（`.inline-field`）+ 通栏表格 + 底部分页与批量操作 | 运行总览、题目清单、评分历史 |
| D 指标 + 钻取 | 指标卡行（5 列，见 5.11）→ 逐题信号格 / 表格 → `.drill-detail` 下钻 | 结果、对比、Terminal-Bench |

跨模式通则：主动作每屏一个（primary），次操作 secondary/ghost，破坏性操作永远 danger-ghost 行内两步确认；过滤行永远在表格上方同一面板头内。

### 3.5 响应式

| 断点 | 规则 |
|---|---|
| ≤1100px | 指标卡 5→3 列；双栏（时间线+终端等）降为单列；`wide/narrow` 降为单列 |
| ≤760px | 侧栏改为顶部横向滚动导航条（隐藏品牌与分区标签，导航项单行）；面板全部通栏；表格自身横向滚动，禁止页面级横向溢出；Dialog 占满视口宽；`form-panel`/`list-panel` 通栏 |

## 4. 状态语义（唯一映射来源：`src/components/statusMeta.ts`）

全站运行 / 步骤 / 资源 / 实验 / 比较 / 基线 / 门禁 / 规则状态收敛为 5 种语气，
**全站（徽章、LED、时间线、过滤下拉、信号格、通知条）只从这一张表取值**：

| 语气 | 状态（现行登记） |
|---|---|
| neutral | queued、cancelled、pending、skipped、not_run、deprecated、not_applicable、skipped_diagnostic |
| accent(info) | preparing、running、collecting、scoring、draft、allocating |
| success | completed、passed、calibrated、allocated、comparable、formal、pass、published |
| warning | unsupported、profile_stale、needs_review、unknown、experimental、partially_comparable、diagnostic、insufficient_evidence、execution_error、insufficient、unavailable |
| error | failed、cleanup_failed、not_comparable、quality_fail、safety_block |

规则：

1. 新增状态先在 `STATUS_META` 登记语气、中文标签与 `scope`（run / step / resource / experiment / comparison / baseline / gate / rule），徽章、过滤选项自动继承；运行总览的状态过滤只列 `scope: "run"`。
2. **禁止在任何组件里手写状态颜色**；非 Run 状态必须写 `scope`，避免污染运行总览过滤。
3. 进行中语气（accent 档）的徽章 LED 带呼吸脉冲；终态（success / warning / error / neutral 的已定态）LED 恒亮；`unknown` 视为需人工处理，不得当作成功展示。

## 5. 组件规范

| 组件 | 规范 |
|---|---|
| 状态徽章 | pill（9999px）+ 12px mono + 语气三件套；左内嵌 7px LED（右距 7px）；进行中语气 LED 呼吸，终态恒亮；无数据不渲染徽章而是显示「未知」文案 |
| 测试状态点 `.state-dot` | 8px 圆点，只映射本次会话内真实测试结果（pass=success / fail=error），无数据不渲染，禁止装饰性常亮 |
| 按钮 | primary：`--accent` 实底 `--on-accent` 字，hover `--accent-hover`，active `scale(.98)`；secondary：透明底 + `--border-strong` 描边 + 次色字，hover 提亮为 `--text-primary` + accent 描边；ghost：无框次色，hover `--bg-subtle`；danger-ghost：error 前景，hover error dim 底。主动作用 `button.primary` 显式声明，`type="submit"` 不隐式获得主按钮外观 |
| 破坏性确认 | 行内两步：首次点「删除」原地切换为「确认删除 / 取消」两个 link 按钮，确认项 error 前景；禁止弹窗与 `window.confirm` |
| 链接 | accent 前景、无下划线，hover 加下划线；表格内 Run ID 等单值标识用 link 样式 mono |
| 表单 | label 13px 次色在控件上方；控件 `--bg-inset` 底 + 1px `--border-strong` + 6px 圆角 + 12.5px 文字；focus-visible 双层焦点环；行内过滤用 `.control` + `.field-label` + `.inline-field`，禁止浏览器默认外观裸奔 |
| 开关 Switch | 32×18 pill；关闭 `--bg-inset` + `--border-strong`，开启 `--accent`；thumb 12px 离边 2px，只 transform 200ms；显式压掉全局 button hover 底（Radix Root 是 button） |
| 分段页签 Tabs | 见 3.2；用于套件五段与同一份草案的文本/Schema 双视图 |
| 表格 | 无外框，仅行间 1px `--border`；表头 10px mono uppercase 字距 .14em `--text-faint` 不换行 weight 500；行高 1.5，行 hover `--bg-subtle`；ID / 数字列 `.mono`，单值 ID 列 `.nowrap`；操作列 `td.row-actions` 右对齐 |
| 行内反馈行 | 即时操作结果（连通性测试等）用 `colSpan` 整行嵌在目标行下：`--bg-subtle` 底、12px、success/error 前景；进行中用 `--text-faint` 文案 |
| 指标卡 | `.metric-cards` 5 列等宽（≤1100px 变 3 列）；白卡 + 左侧 2px 语气条（默认 `--border-strong`）；26px mono 600 数值 + 10px uppercase 标签 + 11px mono 副注；**数值只染语气色（success/error/accent），标签永远是 faint**；指标语气跟随运行整体状态，仅 completed 用 success，失败/取消降为 neutral |
| 进度条 | 4px 胶囊，`--bg-inset` 轨道；进行中 accent 填充 + 流光，终态按语气纯色无流光；行内配 11px mono `x/N` 文本 |
| 逐题信号格 | `.progress-grid`（auto-fill 28px 格，gap 4）：pass/fail/warn 用语义三件套（dim 底 + 35% 描边 + 前景编号），pending 用 `--bg-inset` 底 + `--border` 描边 + faint 编号；禁止新色 |
| 时间线 | 条目 `--bg-subtle` 底 + 1px `--border` + 左侧 3px 语气条；seq 10px mono faint；事件时间戳 `MM-DD HH:mm:ss` mono faint 右对齐；无内层滚动，随容器统一滚动 |
| 钻取 `.drill-detail` | 3px 左语气条 + `--bg-subtle` 底 + 右侧 6px 圆角；字段前缀 `.field-label` |
| 通知条 `.notice` | 3px 左语气条 + 对应 dim 底 + 6px 右侧圆角 + 12.5px 次色；warning=需人工处理、info(accent)=只读说明、error=失败原因；服务端 4xx 照实显示错误码 + 消息 + 允许取值（`.error` + `.hint mono`），不吞结构 |
| 骨架屏 `.skeleton` | 高 12–14px 圆角 4px，渐变扫描 1.4s ease-in-out；列表/表格/详情加载态统一使用，替代裸空表 |
| 空状态 | 1.5px `--border-strong` 虚线框 + 居中 faint 文案 + 16px 图标 + 可选 primary 引导按钮 |
| 终端日志 `.terminal-log` | inset 深底（`--bg-inset`）+ 1px `--border` + 8px 圆角；窗口栏：10px mono uppercase + 左三点 + sha256/大小/编码/校验状态；正文 12px mono 行高 1.7 `pre-wrap`，内部滚动，6px 细滚动条；截断用 `.hint.fail`，`verified=false` 只显示不可读原因，`encoding=binary` 只说明需下载，不渲染伪造正文 |
| 滑出面板 Dialog | 右侧滑出 `min(720px, 100vw - 280px)`，左边框 1px，200ms 右滑入场；遮罩 `rgba(27,35,49,.32)`；标题左、X 关闭右；内容分区用 `.embed-title`，不套第二层卡片 |
| 下拉菜单 | 行操作收敛 `···` 触发；白底 1px `--border` 8px 圆角 + 极淡阴影 `0 4px 16px rgba(27,35,49,.08)`（**唯一允许阴影处**）；破坏性操作 error 前景 |
| 下拉选择 | Radix Select，trigger 与原生控件同规格（168px 起）；简单固定枚举可用同款原生 `<select class="control">` |
| 键值 `.kv` | `dl` 两列网格，dt faint、dd 12px mono |
| 本地文件选择 | 标准按钮 + 隐藏 `input[type=file]`，文件名 `.hint mono` 行内回显；多行输入 `<textarea class="mono">`；禁止原生 file 控件暴露在表单里 |
| 模型紧凑行 `.model-row` | mono ID + neutral pill 徽章（256000→256K）+ 12px mono 参数摘要；行尾 Switch + link 操作；行 hover `--bg-subtle`；测试结果 `.model-test-result` 行内反馈 |
| 步进与批次行 `.batch-row-head` | run ID link + mono 模型名 + 状态徽章 + `RunProgress` + 操作区；排队提示 `.hint` |
| 操作页卡片 `.operate-card` | 模式 A 用卡：白底 1px 8px 圆角，卡内 `.embed-title` 分节，卡内字段与表单同规格；高级设置 `.disclosure`（details 默认收起，summary 13px 600）；卡片顶对齐、高度随内容 |
| 密钥更新行 | 已有卡片内嵌 `.control` 密码输入 + 保存/取消，不另开卡片 |

## 6. 图标

- 库：`@phosphor-icons/react`，**Bold** 字重做操作图标、**Fill** 字重做状态点缀；导入一律带 `Icon` 后缀导出名
- 尺寸：侧栏/面板 16px，行内操作 14px，空状态 16–22px；与文字光学对齐（translateY 微调）
- 禁止：emoji 当图标、Lucide / Feather / Heroicons、混合字重、彩色填充图标（图标颜色跟随文字色或语气前景）

## 7. 组件库与技术约束

- 交互行为与无障碍原语用 **Radix UI**（无头库，视觉零主见）：常备 tabs / dialog / dropdown-menu / select / switch，tooltip / popover / accordion 按需加装，样式全部用本规范 token 手写
- **禁止引入带视觉主见的组件库**（Ant Design、MUI、shadcn/ui 等）与 Tailwind；样式只写在 `index.css`
- 路由 react-router-dom（纯导航行为，不违反上条）
- 资源与结果只从既有 API 客户端模块取值；服务端未注册端点（404/405/501）统一渲染「能力不可用」——入口保留、明确禁用并给出原因，不静默隐藏也不伪造结果

## 8. 禁止清单

装饰性用色、渐变填充（品牌方块除外）、任何投影（5.16 下拉除外）、卡片套卡片、
emoji、Lucide / Feather / Heroicons、Inter / Roboto / Open Sans、中文斜体、
`window.alert` / `window.confirm`、Lorem Ipsum 与假占位数据、自造状态颜色、
绕过 token 直接写十六进制色值、进行中超时无反馈的静默轮询。

## 9. 数据与文案诚实性规则（沿用，不随风格变更）

- 未知身份 / 成本 / reward 一律显示「未知」，缺工件显示「缺工件」，绝不填 0
- 付费提交（Judge 校准等）独占 `form-panel`：用途、模型、样本数、最大调用次数、已知/未知费用与预算齐全，先「读取预检（只读）」再显式确认才可提交
- GET 历史、切换 pass、刷新零调用零费用，用 info 通知条明示
- 迟到响应按请求序号丢弃，不得覆盖新的 Run / Case / pass / Skill / 版本选择；切换选择（含同 Trial 换工件）时按下钻身份重挂载视图
- 小计一律写明「小计」，成本卡标注统计范围（Run 范围 / Task 范围）
- UI 文案全中文；Provider、Case、Manifest、Run、Judge 等领域术语保留英文原文

## 10. 变更流程与完工门禁

1. 改设计 = 改 `DESIGN.md` = 改 `:root` token，三者同一个提交；规范文档与代码不脱节
2. 新增页面 / 新增界面区块：先按第 3 节确定页面模式（A/B/C/D），再按第 5 节取组件
3. 修改现有界面：先对照本文审计差异，增量改，不重写
4. 审美争议以「状态优先、密度优先、诚实优先」三条原则裁决
5. 完工必须跑：`pnpm --dir apps/web test` 与 `pnpm --dir apps/web build`（CI 同款门禁）
