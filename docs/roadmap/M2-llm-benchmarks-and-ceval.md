# M2：正式 LLM Benchmark 与 C-Eval 迁移详细规划

> 状态：待实施。本文定义阶段范围、契约、实施工作包和退出门；不代表外部 Runner 已接通。实施采用测试先行与逐任务评审，可使用 superpowers:subagent-driven-development 或 superpowers:executing-plans。

**Goal：** 使用固定数据、Profile、模型与 Runner 运行 C-Eval，得到样本明细、学科指标、失败解释、原始/诊断分数和最低限度的可比性与门禁。  
**Architecture：** 在现有 Backend Registry 上增加 job-based 执行，复用 BenchmarkPlugin、RunDispatcher、CaseAttempt、Artifact 和 ScoringPass；Runner 只执行作业，MoTTEavl 保持运行主权。  
**Tech Stack：** 现有 Python/FastAPI/React/Vite 与 SQLite/PostgreSQL；外部 Runner 使用独立、固定的环境，不把其大型依赖直接加入 API 进程。  
**Spec：** [总路线](../ROADMAP.md)第 7 节；[共通约束与依赖](README.md)；[M1](M1-native-agent-and-evaluation.md)的 Observation/评分接口。  
**基线：** MoTTEavl `a668d13ee5ea0c3613648f8992ecaa4a148d3855`；旧项目 `b661bcdf83e1c3dfb8d6062ee78817d249e86a4c`。

## 1. 起点与范围

当前已有 GSM8K/Direct LLM 插件的 prepare/score/aggregate 扩展点；外部 Benchmark backend 有身份但不可执行。现有 ExecutionHandle 主要使用逐 Case 的 invoke 入口，因此不能把一个 OpenCompass Job 简化为每道题启动一个子进程。[B1][B2]

M2 要交付首个真实 job-based 后端，以及在其上的 C-Eval 纵向切片。M1 的文件任务 UI 不是外部 Job 协议的硬依赖；Observation、Artifact 和指标状态接口对齐后即可并行准备 Parser 与 fixture。

### 必备范围

外部 Job 契约与持久句柄；Benchmark Catalog/Profile/数据准备；OpenCompass/C-Eval 配置和 Parser 迁移；凭据与重试边界；API/CLI/Web 五类页面；固定样本对照与 M6-Lite 覆盖门禁。

### 条件追加与非目标

CMMLU 复用 Runner 后可追加，但单独登记数据、Profile 和验证，不阻塞 C-Eval 首次交付。lm-eval、完整 Inspect 执行、公开排行榜提交、自动下载任意 URL、任意 Python 配置上传执行、所有 Runner 版本兼容和多 Worker 均不属于本阶段。

## 2. 最终具体目标清单

- [ ] M2-G01：一个 job-based Run 只启动一个明确的外部 Job，不逐题重启 Runner。
- [ ] M2-G02：Job 句柄、配置哈希、工作目录、进程/容器所有权和采集进度持久化。
- [ ] M2-G03：启动不确定、非零退出、缺结果、部分结果、取消有独立处理。
- [ ] M2-G04：重复采集幂等；同键不同内容必须冲突报警，不能静默覆盖。
- [ ] M2-G05：Catalog 区分 registered、prepared、runnable、verified 和不支持原因。
- [ ] M2-G06：Dataset/Profile/Runner/scorer 及选中 Case 集合冻结；没有占位 revision/digest。
- [ ] M2-G07：数据与代码许可、获取方式、来源、校验和分别记录。
- [ ] M2-G08：C-Eval 学科、split、few-shot、prompt、提取策略和 token 设置可解释。
- [ ] M2-G09：输入仅包含任务要求和合法示例，不泄漏目标样本 gold。
- [ ] M2-G10：Runner 原始指标与平台诊断指标分开存储、命名和显示。
- [ ] M2-G11：每个 selected Case 有明确 disposition；未尝试不消失。
- [ ] M2-G12：调用路径、身份可见度、usage/cost 完整度和重试设置进入证据。
- [ ] M2-G13：假 Runner E2E、真实 Runner 确定性集成、显式 live 分层验收。
- [ ] M2-G14：C-Eval 操作/题目/监控/结果/比较页面复用统一组件。
- [ ] M2-G15：M6-Lite 可以判断同集同 Profile 的覆盖与阈值，不依赖完整实验系统。
- [ ] M2-G16：旧新 Parser 对照报告存在，所有差异有版本化解释。
- [ ] M2-G17：关闭外部 adapter 不影响 Direct/GSM8K/Replay，也不破坏历史读取。

