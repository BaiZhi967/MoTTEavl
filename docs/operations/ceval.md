# C-Eval 外部基准操作指南（job-based）

> 状态：**合成/假 Runner、真实固定 OpenCompass 0.4.2 + 本地确定性 HTTP
> 端点已验证；真实模型 smoke 与 full Profile 仍未执行（not_run）**。
> 本地验证仅证明生产推理与平台评分链路，不代表官方数据或原生准确率对照验收。

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
OpenCompass **0.4.2** 形态配置（位置参数调用，凭据引用只在模型构造时经
`os.environ` 解析，不进入上游 Config dump）、定位时间戳实验目录并写
`outputs/experiment.json` 指针（解析侧据此
选择唯一实验，绝不混入其他运行）、结束原子写 `.motte-job-complete` 完成标记。
同 revision 重写不同内容在 prepare 层拒绝（`DATASET_REVISION_REUSED`）。
`preparation.default_split` 参与 revision 不可变身份。旧 payload 仍可读取；
缺少 `preparation` 与空对象的无变化 roundtrip 保持幂等，但未知旧参数不能
证明与新的明确参数相同，重准备需使用新 revision。
无配置且 wrapper 不存在 → RUNNER_NOT_CONNECTED，创建在提交前 422。

### 固定 Runner 安装与本地验收

```bash
scripts/runner/install-opencompass /absolute/path/to/motte-runner
scripts/runner/verify-opencompass-local /absolute/path/to/motte-runner/bin/python
```

安装脚本使用独立 Python **3.10.20** 和
`scripts/runner/opencompass-0.4.2-py310.lock`，并把独立桥接模块与 wrapper
复制进该环境。API/SDK 仍使用 Python >=3.12，不向 API 环境安装 OpenCompass。
0.4.2 的 pandas==1.5.3 在本机 Python3.12 安装失败，因此不把主项目的 Python
版本约束绕过。固定依赖集合已在 macOS arm64 验证；Linux 部署须重复验收，
不能把该平台结果当作 Linux/CUDA 依赖兼容证据。

安装需要下载公开依赖和 gpt-4 tokenizer 资产；部署时应保留该 tokenizer 缓存。
测试只把模型请求发送到临时 localhost HTTP 服务，使用合成凭据，并关闭 HF
联网下载。已安装 wrapper 自动使用相邻 `bin/python`，且把解释器传给下游 CLI；
可直接将 adapter argv 指向任意安装目录的 `bin/opencompass-entry`。
显式 `MOTTE_RUNNER_PYTHON` / `MOTTE_RUNNER_ROOT` 仍可覆盖默认路径。

推理配置导入 `LocalMCQDataset`（返回 HuggingFace Dataset）与完整 reader /
PromptTemplate / ZeroRetriever / GenInferencer。生产冻结 prompt 已包含选定
few-shot 示例；ZeroRetriever 不重新选样。temperature 走模型参数，top_p 走
OpenAI.extra_body，max_output_tokens 走 max_out_len。base_url 引用可为 `/v1`
或完整 `/chat/completions` URL；未给引用时保留上游默认端点。

目标 gold 仅在平台 manifest 中，Runner 使用 `--mode infer` 产生真实
`predictions/<model>/<benchmark>-<subject>.json`，不产生伪造的 results/native
accuracy。Parser v2 接受该形态并拒绝多个模型混入同一 Job，平台从冻结 gold
生成 ScoringPass 和 report；没有 native accuracy 时不宣称 native 对照通过。
Parser 身份升级为 `<benchmark>-opencompass-parser@2`，历史报告保持固定证据读取。

升级到 Parser @2 后，尚未完成导入的旧 @1 Job 不会被新规则重解析或重新启动；返回 `EXTERNAL_PARSER_VERSION_MISMATCH`，保留原 Job、checkpoint 和 Artifact，交由操作员使用匹配版本处理。已取消 Job 保持取消，已完成导入的历史报告仍可只读查询，并保留原 parser 版本。

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
| 真实固定 Runner + 本地确定性端点 | **已验证：本地推理与平台评分** | `scripts/runner/verify-opencompass-local /absolute/runner/bin/python`：生产 prepare inputs → RunService/Dispatcher → 实际安装 wrapper/0.4.2 → localhost HTTP → 真实 predictions → Parser → SQLite ScoringPass → report API；zero/few-shot、选样、参数、默认 URL、整个临时目录凭据扫描。未验证官方完整 Profile/native accuracy 对照。 |
| 真实模型 smoke（小样本，授权预算） | **not_run / blocked** | 需显式授权（预算、凭据、预算上限）；授权后：`ceval prepare`（官方数据需先完成受信核验器部署）→ `POST runs {model, scope:"smoke"}` → worker。 |
| full Profile（选定完整集） | **not_run / blocked** | 同上，且需确认选择集合与目标 Profile 完全一致后才可记录为完整成绩。 |

## 5. 回退

停用 C-Eval 能力：`motte_benchmark.registry.unregister_adapter("ceval-opencompass")`
（或不在 Runner 环境注册）→ 创建在提交前 422（RUNNER_NOT_CONNECTED），
分派层对存量失败 Run 标 unsupported；Direct/GSM8K/Replay 与历史报告不受
影响（tests/integration/test_ceval_external_job.py::test_disabling_external_adapter_keeps_other_suites）。
保留 Job/Artifact/ScoringPass 表与历史记录；不删除未知状态的工作目录。
