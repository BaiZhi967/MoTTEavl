# MoTTEavl Web 控制台设计规范（DESIGN.md）

本文件是 `apps/web` 所有 UI 工作的唯一设计依据。任何 UI 改动前先读本文。
修改任何设计决策时，必须同步修改 `src/index.css` 中的对应 token，并放在同一个提交里。
token 与本文冲突时，以本文为准并立即修正 token。

## 0. 设计语言与基调

一句话：**安静、密集、精确的文档式工作台**（Linear / Notion 一系的 utilitarian minimalism）。

- 审美锚点：项目内 skill `minimalist-ui`（`.agents/skills/minimalist-ui/SKILL.md`）
- 生成新界面时的拨盘（skill `design-taste-frontend`）：`DESIGN_VARIANCE 3 / MOTION_INTENSITY 2 / VISUAL_DENSITY 7`
- 气质关键词：暖灰单色画布、扁平、1px 细边框、留白来自间距而非阴影、颜色只用于表达语义

## 1. 设计原则

1. **颜色是稀缺资源。** 只有语义（运行状态、通过/失败、警告）才允许用色，禁止装饰性用色。
2. **扁平。** 无渐变、无重阴影。阴影只允许一种：hover 时 `0 2px 8px rgba(0,0,0,0.04)`。
3. **卡片只做分组。** 白底 + `1px solid var(--border)` + 8px 圆角，禁止卡片套卡片。
4. **留白来自间距**（8 的倍数刻度），不来自描边层数和阴影。
5. **动效只到 hover 级**：200ms，只动 `transform` / `opacity` / 背景色。
6. **数据密度优先。** 表格 13px、紧凑行高，所有数字等宽（tabular-nums），ID / seq / JSON 一律 mono 字体。
7. **中文为第一语言。** UI 文案全部中文；Provider、Case、Manifest 等领域术语保留英文原文。

## 2. 设计 token

唯一取值来源是 `src/index.css` 的 `:root`。写样式时只能引用 `var(--…)`；
需要新值时，先在本文登记，再加进 `:root`，然后才能使用。

### 2.1 色彩：暖灰单色一家

禁止混入冷灰或 Tailwind 默认灰阶（`#f5f6f8`、`#1f2937`、`#6b7280` 系全部弃用）。

| Token | 值 | 用途 |
|---|---|---|
| `--bg-canvas` | `#F7F6F3` | 页面画布（暖骨白） |
| `--bg-surface` | `#FFFFFF` | 卡片 / 面板 / 顶栏 |
| `--bg-subtle` | `#FAFAF9` | 次级面：时间线条目、行 hover、tab hover |
| `--border` | `#EAEAEA` | 一切边框与分隔线，永远 1px |
| `--text-primary` | `#2F3437` | 正文与标题（禁止纯黑 `#000`） |
| `--text-secondary` | `#787774` | 标签、说明文字、表头 |
| `--text-faint` | `#A8A7A3` | 序号、占位、空状态 |
| `--ink` | `#111111` | 主按钮实底、active 强调 |
| `--ink-hover` | `#333333` | 主按钮 hover |

### 2.2 语义色（全站唯一的彩色，低饱和粉彩）

| 语气 | 背景 token | 前景 token | 值 |
|---|---|---|---|
| success | `--tone-success-bg` | `--tone-success-fg` | `#EDF3EC` / `#346538` |
| error | `--tone-error-bg` | `--tone-error-fg` | `#FDEBEC` / `#9F2F2D` |
| info | `--tone-info-bg` | `--tone-info-fg` | `#E1F3FE` / `#1F6C9F` |
| warning | `--tone-warning-bg` | `--tone-warning-fg` | `#FBF3DB` / `#956400` |
| neutral | `--tone-neutral-bg` | `--tone-neutral-fg` | `#EFEFED` / `#5F5E5B` |

### 2.3 字体

| Token | 值 | 说明 |
|---|---|---|
| `--font-sans` | `"PingFang SC", "Microsoft YaHei", system-ui, sans-serif` | 中文优先系统栈；未来可自托管 Geist Sans，禁止引入 Inter / Roboto / Open Sans |
| `--font-mono` | `ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace` | run ID、seq、版本号、JSON、数字 |

