# M2 第三轮代码审查（2026-09-20）

范围：`569a3b5..e454081`，一个修复提交，23 个变更文件。对照第二轮 R2-01–12、M2 roadmap 的 Runner、不可变证据、恢复、预检要求。

**结论：暂不通过。确认 11 项问题：7 项 P1、4 项 P2。** 上轮若干反例已被修复；本轮问题主要位于新增桥接与冻结读取路径，以及未覆盖的恢复/并发分支。编号使用 `M2-R3-xx`，与前两轮区分。

本轮未修改业务代码、未提交或推送。以下复现全部使用合成数据、临时目录/SQLite 和本地假进程；没有调用真实模型，也没有读取真实凭据。

## 验证证据

- 本轮 `make check` 退出码 **0**：Python **1201 passed / 18 skipped / 2 warnings**，44.68s；Web **14 files / 187 passed**；lint、contracts 类型检查、compileall、Web build、bridge selftest、Compose config、OpenAPI 一致性检查通过。
- 补充复现使用生产 `prepare_external_run_inputs`、实际 adapter/dispatcher、两个 Catalog 实例，以及在明确边界注入目录替换、导入中断和工作目录清理。
- 生成配置执行测试只替换 `opencompass.datasets.base` 的导入依赖；直接执行仓库生成的 Python 源码，验证名称解析，不冒充完整 OpenCompass 集成。
- 固定版上游契约检查依据 OpenCompass 0.4.2 官方源码；真实固定 Runner + 本地确定性端点、真实模型、PostgreSQL 实例集成仍未执行。
- 按 requesting-code-review 流程尝试独立 reviewer，调度返回 agent thread limit；本轮由主代理完成复查与复现。

## P1

### M2-R3-01：生成的 OpenCompass 配置无法执行，仍未形成可用桥接

位置：[entry.py:125](/Users/whitezhi/Dev/MoTTEavl/packages/benchmark-runtime/motte_benchmark/opencompass/entry.py:125)、[entry.py:164](/Users/whitezhi/Dev/MoTTEavl/packages/benchmark-runtime/motte_benchmark/opencompass/entry.py:164)。对应 R2-01。

生成的 C-Eval 配置引用 `CevalDataset`，CMMLU 引用 `CmmlUDataset`，但源码只定义 `LocalMCQDataset`，没有导入前两者。即使固定环境存在，配置加载也会先遇到 NameError。

已从生产创建函数生成配置并执行，得到：

```text
GENERATED_CONFIG_EXEC NameError name 'CevalDataset' is not defined
```