## 3. 模块、文件与责任边界

| 模块 | 现有触点 / 新增建议 | 实现范围 | 不承担 |
|---|---|---|---|
| 执行契约 | `packages/sdk-python/motte_sdk/execution_backends.py`；新增 `packages/contracts/motte_contracts/external_job.py` | sample/job 两种模式、JobHandle、归一化结果、能力声明 | 各 Runner 的私有类与命令行细节 |
| Job 应用服务 | `dispatcher.py`、`service.py`；新增 `motte_sdk/external_jobs.py` | 排队、启动边界、poll/collect、结果导入、取消与最终化 | Runner 的评分算法、第二套队列 |
| 外部适配器包 | 新增 `packages/benchmark-runtime/motte_benchmark/`、`pyproject.toml` | `protocol.py`、`process.py`、`registry.py`；显式内置注册 | 在线安装未经审核的用户插件 |
| C-Eval/OpenCompass | 新增 `motte_benchmark/opencompass/config.py`、`parser.py`、`adapter.py`、`profiles.py` | 固定版本配置、原始产物解析、学科/样本映射 | 旧 SQLAlchemy 服务和旧回调鉴权 |
| Catalog/数据 | 现有资源 store 和插件；新增 `motte_sdk/benchmark_catalog.py` | revision/checksum/license、prepared 状态、样本选择 | 把同名任意 JSONL 视为官方数据 |
| Job 持久化 | `packages/storage/`与现有 migration | Job、artifact import key、检查点、事务与冲突 | 改写原 CaseAttempt 或 ScoringPass 历史 |
| 评分聚合 | `packages/evaluators/motte_eval/`；新增 `ceval.py` | 原始指标导入、显式诊断指标、coverage | 猜测上游缺失的 gold、cost 或完整分数 |
| 用户入口 | 现有 API/CLI；新增 `apps/web/src/evalTypes/ceval/` | 预检、学科选择、任务/结果、对比 | 新的 Provider 管理或独立 Run 状态 |
| 最小门禁 | [M6](M6-experiments-comparison-and-gates.md) T01–T03 的同一实现 | 比较条件、覆盖、阈值、结构化原因 | 第二份 C-Eval 私有 Gate 引擎 |

上表中的新增路径是落位建议，执行前与已合入 M1 路径核对；同一职责已有实现时扩展它，不机械创建同义文件。

## 4. job-based 契约

### 4.1 对象与接口

| 对象 | 必须固定/保存的字段 |
|---|---|
| ExternalJobSpec | run_id、adapter_id/version、runner_version、execution_config_hash、dataset revision、selected_case_ids、profile、work_root、environment digest、limits、retry policy |
| ExternalJobHandle | job_id、run_id、launch_token、external_id、owned_process/container refs、work_dir、created_at、status、collection_cursor |
| NormalizedCaseResult | stable_case_key、source_case_id、output_ref、native_score_refs、status、error_category、usage、evidence_coverage |
| ImportBatch | job_id、source_artifact_hash、parser_version、record_keys、checkpoint、conflicts |

拟议 adapter 操作：prepare(spec)、start(spec)、poll(handle)、interrupt(handle)、collect(handle,cursor)、cleanup(handle)。prepare 校验与创建受控工作目录；start 才能产生执行副作用。接口的持久化由应用层负责，adapter 不直接修改 Run 表。

Job 状态描述子过程，不替代 Run 状态：prepared → launching → active → collecting → settled；其他结果包括 failed/cancelled/indeterminate。Run 继续使用现有合法迁移，ScoringPass 继续追加。

### 4.2 启动与恢复边界

先持久化 JobSpec/启动意图，再启动外部进程。为每次启动生成不可复用的 launch_token，并把它传给受控 wrapper/资源标签。保存 PID 时同时保存启动身份；恢复不能仅凭 PID 判断是原任务。

