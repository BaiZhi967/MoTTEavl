# M8 剩余能力设计（待范围确认，不代表已实现）

目标：保持原 G01–G12/T00–T12/A01–A20，不以合成测试、改名或缩范围虚报闭合。
本轮已修既有一致性/证据保护漏洞；新架构与真实外部验收继续单列。

## 推荐边界

1. Harbor/外部 Runtime 先只接能够证明并强制模型调用硬上界的 Profile。
   不把 Task×Trial 或 wall time 当成模型调用硬上界。若需要非硬预算运行，需显式新合同与授权。
2. Judge 保持现有校准阈值与真实人审要求，补 pairwise 换序。不得原地降低阈值换取合格。
3. Trace 先归档后裁剪终态、未受保护 Run 的旧前缀；未知写入时间保留。
   保留期尚待选择，默认不得启用或实际删除任何用户 Trace。

## 外部 suite 组装

采用薄 assembler 返回冻结 manifest/case IDs/资源摘要/可证明预算，再复用现有 Cell、RunService、Worker。
C-Eval 复用 prepare_external_run_inputs/validate_external_run_request，固定数据 revision、scope、split、
few-shot、seed、Runner/Profile 与样本内容摘要，不走 catalog.latest。
Scenario/Skill 复用工作流/Target/Fixture 冻结与 plan_skill_ablation，新增 suite 专属配置或已发布 Profile 引用，
不开放任意 manifest 注入；no-skill 有明确身份。
Harbor 复用 build_run_inputs，Cell 派生 Run/Job ID，Trial 与实验重复分层，恢复不二次启动。
以上须离线 parity、API/CLI→Worker、取消/恢复/资源漂移负例；真实后端另验。

## Judge 公开校准闭环

新增不可变校准集、真实人工来源/权限、版本化报告与精确资格持久仓储；复用 ScoringJob 的校准 owner、预算、
调用落盘与不重复付费恢复。公共导入/审核→preflight→有界授权提交→Worker→报告→资格→正式 Gate 全链接通。
资格按所选 Pass 的 spec/rubric/model/calibration/policy 摘要查询，不能按名称或客户端标志继承。
本轮 Gate 已先 fail-closed，但没有虚构持久资格或调用模型。

## 独立统计发布

已增加 subject Case/Task 成本/耗时描述与输入摘要、JSON/JUnit。
下一步是 append-only 内容寻址 StatisticalReport 仓储与 GET/export，冻结实际计量 inputs、refs、policy、unit、k、
missing、seed/iterations；只固定 Run/Pass 不足以冻结可变 Case 计量。

## Trace retention

需服务端写入时间/索引，旧未知时间保留；保护 active/needs_review/imported、Baseline/Gate/报告 pin、评分事件引用。
维护锁内重验引用，先有可校验归档和审计，再事务裁剪。保留 seq high-water mark 或最高 seq 前缀规则；
SSE gap 与 events_snapshot.partial 必须反映实际裁剪。SQLite/PG、plan后新pin、崩溃、seq不回退、恢复均需真实测试。

## 授权与验证阻断

付费 L1–L5/Judge 调用、真实人审来源、私有旧导出、官方数据/Harbor真Agent、生产备份恢复/GC/切换、发布/合并
分别需要明确执行授权。本轮无新付费额度，旧六次额度已耗尽。
本云环境未提供 Docker/PG/Windows；对应真实组合、Compose、PG快照和升级/回退收据仍缺，不能由SQLite通过代替。
