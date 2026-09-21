# Judge 运维说明（M5）

状态按三层证据分别记录，不再混用：

| 能力 | 代码已存在 | 已接公共入口 | 已验证 |
|---|---|---|---|
| T09a 契约、输入 allowlist、预检 | 是 | 是（`POST /api/v1/judges/preflight`、`motte judge preflight`） | 单元/行为测试；API 预检零副作用（零作业、零调用） |
| T09b 持久 ScoringJob、judge 调用账本、取消 | 是 | 是（`POST/GET /api/v1/judges*`、`motte judge submit/status/history/cancel`、WorkerLoop 领取） | 单元/行为 + 集成测试（SQLite + memory；真实 PG 需 `MOTTE_PG_DSN`，未设置时 NOT VERIFIED） |
| T09c 原子发布 ScoreSet + pass + receipt + current | 是 | 是（Worker 执行后经 `GET /runs/{id}/scoring-passes`、`/report` 读取） | 单元/行为 + 集成测试（含发布事务中断与 CAS 冲突窗口） |
| T10 校准报告、资格登记、人工修订 | 是 | 库内（无独立公开入口） | 单元/行为测试；**真实人工校准资料仍缺** |
| R8 提交期冻结 Provider 快照 | 是 | 是（API/CLI 同一条编译路径） | 集成测试：资源改名/升级后旧作业仍用原快照；秘密明文不入库 |

"已接公共入口"指今天的真实路由与 Worker 领取接线；库内测试通过不等于用户能力已交付。
真实付费调用与真实至少 30 条人工复核资料均未发生，Judge 保持 experimental。
真实 PostgreSQL 未执行（`MOTTE_PG_DSN` 未设置）：PostgreSQL 语义 NOT VERIFIED。

## 0. 公共入口（R8 接线）

```text
POST /api/v1/judges/preflight   零费用预检：解析资源与证据，不落作业、不构造 Provider
POST /api/v1/judges             持久提交（202）；request_key 幂等，内容不同 → 409
GET  /api/v1/judges/{job_id}    作业视图（无输入原文与响应正文）
GET  /api/v1/runs/{run_id}/judge-jobs   评分历史（零模型调用）
POST /api/v1/judges/{job_id}/cancel     幂等取消

motte judge preflight --spec <json|@file> [--db PATH]
motte judge submit    --spec <json|@file> [--request-key K] [--db PATH]
motte judge status    <job_id> [--db PATH]
motte judge history   --run <run_id> [--db PATH]
motte judge cancel    <job_id> [--reason TEXT] [--db PATH]
```

API 与 CLI 调用同一个 `motte_sdk.scoring_jobs.build_judge_submission`：请求形状一致，
解析与冻结只实现一次。`mode=pairwise` 目前只在库内路径可用；公共入口返回
`JUDGE_MODE_UNSUPPORTED`，不假装已接线。

不可协商的边界：**GET、历史、取消、预检都不构造 Provider、不调用模型**。
API 进程即使持有 `FrozenProviderFactory` 也从不 dispatch；领取与执行只在
`WorkerLoop` 的执行锁内发生。`submit()` 在 `provider_factory is None` 时仍然**明确拒绝**
（API 映射为 503 `JUDGE_PROVIDER_UNAVAILABLE`），不产出永远无法执行的作业。

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
  公共入口不接受客户端填写的 `estimated_prompt_tokens` / `estimated_completion_tokens` /
  `sample_count`（schema 层拒绝未知字段）；计数只来自服务端解析出的证据。
- 金额硬上限只有在「价格已知 + prompt 上界可证明 + Provider 强制执行输出上限 +
  估算不超过声明上限」同时成立时才成立；否则硬预算请求在提交期被拒绝
  （`JudgeBudgetError` / `JUDGE_BUDGET_NOT_EXECUTABLE`），估算绝不冒充硬上限。
  `price_known` 与 `price_table_version` 也由服务端从解析出的价格表填写。
- 每次 dispatch 在同一个存储事务里原子预留额度（call/token/cost）；已发出未结算的
  调用占用额度且**绝不重发**（重复 `call_id` 被 `begin_call` 拒绝）。费用与 usage
  只在全部已结算调用都有值时才累计，任一次未知就让总额保持未知；崩溃恢复把
  「暂无支出」的 0.0 改回 `None`，不确定作业不会假装零费用。

## 2. 证据归属与人工修订（R3 固定）

- subject 作业必须绑定**保存过的** Run 与同属该 Run 的 `source_pass_id`；Observation
  的 `run_id/case_id`（以及给出时的 `attempt_id`）必须与请求键和目标 Run 一致；
  pairwise 的两个候选必须是同一 Run 的 subject 候选。任何不匹配都在提交期拒绝：
  零调用、零发布，current 与原 subject 证据不变。
- R8 起，公共入口的证据**由服务端解析**：`resolve_saved_observations()` 只读
  `case_runs` 里保存的 Observation，校验 `FrozenObservation` 契约、`evidence_hash`
  与 run/case 归属后才进入请求。缺失或不匹配 → 422 `JUDGE_EVIDENCE_MISSING` /
  `JUDGE_EVIDENCE_INVALID`，客户端无法提交 observation 内容或计数。
