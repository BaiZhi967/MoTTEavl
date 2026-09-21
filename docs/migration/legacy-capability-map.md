# 旧平台能力映射（legacy capability map）

> M7-T12 交付物。本表把旧评测平台（参考 commit `b661bcdf`）的主线能力映射到
> MoTTEavl 的对应机制，并标注迁移状态：`covered`（新平台等价或更强）、
> `imported_read_only`（历史只读导入，不参与新执行）、`gap`（未迁移，需明确
> 决策）、`dropped`（有意不迁移）。切换验收以本表 + 三类替代场景为准。

| 旧平台能力 | MoTTEavl 对应 | 状态 | 说明 |
|---|---|---|---|
| 模型评测 Run（数据/Profile/逐题调用） | Run + CaseRun + ScoringPass + RunReportRef | covered | 替代场景 1（C-Eval 双模型）；旧 Run 只读导入（imported origin，不可分发，永不入队执行） |
| 聚合分数（如 accuracy=0.5） | legacy summary | imported_read_only | 只有聚合时保存 `legacy_summary`，不伪造逐 case 分数（协议 §5.3） |
| 逐题分数 | ScoreSet（多指标复合键） | covered | 原始/归一化分数与状态都保留并参与对账 |
| Trace / 调用日志 | TraceEvent（持久 seq，SSE 游标） | covered | 旧 trace 作为 imported 事件只读保留 |
| Artifact（产物文件） | ArtifactStore + 内容 hash 校验 | covered | 导入时按声明 sha256 暂存校验后才关联 |
| Baseline / 基线对比 | M6 Baseline v2（immutable + 默认指针） | covered | 旧系统基线只能作为历史结论导入，不自动成为新平台正式 Baseline |
| Gate / 门禁 | GatePolicy 版本化 + GateResult + 退出码 0–6 | covered | 旧系统 pass 不等于新平台 pass（证据资格需重新满足） |
| CLI 触发评测 | motte_cli（local/server 双模式） | covered | 远端不可用非零退出，不静默回退本地 |
| pytest/CI 门禁 | motte_sdk pytest plugin（opt-in 读取导出 JSON） | covered | 普通 pytest 零模型/零 Judge/零 Run（A06） |
| SDK 客户端 | motte_sdk.MotteClient（同步、typed、幂等） | covered | 旧 SDK 路径/权限模型/资源 ID 不直接复制 |
| 凭据管理 | credentials.toml + api_key_env 引用 | covered | 导入只迁引用并输出 rebind 清单，永不复制密钥值 |
| 多租户 / RBAC | —（有意不做） | dropped | 单用户平台；远程访问 = token + TLS（协议 §10） |
| 旧 DSL 工作流 | M5 受控转换（无法表达的字段保留诊断） | covered（受控） | 不静默删除分支/循环/checker 后发布可执行版本 |
| 进行中旧任务迁移 | —（有意不做） | dropped | dry-run 即 rejected：`in_flight_job_not_importable`；不跨执行器接管付费请求 |
| 长期双写/双库同步 | —（有意不做） | dropped | 切换后新任务单路进入新平台，旧库只读保留 |
| 旧平台自动归档 | —（有意不做） | dropped | 归档是另外的显式操作，不在本计划内 |

## 导入次序与账本

配置 → Dataset/Case → Scenario/Skill → 历史 Run/Trial/Trace/Artifact/Score/Baseline。
每条记录以 `mapping_key = sha256(source_system|record_type|source_id|source_version|
transform_version)` 幂等；重复导入 reused、同键异内容 conflict、中断按 checkpoint
恢复、回退只撤销本批未共享对象（协议 §5.4）。

## 未迁移能力分类

`gap` 项：无（当前清单内所有旧主线能力均有 covered/imported_read_only/dropped
归类）。若真实旧导出核对发现新 gap，必须先登记本表并给出分类，才能继续 apply。
