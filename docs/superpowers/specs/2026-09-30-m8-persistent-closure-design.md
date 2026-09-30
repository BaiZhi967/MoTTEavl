# M8 新增持久能力设计：校准、统计发布、Trace 保留与 suite 装配

状态：供审阅；不代表实现或真实验收通过。接续已验证修复提交 `545fd46`。

## 已批准目标与边界

用户已批准按推荐方式修复并提交 PR：外部 Runtime 只接受可强制模型调用硬上界；
Judge 阈值保持现状、验证 pairwise 换序、正式门禁拒绝未校准配置；
Trace 先归档后裁剪终态未受保护旧记录，未知时间保留。
本设计取未指定保留期时的安全默认：机制可配置，但自动裁剪关闭、不预设 30/90 天。
没有付费模型、真实人审样本、私有导出、生产操作、合并或部署授权。

现有库有校准纯函数与 calibration-owned ScoringJobs，尚无公开校准仓储/生命周期；
统计只有动态只读结果与导出；Trace GC 目前不裁剪数据库记录。
这些是新增持久接口。已复现的既有换序校验漏洞可独立修复，不依赖本设计。

## 方案选择

采用现有单服务/单存储后端内的薄服务、不可变仓储和显式操作，复用现有 Worker、
ScoringJob、维护所有权与人工来源合同。不引入新的任务系统、外部对象存储或认证体系。
不采用“直接把客户端 qualified=true 写入门禁”或“按 TTL 删除事件”的捷径。
实现顺序：统计发布 → 校准生命周期 → Trace 保留；suite 装配独立实现。
每个子项目独立测试和提交，未实现或未验证的组合继续明确拒绝。

## A. StatisticalReport 不可变发布

- 保留 GET `/api/v1/comparisons/statistics` 的只读动态视图。
- 新增显式 POST `/api/v1/statistical-reports`，由服务器运行现有固定 Pass 统计计算，
  将完整结果封成版本化报告；GET `/api/v1/statistical-reports/{id}` 与 `format=json|junit`
  只读取已存正文，不再次计算或追随 current Pass。
- ID 来自规范化正文摘要。正文包括 refs、实际 Case/Task 计量输入、input_digest、
  统计政策、unit/k、missing、seed/iterations、算法版本和结果；服务端发布时间独立存储，
  不使相同正文产生不同 ID。同内容幂等，异内容同 ID 拒绝。无更新/删除接口。
- 仓储提供 put/get/list，Memory/SQLite/PostgreSQL 相同语义。新表进入迁移、维护写屏障、
  备份快照；refs 纳入统一 Run/Pass/Artifact 保护，不能只保护导出文件。
- CLI/SDK 提供 publish/get/export；现有 compare --statistics 保持不写入的兼容行为。
- 验收：发布后 current/计量漂移不改变正文；同内容重放；并发发布；篡改摘要；
  无效/缺失计量仍保留；JSON/JUnit 同一正文；GC/rollback/backup 保护引用。

## B. Judge 公开持久校准闭环

- 复用 `motte_eval.calibration` 的 CalibrationSample/Set/Report/Qualification 与固定政策。
  不可变仓储分别保存校准集版本、人工复核记录、执行请求、报告及资格来源。
- 公开服务流为 import → review → preflight → submit → Worker → report → qualification。
  `/api/v1/judge-calibrations` 及其 version/reviews/preflight/jobs/reports 子资源暴露此流，
  CLI/SDK 对应调用。import 不执行模型，preflight 不写作业/不执行模型。
- 人工来源仍由明确操作人提供并留审计：annotator/reviewer/time/原因/样本摘要。
  synthetic_candidate 不能通过软件路径改成 human_reviewed；只接受现有来源合同的真实
  人工输入。沿用部署现有认证边界；不声称仅凭姓名字段能验证真人身份，不创建新凭据。
- 服务端从持久校准集生成计划和 observation/pair 所有权；不接受客户端报告或资格标记。
  复用 calibration-owned ScoringJobs、冻结 Provider 快照、预算编译和不重复付费恢复。
  pairwise 每个样本对固定候选身份，正反序为两个不同调用；重复评分也独立计量。
- 发布报告必须从该 Job 的实际终态调用账本及已冻结校准集构造，验证完整计划覆盖、
  所有权、输入摘要、调用唯一性、成功状态及真正的换序。未知/失败/重复/缺项保持不合格。
- 资格按所选 Pass 的 JudgeSpec、rubric、model/provider 快照、校准集、政策与报告摘要
  精确绑定；配置变化不继承。Gate 检查服务端存储的完整来源链；缺失、错配或篡改 fail-closed。
  人工修订继续追溯源 Pass，不把修改 current 当作获得校准资格。
- 测试仅用明确标注的人工构造 fixture 与 scripted Provider，证明软件路径；不能据此
  宣称真实人审或真实模型校准验收完成。真实执行另需额度与人审来源授权。
