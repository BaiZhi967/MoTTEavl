# M3：Harbor 与 Terminal-Bench 详细规划

> 状态：待实施。每个任务、数据版本和 Agent Profile 必须独立验收；本文不是“Harbor 所有任务均已支持”的声明。实施使用测试先行、独立提交与评审，可用 superpowers:subagent-driven-development 或 superpowers:executing-plans。

**Goal：** 将固定版本的 Terminal-Bench 任务经 Harbor 执行，保留 Task、Trial、Verifier、模型与环境证据，形成可诊断、可比较的 Agent Benchmark 工作区。  
**Architecture：** 复用 M2 外部 Job 生命周期和结果导入，Harbor 负责其原生环境/Agent/Verifier 执行，MoTTEavl 负责快照、运行控制、统一证据和评分历史。  
**Tech Stack：** 现有 Python/React/存储，独立固定的 Harbor Runner 环境和受控 Docker；不在任务容器暴露平台数据库或 Docker socket。  
**Spec：** [总路线](../ROADMAP.md)第 8 节、[阶段索引](README.md)、[M2 外部 Job](M2-llm-benchmarks-and-ceval.md)、[M1 证据与评分](M1-native-agent-and-evaluation.md)。  
**基线：** MoTTEavl `a668d13ee5ea0c3613648f8992ecaa4a148d3855`；迁移参考旧项目 `b661bcdf83e1c3dfb8d6062ee78817d249e86a4c`。

## 1. 依赖与阶段范围

硬依赖：M2-T01/T02/T03 的 ExternalJob、句柄与幂等采集；M1 的 Observation/Artifact/MetricResult 约定。C-Eval UI 全部完成不是 Harbor Parser 的前置条件。M4 的 Pi/CLI backend 也不是运行 Harbor 已支持 Agent 的前置条件。

首批范围：一个明确版本的 Terminal-Bench 2.x、一个明确 Agent Profile、Task/Trial 结果导入、Verifier 评分、资源预检、取消/清理、工件查看、任务级报告，以及后续跨 Agent 比较所需的快照。

官方文档将 Task 定义为任务指令、环境与测试，Agent 有 external/installed 两种形态。[U1][U2] 本项目保持这些职责，不复制 Harbor 的环境管理内核。在线文档用于理解职责；确切命令、字段、包版本必须由所选锁定版本的兼容测试确认。

不包含：云端 Harbor 服务、多机调度、训练数据生成、自动公开提交榜单、同时导入全部 SWE/Aider 等数据集、重新编写 Terminus Agent、无差别嵌套双层沙箱。

## 2. 最终具体目标清单

- [ ] M3-G01：明确记录 Terminal-Bench 数据版本、source revision、任务内容 hash 与 Runner 版本。
- [ ] M3-G02：本地任务包和固定来源任务集均可受控准备，不自动执行任意 Git 仓库脚本。
- [ ] M3-G03：Task 身份不使用 basename 作为唯一键，同名不同路径不合并。
- [ ] M3-G04：Trial 表示计划中的实验重复，与传输重试/操作员 retry 分开。
- [ ] M3-G05：CaseRun、Trial、CaseAttempt、Score 和 Artifact 可稳定关联。
- [ ] M3-G06：模型、Agent、工具、环境、Verifier、重试和预算快照可复核。
- [ ] M3-G07：一 Job 多 Task/Trial 的启动、监控与采集由同一 Dispatcher 管理。
- [ ] M3-G08：reward=0、缺 reward、无效 reward、Verifier 出错有不同语义。
- [ ] M3-G09：进程退出正常但任务失败，不被报告为质量通过。
- [ ] M3-G10：部分 Task 失败不丢失其他已取得证据；全部计划单元有 disposition。
- [ ] M3-G11：取消/超时后受控资源清理，清理失败和残留可定位。
- [ ] M3-G12：未知副作用或运行句柄不确定时保守 needs_review，不自动重跑。
- [ ] M3-G13：日志、轨迹、文件、patch、Verifier 结果具有原始来源与完整度。
- [ ] M3-G14：任务隔离、gold/Verifier 可见边界、凭据及网络策略有实际记录。
- [ ] M3-G15：通过率、成本、耗时、覆盖及重复次数都带明确分母和单位。
- [ ] M3-G16：Web 支持 Task 列表、Trial 切换、轨迹/终端文本、工件和评分下钻。
- [ ] M3-G17：确定性通过/失败任务、真实 Docker 与用户授权真实 Agent 小批次分别验收。
- [ ] M3-G18：兼容矩阵按 dataset-version × agent-profile × environment 登记，不批量推断支持。