字号阶梯：26 指标卡数值 / 18 顶栏标题 / 15 面板标题（weight 600）/ 14 正文 / 13 表格与表单 / 12 徽章。
行高：正文 1.6，数据行 1.5。全局开启 `font-variant-numeric: tabular-nums`。

### 2.4 间距、圆角、边框

- 间距刻度：`4 / 8 / 12 / 16 / 24 / 32`（8 的倍数，个别 4 的半步）
- 圆角：`6px` 控件（按钮、输入框）/ `8px` 卡片 / `9999px` 徽章 pill
- 边框：一律 `1px solid var(--border)`，不存在第二种边框

### 2.5 模型配置分组 token

| Token | 值 | 用途 |
|---|---|---|
| `--space-none` | `0` | 内嵌分组清除原生边距 |
| `--space-sm` | `8px` | 选项行间距 |
| `--space-md` | `12px` | 分组内留白 |
| `--size-full` | `100%` | 分组占满表单宽度 |
| `--border-width` | `1px` | 分组分隔线 |
| `--font-label` | `13px` | 分组标题字号 |
| `--weight-semibold` | `600` | 分组标题字重 |

模型配置沿用 Provider 内联表单，不另开页面：基础限制、输入类型、模型能力、推理等级用无外框 fieldset 分组；选项描述在左、Radix Switch 在右。文本输入类型固定开启。高级采样参数用 details 默认折叠；CEL 多行编辑器使用 mono 字体，附接口示例、变量和合并语义说明。保存期间禁用提交，错误就地显示并保留输入。

## 3. 状态语义（唯一映射来源：`src/components/statusMeta.ts`）

10 种运行状态收敛为 5 种语气，**全站（徽章、时间线、过滤下拉）只从这一张表取值**：

| 状态 | 语气 |
|---|---|
| `queued`、`cancelled` | neutral（取消是用户行为，不是故障） |
| `preparing`、`running`、`collecting`、`scoring` | info（进行中） |
| `completed` | success |
| `failed` | error |
| `unsupported`、`profile_stale`、`needs_review` | warning（环境 / 配置或调用结果不确定，需人工处理） |

规则：新增状态时先在 `statusMeta.ts` 的 `STATUS_META` 登记语气与中文标签，
徽章、时间线、过滤选项自动继承。**禁止在任何组件里手写状态颜色。**

## 4. 组件规范

