# C-Eval 外部基准操作指南（job-based）

> 状态：**合成/假 Runner 链路已验证；真实固定 Runner、真实模型 smoke 与
> full Profile 验收未执行（not_run）**。本文同时是后两层的验收清单。

## 1. 链路概览

一个 ceval-external Run 只启动一个外部 Job：

```
POST /api/v1/benchmarks/external/ceval/prepare   # 数据准备（来源治理门禁）
GET  /api/v1/benchmarks/external/catalog         # registered/prepared/runnable/verified
GET  /api/v1/benchmarks/external/ceval/preflight # 静态预检（0 模型调用）
POST /api/v1/benchmarks/external/ceval/runs      # 202 排队；422 时 0 调用
uv run python -m apps.worker.motte_worker --once # 分派：一个 Run 一个 Job
GET  /api/v1/runs/{id}/external-jobs             # Job 记录（状态/token）
```

CLI：`motte ceval prepare|preflight|run`（与 API 共用同一服务；`--split/
--few-shot/--few-shot-split` 等参数与创建预检一致）。准备结果持久化
（`benchmark_datasets` 表），重启/第二个 API 实例不丢。
Web：`/ceval` 五页（操作/题目/监控/结果/比较）。

**Runner 接入（review R01 + R2-01/R2-02）**：创建入口会把冻结的
`runner-config.json`（模型快照/逐题 prompt/few-shot/凭据引用/config_hash）
写入 Job 工作目录，并冻结任务内容身份（`case_content_hashes`/`few_shot_hashes`，
R2-05）。adapter 注册经同一受控配置加载：

```
# var/runner/adapters.json（或 MOTTE_RUNNER_CONFIG 指向的文件）
{"adapters": [{"benchmark": "ceval", "argv": ["/opt/motte-runner/bin/opencompass-entry"]}]}
```

固定环境 wrapper 见 `scripts/runner/opencompass-entry`：以 pinned 解释器调用
`motte_benchmark.opencompass.entry` 桥接——按学科导出本地数据、渲染
OpenCompass **0.4.2** 形态配置（位置参数调用，凭据引用经 `os.environ`
解析）、定位时间戳实验目录并写 `outputs/experiment.json` 指针（解析侧据此
选择唯一实验，绝不混入其他运行）、结束原子写 `.motte-job-complete` 完成标记。
同 revision 重写不同内容在 prepare 层拒绝（`DATASET_REVISION_REUSED`）。
无配置且 wrapper 不存在 → RUNNER_NOT_CONNECTED，创建在提交前 422。

## 2. 数据与来源治理

- 本地 JSONL（每行 `id/subject/question/A-D/answer`；`answer` 缺省=unscored）
  经 `prepare` 校验：checksum（可选声明）、重复 ID、半下载、缺文件都会
  失败并给出原因；成功标记 **user-supplied**。
- **verified-offical** 需要受信核验器（ApprovalVerifier 实现）+ 固定版本
  许可证据；当前未部署核验器，官方溯源路径保持阻断（不自动同意、不从
  代码许可推断数据许可）。C-Eval 官方数据许可（CC-BY-NC-SA-4.0）的获取
  与分发约束由操作员核对。
- 无 gold 分区 ready 但 unscored：只产出预测/提交工件语义，不编造正确率。

## 3. 运行口径

- Profile 钉住：dataset revision、split、学科选择、few-shot（必须来自与
  评测 split 不同的合法分区）、prompt/提取器/聚合版本、Runner 版本
  （`opencompass-0.4.2` 基线，自旧适配器迁移）、环境 digest（覆盖
  runner/adapter/parser/benchmark 身份）。
- **学科白名单**：C-Eval 只接受官方 52 学科，未知学科在 prepare 阶段
  拒绝（不静默聚合失败）。
- **入队前统一预检（review R14）**：模型须 published；split 必须是准备
  数据的实际分区；few-shot 需有足够 dev 示例；scope=full 必须覆盖分区
  全部行；上下文预算校验。失败 422（`RUN_REQUEST_INVALID` + reasons），
  0 Job 启动。GET preflight 与创建共用同一校验与输入。
- 凭据按引用（`{"ref": "env:NAME"}`）传入 Runner 配置；任何原始值拒绝。
- scope ∈ smoke / custom-subset / full，始终随配置与报告；零结果的
  选择集不能伪装 completed（`EXTERNAL_EMPTY_RESULTS`）。
- 指标双命名空间：`native.*`（OpenCompass 原始聚合）与 `diagnostic.*`
  （平台重算，提取器=结论优先两段式，迁移自旧适配器 b661bcdf 并有 golden
  对照，见 `docs/migration/ceval-parser-parity.md`）；两者不一致记
  discrepancy，不互相覆盖。评分 gold 以冻结 `case_expectations` 为权威。

## 4. 分层验收清单

| 层 | 状态 | 证据 / 复现 |
|---|---|---|
| 合成 golden（旧 Parser 对照） | ✅ 已验证 | `uv run pytest -q -m "not live" tests/benchmarks/test_ceval_parser_parity.py` |
| 假 Runner 全链路（API→排队→分派→Job→查询；禁用 adapter 不影响旧套件） | ✅ 已验证 | `uv run pytest -q -m "not live" tests/integration/test_ceval_external_job.py` |
| 真实固定 Runner + 本地确定性端点 | **not_run** | 需要：独立环境安装 `opencompass==0.4.2`（容器/venv，不进 API 进程）；`CevalJobAdapter` argv 指向 `opencompass-entry`（`/opt/motte-runner/bin/opencompass-entry` 默认，可配置）；本地假 HTTP 端点（不付费）承载 OpenAI 兼容协议；执行 `motte ceval prepare → run → worker`，用迁移 Parser 解析真实输出目录并与 native 对照。完成后在 `docs/verification/M2.md` 记录原始 hash 与差异。 |
| 真实模型 smoke（小样本，授权预算） | **not_run / blocked** | 需显式授权（预算、凭据、预算上限）；授权后：`ceval prepare`（官方数据需先完成受信核验器部署）→ `POST runs {model, scope:"smoke"}` → worker。 |
| full Profile（选定完整集） | **not_run / blocked** | 同上，且需确认选择集合与目标 Profile 完全一致后才可记录为完整成绩。 |

## 5. 回退

停用 C-Eval 能力：`motte_benchmark.registry.unregister_adapter("ceval-opencompass")`
（或不在 Runner 环境注册）→ 创建在提交前 422（RUNNER_NOT_CONNECTED），
分派层对存量失败 Run 标 unsupported；Direct/GSM8K/Replay 与历史报告不受
影响（tests/integration/test_ceval_external_job.py::test_disabling_external_adapter_keeps_other_suites）。
保留 Job/Artifact/ScoringPass 表与历史记录；不删除未知状态的工作目录。