此外，datasets 项把类放在 `path`，没有 `type`；上游会把无 type 的配置作为 custom dataset，要求 path 为文件路径。模型项使用 `url`，而固定版 OpenAI 构造器参数为 `openai_api_base`；不能只补一个 import 就关闭问题。依据：[固定版 custom dataset 配置解析](https://raw.githubusercontent.com/open-compass/opencompass/0.4.2/opencompass/datasets/custom.py)、[固定版 OpenAI 构造器](https://raw.githubusercontent.com/open-compass/opencompass/0.4.2/opencompass/models/openai_api.py)。

当前测试仅断言生成文本包含 models/datasets，再用忽略配置内容的 shell stub 验证 argv，无法发现上述错误。

修复验收：以公共入口实际生成的配置，在固定版环境完成配置加载、dataset/model 构造与本地确定性端点推理；验证数据集 abbr 与解析器命名契约、参数转换和实际 HTTP 请求。该测试不需要付费模型。

### M2-R3-02：桥接导出空题目，丢弃生产配置中的 prompt 和 few-shot

位置：[entry.py:65](/Users/whitezhi/Dev/MoTTEavl/packages/benchmark-runtime/motte_benchmark/opencompass/entry.py:65)。对应 R2-01 的数据链路。

生产 `build_opencompass_config` 的 cases 只有 `case_id/subject/prompt`；新 exporter 却读取 `question/A/B/C/D/answer`，全部使用空字符串兜底，且没有消费已经渲染、包含合法 few-shot 的 prompt。桥接只保留 few-shot 数量，未导出对应示例内容。

已使用生产 `_inputs` 导出一条非空题目，结果为：

```text
PRODUCTION_CASE_KEYS ['case_id', 'subject', 'prompt']
EXPORTED {'id': 'logic-1', 'question': '', 'A': '', 'B': '', 'C': '', 'D': ''}
```

现有桥接测试手写了 question/options/answer，因此与真实上游数据契约不同。即便修复 R3-01，Runner 仍得不到被选择的题目，报告中的冻结任务与实际调用内容不一致。

修复验收：桥接消费实际冻结 prompt，或定义并贯通完整结构化数据契约；验证逐题内容、选项、选样顺序和 few-shot 一致；gold 继续与 subject 输入隔离，不能为修复导出而把目标答案放进 prompt。

### M2-R3-03：wrapper 没有保留可核验的 launch_token，重启后无法认领活进程

位置：[scripts/runner/opencompass-entry:32](/Users/whitezhi/Dev/MoTTEavl/scripts/runner/opencompass-entry:32)，[process.py:342](/Users/whitezhi/Dev/MoTTEavl/packages/benchmark-runtime/motte_benchmark/process.py:342)。对应 Runner 生命周期要求。

`ProcessJobAdapter._verify_identity` 跨实例只检查 PID 的 argv 是否包含 launch_token。默认 adapter argv 只有 wrapper 路径；wrapper 又 exec 为 `python -m motte_benchmark.opencompass.entry`，token 仅存在环境中。entry 注释称其消费身份，实际没有把身份保留到被核验的进程参数，也没有其他身份验证机制。

离线复现使用仓库实际 wrapper，仅将 pinned interpreter 换成本地 sleep stub：原进程仍存活，新 adapter.poll 却返回 `indeterminate`，`token_in_argv=False`。因此 Worker 重启后不能继续监督/核验取消该 Job，仍在运行的进程可能失去管理。

修复验收：wrapper/entry 自身接受并保留 token，或实现另一种可靠的持久身份核验；不要把平台身份 flags 传给不支持它们的上游 CLI。用真实 wrapper 验证新实例恢复 active Job、取消进程组和 PID 重用拒绝。

### M2-R3-04：新的冻结读取路径绕过 fd 锚定，父目录替换可越界读取

位置：[adapter.py:139](/Users/whitezhi/Dev/MoTTEavl/packages/benchmark-runtime/motte_benchmark/opencompass/adapter.py:139)。新增回归，重新影响第一轮 R06。

生产采集改走 `read_output_files → CaseWorkspace.read_text`，不再使用 Parser 的 `_TrustedDir`。CaseWorkspace 先按字符串路径检查父链，再 `os.open(target, O_NOFOLLOW)`；这个 flag 只防最后一个组件，检查后替换的父目录仍可被跟随。因此此处“fd 锚定”的注释与实际调用不符。

已在临时目录中，在 `_safe_target` 返回与 `os.open` 之间把结果文件父目录替换成指向外部目录的 symlink。实际 adapter 读取到外部合成标记 `OUTSIDE_REVIEW_SENTINEL`。这是新生产读取入口的复现，不是只调用旧 Parser 的静态 symlink 测试。

修复验收：沿已打开的可信目录 fd 逐组件打开文件，完整冻结原始 bytes；在真实 `read_output_files → supervisor → Artifact` 路径注入父链/根目录/文件替换，必须拒绝越界。

### M2-R3-05：无差别归档 outputs 会将上游解析后的密钥配置持久化

位置：[adapter.py:135](/Users/whitezhi/Dev/MoTTEavl/packages/benchmark-runtime/motte_benchmark/opencompass/adapter.py:135)，[entry.py:164](/Users/whitezhi/Dev/MoTTEavl/packages/benchmark-runtime/motte_benchmark/opencompass/entry.py:164)。新增证据范围问题。

新入口在配置加载时把环境引用解析为 `models[].key`。固定版 OpenCompass 将加载后的配置 dump 到实验目录下的 `configs/*.py`；新 `read_output_files` 收集 outputs 下所有文件，随后原样保存到永久 raw bundle，没有排除或脱敏该配置。由这条数据路径可知，只保证初始配置源码使用 env 引用不能保证 Runner 输出中没有密钥。[固定版 CLI 的配置 dump](https://raw.githubusercontent.com/open-compass/opencompass/0.4.2/opencompass/cli/main.py)。

离线验证：在 fake Runner 输出中加入合成的 `outputs/configs/resolved.py`，其中 key 仅为测试标记 `SYNTHETIC_REVIEW_API_KEY`；实际 dispatch 完成后，raw Artifact 原样含该标记，Run=completed。本轮没有使用真实凭据，也没有声称观察到真实密钥泄漏。

修复验收：结果证据采用明确清单；需要保存的配置只保留脱敏形式，运行时秘密不得进入上游配置 dump、永久 Artifact、日志或报告。用合成秘密跑真实固定 Runner，并扫描全部落盘产物和公开查询结果。

### M2-R3-06：冻结输出后仍从可变 runner-config 读取 Case 映射

位置：[adapter.py:213](/Users/whitezhi/Dev/MoTTEavl/packages/benchmark-runtime/motte_benchmark/opencompass/adapter.py:213)，[adapter.py:165](/Users/whitezhi/Dev/MoTTEavl/packages/benchmark-runtime/motte_benchmark/opencompass/adapter.py:165)。R2-03 原始行号反例已修复，但映射权威来源仍不固定。

`collect_from_files` 从冻结结果 bytes 解析，却调用 `_frozen_case_order(handle)` 重新读取工作目录的 runner-config.json。没有与持久化 spec/config_hash 核验；该文件也不在冻结输出 bundle 中。文件中的 config_hash 即便仍为旧值，也不会被验证。

已复现：输出行 `0:B,1:A`，创建时 Case 顺序 `[c1,c2]`；读取输出后仅将工作目录配置里的 cases 顺序反转。解析返回 `[(c2,B),(c1,A)]`，没有报错。同一冻结输出会因可变映射被归到不同题目并使用不同 gold 评分。

修复验收：映射使用持久化 JobSpec/Run 的不可变来源，或将映射连同输入 bytes 一并固定并验证 hash；修改工作目录配置不得改变 Case 身份。

### M2-R3-07：导入中断恢复仍不读取已经冻结的完整 Artifact

位置：[external_jobs.py:449](/Users/whitezhi/Dev/MoTTEavl/packages/sdk-python/motte_sdk/external_jobs.py:449)。R2-07 只修复了 import_completed=True 分支。

完整 raw bundle/outcome 在导入前已经落盘，但其引用直到所有导入完成才写 checkpoint。导入中途崩溃时，恢复分支仍重新观察进程、读取可清理的 work_dir，不消费已有冻结结果。

已用 2 条结果在第二次 import 注入模拟进程中断：Job=active、库内 1 条、完整 raw bundle 已存在，checkpoint 只有 `import_completed=False,records_consumed=1`。清理 work_dir 后以新 runner 恢复，得到 `indeterminate/results=0`，数据库仍只有 1 条。已保存的完整结果不能被恢复流程使用，未完成样本永久缺失于导入记录。

修复验收：冻结成功后、导入前持久化可恢复 Artifact 引用及其身份；恢复优先从已固定结果幂等补齐，不能依赖临时目录。覆盖每条导入前后中断和 work_dir 删除；最终记录齐全且 start 次数不增加。

## P2

### M2-R3-08：证据超过冻结预算时，仍产出 completed 和正式分数

位置：[external_jobs.py:292](/Users/whitezhi/Dev/MoTTEavl/packages/sdk-python/motte_sdk/external_jobs.py:292)，[external_jobs.py:104](/Users/whitezhi/Dev/MoTTEavl/packages/sdk-python/motte_sdk/external_jobs.py:104)。对应 R2-06 完整性要求。

读取上限默认 10 MiB，冻结内容预算为 8 MiB；超过冻结预算只保存 hash/content=None、complete=False，调用方仍解析内存中的完整内容并正常最终化。总量超过 64 MiB 也有同类分支。

已给正常结果 JSON 加入 9 MiB 合成 padding，实际链路返回 `Run=completed,evidence.complete=False`，关键结果文件的持久 content=None。工作目录清理后，无法从正式证据重建 Parser 输入；“完整 bytes 已冻结”不成立。

修复验收：结果文件原始 bytes 作为独立 Artifact 完整保存；超过支持上限时应在正式导入/评分前明确失败或不足证据。通过超预算标记不能代替这个门禁。

### M2-R3-09：生产 adapter 不消费桥接生成的 experiment.json

位置：[adapter.py:209](/Users/whitezhi/Dev/MoTTEavl/packages/benchmark-runtime/motte_benchmark/opencompass/adapter.py:209)。对应 R2-02。

目录版 `parse_opencompass_results` 会读取指针；生产 adapter 已改用纯内存 `parse_opencompass_files`，既不读取指针也不传 experiment。文件虽然被冻结，选择信息却没有进入解析。

已构造 exp-a、exp-b 两个实验目录并写 `experiment.json={experiment:exp-b}`。目录版解析成功选择 exp-b；同一 Job 的 `adapter.collect` 抛 ambiguous-experiment。当前全链测试只含一个实验目录，因此未检验指针实际被消费。

修复验收：从冻结 bundle 解析并校验指针，将同一固定选择交给解析器；覆盖多候选有效指针、无指针、无效指针，保持不同实验不混合。

### M2-R3-10：few-shot 预算未叠加到最长实际题目

位置：[benchmark_catalog.py:789](/Users/whitezhi/Dev/MoTTEavl/packages/sdk-python/motte_sdk/benchmark_catalog.py:789)。对应 R2-10。

预算取 `max(第一题+few-shot, 最长题目不带few-shot)`，遗漏 `最长题目+few-shot`，因此数据行顺序影响是否被预检拦截。第一题短、后续题长时会低估有效输入。

已复现：第一题 1 字符、第二题 500 字符、示例 300 字符，output=64、context_window=700，preflight reasons=[]；生产渲染的最长 prompt 为 895 字符，按本实现采用的字符预算口径已经超过 700，更未计入输出。此处只验证预算算法未覆盖实际渲染输入，不把字符数宣称为精确 tokenizer 结果。

修复验收：对最终 selected cases 使用相同 few-shot/模板渲染后逐题校验，或正确累加公共示例开销；题目重排不得改变预检结论。

### M2-R3-11：相同 revision 的防覆盖检查存在跨实例竞争

位置：[benchmark_catalog.py:526](/Users/whitezhi/Dev/MoTTEavl/packages/sdk-python/motte_sdk/benchmark_catalog.py:526)。对应 R2-05 新增防重写逻辑。

新增约束在 SDK 中执行独立 get→比较→put，而存储仍是 INSERT OR REPLACE。两个 API/Catalog 实例同时读取“尚不存在”，都能提交不同内容，后写者覆盖先写者；各实例内存还会持有不同的同 revision 数据。

已用两个 Catalog、同一 SQLite store，在 get 后设置并发屏障，提交同 benchmark/revision、不同题干，结果为 `['accepted','accepted']`。当前仅顺序更新的测试无法验证不可变性。

修复验收：在存储事务内实现同内容幂等、异内容冲突，覆盖 Memory/SQLite/PG 一致语义；并发异内容只能一个成功，另一请求必须明确冲突，既有记录不得被覆盖。

## 第二轮问题复验

“原反例已修复”只说明对应离线触发条件消失，不等于真实 Runner/live 阶段验收通过。

| 第二轮 | 本轮判断 |
|---|---|
| R2-01 wrapper/原生配置 | 参数位置已改；新桥接仍被 R3-01/02 阻塞 |
| R2-02 时间戳目录 | 唯一目录和目录版显式指针已支持；生产指针遗漏见 R3-09 |
| R2-03 稀疏/游标串题 | 原反例已修复；可变映射的新反例见 R3-06 |
| R2-04 Baseline 绕过比较 | 原反例已修复，快照分支调用统一比较算法 |
| R2-05 实际内容漏检 | 内容 hash/few-shot hash 已加入；revision 并发覆盖见 R3-11 |
| R2-06 解析后冻结 | 输出文本先冻结再解析已接入；读取安全、配置泄密、内容预算见 R3-04/05/08 |
| R2-07 已完成导入恢复替换证据 | 原反例已修复；未完成导入分支仍不使用 Artifact，见 R3-07 |
| R2-08 不存在的 ScoringPass | 原反例已修复，report_ref 统一调用 _resolve_pass |
| R2-09 默认超时不生效 | 原反例已修复，adapter 暴露 default_limits |
| R2-10 最终输出预算 | 默认输出/模型上限已校验；最长题目与 few-shot 组合遗漏见 R3-10 |
| R2-11 修改输入后的晚到响应 | 原反例已修复，输入变化使 submitRef 失效，回归测试通过 |
| R2-12 OpenAPI 漂移 | 提交契约与前端类型已同步，本轮 openapi-check 通过 |

## 建议的下一轮验收安排

1. 先打通**公共创建输入 → 真实固定 Runner → 本地确定性端点 → 原始输出 → 正式评分**，断言实际请求题目、模型、few-shot 和选样；配置桥接测试必须消费生产生成的 runner-config。
2. 同时收敛证据边界：fd 安全读取、脱敏范围、完整 bytes、固定 Case 映射，以及冻结完成后即可恢复的持久引用。
3. 用真实 wrapper 做跨实例认领/取消；用真实存储竞争验证 revision 不可覆盖；为本轮指针和预算反例补正式测试。
4. 重跑 make check，将本地确定性 Runner 集成、离线测试、真实模型验收分别记录。真实模型与 PostgreSQL 等未执行项目继续明确标注 not_run。
