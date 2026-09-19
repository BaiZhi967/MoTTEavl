# M2 第二轮代码审查（2026-09-20）

审查范围：`50c8070..569a3b5`，包含 `38abcc1`、`569a3b5` 两个修复提交，共 52 个文件。依据 M2 roadmap、上轮 R01–R16 反馈和本轮新增实现。

**结论：暂不通过。确认 12 项问题：6 项 P1、6 项 P2。** 部分上轮问题已经修复，但真实 Runner 接入、样本身份、不可变证据和比较门禁仍有缺口。

本轮只审查与复现，未修改业务代码、未提交或推送。以下 `M2-R2-xx` 为本轮独立编号，避免与上一轮 R01–R16 混淆。

## 验证与证据边界

- 本轮执行 `make check`：退出码 0；Python **1183 passed / 18 skipped**（43.40s）；前端 **14 files / 186 passed**；Ruff、contracts mypy、compileall、Web build、bridge selftest、Compose config 通过。
- 追加离线复现：临时 SQLite、合成题目、fake Runner 子进程、可控时钟、输出文件改写、工作目录清理和比较/Gate 服务。均未调用真实 Provider。
- 追加一项临时 Vitest 回归：候选输入改变但不再次提交，旧比较响应应被丢弃；**实际失败**，旧结论仍显示。临时测试已移出仓库，未改变正式测试集。
- 对照 OpenCompass **0.4.2** 官方源码，隔离运行其参数解析函数及配置校验入口；没有安装或运行完整 OpenCompass。参数和配置不兼容是已复现的代码问题，不能把这项验证称为真实 Runner 集成通过。
- 未执行 PostgreSQL 实例集成、真实固定 Runner、本地确定性模型端点或真实模型 smoke；文档中的对应 `not_run` 应继续保留。
- 尝试增加独立 reviewer 时遇到 agent thread limit，本轮由主代理完成复查和复现。

## P1

### M2-R2-01：新 wrapper 仍不能驱动固定版 OpenCompass

位置：[scripts/runner/opencompass-entry:40](/Users/whitezhi/Dev/MoTTEavl/scripts/runner/opencompass-entry:40)，[opencompass/config.py:179](/Users/whitezhi/Dev/MoTTEavl/packages/benchmark-runtime/motte_benchmark/opencompass/config.py:179)。对应上轮 R01。

