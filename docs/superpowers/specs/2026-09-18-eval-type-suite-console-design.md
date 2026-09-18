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

- **操作页**：五张卡——数据集卡（当前 pinned 数据集：场景名、数据集名@版本、范围、题数、revision、synthetic 提示；存在多个数据集时卡内「运行数据集」下拉显式选择，默认题数最多即全量优先、同题数冒烟在前）+ 题目卡（本次运行跑哪些题：全部 / 随机 N 题（题数 + 可复现种子，可重新生成）/ 指定题目（读「题目」页勾选，数据集不匹配时提示并禁用发起））+ 模型多选卡（勾选支持推理的模型后就地出现该模型的思考强度下拉，默认档案默认等级，并提示 1024 输出预算可能被思考耗尽）+ 参数卡（题数为本次真实选中数只读展示——后端 benchmark case 选择由 case_selection 固定；输出上限与零重试同为 preset 固定值只读展示）+ 下载并导入卡（默认单按钮「下载最新全量数据集」＝解析官方最新 commit + 整个 test split + 自动版本号；`.disclosure` 折叠的「高级设置」里才是题目范围 / 指定 commit / 数据集名 / 版本 / License，按钮文案随 commit 是否填写变化）。发起按钮明确提示「真实调用 × 每模型题数（本次真实题数）」。可选题数上限（limit 子集）不在本期范围。
- **题目页**（`/{suite}/cases`，操作页可达）：数据集下拉 + 关键词/case id 搜索 + 分页表格（勾选列、mono case id、题面、mono 期望答案）+ 全选本页 / 清空 / 粘贴 case id（精确校验数据集范围）+「用所选 N 题发起跑测」（选择经 sessionStorage 传回操作页）。数据集只读，运行级子集与随机种子写进运行快照。
- **过程页（批次）**：每个 run 一行（ID、模型、状态徽章、x/N 进度条、取消）；展开单 run 显示逐题网格（绿=答对、红=答错、灰=未跑，悬停显示题目与判定）+ SSE 实时事件流；全量运行的格子数与 N 一致。
- **结果页**：accuracy / tokens（输入+输出）/ 成本（含 price_table 版本）/ 口径（selected_cases：选中·应答·正确·未尝试）四指标卡 + 逐题钻取表；答错行展开显示题目、模型完整输出、期望答案、失败原因（如输出截断达到 token 上限）。分母取运行快照的 selected_count（冒烟 20 / 全量为源文件题数），不假设全局常量。
- **对比页**：模型列 × 指标行（accuracy、tokens、成本、耗时）+ 答错题重合分析 + 逐题矩阵下钻（任一题点开看各模型输出与期望并排）。

### 6.2 Direct LLM（通用直连评测）

实施后已升级为「数据集驱动」的完整纵向切片，本节按最终形态描述（原「手填 case 列表」形态作废）：

- **操作页**：六卡——数据集卡（当前 pinned 数据集：场景名、数据集名@版本、题数、数据集级评分器、来源、prompt/scorer 版本）+ 题目卡（全部 / 随机 N 题 + 可复现种子 / 指定题目，与 GSM8K 同构）+ 模型多选卡（勾选支持推理的模型后就地出现该模型的思考强度下拉）+ 跑测参数卡（temperature、max_output_tokens 可覆盖数据集预设 1024 且不得超模型上限；题数与重试为只读）+ 内置样例卡（仓库自带样例一键导入）+ 导入本地 JSONL 卡（按钮触发隐藏 file input 或直接粘贴正文，高级设置里是数据集名 / 版本 / 默认评分器 / License / 来源标记）。
- **题目页**（`/direct-llm/cases`）：数据集下拉 + 关键词/case id 搜索 + 分页表格（勾选列、mono case id、题面、mono 期望答案或「（无判定）」、生效评分器）+ 全选本页 / 清空 / 粘贴 case id +「用所选 N 题发起评测」。
- **过程页（批次）**：与 GSM8K 同构（批次行 + 逐题格子 + 运行时间线）；格子把 `no_expectation` 也视作「未判定」的灰格。
- **结果页**：通过率（分母＝判定题数）/ 判定题数（选中·无判定）/ tokens / 成本（含 price_table 版本）/ 模型五张指标卡 + 逐题钻取表；词汇为 通过 / 不通过 / 无判定 / 调用失败 / 未尝试。
- **对比页**：模型列 × 指标行（通过率、判定题数、tokens、成本）+ 共同不通过题 + 逐题矩阵下钻（各模型输出与期望、评分器并排）。

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

## 15. Direct LLM 接入记录（2026-09-20，实施后）

第 14 节第 1 条（Direct LLM 场景固定 `direct-llm@1`）与第 6.2 节的原形态被本次接入取代。原因：原形态只把 `case_ids` 发给 `POST /api/v1/runs`，而 `manifest.cases`（题面→prompt 的映射）没有任何来源，`CaseDrivenProvider.invoke` 必然 `KeyError`，即该套件此前无法真正跑通。

本次按「参考 GSM8K」补齐完整纵向切片：

| 层 | 产物 |
| --- | --- |
| 契约 | `motte_contracts/direct_llm.py`（JSONL 导入、`exact`/`contains`/`regex` 评分器、`eval` 顶层键做套件判定）；`motte_contracts/selection.py`（运行级题目选择，与 GSM8K 共用）；`motte_contracts/suites.py`（套件分发单一来源） |
| 评分 | `motte_eval/direct_llm.py`；`exact` 两侧 strip 后全等、`contains` 子串、`regex` 用 `re.search`；**无 `expected` 的题记为 `no_expectation`，不进 accuracy 分母**（分母＝判定题数 judged） |
| SDK | `motte_sdk/direct_llm.py`（导入落库、内置样例注册表、创建期展开、评分）；`motte_sdk/suites.py`（manifest 展开 / 评分 / 聚合分发） |
| 数据 | `datasets/direct-llm/*.jsonl` 三份内置样例（exact 8 题 / contains 8 题 / regex 7 题）+ 该目录 README |
| API | `GET /benchmarks/direct-llm`、`GET .../builtins`、`POST .../import`、`GET .../cases`、`POST .../runs` |
| CLI | `motte direct-llm builtins / list / import (--builtin|--file) / run` |
| Web | 操作 / 题目 / 过程 / 结果 / 对比五页（第 6.2 节） |

范围裁剪记录（本次不做）：

1. Web 端不做 JS 侧字符级 diff 高亮（与第 14 节第 3 条一致）。
2. case_id 由导入方自由指定，题目页在分页下无法精确校验粘贴的 id，未知 id 交由服务端在发起时以 422 拒绝。
3. 导出/导入不引入 multipart：本地文件由浏览器读成文本后放进 JSON body（`content` 字段）。

套件判别：GSM8K 与 Direct LLM 的运行都带 `manifest.benchmark_provenance`，因此新增 `provenance.suite`（`gsm8k` / `direct-llm`）作为唯一判别字段；旧运行缺该字段时按 `gsm8k` 兼容（`motte_contracts.suites.suite_of_run` 与 `apps/web/src/evalTypes/registry.ts` 同一规则）。
