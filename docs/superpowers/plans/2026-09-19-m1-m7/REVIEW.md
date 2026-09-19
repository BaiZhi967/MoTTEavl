# M1–M7 计划 review 记录

审阅日期：2026-09-19。审阅对象：[总安排](README.md)、M1–M7 执行计划、coverage.json，以及需求目录全文。代码基线：`b6c402b62f0a19f79cde719af60c8bb73672c877`。

审阅方式：当前执行者静态自审，加结构化覆盖、依赖和路径检查。没有启动独立 reviewer，没有进行功能实现或 runtime 验收。下文“已修订”指**计划已修订**，原应用中的缺口仍需后续任务实现。

## 1. 审阅范围

- 需求：逐项对照 137 个 G 目标、77 个 T 工作包、119 个 A 场景；同时对照七份阶段文档的全部二级章节及总路线横向要求。
- 当前事实：读取 contracts、backend/dispatcher/service、评分 registry、三 store 审计、Builtin runtime、Scenario/Skill、maintenance、数据来源治理、Web suite registry/Vitest、Makefile/CI 与迁移链。
- 执行安排：工作包依赖 DAG、Lite 先行、可提前事项、增强项和完整发布退出门，验证所有工作包最终均被 M7-T12 收口。
- 质量：失败测试输入/预期、实际收集路径、跨入口一致性、不可变历史、费用未知、真实验证边界、恢复与回退。
- 文档：路径的现有/拟新增标注、链接、原需求保留、未完成状态、估算口径与 review 修复循环。

## 2. 发现与计划修订

| ID | 级别 | 问题与影响 | 修订及责任 | 计划状态 |
|---|---|---|---|---|
| PR-01 | P1 | 原基线晚近增加 Direct LLM v2、SourceSpec、资源发布；机械执行会重复实现 | 总安排第3节记录当前基线；M1-T02、M2-T04/T06/T11 复用现存注册/转换/治理 | 已修订 |
| PR-02 | P1 | Observation 已有同名旧契约；直接替换会破坏旧读 | M1-T01 定义显式 schema 演进及旧 roundtrip，拒绝双模型事实源 | 已修订 |
| PR-03 | P1 | ScoreSet 和持久查询仍是 case-only，不能装入多指标/Trial | M1-T01/T03 扩复合键、SQL 无 Trial 规范键与旧查询；M3-T02 扩 attempt 冲突域 | 已修订 |
| PR-04 | P1 | second-chance 可能被新 Agent/Job/Judge 当作安全重跑 | M1-T07、M2-T03、M5-T05/T09 增加调用计数与 dispatch 后崩溃测试 | 已修订 |
| PR-05 | P1 | 数据转换可用被误作 C-Eval 官方 Runner 支持或治理批准 | M2-T04/T05/T06 分离 Direct 变体、正式 Profile 与受信来源证据；无证据保持阻断 | 已修订 |
| PR-06 | P1 | 原建议 Web 测试位于 src，当前 Vitest 不收集 | 全阶段测试统一落 apps/web/tests；机械检查每个计划路径 | 已修订 |
| PR-07 | P2 | 旧文档 head=0002，实际已存在 0003 | 全局要求以实际 head 追加；开工更新兼容/升级文档，不预占 revision | 已修订 |
| PR-08 | P1 | M2-T09 与 M6-Lite 若相互依赖会死锁排期 | Lite 只依赖 M1，M2-T09 单向消费；算法工期不重复计算 | 已修订 |
| PR-09 | P2 | Scenario 必填 model 与 CLI 原生认证冲突；Skill 必填 entrypoint 与纯指令冲突 | M4-T01 / M5-T06 按 backend/kind 演进，旧读回归独立安排 | 已修订 |
| PR-10 | P1 | M7 若直接沿用目录备份/覆盖恢复/mtime GC，无法保护正式证据 | T09/T10 明确全部写入口维护边界、引用清单、staging、pin 和 tombstone 测试 | 已修订 |
| PR-11 | P2 | make check 与全部 CI 检查并不完全相同 | 明列 Web/bridge 递归测试、审计、配置扫描和生成类型无漂移 | 已修订 |
| PR-12 | P1 | 只核对编号清单会遗漏总路线 Provider 流式与探针横向项 | M4-T02/T08 + M7-T11 承接，追加真实文件/测试触点并计入初步工期 | 已修订 |
| PR-13 | P2 | 增强交互可能被误设为完整 Gate 核心依赖 | M6-T08 消费 M4 Runtime 契约与合成 intervention 证据，不依赖 T10 consumer | 已修订 |
| PR-14 | P1 | CMMLU/交互/Inspect 被称为可选后可能从“全部完成”中消失 | 三项都保留独立卡片和验收；Core/RC 与全77项完成分别定义，最终 T12 对账 | 已修订 |
| PR-15 | P2 | DESIGN.md 指定的项目 skills 在本 checkout 不存在 | 总安排记录 UI 开工前置；定位 skill 后执行指定流程，不以此阻止本次文档规划 | 已记录实施前置 |
| PR-16 | P1 | Judge 校准与真实后端支持易被合成 fixture 代替 | M5-T10 至少30个人工复核样本、用途 CalibrationPolicy；各阶段末包与 M7-T11 分层验收 | 已修订 |
| PR-17 | P2 | 相同 Gate 输入确定性与 evaluated_at 变化看似矛盾 | M6-T03 及总安排明确结论语义 hash 排除求值审计时间，GET 历史结果不修改 | 已修订 |
| PR-18 | P1 | 只写领域服务和 UI 容易遗漏 Experiment/Baseline/Gate 公共 API；SDK 也可能漏方法域 | M6-T05/T07/T08 补 API/schemas 触点与公开操作；M7-T01 补 Experiment/Baseline/Artifact 并依赖完整 baseline 契约 | 已修订 |