| 组件 | 规范 |
|---|---|
| 应用骨架 | `.app-shell` 使用 fixed 定位与 `inset: var(--space-none)` 固定在视口内，外框不滚动；左侧悬浮导航栏（216px 白卡、1px 边框、8px 圆角、品牌区 + 路由侧边栏，图标 + 文字，导航项由 react-router `NavLink` 驱动、active 态 `aria-current="page"` 同 Radix active 观感），分组标签「评测类型」「通用」用 `.nav-group-label`（11px `--text-faint`）；右侧全屏工作台独立滚动（`.page` 通栏铺满、padding 16/32） |
| 面板宽度分级 | `.page` 内 `.panel` 默认占满剩余宽度；辅助表单列用 `.panel.form-panel`（340px 固定窄列，窄屏自动换行为单列）；导航型清单列用 `.panel.list-panel`（240px 固定窄列）；不对称双表布局用 `.panel.wide-panel`（flex 2）/ `.panel.narrow-panel`（flex 1）——禁止两个内容面板 50/50 平分 |
| Provider 清单 | 列表头 `.panel-head`（15px 标题 + `icon-btn` 刷新 + link「添加」）；列表项 `.provider-item`（Phosphor Plug 16px + mono 名称 + 会话内测试状态点），选中态同导航 active（`--tone-neutral-bg` + 500 字重），禁用项名称弱化为 `--text-faint` |
| 测试状态点 | `.state-dot`（8px 圆点）：只映射本次会话内真实测试结果（pass 用 success 前景色 / fail 用 error 前景色），无数据不渲染；禁止装饰性常亮 |
| Provider 详情头 | `.detail-head`：mono 名称 + kind 徽章（neutral pill）+ 启用 Switch（随行 `.field-label`，包在 `.inline-field` 里且清零其下边距，与操作 link 同一中心线）+ link 操作组（更新密钥 / 两步确认删除） |
| 连接分节 | `.connection-form`：`.embed-title`「连接」+ `.connection-grid` 双列（协议 select + Base URL 输入，窄屏换行）；凭据 profile / 密钥 hint 用 `.kv` 只读；表单脏状态才显示「保存连接」 |
| 模型紧凑行 | `.model-row`：mono ID + neutral pill 徽章（上下文格式化 256000→256K、1000000→1M；能力如「工具」）+ 参数摘要 12px mono 次色；行尾启用 Switch + link 操作（测试/编辑/删除）；行 hover `--bg-subtle`；测试结果用 `.model-test-result` 行内反馈（`--bg-subtle` 底、12px、pass/fail 前景色） |
| 导航项 | 图标（Phosphor Bold 16px）+ 13px 文字，静默态次色，hover `--bg-subtle`，active `--tone-neutral-bg` + 主文字色 + 500 字重 |
| 表格 | 无外框，仅行间 1px 分隔线；表头 12-13px 次色 weight 500、表头不换行；行 hover `--bg-subtle`；行高 1.5；ID / 数字列加 `.mono`，单值 ID 列加 `.nowrap` 防连字符折行；操作列统一 `td.row-actions` 右对齐（不得使用 `.actions`，该类是 flex 工具类） |
| 行内反馈行 | 即时操作结果（如连通性测试）用 `colSpan` 整行嵌在目标行下方：`--bg-subtle` 底、12px、成功 `pass` / 失败 `fail` 前景色；进行中用 `--text-faint` 文案 |
| 状态徽章 | pill（9999px）、12px、语义粉彩底 + 对应前景色 |
| 时间线 | 左侧 3px 语气色条 + `--bg-subtle` 底；seq 用 `--text-faint` mono，事件时间戳列（recorded_at，`MM-DD HH:mm:ss` mono `--text-faint`）；无内层滚动，由所在容器统一滚动 |
| 表单 | label 13px 次色在控件上方；控件白底 1px 边框 6px 圆角；focus-visible 2px info 色描边（`.operate-card` 内的字段同规格，见「卡片内字段」） |
| 行内过滤控件 | 表单体系之外的控件用 `.control`（与表单控件同款）+ `.field-label` + `.inline-field`（label 与控件并排），禁止浏览器默认外观裸奔 |
| 嵌入分节 | 已有卡片壳的容器（滑出面板等）内部分节用 `embedded` 组件 + `.embed-title`（14px 标题、无壳），禁止卡片套卡片 |
| 按钮 | primary：`--ink` 实底白字，hover `--ink-hover`，active `scale(0.98)`；主动作用 `button.primary` 类显式声明，`type="submit"` 不再隐式获得主按钮外观；secondary：白底 1px 边框；link：文字按钮用 info 前景色 |
| 空状态 | 居中、`--text-faint`，文案「暂无 X」 |
| 错误提示 | 内联 alert：`--tone-error-bg` 底 + 前景色，禁止 `window.alert`；逐模型失败清单用 `.failure-list`（无原生列表样式） |
| 键值展示 | `<dl class="kv">` 两列网格，dt 次色 |
| 滑出面板（Dialog） | 富视图详情与创建表单（运行详情、创建 Provider）用右侧滑出：`min(720px, 100vw - 280px)` 宽、左边框 1px、200ms 右滑入场；遮罩 `rgba(17,17,17,0.32)`；标题左侧、X 关闭按钮右侧 |
| 下拉菜单（DropdownMenu） | 行操作收敛为 `···` 触发；白底 1px 边框 8px 圆角 + 极淡阴影 `0 4px 16px rgba(0,0,0,0.05)`；破坏性操作文字用 error 前景色 |
| 下拉选择（Select） | Radix Select，trigger 与原生输入控件同规格（168px 起、1px 边框、6px 圆角），选中项右侧 Check 指示；简单过滤场景可用同款样式的原生 `<select>`（如时间线事件过滤、表单内固定选项的协议类型） |
| 开关（Switch） | 32×18 pill（`padding: 0`，并显式压掉全局 `button` 的 hover 底色——Radix Switch.Root 本身就是 `<button>`），关闭态 `--tone-neutral-bg`，开启态 `--ink` 实色；thumb 12px 绝对定位、离边 2px，位移只用 `transform` 200ms，禁止把滑块写成 flex 流内元素（会被压扁并顶出胶囊） |
| 破坏性确认 | 行内两步确认：首次点「删除」原地切换为「确认删除 / 取消」两个 link 按钮，确认项用 error 前景色；禁止弹窗与 `window.confirm` |
| kind 徽章 | Provider 协议标识复用状态徽章 neutral pill（12px、mono），置于名称右侧；不是运行状态，不得手写新颜色 |
| 密钥更新行 | 已有卡片内嵌一行 `.control` 密码输入 + 保存/取消（`--bg-subtle` 底、1px 边框、6px 圆角），不另开卡片 |
| 类型操作页卡片 | `.operate-grid` / `.operate-card`：flex 换行布局、1px 边框 8px 圆角白卡（GSM8K 为数据集 / 题目 / 模型 / 参数 / 下载并导入五卡；Direct LLM 为数据集 / 题目 / 模型 / 跑测参数 / 内置样例 / 导入本地 JSONL 六卡）；卡内分节用 `.embed-title`，不套第二层卡片；卡内数据集选择与表单内的范围选择用原生 `<select class="control">`（与过滤控件同规格，选项为固定枚举）；卡片顶对齐、高度随内容，不强行等高 |
| 卡片内字段 | `.operate-card` 内的 `label` 与 `input/select/textarea` 与 `form` 内同规格（13px 次色 label、白底 1px 边框 6px 圆角控件），禁止浏览器默认外观裸奔 |
| 卡片内高级设置 | `.disclosure`（`<details>`）：默认收起，summary 13px 600 主文字色 + pointer；展开后才是次级表单字段，间距走 `--space-sm` |
| 本地文件选择 | 全部走「标准按钮 + 隐藏 `<input type="file" hidden>`」模式（按钮点击触发 `ref.click()`），文件名用 `.hint .mono` 行内回显；**禁止把原生 file 控件直接暴露在表单里**（原生外观无法与 token 体系对齐）。多行输入用 `<textarea class="mono">` |
| 判定词汇（结果页 / 对比页） | 每种套件的结果页各自声明 outcome 词汇与语气，唯一来源是对应页面文件里的 `OUTCOME_LABELS`：GSM8K 为 答对 / 答错 / 解析失败 / 调用失败 / 未尝试（`src/evalTypes/gsm8k/Gsm8kResult.tsx`）；Direct LLM 为 通过 / 不通过 / 无判定 / 调用失败 / 未尝试（`src/evalTypes/directllm/DirectLlmResult.tsx`）。语气只允许 success / error / neutral 三档，禁止自造色 |
| Terminal-Bench（Harbor）页面 | 操作页沿用 `.operate-grid` / `.operate-card`：任务筛选（多选清单）、Agent Profile 与重复次数（Agent Profile 卡内含 Agent 选择 oracle / claude-code、真实 Agent 的显式版本输入与模型必填规则、凭据引用行「名称 → 环境变量名」）、预算与限制（期限字段名就是提交给 API 的 `timeouts.*`，资源限制放 `.disclosure` 默认收起）、预检与创建四卡；凭据只有引用输入（`.control` 文本行 + `.link` 增删，无值字段、无密码框、无回显），本地只做「env: 前缀 / 名称非空 / 与 Agent 要求的变量名有交集」校验，预检与创建必须带同一组引用；请求被 4xx 拒绝时照实显示服务端错误码 + 消息 + 允许取值（`.error` + `.hint mono`，不吞掉结构）。监控页 `.tb-tree` 呈 Job（Run）→ Task → Trial 三层嵌套清单，`.tb-node` 为单行节点（刷新用 `icon-btn`），queued 且无 Trial 用 `.empty` 显式空态。结果页 6 张指标卡（通过率 / 有效 Trial / 有效 Task / 无效 Trial / 已知成本 / 每成功成本），其中成本卡消费**全 Run** `aggregate.cost`（不随所选 Task 变化，卡标签写明范围），Task 范围的已报道成本小计单独用 `.hint mono` 标注「Task 范围」；`per_success_usd` 为 null 时按 `per_success_usd_basis` 说明原因（无成功 Trial 才显示「不适用」），小计一律写明是小计。Trial 钻取用 `.drill-detail` + 通用表格，终端文本与工件内容用 `.terminal-log`（`--bg-subtle` 底、1px 边框、mono 12px、内部滚动、`white-space: pre-wrap`）；工件按引用请求内容（含斜杠的 artifact_id 逐段编码），终端/内容区固定展示 sha256 / 大小 / 编码 / 校验状态，截断用 `.hint.fail` 提示，`verified=false` 只显示不可读原因（`.error`），`encoding=binary` 只说明需下载原始工件，都不渲染伪造正文。未知身份 / 成本 / reward 一律显示「未知」，缺工件显示「缺工件」，绝不填 0；Trial 或 Task 切换（含同 Trial 内换工件）时按 run+Trial+工件身份重挂载下钻视图，晚到响应不得覆盖新选择 |
| 题目清单页 | 沿用通用表格规范（Case 用 mono、期望答案用 mono、行 hover `--bg-subtle`）；首列原生 checkbox 做多选，工具栏用 `.inline-field`（数据集下拉 / 搜索 / 全选本页 / 清空 / 已选题数），底部 `.actions` 放翻页与「用所选 N 题发起…」；Direct LLM 额外展示每题生效的评分器列 |
| 步进与批次行 | `.batch-row-head`：run ID link + mono 模型名 + 状态徽章 + `RunProgress`（x/N 进度条）+ 操作区；排队提示用 `.hint` |
| 逐题格子 | `.progress-grid`（`auto-fill` 28px 格）+ `.grid-cell-pass/fail/pending`：语义粉彩底 + 对应前景/描边（pass/fail 用语义 token，pending 用 neutral 底 + `--text-faint`），禁止新色 |
| 指标卡 | `.metric-cards` / `.metric-card`：flex 换行、内容居中、26px mono 数值、12px 次色标签；success / error 语气只染数值色；指标语气跟随运行整体状态，仅 completed 用 success，失败/取消等终态降为 neutral |
| 钻取行 | `.drill-detail`：3px 左语气条 + `--bg-subtle` 底、6px 圆角右侧；字段前缀用 `.field-label` |
| 对比页 | 模型列 × 指标行表格沿用通用表格规范；`.compare-case-list` 逐题下钻用 link 按钮 + `.drill-detail` |