崩溃后能通过受控资源和 token 找到已存在 Job 时，只恢复观察/采集，不再 start。无法证明启动是否发生时，Run needs_review。禁止仅依据“没有 result.json”就自动重新付费执行。

取消先记录请求，再中断本 Job 拥有的进程树/容器，采集部分工件并记录清理状态。迟到数据可作为审计证据，但不能把已取消 Run 改回 completed。资源清理失败列出精确剩余资源，不执行全系统 Docker prune。

### 4.3 幂等导入与原始结果

建议幂等键：job_id + source_record_key + parser_version；Artifact 另按内容 hash 校验。相同键相同内容重复导入为 no-op；相同键不同内容为冲突，停止最终化并保留两份来源摘要。

必须防范目录遍历、外部 symlink、压缩炸弹、超大 JSON/CSV、未完成写入。Runner 原始文件先转成受控不可变 Artifact，再交 Parser；不能边读可变文件边生成正式分数。

## 5. 数据、Profile 与 C-Eval 口径

### 5.1 Dataset 来源与准备

支持两条首批路径：用户提供本地合法数据；受控获取器从明确允许的官方来源获取固定 revision。获取与模型执行分开，记录来源 URL、观察时间、来源 revision、文件 hash、格式、条数、split 和许可。

本地数据可以声明与官方格式兼容，但没有来源校验时只标为 user-supplied，不自动取得 official provenance。许可确认记录引用许可内容 hash；许可信息缺失不自动同意、不从代码许可推断数据许可。

数据准备状态建议 unprepared/preparing/ready/failed；ready 必须同时满足完整文件、hash 校验、样本清单生成。部分下载不得标为 ready。重试获取不触发模型调用。

### 5.2 Profile 必須包含

benchmark_id/version；dataset revision/split；subject selection；sample selection/order/seed；few-shot 数与示例来源；prompt template/hash；system policy；参数和输出限制；答案提取器/version；aggregation/version；Runner 版本和环境；允许的模型参数映射。

C-Eval 的具体可评 split 以所选数据版本为准。有 gold 才能本地准确率评分；无 gold 时只产出预测/提交工件并标记 unscored，不伪造正确答案。few-shot 示例只能来自 Profile 指定的合法分区，不得把当前待评分样本答案拼入提示词。

### 5.3 原始与诊断指标

旧适配器含两阶段答案提取和重算逻辑，同时保留 OpenCompass 原始结果。[B3] 迁移必须保留两条不同指标命名空间：`native.*` 与 `diagnostic.*`。每个诊断指标记录 parser/extractor/scorer 版本、使用的样本与规则。

不能把 native aggregate 自动按 diagnostic case 分数覆盖。若原始聚合与样本重算不一致，记录 discrepancy 与原因；无法解释则证据不足。学科宏平均、样本加权平均等只按 Profile 的明确公式计算，不能任选一种并标为同一官方指标。

小样本配置带 scope=smoke/custom-subset；界面和导出持续显示该范围。全量成绩必须确认选择集合与目标 Profile 完全一致。

## 6. 模型、费用与环境边界

优先使用所选 Runner 公开扩展点复用现有 Provider 配置/身份记录。不能无损复用时保留 Runner 原生调用，但明确写入 transport_owner、reported identity、request visibility、cost coverage 和 budget enforcement。未观测到的请求模型身份不补填为已核验。

Runner 重试、Provider transport 重试、平台操作员 retry 分开记录；配置转换不能额外叠加默认重试。失败请求是否产生费用按可见证据表示 unknown，不当作免费。

模型凭据在运行时按引用解析，经受控环境或适配钩子传入；不写入永久 Runner 配置、命令行参数、日志或报告。原始配置可持久化脱敏版本与非秘密 hash，不保存包含秘密的文件副本。

外部 Runner 独立镜像/环境，避免 OpenCompass 与 API 的依赖互相牵制。版本从旧适配器固定基线开始验证，升级另开兼容任务，不在迁移过程中无记录切换 latest。

## 7. 工作包与执行次序