## 3. 模块与文件落位

| 模块 | 现有 / 拟新增触点 | 实现范围 | 明确不承担 |
|---|---|---|---|
| Trial 契约 | 扩展 `packages/contracts/motte_contracts/`；新增 `trial.py` | Task identity、TrialPlan、TrialResult、VerifierObservation | 把网络重试当新的实验样本 |
| Harbor adapter | M2 新包下新增 `motte_benchmark/harbor/adapter.py`、`config.py`、`parser.py` | 命令/配置转换、Task/Trial 解析、上游版本适配 | 平台生命周期、数据库写入 |
| 任务来源 | 新增 `motte_benchmark/harbor/tasks.py` | 来源/revision、目录清单、内容 hash、Profile | 自动信任任意任务中的代码 |
| 环境预检 | 新增 `motte_benchmark/harbor/environment.py` | Docker、镜像、磁盘、架构、网络、权限、预算预检 | 重新实现 Harbor Environment |
| Verifier 映射 | 新增 `packages/evaluators/motte_eval/harbor.py` | 原始 reward、多维指标、缺失/错误政策 | 在无证据时替 Verifier 猜分 |
| Trial 存储 | `packages/storage/`、migration；新增对应 repository | Trial 与 CaseAttempt 关联、幂等、来源引用 | 覆盖旧 CaseRun 结果来保存“最后一次试验” |
| Job 与回收 | M2 `motte_sdk/external_jobs.py`及受控 wrapper | 所有权、停止、checkpoint、残留核查 | 全局杀进程、删除无关容器 |
| 产品入口 | 现有 API/CLI；新增 `apps/web/src/evalTypes/terminalbench/` | 任务筛选、Agent 参数、Trial/Verifier 视图 | 未经能力验证开放所有 Agent 选项 |
| 比较统计 | M6-Lite 及后续统计 | 固定条件下 Task/Trial 结果差异 | 仅看最终文本比较复杂任务成功率 |

旧迁移来源优先是 `packages/benchmark-adapters/src/evalstudio_benchmark_adapters/harbor.py` 的独立配置和 Parser，以及旧 Harbor runner 的 identity/observability regression fixtures。[B1] 迁入时重新检查路径、attempt 和版本语义，不直接搬旧回调协议和 Secret 注入实现。

## 4. Task、Trial 与 Attempt 模型

### 4.1 稳定身份

拟议 TaskIdentity 包含 source_id、dataset_revision、normalized_relative_path、task_content_hash、upstream_task_id。task_key 使用规范 JSON 的内容 hash；展示名独立保存。路径解析只允许位于已准备的受控任务根目录。

同一路径内容变化必须视为新版本；同名不同目录不相同。比较时使用任务集合及内容 hash，跨版本匹配必须显式提供映射与差异，不能只按名称自动对齐。

### 4.2 实验重复

TrialPlan 字段：run_id、task_key、repeat_index、seed（可空）、agent_config_hash、environment_hash。TrialResult 包括 trial_id、source_trial_id、termination、verifier_observation、artifact_refs、usage/cost coverage。

建议保留一条逻辑 CaseRun 表示一个 Task，并新增 Trial 子记录；CaseAttempt 可关联可空 trial_id。无 Trial 的旧 Run 不需要伪造重复记录。现有唯一键与聚合接口必须通过 migration 和契约测试扩展，禁止把多个 Trial 覆盖进原 `(run_id,case_id)` 行。

一个 Trial 可以因基础设施产生多个操作记录，但只有事前计划的 repeat_index 才计入实验重复。操作员 retry 仍创建新 Run。attempt、trial、run 三层费用应聚合但不能重复加总。