- calibration owner 使用 `calibration:<job>` 独立命名空间，绝不写入 subject Run；
  校准样本没有服务端存储，因此校准作业仍留在库内路径，公共入口只接 subject 作业。
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

## 3. 提交期冻结的 Provider 快照（R8 选择 A）

- `model` 在公共入口里是**已发布的 ModelProfile id**。提交时用既有
  `resolve_manifest` 解析它：生命周期（draft/deprecated 拒绝）、enabled、provider
  连接、adapter 版本与价格表版本都在这里失败或成功；随后封存
  `JudgeProviderSnapshot`（内容寻址 `snapshot_sha256`）：

```text
model_resource_id / model_profile_generation / model_profile_sha256
provider_connection / provider_connection_generation / provider_connection_sha256
adapter_id / adapter_version / endpoint / request_path
model(线路模型名) / parameters / max_output_tokens / reasoning / identity_*
credential_ref(profile 名) / api_key_env(变量名)      # 只是引用，绝不是密钥
price_table_version / price_table_sha256 / price_table / transport / frozen_at
```

- **执行期只按快照构造 Provider**（`FrozenProviderFactory`）：`provider_factory` 的
  参数契约从「模型字符串」升级为「冻结快照」，`_snapshot_for()` 还会核对
  `snapshot.model == spec.model`。Worker 不持有资源仓库，也不会按可变名称重新选模型。
- 秘密只在**构造 Provider 时**经 `motte_provider` 凭据链解析（`credential_ref` →
  凭据文件 profile → `api_key_env` 环境变量）；快照与作业记录只保存引用，
  `JudgeProviderSnapshot` 自带 `find_secret_paths` 校验，任何凭据形状的键都会让
  封存失败。集成测试断言真实密钥明文不出现在 job / pass / invocation / event /
  report / Worker 日志里。
- 快照身份进入请求 fingerprint：资源变了，同一 `request_key` 就是不同内容（409），
  不会静默复用旧快照。`frozen_at` 与 `recorded_at` 一样只是审计时间，不参与 hash，
  因此同内容重复提交仍然幂等。
- 非 HTTP adapter（如 `replay`）没有 `.complete`，不能作为 Judge Provider，提交期
  返回 `JUDGE_ADAPTER_UNSUPPORTED`；冻结的 `adapter_version` 与安装版本不一致时
  构造 Provider 会失败（pinned 版本门）。

## 4. Worker 接线与领取顺序（R8）

- `WorkerLoop` 持有 `ScoringJobService`（默认 `FrozenProviderFactory`；显式传
  `scoring_jobs=None` 可关闭 Judge 领取）。恢复、领取与执行都在**既有执行锁**内：
  `recover_interrupted()` 先恢复 Judge 作业（prepared 回队列、dispatching 标不确定），
  再恢复 Run。
- 领取顺序：每一轮**先**领取至多一个 Judge 作业，**再**领取一个 Run；两类队列在同一轮
  都推进，任何一类都不会因为另一类持续入队而永远得不到调度。显式 `run_id` 时只领该 Run。
  集成测试同时覆盖「Run 持续入队时 Judge 仍被调度」与「Judge 持续提交时 Run 仍完成」。
- 崩溃窗口（prepared / dispatching / 响应已持久化 / 发布事务中断 / 通知丢失）都有
  集成测试：不重复计费（同一 `call_id` 绝不重发）、终态 ScoreSet+pass+receipt+current
  原子可见、pending/failed/indeterminate **不推进 current**，`--once` 会继续处理下一个
  可领作业。

## 5. 不可协商的约束

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
- 公共读取返回的 pass 视图保留完整 Judge 身份（`purpose` / `job_id` / `judge` 的
  rubric、spec、calibration、owner、`provider_snapshot_sha256`）；契约模型 `ScoringPass`
  是 `extra="forbid"` 且没有这些字段，直接用它序列化会丢掉身份甚至 500，因此 API 用
  `ScoringPassView`（`extra="allow"`）投影。

## 6. 已知阻断与未验收项

- **跨包阻断（R9）**：多指标 Judge pass 走 `POST /api/v1/gates` 会抛未处理的
  `ValueError: attempted 3 exceeds selected 1`（3 条指标 ScoreSet 行被当成 3 个
  attempted case）。位置：`motte_eval/coverage.py:31`，由
  `motte_sdk/comparisons.py:376`（`candidate_summary`，attempted 在 `:355` 按 ScoreSet
  行数计）经 `:430`（`evaluate_gate`）调用。`coverage.py` / `comparisons.py` 不在 R8
  范围，未修、未绕行：`tests/api/test_judge_flow.py` 用运行时 `pytest.xfail` 精确记录
  该失败，只有同一错误才被标 xfail。
- pairwise 的公共提交、校准作业的公共提交、校准报告 API 均未接线（库内路径可用）。
- 真实付费调用未发生（本环境无授权）；真实至少 30 条人工复核校准资料仍缺；
  真实 PostgreSQL 未验证。