wrapper 把内部 `runner-config.json` 直接交给 `opencompass.cli.main`，并传入 `--config`、`--launch-token`、`--job-id`、`--run-id`。0.4.2 的 config 是位置参数；`--config` 被 argparse 当成 `--config-dir` 的缩写，后三项不是该版本支持的参数。内部 JSON 只有 `model/cases/credentials` 等平台字段，尚未转换为 OpenCompass 所需的 `models/datasets` 配置，也没有消费凭据引用的桥接实现。依据：[固定版 CLI](https://raw.githubusercontent.com/open-compass/opencompass/0.4.2/opencompass/cli/main.py)、[固定版配置加载函数](https://raw.githubusercontent.com/open-compass/opencompass/0.4.2/opencompass/utils/run.py)。

离线隔离复现输出：

```text
PINNED_CLI_EXIT 2: unrecognized arguments: --launch-token --job-id job --run-id run
WITHOUT_IDENTITY_FLAGS: config=None, config_dir='runner-config.json'
CONFIG_LOAD: KeyError 'datasets'
```

因此即使操作员正确安装固定环境，文档推荐的 wrapper 也会在推理前失败；只删除不支持的 flags 仍不足以修复。

修复验收：交付实际的平台配置→OpenCompass 配置/数据/模型桥接；生命周期身份由 wrapper 消费，不向上游透传未知参数；使用固定环境对本地确定性 HTTP 端点跑一次真实 Runner，验证请求模型、题目、选样、few-shot、凭据引用解析和完成标记。该验证不需要付费模型。

### M2-R2-02：Parser 拒绝 OpenCompass 实际生成的时间戳目录

位置：[opencompass/parser.py:379](/Users/whitezhi/Dev/MoTTEavl/packages/benchmark-runtime/motte_benchmark/opencompass/parser.py:379)。本轮新增回归。

`_result_candidate` 要求固定层级 `outputs/results/<model>/<dataset>.json`。固定版 CLI 会在 `--work-dir` 下增加本次执行的时间戳子目录，实际形态为 `outputs/<timestamp>/results/<model>/<dataset>.json`；当前 wrapper 没有移动或导出这个目录。[OpenCompass 0.4.2 的 work_dir 处理](https://raw.githubusercontent.com/open-compass/opencompass/0.4.2/opencompass/cli/main.py)。

已将可解析的合成树移到 `outputs/20260920_090000/results/mock-model/ceval-logic.json`，立即得到 `missing-results`。所以修复 R2-01 后仍会丢失正常 Runner 输出；C-Eval、CMMLU 共用此路径。

修复验收：冻结并持久化本 Job 的实际实验目录，或由桥接统一导出结果树；支持真实固定版产物，同时保持 fd 锚定与 symlink 拒绝。不要通过无约束递归搜索混入其他实验结果。

### M2-R2-03：稀疏结果与增量游标仍会串题

位置：[opencompass/adapter.py:171](/Users/whitezhi/Dev/MoTTEavl/packages/benchmark-runtime/motte_benchmark/opencompass/adapter.py:171)。对应上轮 R03。

映射使用“本次遍历中第几个结果”，而不是 Runner 明细实际行号；`records_consumed` 切片后，subject 计数又从 0 开始。原始行号已在 `sample_id` 中保留，却没有用于定位冻结 Case。

已复现：冻结 `[c1,c2,c3]`，输出只有行 `0:A, 2:C`，实际映射为 `c1:A,c2:C`，应为 `c1:A,c3:C`。同一输出以 `records_consumed=1` 采集，行 2 又归到 `c1`。后续评分会拿错误题目的冻结 gold 评分，缺失题目的 disposition 也随之错误。

修复验收：按 subject + 原始 row index 查冻结映射，游标只决定哪些记录需要导入，不改变身份；覆盖缺首行、缺中间行、多学科、游标恢复和越界索引，缺行保持 not_attempted。

### M2-R2-04：Baseline 快照路径绕过可比性判断

位置：[motte_sdk/comparisons.py:224](/Users/whitezhi/Dev/MoTTEavl/packages/sdk-python/motte_sdk/comparisons.py:224)。对应上轮 R12，新增快照分支问题。

指定 `baseline_snapshot_id` 时，不比较 Baseline 与 Candidate 的固定报告，而是直接信任 `snapshot.metrics.comparable`，且字段缺省为 True。可比性是两份报告之间的关系，不能由 Baseline 自身的布尔值决定。

已通过 fake Runner 完成两份 Run，数据 revision 分别为 `rev-A`、`rev-B`：普通 `compare` 返回 `eligible=False`；把第一份 Run/pass 保存为仅含 accuracy 的合法 Baseline 快照，再对第二份运行 `require_comparable=True` 的 Gate，返回 `passed=True`、`reports comparable`。

修复验收：从快照解析固定 RunReportRef/ScoringPass 与评测身份，使用同一比较算法对当前 Candidate 求值；不同 revision/Profile/scorer/case 集合必须阻断，缺失来源不能默认可比。

### M2-R2-05：相同 revision/ID 下的题目内容变化仍被判可比

位置：[motte_eval/comparison.py:125](/Users/whitezhi/Dev/MoTTEavl/packages/evaluators/motte_eval/comparison.py:125)。对应上轮 R08。

共同 Case 只比较 gold。冻结题干、选项与 few-shot 内容没有参与差异判断；创建流程已有 row hash，但未投影成比较所用的任务身份。准备存储允许相同 benchmark/revision 重写，因此 revision 字符串相同并不能证明内容相同。

已复现：同 `logic-1`、同 revision、同 gold=B，题干从 `What is 1+1?` 改成 `What is 100+100?`。两份 `runner_config.config_hash` 不同，比较却返回 `eligible=True`、`case_diff.changed=[]`。

修复验收：冻结并比较源 Case、选中 few-shot 的内容身份；重用 revision 时拒绝不同内容，或给出显式新快照身份。允许模型变化时，不应直接比较包含模型字段的整个 config hash；需把任务内容身份与合法变量分开。

### M2-R2-06：原始文件仍在解析后才冻结，分数可与证据不一致

位置：[external_jobs.py:240](/Users/whitezhi/Dev/MoTTEavl/packages/sdk-python/motte_sdk/external_jobs.py:240)、[external_jobs.py:462](/Users/whitezhi/Dev/MoTTEavl/packages/sdk-python/motte_sdk/external_jobs.py:462)。对应上轮 R09。

解析前只保存 hash 清单，Parser 仍读取可变工作目录；`_settle` 随后再次读取这些文件保存 bundle。两次内容没有一致性校验，hash 不同也不阻断最终化，未达到 roadmap 4.3 的“先转受控不可变 Artifact，再交 Parser”。

故障注入：在 `collect` 读完结果后、返回前，把输出预测从 `[B,A]` 改为 `[D,D]`。实际 Run 仍 `completed`，Case 记录保留 `[B,A]`，持久原始 bundle 为 `[D,D]`，`hashes_before_parse` 与 bundle hash 明显不一致。按保存的原始证据重新解析无法重现正式分数。

修复验收：先冻结完整输入字节，再从同一 Artifact 解析、评分与恢复；快照失败/预算不足不能冒充完整证据。覆盖解析前后外部改写及删除，正式结果必须绑定唯一内容 hash。

## P2

### M2-R2-07：恢复会替换已经固定的原始证据引用

位置：[external_jobs.py:360](/Users/whitezhi/Dev/MoTTEavl/packages/sdk-python/motte_sdk/external_jobs.py:360)、[external_jobs.py:519](/Users/whitezhi/Dev/MoTTEavl/packages/sdk-python/motte_sdk/external_jobs.py:519)。对应上轮 R04/R09。

终态且 `import_completed=True` 的快路径虽然读取已导入记录，随后仍调用 `_settle`，从当前工作目录重新生成 bundle 并覆盖 checkpoint.evidence。内容寻址避免了旧文件字节被覆盖，但没有保护 Job 对原证据的引用。

已复现：正常完成后删除 work_dir，再用新 Durable runner 实例恢复。结果仍有 2 条，但 checkpoint 的 raw bundle ID 改变，`raw_files` 从一个 JSON 变为 `[]`。恢复窗口也可能出现在 Job 已落终态而 Run 尚未完成时，不能假定只会对完成 Run 重放。

修复验收：导入完成后的恢复复用原 Artifact/指标/错误事实，保持证据引用与 hash 不变；中断恢复依赖已冻结产物，不依赖可清理的工作目录。

### M2-R2-08：compare 接受不存在的 ScoringPass

位置：[motte_sdk/comparisons.py:90](/Users/whitezhi/Dev/MoTTEavl/packages/sdk-python/motte_sdk/comparisons.py:90)。对应上轮 R12。

`report_ref` 只检查 pass_id 是否为 None，不调用已有 `_resolve_pass` 校验归属与存在性。compare 的两个显式 pass 参数由此可以绕过“固定评分证据”的前置约束。

已复现：对同一完成 Run 比较，传 `baseline_pass_id='nonexistent-a'`、`candidate_pass_id='nonexistent-b'`，仍返回 `eligible=True`。公开 `/api/v1/comparisons` 参数会进入同一路径。

修复验收：统一解析指定 pass，错误或跨 Run 的 ID 返回明确 422；比较口径使用所选 pass 的冻结评分身份，而不是只看 Run manifest。

### M2-R2-09：默认 3600 秒超时没有应用到 C-Eval/CMMLU

位置：[external_jobs.py:157](/Users/whitezhi/Dev/MoTTEavl/packages/sdk-python/motte_sdk/external_jobs.py:157)。对应上轮 R05。

Supervisor 从 `adapter.default_limits` 获取缺省超时，OpenCompassJobAdapter 却只把该字段保存在内部 `_process.default_limits`。公开 RunSpec 只有 poll_interval，故 deadline=None，常驻挂起进程没有默认截止时间。

已复现：公开输入投影得到 `limits={'poll_interval_seconds':0.1}`；外层 default_limits 缺失，内层为 3600。用可控时钟和 active→active→settled 的 poll 序列观察，monotonic 调用数为 0，中断数为 0，证实未建立超时判定。

修复验收：监督层统一合并有效 Job limits，或由 adapter 明确暴露；在真正 C-Eval/CMMLU 适配器下推进假时钟，应得到 JOB_TIMEOUT 并中断自有进程。恢复时也应保留原运行时间预算。

### M2-R2-10：预检未约束最终生效的输出 token 预算

位置：[benchmark_catalog.py:729](/Users/whitezhi/Dev/MoTTEavl/packages/sdk-python/motte_sdk/benchmark_catalog.py:729)、[opencompass/config.py:194](/Users/whitezhi/Dev/MoTTEavl/packages/benchmark-runtime/motte_benchmark/opencompass/config.py:194)。对应上轮 R14。

上下文估算只算题干/选项字符数，遗漏完整模板和输出预算；模型上限校验只检查 `model.parameters.max_output_tokens`，没有检查 Profile 默认生成长度。

已复现：模型 `context_window=32,max_output_tokens=8`，短题目预检 reasons=[]，prepare 成功；实际配置 `max_output_tokens=1024`、model ceiling=8，渲染后 prompt 已有 62 个字符。这里不把字符数等同 token 数；仅输出 1024 超过模型声明上限 8 就足以证明配置不合法。

修复验收：明确最终参数优先级，以最终渲染输入与有效输出长度做预算；输出不得超过模型上限，prompt+output 不得超过上下文窗口。覆盖 Profile 默认值、模型覆盖值和 few-shot 模板开销。

### M2-R2-11：修改比较输入时，仍接受上一组请求的晚到响应

位置：[ExternalBenchmarkPages.tsx:389](/Users/whitezhi/Dev/MoTTEavl/apps/web/src/evalTypes/external/ExternalBenchmarkPages.tsx:389)。对应上轮 R15。

输入改变的 effect 只清空 UI，没有使 submitRef 失效；只有再次 submit 才递增 token。因此“提交旧候选→请求未完成→改成新候选但不提交→旧请求返回”会重新显示旧组的可比性，并继续请求旧组 Gate，界面输入却已是新 Run。

已用临时 Vitest 复现，断言候选输入为 `candidate-new` 成立，但 `comparison-panel` 应为空的断言失败，实际显示“两份报告可比”。现有测试覆盖重复提交以及请求完成后的输入变化，未覆盖该时序。

修复验收：输入改变、卸载时也失效当前 compare/gate 请求，或按完整输入身份绑定结果；覆盖旧 compare、旧 gate 两种在途响应，不要求用户再次点击比较才清理。

### M2-R2-12：新增公共 API 未同步 OpenAPI 和前端类型

位置：[api/openapi.json:4264](/Users/whitezhi/Dev/MoTTEavl/api/openapi.json:4264)、[apps/api/app/main.py:1871](/Users/whitezhi/Dev/MoTTEavl/apps/api/app/main.py:1871)。本轮契约同步回归。

对 `create_app(...).openapi()` 与仓库 `api/openapi.json` 做只读比较，发现后者缺少四个 `/api/v1/benchmarks/external/{benchmark_id}/...` 路径，缺少比较接口的 `baseline_pass/candidate_pass`，旧 C-Eval preflight 也缺少 `few_shot/few_shot_split/scope/split`。本轮没有更新对应生成的前端 schema 类型。

影响：依赖发布契约的客户端无法发现/生成 CMMLU 通用入口和固定评分版本参数；Web 的手写 fetch 与现有 make check 不会检测这种漂移。

修复验收：运行 `make openapi` 同步契约与类型；增加生成结果与提交产物一致的检查，避免公共接口演进只更新 Python 路由。

## 上轮问题复验状态

此表的“已修复”限于上轮具体触发条件和本轮离线证据，不代表整个工作包或 live 验收通过。

| 上轮编号 | 本轮判断 | 说明 |
|---|---|---|
| R01 Runner 接入 | 部分修复 | 配置进入 Run/Job、受控注册已接入；真实兼容仍阻塞，见 R2-01/02 |
| R02 评分缺失 | 已修复 | 外部 Run 进入 managed scorer，有固定 ScoreSet/ScoringPass；本轮两个 fake Run 也验证完成评分 |
| R03 Case 映射 | 部分修复 | 连续行/任意 Case ID 已支持；稀疏与游标仍串题，见 R2-03 |
| R04 导入恢复 | 部分修复 | 导入完成后才写终态、Artifact 内容寻址已落地；恢复仍替换证据引用，见 R2-07 |
| R05 跨实例取消 | 部分修复 | Worker 已消费持久取消；默认超时未作用于实际 adapter，见 R2-09 |
| R06 outputs symlink | 已修复原复现 | 信任边界固定、fd 读取和输出子树 symlink 拒绝测试通过 |
| R07 metric 被忽略 | 已修复原复现 | 按 accuracy/cost 身份取值，未知 metric 拒绝、未知费用不误通过 |
| R08 比较口径 | 部分修复 | 增加 Profile 不变量；实际任务内容仍漏检，见 R2-05 |
| R09 证据持久化 | 部分修复 | native/diagnostic 与 raw bundle 已写入；冻结时序与恢复引用仍有缺陷，见 R2-06/07 |
| R10 部分结果被当成功 | 已修复原复现 | 缺可信完成标记保持 indeterminate，不再凭目录存在推断成功 |
| R11 零结果成功 | 已修复 | 返回 EXTERNAL_EMPTY_RESULTS，selected Cases 保留 not_attempted |
| R12 固定评分/Baseline | 部分修复 | candidate_summary 已消费固定 pass；snapshot 比较和显式 pass 校验仍有缺口，见 R2-04/08 |
| R13 Catalog 重启丢失 | 已修复原复现 | SQLite 持久准备结果、第二实例可恢复；未声称 PG 实例已验证 |
| R14 入队预检 | 部分修复 | lifecycle、split、full 选择和 few-shot 已加检查；最终 token 预算仍漏检，见 R2-10 |
| R15 UI 异步串数据 | 部分修复 | 结果页切换、重复提交已保护；只修改输入的在途响应仍覆盖，见 R2-11 |
| R16 CMMLU 公共链 | 离线路径已补齐 | 通用 API、CLI/Web、独立 fixture 已增加；共享真实 Runner 缺陷仍适用，OpenAPI 漂移见 R2-12 |

## 下一轮修复顺序与验收

1. 先完成 R2-01/02：固定 Runner 桥接和真实目录契约，以本地确定性端点验证，避免继续仅用忽略真实配置的 fake Runner 证明接通。
2. 完成 R2-03/06/07/09：样本身份、先冻结后解析、恢复证据不漂移和有效超时。注入稀疏输出、工作目录清理和采集期间改写。
3. 完成 R2-04/05/08：所有 Gate/compare 分支统一消费实际存在的固定报告，对内容与评分身份做同一套资格判断。
4. 完成 R2-10/11/12，补上本轮反例的正式测试，运行 `make check` 与生成契约一致性检查；真实 Runner 确定性集成和显式 live 分别记录，不合并为同一通过结论。

## 复现线索

本轮 Python 复用 `tests/review/test_m2_review_fixes.py` 的 `_dataset`、`_inputs`、`_adapter_with_tree`、`_review_service`，所有数据及 SQLite 位于临时目录。关键反例输入已在各项逐一记录，开发者可直接将其加入现有验收测试。

例如稀疏映射可以独立复现：

```bash
uv run python - <<'PY'
import runpy, tempfile
from pathlib import Path
h = runpy.run_path('tests/review/test_m2_review_fixes.py')
with tempfile.TemporaryDirectory() as root:
    cases = [{'case_id': f'c{i}', 'subject': 'logic'} for i in range(1, 4)]
    details = {
        str(i): {'origin_prediction': x, 'predictions': x, 'references': x}
        for i, x in [(0, 'A'), (2, 'C')]
    }
    adapter, handle = h['_adapter_with_tree'](Path(root), cases, details)
    for cursor in ({}, {'records_consumed': 1}):
        rows, _ = adapter.collect(handle, cursor)
        print([(r.stable_case_key, r.evidence_coverage['sample_id']) for r in rows])
PY
```

本轮结果为 `[('c1','logic-0'),('c2','logic-2')]` 与 `[('c1','logic-2')]`；正确映射应分别为 `[('c1','logic-0'),('c3','logic-2')]` 与 `[('c3','logic-2')]`。
