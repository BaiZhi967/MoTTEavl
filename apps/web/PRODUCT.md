# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

React 19 + Vite 8 + TypeScript + react-router-dom 7；交互原语 Radix UI；**Tailwind CSS 4** 做布局与工具层；
**Semi Design**（`@douyinfe/semi-ui`）做表单控件与外壳（深路径引入）；视觉世界是自研的**深色信息板**。

样式分三层，顺序即优先级：`tailwind.css`（preflight + `@theme` 令牌）→ `ui.css`（控制台类层，板面令牌）
→ `theme.css`（令牌别名 + 字体 + Semi 覆盖 + 浏览器表面）；板面基础件在 `board/board.css`。

**已完成的全量替换**：旧的 `index.css`（约 2100 行浅色纸面）已删除并改写成 `ui.css`；
迁移桥（旧 token 名 + 未迁移页面的组件层补丁）已拆除；`DESIGN.md` §7 与 `AGENTS.md` 里
「禁止 Tailwind 与带视觉主见的组件库」的禁令已删除，改为「允许 Tailwind + 指定组件库 Semi Design」。

## Users

**单人评测工程师**（本项目的日常使用者），一天内反复使用同一套控制台。使用场景固定：
宽屏桌面（16:9）、长时间驻留、经常同时有多个 Run 在跑；夜间批量发起，次日集中排查失败。
不是多人协作台，不是给外部演示用的门面——它是这个人的工作台。

## Product Purpose

把「某个模型行不行」这个说法，变成**可复现、可追溯的测量结果**。

控制台承载四件日常事，缺一不可：

1. **发起跑测**：选数据集 / 题目范围 / 模型 / 参数，确认后果后创建 Run；
2. **盯运行**：多个 Run 并行时的进度、阶段、异常，出问题第一时间看见；
3. **翻历史与定位失败**：从 Run 到 Case 到工件，回答「为什么失败、哪个模型差、差在哪一题」；
4. **配置与结论**：Provider / 模型 / Harness / Workflow / Skill / Judge 的维护，以及实验、比较、基线、门禁的结论。

成功的定义：一次判定能在几秒内被解释清楚，且每个数字都能追到它的证据。

## Positioning

**证据链测量**。每一个分数都可以回溯到：固定的数据集 revision、scenario 版本、scoring pass、以及工件哈希。
相邻工具能显示一个分数；MoTTEavl 能证明这个分数。这个机制是产品的立足点，也是界面必须始终可读的东西。

## Operating Context

- 夜间批量跑测，白天集中排查；同一时间常有多个 Run 在跑；
- 中文界面，领域术语保留英文原文（Run / Case / Provider / Judge / pass / scenario / revision / manifest）；
- 与 CLI / API 对等：控制台能做的事，命令行也能做（如 `agent-tasks import`）；
- 工件真实存在于磁盘（`var/datasets/…`），终端日志、sha256、编码、校验状态都是真数据；
- 服务端未实现的端点（404/405/501）渲染为「能力不可用」，入口保留、明确禁用并给出原因。

## Capabilities and Constraints

- **评测套件**：Agent 文件任务、CMMLU、Terminal-Bench（Harbor）、C-Eval、GSM8K、Direct LLM、Replay、外部 Runtime，
  每个套件五段：操作 / 题目 / 监控 / 结果 / 对比；
- **资源**：Provider 与模型、Agent / Harness、场景 Workflow、Skill 校验、Judge 校准；
- **实验体系**：实验、比较、基线、门禁；
- **硬性诚实规则（不可协商）**：未知就写「未知」，缺工件写「缺工件」，绝不填 0、不伪造、不静默隐藏；
  读不到的数据不能渲染成 0；服务端 4xx 原样显示错误码与允许取值；
- **状态语义唯一来源**：成功 / 失败 / 警告 / 进行中 / 中性 五语气，全站（徽章、LED、进度、时间线、过滤）只从一处映射取值；
- 未决事实：无。

## Brand Commitments

- 产品名 **MoTTEavl**，副题「评测控制台」；
- 界面文案全中文，领域术语保留英文；
- 诚实规则与证据可查性属于品牌承诺，不随视觉风格变化；
- 状态五语气继续作为唯一的信号系统，不得被装饰性用色稀释。

## Evidence on Hand

- 本机已有真实数据：Run 记录（gsm8k / direct-llm / agent-tasks 多批，含完成、失败、取消三种终态）、
  真实 Provider（`6a` / `openai_compatible`，模型 `deepseek-v4.1-flash` 与 `gpt-5.6-luna`）、
  Terminal-Bench 的「数据集未准备」真实空态；
- 现有实现代码即现有界面的证据：`apps/web/src/**`；
- **不存在**：外部用户证言、商业指标、性能基准、客户案例、定价信息。后续设计不得编造这些。

## Product Principles

1. **证据优先于数字**：任何读数旁边都要能走到它的来源（revision / pass / 工件哈希 / 日志）。
2. **失败比成功更值得占位置**：出错的那一题、那一步、那次调用，必须比通过的部分更容易被找到。
3. **一个操作员、一块屏幕**：按单人长时间驻留设计，密度与键盘路径优先于协作与展示。
4. **诚实是默认值，不是兜底**：读不到就说读不到，未知永远不被渲染成零或绿色。
5. **并行是常态**：多个 Run 同时存在不是例外，界面必须让「谁在跑、谁挂了、谁完成了」在一屏内成立。
