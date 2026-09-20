# M2 第四轮代码审查（2026-09-20）

> 后续修复与复验见 [M2-R4 修复记录](M2-R4-fixes-2026-09-20.md)。下文保留修复前的审查结论与复现证据。

审查范围：`e454081..7774f42`，一个修复提交、13 个变更文件。依据第三轮 R3-01–11，复验生产配置、证据恢复、数据不可变与相邻回归。

**结论：暂不通过，尚不满足进入 M3 规划/开发交接的前置条件。** 确认 2 项 P1、2 项 P2，另有 1 项 P3 回归。M3 复用外部 Job 与证据基础设施，当前恢复缺陷会直接影响后续阶段。

本轮只审查、离线复现并新增本文；未修改业务代码、未提交或推送。

## 验证证据与边界

- `make check`：退出码 0。Python **1218 passed / 18 skipped / 2 warnings**，46.98s；Web **14 files / 187 passed**；Ruff、contracts 类型检查、compileall、Web build、bridge selftest、Compose config、OpenAPI 一致性检查通过。
- 补充复现：生产配置生成、固定版 BaseDataset 构造函数的隔离执行、临时 SQLite/fake Runner 导入中断恢复、同 revision 不同准备参数的重启一致性、旧 snapshot 方法直接调用。
- PostgreSQL 项为 SQL 路径审查与异常注入，不是实际 PostgreSQL 并发集成；真实固定 Runner、本地确定性模型端点和真实模型均未运行。
- 使用 requesting-code-review 流程尝试独立 reviewer，调度仍返回 agent thread limit，本轮由主代理完成。

## P1

### M2-R4-01：生成配置仍不满足 OpenCompass 的实际构造与推理契约

位置：[entry.py:131](/Users/whitezhi/Dev/MoTTEavl/packages/benchmark-runtime/motte_benchmark/opencompass/entry.py:131)、[entry.py:159](/Users/whitezhi/Dev/MoTTEavl/packages/benchmark-runtime/motte_benchmark/opencompass/entry.py:159)。对应 R3-01/02。

未定义名称已经移除，导出的题干/选项也已补齐，但生成配置尚不能真正执行：

- dataset 项只有 `type/name/data_file/few_shot_file`，没有推理任务必需的 `infer_cfg`；也没有与 Parser 输出命名配套的 benchmark/subject abbr、reader/prompt/retriever 配置。
- `BaseDataset.__init__` 将配置 kwargs 传给 load；生成项传 `name/data_file`，而本地 load 签名是 `load(path, few_shot_file=None)`，参数不匹配。
- zero-shot 时 `str(few_shot_file)!r` 生成的是字符串 `'None'`，load 把它当路径打开。
- load 返回 Python list，而固定版 BaseDataset 的返回契约是 `Dataset/DatasetDict`；few-shot 示例仅塞入第一行数据，未接入推理模板/检索器。
- 普通只有 api_key 引用的模型配置被显式写入 `openai_api_base=''`，覆盖上游合法默认端点。端点不能靠空串兜底。

