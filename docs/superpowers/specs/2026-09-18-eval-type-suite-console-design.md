# 评测类型专区控制台设计

日期：2026-09-18  
状态：设计已确认（待实施）  
范围：`apps/web` 前端重构为主、API 最小增量；后端评测协议与执行层不动

## 1. 背景与问题

当前 Web 控制台「运行评测集」存在两类问题：

1. **配置与发起脱节**：Provider 与模型资源页已支持完整的连接管理、凭据、连通性测试，但发起评测时（运行页手写 Manifest JSON、测试集页手填模型档案 ID）无法选择已配置资源。后端 `motte_sdk/resolve.py` 早已支持 `manifest.model`（模型档案 ID）与 `manifest.provider`（连接名字符串）引用并在创建期展开为快照，前端未暴露。
2. **单一界面硬适配**：运行页与测试集页是两个割裂的通用表单 + 通用列表，GSM8K 冒烟跑测作为「测试集」页特例存在；结果展示只有「case + 通过与否」两列，无进度聚合、无逐题钻取、无成本展示。

## 2. 已确认决策

| 决策点 | 结论 |
| --- | --- |
| 界面组织 | 每种评测类型注册专属的「操作页 / 运行过程页 / 结果页」三套界面，不用统一界面硬适配 |
| 页面形态 | 三页分离、自动流转；三个 URL 独立可分享、可回溯 |
| 导航 | 类型即一级导航；侧边栏分「评测类型」「通用」两组 |
| 第一期范围 | GSM8K / Direct LLM / Replay 三套件完整版 + 多模型对比 + 运行总览 |
| 后端 | 改动最小化：多模型 = 前端循环创建多个 run；批次为前端 URL 概念，不引入后端批次 |

## 3. 信息架构与路由

应用从 Radix Tabs 单页结构迁移到 react-router 多页路由（三页分离与 URL 分享的前提）。

```
/                        → 重定向 /gsm8k
/gsm8k                   → GSM8K 操作页（数据集导入迁入此处）
/gsm8k/monitor?runs=a,b  → 批次过程页（1..N 个 run 并行进度）
/gsm8k/runs/:id/result   → 单 run 结果页
/gsm8k/compare?runs=a,b  → 多模型对比页
/direct-llm/…            → 同构三页
/replay/…                → 同构三页
/runs                    → 运行总览（跨类型列表 + 类型/状态筛选）
/providers               → Provider 与模型（保持现状）
/harnesses               → Agent / Harness（保持现状）
```

自动流转：操作页发起（勾选 N 个模型 → 创建 N 个 run）→ 跳转批次过程页（SSE 驱动进度）→ 全部终态后出现「查看对比结果」入口 → 对比页。

侧边栏视觉沿用 DESIGN.md 应用骨架条款（216px 白卡、图标 + 文字、active 态 `--tone-neutral-bg`），由路由 Link 驱动，不改变观感只改变机制。

## 4. 类型套件注册表

`apps/web/src/evalTypes/registry.ts`：

```ts
interface EvalTypeSuite {
  id: string;                 // "gsm8k" | "direct-llm" | "replay"
  label: string;              // 侧边栏与页面标题
  icon: PhosphorIcon;
  match(run: RunRecord): boolean;   // 按 scenario_version 前缀 / manifest.benchmark 判定归属
  routes: { operate: string; monitor: (ids: string[]) => string; result: (id: string) => string; compare: (ids: string[]) => string };
  pages: { Operate: ComponentType; Monitor: ComponentType; Result: ComponentType };
}
```

规则：

- 运行总览与批次/结果页按 `match` 将 run 路由到所属套件；未匹配的 run 落**通用兜底三页**（现有 RunDetail 的时间线 + 评分表改造为路由页）。
- 新增一种评测类型 = 注册一个套件对象 + 侧边栏一个入口，不修改其它套件。
- 批次仅是 URL query 携带的 run id 集合，无后端概念、无持久化。

## 5. 公共底座（三套件共用）

| 组件 / hook | 职责 |
| --- | --- |
| `ModelPicker` | 多选；数据源 `GET /api/v1/models`；按 Provider 分组、上下文徽章（256K）、停用项置灰不可选 |
| `useRunEvents` | SSE hook，封装现有 `/runs/{id}/events` 订阅与 Last-Event-ID 断线续传；返回事件流与聚合进度 |
| statusMeta 徽章 / 时间线 | 沿用现有唯一状态语义来源 |
| 指标卡 / 钻取行骨架 | 结果页与对比页共用的展示原语 |
| 报告导出 | 沿用 `GET /runs/{id}/report` |

样式全部引用现有 `:root` token，无新颜色、新字号。

## 6. 套件页面规格

### 6.1 GSM8K（数学能力评测）

- **操作页**：数据集卡（当前 pinned 数据集、题数、revision、换版本/导入入口）+ 模型多选卡 + 参数卡（题数为 preset 固定值只读展示——后端 benchmark case 选择固定、导入固定取前 20 题；输出上限与零重试同为 preset 固定值只读展示）。发起按钮明确提示「真实调用 × 每模型题数」。可选题数上限（limit 子集）不在本期范围。
- **过程页（批次）**：每个 run 一行（ID、模型、状态徽章、x/N 进度条、取消）；展开单 run 显示 20 格逐题网格（绿=答对、红=答错、灰=未跑，悬停显示题目与判定）+ SSE 实时事件流。
- **结果页**：accuracy / tokens（输入+输出）/ 成本（含 price_table 版本）/ 口径（selected_cases：选中·应答·正确·未尝试）四指标卡 + 逐题钻取表；答错行展开显示题目、模型完整输出、期望答案、失败原因（如输出截断达到 token 上限）。
- **对比页**：模型列 × 指标行（accuracy、tokens、成本、耗时）+ 答错题重合分析 + 逐题矩阵下钻（任一题点开看各模型输出与期望并排）。