| 任务 | 消费 → 产出 | 具体实施内容 | 验收测试（拟新增） |
|---|---|---|---|
| M2-T01 | 现有 Backend → Job 协议 | 增加 execution_mode 和 ExternalJob 数据契约，保留旧 sample 模式 | `tests/contract/test_external_job.py`：缺版本/不合法能力拒绝，旧 manifest 可读 |
| M2-T02 | JobSpec →受控句柄 | start/poll/interrupt/cleanup wrapper、launch_token、资源所有权 | `tests/runtime/test_external_job_lifecycle.py`：一次 start、一 Job 多样本、PID 重用不误杀 |
| M2-T03 | 外部结果 →同一 Run 事实 | Job repository、采集检查点、幂等导入、needs_review 与取消映射 | `tests/storage/test_external_job_store.py`：SQLite/PG 冲突和重复导入一致 |
| M2-T04 | 本地/固定源 → ready dataset | 校验、来源、许可、split、Case 清单、受控获取与断点处理 | `tests/benchmarks/test_ceval_prepare.py`：损坏文件、重复 ID、无 gold 分区不生成分数 |
| M2-T05 | 已发布资源 → Runner 配置 | Profile、few-shot、学科、模型参数 allowlist、脱敏配置 | `tests/benchmarks/test_opencompass_config.py`：未知参数失败，gold 不泄漏，无密钥配置 |
| M2-T06 | 冻结原始输出 →样本/指标 | 迁旧 Parser、提取器和聚合，保留 native/diagnostic | `tests/benchmarks/test_ceval_parser_parity.py`：同份 fixture 新旧结果逐项比较 |
| M2-T07 | Plugin →公共入口 | 注册 C-Eval，Catalog/预检，API/CLI 调用同一准备服务 | `tests/api/test_ceval_run.py`：不可执行时 422，支持时 202 并真实排队 |
| M2-T08 | API 事实 → Web 工作区 | 操作、题目、监控、结果、比较；共享模型与状态组件 | `apps/web/src/evalTypes/ceval/CevalPages.test.tsx`：范围标签、部分失败、版本差异 |
| M2-T09 | 两个报告 →最低质量结论 | 交付 M6-Lite 同一比较/门禁服务的首批子集 | `tests/evaluators/test_benchmark_gate_lite.py`：缺覆盖/不可比不能通过 |
| M2-T10 | 全链路 →验收记录 | 假 Runner、真实 Runner 确定性端点、显式小样本 live、部署和兼容文档 | `tests/integration/test_ceval_external_job.py`：结果可追溯，回归不退化 |
| M2-T11（追加） | 同 Runner → CMMLU | 独立数据/Profile/Parser fixture 与页面声明 | 独立兼容记录；不能复用 C-Eval 的通过勾选 |

依赖：T01→T02→T03；T04/T05/T06 可并行准备；T03+T06→T07→T08；T09 与 T07 联动，T10 最终验收。T11 不属于 C-Eval 首次交付的硬依赖。

每个任务：先增加表中失败测试并确认失败 →实现本项输入/输出 →执行 focused 和相邻回归 →更新 OpenAPI/类型/文档 →独立提交。Parser 迁移禁止只用全绿 happy-path fixture。

### 确定性结果 fixture 示例

```json
{
  "job_id": "fixture-job",
  "selected_case_ids": ["subject-a:1", "subject-a:2", "subject-a:3", "subject-a:4"],
  "records": [
    {"case_id": "subject-a:1", "prediction": "A", "gold": "A"},
    {"case_id": "subject-a:2", "prediction": "B", "gold": "B"},
    {"case_id": "subject-a:3", "prediction": "C", "gold": "D"}
  ],
  "runner_exit_code": 1
}
```

预期：selected=4、attempted=3、correct=2、wrong=1、not_attempted=1、执行失败。诊断 selected-case accuracy=0.5、observed-call coverage=0.75；不能对缺失原始聚合编造 native score。最低完整覆盖门禁不通过。这些数据仅为合成测试，不是官方 C-Eval 内容。

## 8. 验收矩阵