上游依据：[OpenCompass 0.4.2 BaseDataset](https://raw.githubusercontent.com/open-compass/opencompass/0.4.2/opencompass/datasets/base.py)、[推理任务读取 infer_cfg 的路径](https://raw.githubusercontent.com/open-compass/opencompass/0.4.2/opencompass/tasks/openicl_infer.py)。

本轮使用生产 `_inputs` 生成配置，隔离执行固定版 BaseDataset 类（模型依赖用无调用 stub 代替），输出：

```text
DATASET_KEYS ['data_file', 'few_shot_file', 'name', 'type']
MISSING_INFER_CONFIG true
DATASET_CONSTRUCT TypeError: LocalMCQDataset.load() got an unexpected keyword argument 'name'
NO_FEWSHOT_VALUE {'value': 'None', 'type': 'str'}
LOAD_NO_FEWSHOT FileNotFoundError: [Errno 2] No such file or directory: 'None'
MODEL_ENDPOINT ''
```

当前 `_assert_names_resolve` 只检查 AST 名称是否出现过，不执行构造或推理；字符串断言通过无法证明上游契约兼容。这些是已确认的代码缺口，不只是 live 尚未执行。

修复验收：生产创建入口产出的 config 必须在固定版环境完成加载、dataset/model 构造、zero-shot 与 few-shot 的真实推理流水线；使用本地确定性 HTTP 端点，断言最终请求内容/URL/模型/选样，验证实际产物经 Parser、评分、报告闭环。避免以新手写夹具替代生产输入。

### M2-R4-02：Artifact 恢复补齐导入后，会把原证据引用清空

位置：[external_jobs.py:591](/Users/whitezhi/Dev/MoTTEavl/packages/sdk-python/motte_sdk/external_jobs.py:591)、[external_jobs.py:599](/Users/whitezhi/Dev/MoTTEavl/packages/sdk-python/motte_sdk/external_jobs.py:599)。对应 R3-07 的新增恢复分支。

`recovery_reuse=True` 时先从 checkpoint 取 evidence，紧接着无条件执行 `evidence: dict[str, Any] = {}`。该分支又跳过重新构造证据，最终把空对象写回 checkpoint，并在返回的 import 中提供空 evidence。

已复现：第二次 import 注入模拟崩溃；完整 Artifact 和引用已落盘；删除工作目录后恢复。结果成功补齐 2 条且 `recovered_from='frozen-artifact'`，但：

```text
before_keys = [complete, frozen_before_parse, outcome_artifact, outcome_sha256,
               raw_bundle_artifact, raw_bundle_hash, raw_files]
after = {}
returned_import_evidence = {}
```

旧 Artifact 文件没有被删除，但 Job/report 失去正式关联。现有新增测试只断言记录齐全、Job 未重复启动，未断言恢复后证据引用不变。

修复验收：恢复分支直接保留原 evidence，并在补齐导入、最终化、再次恢复后都断言完整引用/hash 不变；原始文件仍可通过正式关联读取和重放。

## P2

### M2-R4-03：同文件不同准备参数被当成幂等，当前实例和重启后数据不一致

位置：[benchmark_catalog.py:532](/Users/whitezhi/Dev/MoTTEavl/packages/sdk-python/motte_sdk/benchmark_catalog.py:532)、[benchmark_datasets.py:58](/Users/whitezhi/Dev/MoTTEavl/packages/storage/motte_storage/benchmark_datasets.py:58)。对应 R3-11 的不可变性修复。

`put_immutable` 只以原始文件名/hash 判断身份，不含影响解析结果的准备参数。对于原始 JSONL 未写 split 的情况，相同字节用 `default_split=val` 或 `dev` 会生成不同 Dataset。第二次被判 identical、存储保留第一份；Catalog 忽略返回的 canonical record，仍将第二份准备结果写进内存。

已复现同 `ceval@rev-shared`、同 JSONL：先按 val 准备，再按 dev 准备，两次均成功；随后：

```text
current_catalog.dataset_splits = ('dev',)
new_catalog_same_store.dataset_splits = ('val',)
```

因此同一 revision 的预检/创建是否支持某 split 会随 API 实例或重启变化。该反例无需并发。

修复验收：不可变身份包含归一化样本、split 和影响语义的准备参数；真正 identical 时 Catalog 使用存储返回的固定记录。API 连续准备、第二实例和重启后读取应一致，语义不同的请求应显式冲突。

### M2-R4-04：PostgreSQL 首次并发插入不满足同内容幂等/异内容冲突契约

位置：[pg_audit_store.py:904](/Users/whitezhi/Dev/MoTTEavl/packages/storage/motte_storage/pg_audit_store.py:904)、[pg_audit_store.py:922](/Users/whitezhi/Dev/MoTTEavl/packages/storage/motte_storage/pg_audit_store.py:922)。对应 R3-11 的 PG 实现。

`SELECT ... FOR UPDATE` 只锁查到的行，不能锁尚不存在的 revision。两个事务同时查到空后都会 INSERT；后提交者受到唯一约束保护，但函数没有处理 UniqueViolation。即使两份内容相同，也不能返回 identical；异内容不能稳定转换成 RevisionConflictError。SDK/API 只捕获 ValueError，因此可能变成 500。[PostgreSQL 行锁语义](https://www.postgresql.org/docs/current/explicit-locking.html#LOCKING-ROWS)。

本轮用“SELECT 返回 None，INSERT 抛 UniqueViolation”的隔离异常注入验证：异常原样外抛，`isinstance(error, ValueError)=False`。没有声称运行过实际 PG 并发环境；并发触发条件由当前 SQL 与官方锁语义确定。

修复验收：实现原子插入/冲突后读取比较，或按 key 可靠串行化首次写入；实际 PG 两连接测试应保证同内容两个成功结果为 created/identical，异内容一个成功、一个明确冲突，均不返回裸数据库异常。

## P3

### M2-R4-05：保留的 snapshot_outputs 调用已删除的 _workspace

位置：[adapter.py:173](/Users/whitezhi/Dev/MoTTEavl/packages/benchmark-runtime/motte_benchmark/opencompass/adapter.py:173)。本轮移除 _workspace 后的遗留调用。

`snapshot_outputs` 仍执行 `self._workspace(handle).snapshot()`，但类中已无 `_workspace`，except 也不捕获 AttributeError。直接调用立即得到：

```text
AttributeError: 'CevalJobAdapter' object has no attribute '_workspace'
```

当前生产新采集路径优先 read_output_files，因此不是本轮主要阻塞项；保留的兼容/直接调用接口已经失效。

修复验收：基于新的 fd 安全读取实现快照，或在明确兼容策略下移除旧方法；不能通过恢复不安全的字符串路径读取来消除异常。

## 第三轮反馈复验状态

| 第三轮 | 本轮结果 |
|---|---|
| R3-01 生成配置不可执行 | 名称问题已修复，构造/推理契约仍阻塞，见 R4-01 |
| R3-02 空题干/选项 | 生产结构化字段与导出已补齐；few-shot 实际推理接入仍在 R4-01 范围 |
| R3-03 argv 身份 | wrapper 注入并保留 token、entry 消费身份的代码已接入；离线回归通过 |
| R3-04 fd 读取 | 新读取入口复用 _TrustedDir，原路径替换反例修复 |
| R3-05 configs 进入永久 bundle | 常规 configs 路径被证据清单排除；真实 Runner 的全产物脱敏验收仍未执行 |
| R3-06 冻结后映射变化 | 映射已进入同一 bundle，原反例修复 |
| R3-07 导入中断后无法补齐 | 从 Artifact 补齐已实现，但恢复会清空证据引用，见 R4-02 |
| R3-08 超预算仍完成 | 正式导入前返回 EVIDENCE_INCOMPLETE，原反例修复 |
| R3-09 实验指针被忽略 | 生产 collector 从冻结文件读指针并传入解析器，原反例修复 |
| R3-10 最长题目+few-shot 预算 | 改为逐个 selected case 渲染，原反例修复 |
| R3-11 revision 并发覆盖 | Memory/SQLite 事务语义已加强；准备语义身份与 PG 首次竞争仍有问题，见 R4-03/04 |

## M3 交接条件

本轮不宣称 M2 已通过，也不发出“可以开始 M3”的开发提示词。优先完成 R4-01 的真实固定 Runner→本地确定性端点闭环，以及 R4-02 的恢复证据不漂移；补齐两类存储反例后，再以新提交复审。

M3 规划的下一步仍是核对 `docs/roadmap/M3-harbor-and-terminal-bench.md`、既有详细计划和可复用 ExternalJob 能力；M2 通过后再形成开发顺序、验收清单和完整 Agent 提示词，避免把当前未闭合的基础能力当成已完成前置。