- 验收覆盖伪造 reviewed/qualified 标记、错 owner、不同版本/政策、部分报告、崩溃恢复、
  换序重复/未换/失败调用、旧 Pass 不继承新资格、API→Worker→Gate、三存储后端。

## C. Trace 保留机制（默认关闭）

- 配置须显式 `enabled` 与正整数 `retention_days`；默认 disabled、无默认天数。
  初版仅提供显式 plan/apply CLI/SDK，不接自动定时器。本轮 apply 只在合成测试库运行。
- Trace 新增服务器写入时间；迁移时旧事件置 unknown，不能用不可信 payload 时间回填。
  写入由仓储统一赋时，调用者不能指定用于保留判断的服务器时间。
- 只选 ended Run 的连续最老前缀，并且每行都早于截止时间、时间已知。
  保留每个 Run 最高 seq 的事件，保证未来分配从 max(seq)+1 开始、不复用序号。
  Memory 同样使用 max(seq)+1，不能用剩余列表长度。未知时间阻断前缀继续推进。
- active/needs_review/imported Run、Baseline/Gate/StatisticalReport pin、评分事件引用及
  其他统一引用均保护；任何引用枚举错误拒绝执行。plan 记录候选事件摘要和截止时间。
- apply 先取得现有维护所有权并排除 Worker，重读状态/pin/事件摘要；变化即失效。
  不通过 reason 字符串或全局 allow-delete 标记绕过写屏障。
- 归档为内容寻址、不可覆盖的规范化文件，保存事件完整原文、seq 范围、数量、摘要、
  嵌套 artifact refs 和服务端时间。独占维护 owner 专属写入路径，写后读回校验及持久化
  成功才可进入 DB 裁剪事务；缺磁盘/损坏/路径逃逸/同名异内容均零删除。
- 专属 owner-bound DB 事务原子记录 archive receipt 与裁剪前缀；SQLite 与 PG 都必须
  对其它写入维持屏障。PG 保持锁与删除使用同一受控连接，避免 SHARE 自阻塞。
- 归档保留在统一 artifact 引用闭包内，以后 backup/GC/rollback 仍保护被裁剪事件的证据。
  崩溃最多产生未引用归档或已提交一致收据；不得产生删除成功却无归档的状态。
- SSE 的 gap 与 events_snapshot.partial 从保留元数据得出，历史 cursor 不能静默表示完整。
  v1 不提供恢复到活动事件流或归档到期删除；备份恢复必须验证归档与收据一致。
- 验收：默认关闭、旧未知时间、混合时间前缀、plan 后新 pin、活动状态改变、归档失败、
  归档后崩溃、事务回滚、两进程竞争、seq 不回退、SSE/快照缺口、恢复与备份引用闭包。

## D. 外部 suite 组装

- 用每 suite 的类型化配置/已发布 Profile 引用扩展现有 ExperimentSpec；不接受任意 manifest。
  assembler 只产生现有 PreparedRun 冻结快照、case IDs、资源摘要、可证明预算。
- C-Eval 复用独立 run 的 prepare/validate 路径，冻结 dataset revision、scope、split、
  few-shot、seed、Runner/Profile、实际样本内容摘要，禁止执行时读取 latest。实际 OpenCompass transport/operator 重试须全部计入可执行上界；
  上游行为或固定 Profile 无法证明时继续拒绝，不能仅按 Case 数估算。
- Scenario/Skill 复用 Workflow/Target/Fixture 冻结与现有消融 planner，首版仅 builtin-agent Target，
  no-skill 保持明确身份；
  真正消费的 factors 才能放行。Cell 的重复与 suite 内 Trial 区分，分配/取消/恢复保持幂等。
- Harbor 现有 Trial/retry/deadline 不是模型调用硬上界。除非实际 Runtime/Profile 的执行路径
  提供且强制该上界（包括 Provider 内部重试），否则继续给出明确 unsupported，不能靠声明字段或乘法推算放行。
  新增可执行 profile 是另外的集成门槛，不能凭合成测试宣称真实 Harbor 已支持。
- 验收：每 suite 与 standalone 冻结 parity；API/CLI→Worker 离线完整路径；资源漂移、
  不消费 factors、预算不足、重复执行、取消/恢复、矩阵原子发布。真实外部 runner 另验。

## 交付与真实门槛

更新数据库迁移、API/OpenAPI/SDK/CLI、协议文档和细粒度测试；每子项目完成后独立审查。
最终稳定源码统一运行 make check、独立 OpenAPI/打包/安全审计；缺失环境精确列出。
提交草稿 PR 后验证远端 HEAD，并等待对应 CI 全部终态，修复范围内可恢复失败。
本地无 Docker/PG/Windows 不是放弃这些门槛：Linux CI 可补真实 PG/Docker 证据，
Windows 与真实外部模型/人审/官方数据仍单列。PR 不等于 M8 全部验收关闭。
