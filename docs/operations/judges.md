# Judge 运维说明（M5）

状态按三层证据分别记录，不再混用：

| 能力 | 代码已存在 | 已接公共入口 | 已验证 |
|---|---|---|---|
| T09a 契约、输入 allowlist、预检 | 是 | 无（库内 API） | 单元/行为测试 |
| T09b 持久 ScoringJob、judge 调用账本、取消 | 是 | 无（等待 R8 接线） | 单元/行为测试（SQLite + memory；真实 PG 需 `MOTTE_PG_DSN`） |
| T09c 原子发布 ScoreSet + pass + receipt + current | 是 | 无（等待 R8 接线） | 单元/行为测试 |
| T10 校准报告、资格登记、人工修订 | 是 | 无 | 单元/行为测试；**真实人工校准资料仍缺** |
| API/CLI 提交、只读查询、幂等取消 | 否 | 否 | 未验证 |

“无公共入口”指今天没有 `/api/v1/judges*` 路由与 Worker 领取接线；不能把库内
测试通过当作已交付的用户能力。真实付费调用与真实至少 30 条人工复核资料均未发生，
Judge 保持 experimental。

## 1. 调用计划与预算（R2 固定）

- `ScoringJobService.submit()` 先编译**唯一冻结计划** `plans`，每项含
  `call_id / owner(case) / mode / repeat_index / presentation_order / input_sha256 /
  budget_sha256 / reservation`；预检的 `max_calls` 就等于 `len(plans)`，不存在第二个
  公式。pairwise 未显式给 `presentation_orders` 时每个 pair 只按自己的顺序评一次，
  不再笛卡尔扩展；`repeats=N` 产生 N 次独立调用与 N 行 ScoreSet（`trial_id=call_id`），
  `save_policy=append_pass_append_trials` 保证后一次运行只追加新 pass。
- prompt token 上界来自**真实渲染请求**的 UTF-8 字节数（字节级 BPE 每个 token 至少
  一个字节，因此是上界）；输出上限取 `spec.parameters.max_output_tokens` 与
  `budget.max_completion_tokens // calls` 的较小值，并作为 `max_output_tokens` 写进真实请求。
- 金额硬上限只有在「价格已知 + prompt 上界可证明 + Provider 强制执行输出上限 +
  估算不超过声明上限」同时成立时才成立；否则硬预算请求在提交期被拒绝（`JudgeBudgetError`），
  估算绝不冒充硬上限。`JudgeProviderPolicy.prompt_token_bound` /
  `enforces_output_limit` 是声明这两项能力的地方。
- 每次 dispatch 在同一个存储事务里原子预留额度（call/token/cost）；已发出未结算的
  调用占用额度且**绝不重发**（重复 `call_id` 被 `begin_call` 拒绝）。费用与 usage
  只在全部已结算调用都有值时才累计，任一次未知就让总额保持未知。

## 2. 证据归属与人工修订（R3 固定）

- subject 作业必须绑定**保存过的** Run 与同属该 Run 的 `source_pass_id`；Observation
  的 `run_id/case_id`（以及给出时的 `attempt_id`）必须与请求键和目标 Run 一致；
  pairwise 的两个候选必须是同一 Run 的 subject 候选。任何不匹配都在提交期拒绝：
  零调用、零发布，current 与原 subject 证据不变。
- calibration owner 使用 `calibration:<job>` 独立命名空间，绝不写入 subject Run。
- 人工修订的 CAS 依据是 `(Run revision, current_scoring_pass_id)` 这一对，且**先读
  Run revision、再读 current**：任何改动 current 的写入都会在同一事务里推进 Run
  revision，append 在该事务里核对 revision，因此 revision 未变蕴含 current 未变。
  冲突时整个事务回滚（无半个 ScoreSet、孤立 pass 或伪成功事件），失败方得到
  `ManualRevisionConflict`。Memory/SQLite 由上述事务语义保证；真实 PostgreSQL 需
  设置 `MOTTE_PG_DSN` 才会执行对应测试（未设置时标 not_run）。
- 校准资格消费**完整覆盖结果 + 明确政策阈值**（`CalibrationPolicy` 的五类样本下限、
  重复稳定率、换序一致率、拒答/缺证据率）；覆盖不足或阈值不达标一律 experimental、
  `gate_eligible=false` 并记录具体原因。政策阈值摘要 `policy_sha256` 进入报告与资格
  记录：阈值变化、model/rubric/spec hash 变化都不会继承旧资格。资格读取走真实发布的
  `pass.judge`（含 `rubric_id/rubric_version/spec_sha256/calibration_version`）。

## 3. 不可协商的约束

- GET、compare、gate、历史切换与普通离线 rescore **永远零模型调用**。
- Judge 是独立授权作业：在 Worker 中执行，不在 API 请求内付费运行。
- 待执行/失败/部分证据只留在 job，不进入终态 pass 表；终态 ScoreSet + 预分配
  ScoringPass + job receipt + current 指针在同一事务 CAS 提交。
- 不确定时不自动重发：已 dispatch 的失败没有「未处理」的证明就保持不确定计费/结果
  （Provider 错误分类复用 `motte_provider.errors`），原始 subject Run 的终态与证据不因
  Judge 故障改写。
- 人工修订只追加新 pass 并保留依据；新 rubric 不继承旧校准，未校准 Judge 为
  experimental，不进阻断门禁。
- `submit()` 在 `provider_factory is None` 时**明确拒绝**提交。这条保护必须保留：
  不能为了方便接线而取消它，否则会产出永远无法执行的作业。

## 4. 接线前剩余的前提

T09 给出的接线面是：WorkerLoop 里构造 ScoringJobService、恢复时调
`recover_interrupted()`、无 Run 可领时调 `claim_and_run()`。
`RunService.__init__` 只持有 `store`，**没有资源仓库**（model profile /
provider connection / price table），所以 Worker 侧今天拿不到按 JudgeSpec 固定模型
解析连接的入口。R8 的选择 A 仍适用：在**提交期**由 API 用资源仓库解析出 Judge 的
provider 与价格快照并冻结进请求/作业，Worker 只按快照构造 Provider。无论选哪种，
都必须保持“GET 与普通离线 rescore 零调用”以及“pending/failed job 不进终态 pass 表”。