这些修订没有改变原阶段目标的完成状态。实际实现 review 必须重新检查代码与真实命令输出，不能把此表当作问题已经在应用中修复。

## 3. 各阶段计划结论

| 阶段 | 需求/工作包/验收覆盖 | 非编号章节 | 本轮计划 review 的重点结论 | 实施状态 |
|---|---|---|---|---|
| M1 | 18 / 10 / 15 | 第1–11节 | 多指标必须同时修改契约、存储、查询与消费者；日志边界先于不确定恢复 | planned / not_run |
| M2 | 17 / 11 / 15 | 第1–11节 | 一 Job 多 Case；来源治理、原始/诊断分数、共享 Lite Gate 都有主责 | planned / not_run |
| M3 | 18 / 10 / 15 | 第1–11节 | Task/Trial/Attempt 不混同；Verifier 失败与 coverage、环境清理分别验收 | planned / not_run |
| M4 | 18 / 11 / 15 | 第1–11节 | Pi/Claude/Codex 单独验证；交互真 ack、Inspect 只读与流式横向项保留 | planned / not_run |
| M5 | 22 / 12 / 18 | 第1–12节 | 过程断言不能被终态覆盖；Skill 不扩权；Judge 不在 API 内收费 | planned / not_run |
| M6 | 21 / 11 / 20 | 第1–12节 | 分母、覆盖、Task 聚类统计与固定 baseline 贯穿 Lite/Full | planned / not_run |
| M7 | 23 / 12 / 21 | 第1–14节 | 客户端幂等、历史不可分发、一致备份、安全、三替代场景完整收口 | planned / not_run |

七份计划均已做静态内容 review；依赖于未来环境、许可、预算、校准样本的事项被分配了责任包和解除条件，不作为当前已通过证据。

## 4. 规划验证与复现

在仓库根目录执行：

```bash
python3 docs/superpowers/plans/2026-09-19-m1-m7/validate_plan.py
git diff --check
git status --short
```

validate_plan.py 只读文档、文件存在性和 Git 状态，不调用模型、不修改数据库。它验证：目标/验收原文保留；每项有有效责任包；每包有实施卡与测试路径；DAG 无环且最终 review 包覆盖全部任务；Lite 不循环依赖；Web 测试路径可被当前配置收集；现有/拟新增路径标注；本地文档链接；无完成勾选；原始阶段需求未被修改；变化仅在 docs 下。脚本只证明这些静态性质，不证明测试内容足够或实现可运行。

本次实际机械检查通过，结果为：137 个目标、77 个工作包、119 个验收场景；依赖图无环，M7-T12 可追溯全部77包；750 个本地链接/锚点有效；所有 Web 测试路径符合当前收集目录；修改/新增路径标注正确；原始 M1–M7 及总路线内容未改动；变化只在 docs 下。

规划交付时，校验脚本经 `uv run ruff check docs/superpowers/plans/2026-09-19-m1-m7/validate_plan.py` 检查通过，`git diff --check` 通过；当时未运行应用检查，也未提交或推送。用户随后授权提交并推送文档，提交前检查结果补充如下。

### 提交前验证补充

- `make check` 执行两次，ruff、mypy（20 个源文件）、compileall 均通过；两次完整 Python 测试均为 **983 passed、15 skipped、1 failed**。失败项为 `tests/test_dev.py::test_cleanup_owned_descendants[False]`，在 `apps/dev.py:142` 的 `os.killpg(pid, 0)` 抛出 `PermissionError: [Errno 1] Operation not permitted`，因此完整门禁未通过。
- `uv run pytest -q -m "not live" tests/test_dev.py` 单独复跑为 **20 passed**。全套与单文件行为不同，根因尚未确认；本次没有修改应用代码或测试，也不将复跑通过描述为问题已修复。
- 初次 Web 构建缺少已在锁文件声明的 `react-router-dom`；执行 `pnpm install --frozen-lockfile` 补齐本地依赖，未修改 lockfile。随后 `make web-build web-test` 通过：Web **11 个测试文件、160 个测试**通过，Pi bridge selftest 通过；构建有大于 500 kB 的 chunk 提示。
- `make compose-config` 通过。文档覆盖校验和 `git diff --check` 通过，提交范围只含本次规划文件、校验工具与路线索引链接。
- 未运行付费 live、真实 Runner 任务、数据库迁移、生产发布或恢复演练。上述检查不改变七阶段的 `planned / not_run` 实施状态；15 个跳过测试不计为通过。

## 5. 后续开工时必须重新核对

- 每阶段最新 HEAD、未提交工作、迁移 head、已合入前置契约以及本卡路径；本次核对不能消除后续漂移。
- M2/M3/M4 固定上游包、原生协议、镜像 digest、数据 revision 与代码/数据许可；本次未核验最新外部网页或运行任何外部 CLI 任务。
- M4 的指定前端 skills 路径、runtime 可观察边界；其他 UI 包同样遵循这个前置。
- M5 Judge 人工校准资料及具体用途阈值、subject/Judge 各自预算；无校准不可正式 Gate。
- M7 支持环境、真实迁移来源、RC、备份和生产替换范围；规划不授权修改现有生产数据。

后续执行默认从 M1-T01 开始。上述重核对服务于计划落实，不要求重新讨论已经确定的七阶段范围。