| ID | 场景 | 必須结果 |
|---|---|---|
| M2-A01 | 未准备数据/Runner 不存在 | 创建前失败，模型调用 0 |
| M2-A02 | 正常完整 Job | start 一次；所有 Case 和原始 Artifact 可查询 |
| M2-A03 | 原始目录缺 details，仅有聚合 | 保留聚合与缺失标记；不伪造样本 |
| M2-A04 | 空输出/全空预测 | 0 条结果或未作答按 Profile 处理，不能 completed+通过 |
| M2-A05 | Runner 非零退出且有部分结果 | 部分结果保留，全部 selected Case 有处置状态 |
| M2-A06 | 同键结果重复/内容改变 | 相同 no-op；改变 conflict，禁止覆盖 |
| M2-A07 | start 后句柄未落库时崩溃 | 可核验 token 时重新观察，否则 needs_review，不重启 Job |
| M2-A08 | 采集落库前后崩溃 | 幂等恢复采集，无重复 Score/Artifact 关联 |
| M2-A09 | 取消时 Runner 仍输出 | 终态不复活，迟到结果仅按审计政策接收 |
| M2-A10 | 选科/子集/不同 prompt | manifest 有差异，比较返回具体原因 |
| M2-A11 | 原始分数和平台诊断分数不同 | 两份保留且说明 extractor/version |
| M2-A12 | 无 gold 或来源未确认 | unscored/user-supplied，不冒充完整官方分数 |
| M2-A13 | 密钥/路径泄漏、超大 CSV、symlink | 拒绝或脱敏且记录，不能读取任意宿主文件 |
| M2-A14 | 费用未知 | unknown+coverage，成本硬门禁不能通过 |
| M2-A15 | 新 adapter 禁用 | 旧 Direct/GSM8K/Replay 与历史报告不受影响 |

## 9. 用户入口与交付物

API 复用 Run endpoints；Catalog 提供数据和依赖准备状态。拟新增操作：prepare dataset、validate profile、preview resolved execution、read Job artifacts。预检只验证静态/本地条件；模型真实探针是显式动作，不能在打开页面时付费调用。

CLI 在现有 benchmark 命令组扩展 prepare/validate/run，并输出明确 scope、版本、执行条件与费用说明；不改变已有 GSM8K 参数含义。Web 提供五页：操作、题目、监控、结果、比较。仅 Backend ready 时允许执行，版本选择与预检失败需保留用户输入。

实施交付：独立 Runner 构建文件、固定版本记录、合成 golden fixtures、旧新 Parser 差异报告、`docs/operations/ceval.md`、`docs/protocols/external-jobs.md`、`docs/verification/M2.md` 和兼容矩阵更新。

## 10. 验证命令与回退

新增 tests/benchmarks 后运行：

```bash
uv run pytest -q -m "not live" tests/contract tests/storage tests/benchmarks
uv run pytest -q -m "not live" tests/integration/test_ceval_external_job.py
uv run ruff check .
pnpm --dir apps/web test
pnpm --dir apps/web build
make check
```

真实 Runner 的本地假端点集成与小样本真实模型运行分开记录。full-profile 验收需授权预算和足够环境，不把未运行事项勾选。数据许可核对是获取与分发约束，不由本规划代替授权。

回退：停用 C-Eval adapter 创建能力，停止/确认存量 Job，再回退应用代码；保留 Job/Artifact/ScoringPass 表与历史记录。新 schema 沿既有 migration 链追加，禁止覆盖旧 migration。不能删工作目录来掩盖未知运行状态。

## 11. 固定来源与交接

- [B1] [BenchmarkPlugin 基线](https://github.com/BaiZhi967/MoTTEavl/blob/a668d13ee5ea0c3613648f8992ecaa4a148d3855/packages/sdk-python/motte_sdk/benchmark_plugins.py)
- [B2] [ExecutionBackend 基线](https://github.com/BaiZhi967/MoTTEavl/blob/a668d13ee5ea0c3613648f8992ecaa4a148d3855/packages/sdk-python/motte_sdk/execution_backends.py)
- [B3] [旧 OpenCompass 源码](https://github.com/BaiZhi967/llm_agent__evaluation_platform/blob/b661bcdf83e1c3dfb8d6062ee78817d249e86a4c/packages/benchmark-adapters/src/evalstudio_benchmark_adapters/opencompass.py)
- [B4] [C-Eval 官方来源](https://github.com/hkust-nlp/ceval)：实施时固定所用 revision，核对该版本的数据许可、split 与评测协议。

M2 向 M3 提供 ExternalJob、采集检查点和结果导入边界；向 M6 提供 Profile/选样/原始指标与覆盖语义。M6-Lite 的先行实现由 M6 文档统一定义，不能建立 C-Eval 专用的永久比较系统。