### 4.3 指标维度

Score key 至少能区分 ScoringPass、Task、可空 Trial、metric 和 evaluator version。Task 聚合策略写入 Profile，例如 first-trial、mean-success 或明确的 pass@k；不默认择优保留一次成功结果。

M3 首版提供 Trial 原始值和事先指定的 Task 通过规则。pass@k 的完整统计由 M6 统一实现；重复数不够或基础设施失败破坏统计条件时返回不适用/证据不足，不扩大分母或自动补跑。

## 5. 配置、预检和运行流程

### 固定配置

dataset/source revision、选中 task_keys、Harbor package version、adapter/parser version、Agent ID/version、native config、model endpoint/profile、工具策略、镜像 digest、CPU/内存/PID/磁盘预算、Agent/Verifier/Job 三层期限、n_trials、native retries、采集政策。

未知或不支持的原生参数创建前拒绝；不能把旧 thinking 标签自动映射为新厂商值而不记录。共享 Provider 仅在上游提供保真扩展点时使用；否则标记 runner-native transport 和观测边界。

### 流程

1. 数据准备：核对任务目录、manifest、hash 和来源；只读取不执行。
2. 预检：检查 Docker daemon、架构、镜像可用性、资源、任务许可和 Agent 依赖；缺项返回行动明确的错误。
3. 调度：冻结 Job/TrialPlan，持久启动意图，按 M2 方式启动 Harbor。
4. 运行：Harbor 管理自己的环境；MoTTEavl 记录可见进度、句柄和任务状态。
5. 采集：冻结原始 Trial 输出，转换为 TrialResult/VerifierObservation/Artifact。
6. 评分：按固定规则创建 ScoringPass，汇总 Task 与 Trial，不删除失败记录。
7. 清理：确认资源所有权后清理；即使清理失败也先保住运行与评分证据。

### 资源所有权

每个 container、volume、workspace 和进程关联 run/job/trial token。可信 Runner 可以访问经过限定的环境管理能力；不可信任务容器不因而获得 Docker socket、宿主根目录或平台凭据文件。

不要无条件在 MoTTEavl DockerSandbox 内再次运行会创建环境的 Harbor。首版让 Harbor 持有任务环境生命周期，MoTTEavl 外围 wrapper 持有 Runner 生命周期和清理审计；实际部署拓扑写入文档。

## 6. Verifier 评分政策

| 原始情况 | 标准化判断 | 质量/证据处理 |
|---|---|---|
| reward 明确为 1，Verifier 正常且证据完整 | scored / pass | 按 Profile 计通过 |
| reward 明确为 0，Verifier 正常 | scored / fail | 是有效失败，不是平台错误 |
| reward 文件不存在 | missing_verifier_evidence | 不默认为 0 或 1；按 coverage 政策处理 |
| reward JSON 畸形/值类型或范围不符 | verifier_protocol_error | 保留原始 hash，不自动修正 |
| Verifier 超时/崩溃 | verifier_error | 独立于 Agent 任务失败 |
| Agent 超时但 Verifier 可运行 | 按所选上游协议验证产物 | 不因 Agent stop reason 自动覆盖有效 Verifier 结果 |
| Agent 正常退出但 reward=0 | execution complete / quality fail | 不将返回码当作任务成功 |
| Job 被取消或结果未知 | cancelled/indeterminate | 保留已确定 Trial，剩余单元明确标注 |

多指标 reward 原样保存并按注册 schema 转换；未知字段保留来源但不自动纳入 Gate。无法观察隐藏 Verifier 隔离时，报告该限制，不能根据目录名字宣称 gold 完全不可访问。

保持上游任务标准协议；安全策略必须修改任务或测试可见性时，产生不同 environment/profile fingerprint，并标为平台自定义安全配置，不能继续冒充完全同口径。

## 7. 证据、计量与界面

证据类型：任务指令、实际非秘密配置、Agent 可见轨迹、工具/终端文本、session metadata、workspace 文件与 patch、Verifier stdout/stderr/reward、退出与清理记录。所有大文件进入 Artifact，事件引用它们；不把完整日志复制到每个 SSE 事件。