### 6.2 Direct LLM（通用直连评测）

- **操作页**：场景选择（`direct-llm@1` 及变体）+ 模型多选 + 参数（temperature / max_output_tokens ≤ 模型上限 / reasoning_level，预填档案默认值）。
- **过程页**：case 流式卡片，每 case 一张（输入 → 输出实时填充），SSE 驱动。
- **结果页**：通过率 + 期望对比表（实际输出 vs 期望，diff 高亮）。

### 6.3 Replay（回放一致性）

- **操作页**：场景选择（`replay@1` / `json_extract@1`）+ fixture 选择或内联 provider 专家模式（保留手写 Manifest JSON 能力，作为本套件的正式形态而非隐藏入口）。
- **过程页**：回放时间线（沿用现有 RunTimeline）。
- **结果页**：一致性对比（实际 vs fixture 期望，逐 case diff 高亮）。

## 7. 多模型与对比

发起 = 前端循环 `POST`（GSM8K 走 `/api/v1/benchmarks/gsm8k/runs`，其余走 `/api/v1/runs`，请求体 `manifest: { model: <id>, … }`）。个别创建失败不阻塞整批：成功者进入批次过程页，失败者就地报错。对比页数据 = 对每个 run 调 `getRun` + `getReport` 前端聚合。

## 8. API 增量（最小）

1. `GET /api/v1/runs` 列表项补模型摘要字段（取自展开快照 `manifest.provider.model` 或原 `manifest.model`），供运行总览与对比页直接渲染；前端不解析完整 manifest。
2. 运行总览的类型 / 状态筛选由前端完成，不加查询参数。
3. 其余端点、CLI、Worker、存储协议不动；旧请求体形状保持兼容。

## 9. 错误处理

- 创建失败（模型停用、`MODEL_NOT_FOUND`、422 参数校验）：表单内联错误，保留输入。
- 批次中个别 run `failed`：不阻塞其它 run；失败 run 显示已脱敏错误信封 + 重试入口（沿用 `POST /runs/{id}/retry` 语义：生成子 run）。
- SSE 断线：自动重连（现有 Last-Event-ID 机制），重连期间进度条显示最近已知状态。
- `queued` 停滞：过程页提示「等待 Worker 领取」，并给出 Worker 启动命令提示。
- 兜底：未匹配类型的 run 在运行总览可正常查看（通用三页），不因注册表缺失而 404。

## 10. 测试与验收门

- vitest（`apps/web`）：注册表 `match` 与路由生成；ModelPicker 分组 / 停用态 / 多选；批次进度聚合（模拟 SSE 事件序列）；钻取行展开渲染；对比页指标计算与答错重合分析；自动流转（发起 → monitor → 终态 → result 入口）。
- API 若实现第 8 节增量：pytest 覆盖列表新字段。
- 门禁：`pnpm --dir apps/web test` 与 `pnpm --dir apps/web build` 全绿；`make check` 通过。

## 11. DESIGN.md 同步变更（与代码同一提交）

1. 应用骨架条款：导航从「Radix Tabs 垂直导航」改为「react-router 侧边栏（视觉规格不变）」；补「评测类型 / 通用」分组规范。
2. 新增组件规范：步进指示（操作 → 过程 → 结果）、指标卡、逐题格子网格（语义色前景/背景既有 token）、钻取行、对比表。
3. 不新增色值与字号；如需新间距/圆角先登记 token。

## 12. 迁移与兼容

- 旧 `RunsPage` 创建表单删除；列表能力并入运行总览。
- 旧 `BenchmarksPage` 整体被 GSM8K 套件替代，数据集下载导入迁入 GSM8K 操作页。
- `App.tsx` Tabs 骨架改为路由 + 侧边栏；`routes/index.ts` 语义由路由表接管。
- 现有测试 `components.test.tsx` / `client.test.ts` 随迁移更新，不留双入口。

## 13. 非目标（本期不做）

- Worker 并发执行、case 级重试（串行执行与整 run 重试保持现状）。
- 新数据集类型接入（GSM8K 之外的基准导入流程）。
- 后端批次 / 对照组概念、统计显著性、跨场景聚合。
- 运行总览分页与后端筛选。

## 14. 范围裁剪记录（2026-09-18 实施后）

以下第一期裁剪经评审接受，作为已确认偏差记录在案（恢复时按本节逐项补齐）：

1. **Direct LLM 场景选择固定 `direct-llm@1`**：当前唯一内置场景，操作页场景输入为只读；变体选择（`replay@1` / `json_extract@1` 之类的多场景下拉）待后续场景库存量出现后再做。
2. **对比页无耗时行**：后端 run / report 无耗时数据源（无 wall-clock 汇总字段），前端不臆造；待后端补充耗时口径后再加行。
3. **实际/期望 diff 高亮留后续**：Direct LLM 与 Replay 结果页暂以纯文本并排展示实际与期望，不做字符级 diff 高亮。
4. **操作页参数不预填档案默认值**：Direct LLM 的 temperature / max_output_tokens / reasoning_level 均从空值起步，不读取模型档案默认值预填。

两个低成本补充已随收尾落地，弥补部分裁剪影响：

- GSM8K 结果页成本卡附带 price_table 版本（`成本 · pt v…`，取 `report.cost.price_table_versions[0]`）。
- GSM8K 过程页行内展开在逐题网格下方附带运行时间线（复用 `RunTimeline` + `useRunEvents`）。