## 5. 图标

- 库：`@phosphor-icons/react`，统一 **Bold** 字重做操作图标、**Fill** 字重做状态点缀
- 导入一律用带 `Icon` 后缀的导出名（如 `ActivityIcon`）；不带后缀的名字在新版 barrel 中不存在，会构建失败
- 尺寸 14-16px，与文字光学对齐
- 禁止：emoji 当图标、Lucide / Feather / Heroicons、混合字重

## 6. 组件库

- 交互行为与无障碍原语用 **Radix UI**（无头库，视觉零主见）。常备集已装：
  `@radix-ui/react-tabs`、`react-dialog`（滑出面板）、`react-dropdown-menu`（行操作菜单）、
  `react-select`（下拉选择）、`react-switch`（开关）；其余（tooltip、popover 等）按需加装，样式全部用本规范 token 手写
- **禁止引入带视觉主见的组件库**（Ant Design、MUI、shadcn/ui 等），它们的默认外观会架空本规范
- 路由用 react-router-dom（纯导航行为、无视觉输出），不违反「禁带视觉主见组件库」
- 不引入 Tailwind；样式只写在 `index.css`（token + 既有类名体系）

## 7. 禁止清单

渐变（尤其紫蓝 AI 渐变）、重阴影、卡片套卡片、大面积纯色背景、整页暗色段落、
emoji、em-dash（——用中文标点或重写句子）、Inter / Roboto / Open Sans、
Lucide / Feather / Heroicons 图标、`window.alert`、Lorem Ipsum 与假占位数据、
自造状态颜色、绕过 token 直接写十六进制色值。

## 8. 变更流程

1. 改设计 = 改 `DESIGN.md` = 改 `:root` token，三者同一个提交。
2. 新增页面 / 新增界面区块：走 `design-taste-frontend` 流程（读需求 → 定方向 → 拨盘已在 0 节锁定）。
3. 修改现有界面：走 `redesign-existing-projects` 流程（先审计 → 列问题 → 增量改，不重写）。
4. 审美争议以 `minimalist-ui` 协议为准裁决。
5. 完成后必须跑：`pnpm --dir apps/web test` 与 `pnpm --dir apps/web build`（CI 同款门禁）。