采集声明覆盖范围：tool trace complete/partial/unavailable、terminal truncated、artifact missing、usage unavailable。前端将不可观察数据标为未知，不生成隐藏推理或未提供的工具步骤。

成本视图同时列已知成本、未知 Trial 数、计量来源和价格版本；成功任务成本计算要说明失败 Trial 的费用是否计入分子。成本比率分母为零时返回不适用。Agent、Verifier 与环境时长分别保留，避免把容器启动时间当作模型推理延迟。

Web 页面：操作（任务/Profile/Agent/重复数/预算/预检）；任务列表（筛选、版本、范围）；监控（Job→Task→Trial）；结果（reward、错误、覆盖、工件）；详情（Trial 切换、终端文本、文件 diff、Verifier）；比较（固定条件差异与 M6-Lite 原因）。

## 8. 详细工作包

| 任务 | 输入 → 产出 | 具体范围 | 测试文件（拟新增） |
|---|---|---|---|
| M3-T01 | 固定任务目录 → TaskIdentity | 路径规范、内容 hash、source revision、任务清单 | `tests/benchmarks/test_harbor_task_identity.py`：同名不同路径、长路径、Windows 分隔符、内容变化 |
| M3-T02 | TaskPlan → TrialPlan/关系 | Trial 契约、CaseAttempt 关联、存储 migration、旧读兼容 | `tests/storage/test_trial_store.py`：多 Trial 不覆盖 Case、唯一性与重复导入 |
| M3-T03 | Profile →原生配置 | Agent/模型/期限/重复/重试 allowlist，配置快照 | `tests/benchmarks/test_harbor_config.py`：未知参数拒绝、默认值明确、不叠加重试 |
| M3-T04 | 原始目录 → TrialResult | 迁 Parser、reward、轨迹和 Artifact 映射 | `tests/benchmarks/test_harbor_parser.py`：reward=0/缺失/错误分别表示 |
| M3-T05 | Task/环境 → preflight | Docker/镜像/架构/权限/磁盘/依赖检查 | `tests/benchmarks/test_harbor_preflight.py`：不满足则零模型调用 |
| M3-T06 | ExternalJob →实际 Harbor | wrapper、Job 管理、poll/collect、可见运行状态 | `tests/integration/test_harbor_job_fixture.py`：一次 start、多 Task、多 Trial |
| M3-T07 | 取消/崩溃 →安全收尾 | 资源所有权、超时、清理、恢复只观察、needs_review | `tests/runtime/test_harbor_recovery.py`：不可证明已结束时不重复执行 |
| M3-T08 | Trial evidence → ScoreSet | Verifier 状态、Task 聚合、覆盖/成本来源 | `tests/evaluators/test_harbor_metrics.py`：有效失败不消失，缺证据不通过 |
| M3-T09 | 公共 API →工作区 | Task/Trial/Artifact 端点与页面、共享组件 | `tests/api/test_terminalbench_flow.py`；`apps/web/src/evalTypes/terminalbench/TerminalBenchPages.test.tsx` |
| M3-T10 | 任务包 →真实环境证据 | 确定性校准、真实 Docker、显式真实 Agent 样本、对照记录 | `tests/integration/test_harbor_calibration.py`及独立 live 入口 |

执行次序：T01/T02→T03/T04/T05→T06→T07/T08→T09→T10。T04 可以先纯解析推进，不需要 Docker 或 API 密钥。各任务先固定失败 fixture，验证红灯，再实现、回归、文档与独立提交。

### 合成 Trial 验收样例

```json
{
  "task_key": "fixture-task-a",
  "planned_repeats": 3,
  "trials": [
    {"repeat_index": 0, "agent_exit": 0, "verifier_exit": 0, "reward": 1},
    {"repeat_index": 1, "agent_exit": 0, "verifier_exit": 0, "reward": 0},
    {"repeat_index": 2, "agent_exit": 0, "verifier_exit": 124, "reward": null}
  ]
}
```

预期保存 3 个计划 Trial：1 个通过、1 个有效失败、1 个 Verifier 错误。valid-trial pass rate=1/2，valid coverage=2/3；完整覆盖门禁不得通过。不得把第三个错误悄悄删掉后只显示 50% 而不提示覆盖，也不得用 first-success 规则把整个任务默认为通过。

## 9. 强制验收场景

| ID | 故障/边界 | 预期 |
|---|---|---|
| M3-A01 | 合成已知通过/失败任务 | Verifier 与标准化分数一致 |
| M3-A02 | path-only task ref、同 basename | 身份稳定且不合并 |
| M3-A03 | 一个 Task 多 repeat | 所有 Trial 保留；聚合可重算 |
| M3-A04 | reward 文件缺失/畸形/零值 | missing/error/fail 三种不同结果 |
| M3-A05 | 环境启动失败 | 分类 environment，不伪造 Agent 回答 |
| M3-A06 | Agent 超时、Verifier 超时 | 两者区分，证据保留 |
| M3-A07 | Runner 非零退出、部分 Trial 完成 | 已确定结果保留，剩余 Trial 有 disposition |
| M3-A08 | 取消时仍有子进程和容器 | 只清理本 Run 所有资源，显示实际停止结果 |
| M3-A09 | wrapper 崩溃，Harbor 仍运行 | 重新观察或 needs_review，不自动重启 |
| M3-A10 | 原始 Artifact 变化/重复采集 | hash 冲突显式失败，不覆盖旧证据 |
| M3-A11 | 轨迹缺失/截断 | 完整度下降，行为断言不假装完整 |
| M3-A12 | 费用与模型身份不可观察 | null/unknown 与对应政策结论 |
| M3-A13 | 恶意任务访问凭据/宿主资源 | 按声明安全边界阻断或预检拒绝执行 |
| M3-A14 | 更换 Agent/native 配置/环境镜像 | manifest 改变，比较解释差异 |
| M3-A15 | 相同状态下重评分 | 不重新执行任务，旧 ScoringPass 不变 |

## 10. 验证、迁移与回退

```bash
uv run pytest -q -m "not live" tests/benchmarks tests/storage tests/evaluators
uv run pytest -q -m "not live" tests/integration/test_harbor_job_fixture.py
uv run ruff check .
pnpm --dir apps/web test
pnpm --dir apps/web build
make check
```

命令针对本阶段将新增的测试；真实 Docker 校准与真实 Agent 运行使用独立标记/手动入口。先通过确定性环境测试，再进行授权的小样本真实调用；全 Profile 验证单独记录数据规模、失败范围和预算。

旧 Parser 的 golden 对照只迁合成或已授权脱敏数据；不将私有完整轨迹直接放进公开仓库。旧任务 ID 与新 task_key 保存映射；无法稳定匹配的记录进入迁移诊断，不自行合并。

回退先关闭 Harbor backend 创建能力，再停止或确认存量 Job。保留 Trial/Artifact/ScoringPass；禁止删除未知活跃 Job 的目录。schema 沿 M2 后的实际 migration head 追加，不预先占用可能冲突的版本号。

交付文档：`docs/operations/terminal-bench.md`、Harbor 原生参数兼容矩阵、任务/Trial 身份协议、残留清理 runbook、`docs/verification/M3.md`。M3 向 M6 交付 Trial 的统计资格与错误语义，向 M7 交付任务资产许可、环境和恢复要求。

## 11. 来源

- [B1] [旧 Harbor adapter](https://github.com/BaiZhi967/llm_agent__evaluation_platform/blob/b661bcdf83e1c3dfb8d6062ee78817d249e86a4c/packages/benchmark-adapters/src/evalstudio_benchmark_adapters/harbor.py)
- [U1] [Harbor Datasets](https://www.harborframework.com/docs/datasets)
- [U2] [Harbor Agents](https://www.harborframework.com/docs/agents)
- [U3] [Harbor Core Concepts](https://www.harborframework.com/docs/core-concepts)

外部资料核对日期：2026-09-19；这些在线页面不是实际运行版本锁。实现 PR 必须补选定包版本、环境 digest 和原生协议 fixture。
