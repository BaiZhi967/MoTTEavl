# Judge 运维说明（M5 / M8）

状态按三层证据分别记录，不再混用：

| 能力 | 代码已存在 | 已接公共入口 | 已验证 |
|---|---|---|---|
| T09a 契约、输入 allowlist、预检 | 是 | 是（`POST /api/v1/judges/preflight`、`motte judge preflight`） | 单元/行为测试；API 预检零副作用（零作业、零调用） |
| T09b 持久 ScoringJob、judge 调用账本、取消 | 是 | 是（`POST/GET /api/v1/judges*`、`motte judge submit/status/history/cancel`、WorkerLoop 领取） | 单元/行为 + 集成测试（SQLite + memory；真实 PG 需 `MOTTE_PG_DSN`，未设置时 NOT VERIFIED） |
| T09c 原子发布 ScoreSet + pass + receipt + current | 是 | 是（Worker 执行后经 `GET /runs/{id}/scoring-passes`、`/report` 读取） | 单元/行为 + 集成测试（含发布事务中断与 CAS 冲突窗口） |
| T10 校准报告、资格登记、人工修订 | 是 | M8 持久校准公开入口见第 7 节；人工修订保留源 Pass | Memory/SQLite/隔离 PostgreSQL 软件 fixture；**真实人工校准资料仍缺** |
| R8 提交期冻结 Provider 快照 | 是 | 是（API/CLI 同一条编译路径） | 集成测试：资源改名/升级后旧作业仍用原快照；秘密明文不入库 |

"已接公共入口"指今天的真实路由与 Worker 领取接线；库内测试通过不等于用户能力已交付。
真实付费调用与真实至少 30 条人工复核资料均未发生。只有可验证的固定资格来源能满足资格规则；
没有来源时保持 experimental。隔离 PostgreSQL 16.15 已验证软件生命周期，不代表生产或真人验收。

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
解析与冻结只实现一次。`mode=pairwise` 接收同 Run、同 Case 的保存 attempt 引用
`pairwise_refs`，可显式选择 `qualification_id`；不接收客户端候选正文或资格声明。

不可协商的边界：**GET、历史、取消、预检都不构造 Provider、不调用模型**。
API 进程即使持有 `FrozenProviderFactory` 也从不 dispatch；领取与执行只在
`WorkerLoop` 的执行锁内发生。`submit()` 在 `provider_factory is None` 时仍然**明确拒绝**
（API 映射为 503 `JUDGE_PROVIDER_UNAVAILABLE`），不产出永远无法执行的作业。

### 0.1 公共请求形状（照抄即用）

公共入口接受的**不是**库内 JudgeSpec 的序列化。直接提交 JudgeSpec.model_dump()
会得到 422 extra_forbidden（mode / profile_sha256 / prompt_id / spec_sha256 /
budget.price_known / budget.price_table_version / schema_version 都由服务端解析）。
正确形状（apps/api/app/schemas.py 的 JudgeSubmissionBase）：

    {
      "run_id": "run-...",
      "mode": "single",
      "spec": {
        "judge_profile_id": "acc-judge-profile",
        "model": "deepseek-v4.1-flash",
        "rubric_id": "answer-quality",
        "rubric_version": "1",
        "criteria": ["task_completion", "constraint_adherence", "evidence_grounding"],
        "budget": {"max_calls": 1}
      },
      "case_ids": [],
      "authorisation": {"authorised": true, "actor": "operator", "max_calls": 1},
      "publish_policy": "all_scored",
      "repeats": 1,
      "request_key": "explicit-idempotency-key"
    }

* model 是**已发布的 ModelProfile id**，不是线路模型名；服务端据此冻结 Provider 快照。
* criteria 省略时取 rubric 的全部判据；rubric 的必选判据不能被省略。
* 预检与提交共用同一形状；提交额外要求 request_key（或 CLI 的 --request-key）。
* 证据由服务端解析：subject Run 的冻结证据取自 result.frozen_observation（Scenario
  Run）或 result.observation（agent / CLI / Pi / Inspect）；客户端不能提交计数或原文。
* 校验失败在提交期返回具名 code 且零调用、零发布（JUDGE_EVIDENCE_MISSING /
  JUDGE_EVIDENCE_INVALID / JUDGE_BUDGET_NOT_EXECUTABLE / JUDGE_MODE_UNSUPPORTED）。

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

- **多指标 Gate 覆盖（已修复，commit 8055516）**：多指标 Judge pass 走
  `POST /api/v1/gates` 曾抛未处理的 `ValueError: attempted 3 exceeds selected 1`
  （3 条指标 ScoreSet 行被当成 3 个 attempted case）。修复在调用方
  `motte_sdk/comparisons.py`：`denominator=False` 的行不计入分母，attempted 按
  **distinct case_id** 计，且一个 Case 只有在其全部计入分母的指标都通过时才算通过
  （与 `goal-achieved` 的合取一致，accuracy 保持比例 <= 1）。
  `motte_eval/coverage.py` 有意不改：它的 "attempted 超过 selected" 拒绝是正确的
  不变量，之前是调用方在说谎。`tests/api/test_judge_flow.py` 的 xfail 钉已移除，
  改为真实断言，并保留两个真实拒绝反例（覆盖不足 0.5、以及三条指标中一条失败
  => accuracy=0.0 且 Gate 拒绝）。
- pairwise 公共提交与持久校准生命周期的当前状态见第 7 节；下文保留早期补丁背景。
- 真实付费调用未发生（本环境无授权）；真实至少 30 条人工复核校准资料仍缺；
  三后端软件 fixture 验证与真实模型/真人验收必须分开记录。
## M8 Gate 资格安全边界（2026-09-30）

公共 Lite 与版本化 Gate 均由服务端解析**所选固定 Pass** 的 Judge 身份及人工修订来源链，
不信任请求中的 `judge_qualified` 或政策自报 `experimental_evidence=false`。
早期补丁在持久校准仓储尚未接线时，将 Judge 与其人工修订来源链限制为诊断证据（当前状态见第 7 节）：
Lite 保留 Case 覆盖与指标并追加失败的 `judge_qualification` 规则；版本化 Gate 返回
`insufficient_evidence` / exit 5（包括 diagnostic 政策，不将诊断误标通过）。
独立 Baseline 所选 Judge Pass 同样受此资格前置约束。缺失/循环修订来源链也 fail-closed。

版本化引擎升为 `gate-engine@2`，资格投影纳入 evaluation input hash，避免旧引擎已缓存的
通过结论与新拒绝结论共用同一个结果身份；旧记录不改写。
此补丁关闭“未校准也能正式放行”的路径，不代表真人校准、pairwise 换序、公开提交/报告及
持久资格闭环已完成。原校准阈值不变，不产生模型调用，不伪造合格登记。

### 库内换序证据校验补强（2026-09-30）

校准报告的换序一致率只接受同一对稳定候选身份、真正相反展示顺序、两次成功且输出有效的
独立调用。重复方向、孤立方向、复用 invocation 或 job/call 账本引用、候选集合变化、
候选对之外的 winner 均保留为无效测量，不再被覆盖或从分母中丢掉。
原固定合格阈值与有效调用的结果不变；费用仍逐调用统计。
这只修复库内统计校验，不证明外部提交的账本真实，也不新增公开校准提交、持久资格或真人复核。

### 库内 pairwise 缺证据与整体偏好（2026-09-30）

pairwise 与 single 一样核对 rubric 的 `evidence_required`：必需引用的判据没有实际白名单
证据时，输出 `missing_evidence` 并投影为既有 `insufficient_evidence` 指标；不要求证据的
判据仍可正常评分。仅声明平局不能绕过缺失判据。整体 `pairwise_preference` 只接受
`status=ok` 的完整有效结果，伪造引用、缺证据或缺判据不再生成 scored 整体偏好；
原始 winner 仅保留为诊断信息。此修复不改校准阈值，不启用公开校准或正式资格。

## 7. M8 持久校准公开生命周期

本节替代上文历史补丁中“公开校准/持久资格尚未接线”的状态描述。服务现已复用既有
`JudgeCalibrationService`、ScoringJobs 与 Worker，不增加队列、认证或模型执行入口。
HTTP、SDK 和 `motte judge-calibration` 的 local/server 模式使用同一生命周期。

### 来源与权限

- import 只接收 `CalibrationImportRequest`（`CalibrationImport` 的公开请求边界）：
  未复核的 `CalibrationSet` 和按 sample_id 索引的 pairs。
  `config.judge_spec` 必须钉住完整 JudgeSpec。内容摘要由现有合同校验，不接收外部报告、
  资格、计划、observation 或 owner。导入已经 reviewed 的版本会拒绝。
- review 接收 `expected_parent_sha256`、`new_version` 与 `HumanReviewInput[]`；保留 annotator、
  reviewer、带时区的 reviewed_at、reason 与明确 gold。服务端另外记录接收时间，原版本不改写。
  `synthetic_candidate` 不能被转成 `human_reviewed`。操作者姓名和时间只是**声明的来源**，
  不代表系统认证了真人、组织身份或复核质量；认证仍是部署已有的 Bearer-token 边界。
- 软件测试中的 human-input fixture 是人工构造协议数据，不是真实人审验收。没有付费调用、
  真实样本或生产执行的默认许可。
- 新公开导入的 calibration_id、version 和 review 的 new_version 必须是可寻址的单段标识：
  不接受 `/`、`\`、ASCII 控制字符、`%HH` 形式的百分号转义，或恰为 `.` / `..` 的值。
  使用原始名称，不要预先 URL 编码；SDK 会编码路径。普通 Unicode、空格、`?` / `#` 等
  可编码标点，以及不构成转义的 `%` 可原样使用。拒绝返回 422，且发生在持久写入/调用之前。
  服务不解码、规范化、改写名称或摘要。旧内部记录仍可通过内部服务、本地读取和目录读取；
  不为不可寻址的历史名称新增 HTTP 路由。sample_id、candidate_id 等其他标识不受此限制。

### 请求文件与命令

预检和提交共用严格请求：

```json
{
  "content_sha256": "sha256:<选定版本的64位摘要>",
  "request": {
    "request_key": "operator-chosen-stable-key",
    "spec_request": "按 CalibrationRunRequest 合同填写的 published model/rubric/budget 对象",
    "authorisation": "按 JudgeAuthorisation 合同填写的明确 actor/aggregate allowance 对象",
    "price_table_version": "选定价格版本",
    "expected_preflight_sha256": null
  }
}
```

上面的两个说明字符串必须替换为实际 typed JSON 对象；完整输入形状由 OpenAPI 的
`CalibrationExecuteRequest` / `CalibrationRunRequest` 和 SDK 同名模型给出。先预检，再把
返回的 `preflight_sha256` 放入 `request.expected_preflight_sha256` 提交，可拒绝预检后漂移。
**预检也需要拟提交的 request_key**，因为它确定计划命名空间，但不保留该 key，不写 Job。
同 key、同内容的提交重放只读已经保存的执行；变更内容返回 409，不重新编译或计费。

```sh
motte judge-calibration import --file import.json --db var/runs.db
motte judge-calibration review --id example --version imported --file review.json --db var/runs.db
motte judge-calibration preflight --id example --version reviewed --file request.json --db var/runs.db
motte judge-calibration submit --id example --version reviewed --file request.json --db var/runs.db
uv run python -m apps.worker.motte_worker --once
motte judge-calibration get --id example --job calexec-... --db var/runs.db
motte judge-calibration report --id example --job calexec-... --publish --db var/runs.db
motte judge-calibration report --id example --report calreport-... --db var/runs.db
motte judge-calibration qualification --id example --qualification calqual-... --db var/runs.db
```

远程模式对每条 calibration 命令使用 `--mode server --api-url URL`，不要传 `--db`；令牌沿用
`MOTTE_API_TOKEN`。远端失败不会退回本地执行。无 `--id` 的 `get` 列出校准目录；指定 `--id`
列版本，另加 `--version` 读固定版本。`report --job` 不带 `--publish` 只列已发布报告。
Worker 与 CLI 必须配置同一数据库；示例的默认路径可通过现有 MOTTE_DB_PATH/存储配置调整。

### 执行与读取

- import/review 不执行 Provider；preflight 不写 Job/Invocation，也不构造 Provider。
- HTTP submit 返回 202、execution_id、child_job_ids、各 child 状态和实际 aggregate allowance；
  API 请求不会运行 Worker。30 个 pairwise 样本固定 120 个调用，按 32/32/32/24 分组，
  原价格、调用/Token/金额上限和不自动重试规则不变。
- GET 只读已保存的版本/复核/执行/报告/资格；不会隐式发布报告或生成资格。
  `POST .../jobs/{execution_id}/reports`（CLI `report --publish`）才从真实持久账本显式发布，
  不接收任何报告内容；无 body 或空对象均可。报告按内容寻址，重复显式发布不增加模型调用。
- Job 摘要不暴露候选原文、响应正文或 Provider 配置。错误只返回稳定 code/message，
  不回显请求、任意字典键、非有限数值或堆栈。版本详情是明确读取的完整输入和来源记录。
- 子资源要求 calibration ID 与版本/执行/报告/资格实际所有者一致；错 owner 返回 404，
  内容/幂等冲突返回 409，不能借其他校准的版本标签或资格 ID。

### 资格与质量是两个条件

发布的合格报告可给出独立持久 qualification source。subject pairwise 请求仍必须显式携带
`qualification_id` 和同 Run、同 Case 的已保存 `pairwise_refs`，并与完整 JudgeSpec、rubric、
Provider、校准和政策摘要精确匹配；不能按当前配置或名称继承。
两种 Gate 分别验证该资格和独立质量指标；pairwise `passed`/`accuracy` 仍不可用，
也不计算 Boolean 每成功费用。新指标 `pairwise_challenger_score@1` 只读取所选 subject
Pass 的真实 Job/Invocation 账本和提交前冻结的 challenger/reference 角色。候选呈现次序
不决定角色；完整胜出/平局/失败分别计 1/0.5/0，先平均每对的调用，再等权平均计划中的配对。
缺失、重复、失败或无法确定的任一计划调用使整体质量不可用，不能丢弃配对或补成平局。
人工修订仍可继承完全匹配的来源资格，但不能借 edited passed=True 获得此质量指标。

### 显式绝对质量门槛

Lite `POST /api/v1/gates` 的 policy 必须明确包含
`{"metric":"pairwise_challenger_score@1","op":"gte","threshold":0.4}`。
版本化政策先通过 `POST /api/v1/gate-policies` 发布，再调用既有
`POST /api/v1/gates/versioned`（SDK `evaluate_gate_versioned` / CLI `gate evaluate`）。
质量规则必须写明 `kind=metric_threshold`、`metric_id=pairwise_challenger_score`、
`metric_version=1`、`operator=gte` 和有限非 Boolean 的 `[0,1]` threshold；也可在
metric_id 中完整写 `pairwise_challenger_score@1`。缺省业务阈值不存在，不能使用 warn-only、
diagnostic skip 或省略版本的质量规则。

上述 0.4 以及测试中的 0.6 仅为软件 fixture，不是推荐的生产发布阈值。真实发布门槛由操作员
按业务目标明确选择，原校准样本数、错误率及 repeat/swap 政策均不随质量阈值变化。

- 只有完整计划配对和全部调用都可验证时才可放行，coverage 必须是 1.0；
  Lite 不接受其他 required_coverage。版本化 coverage 规则必须显式指向相同指标/版本，
  min_coverage=1.0；正整数 min_samples 按 planned_pairs 计算，不按 Boolean case 数或调用数
- 资格/指标缺失：Lite passed=false；版本化 insufficient_evidence，CLI 退出 5
- 完整质量 0.5 与 fixture 阈值 0.6 比较为 quality_fail/退出 1；与 0.4 比较可 pass/退出 0，
  前提是确切校准资格有效且其他成本、安全等阻断规则也全部满足
- v1 不支持 baseline_id/baseline_run_id、require_comparable、baseline_delta、critical_case、
  paired inference 或 pairwise pass@k；allowed_factors 不能绕过此限制
- 只有引用此指标的政策使用 gate-lite@3 / gate-engine@3、metric-registry@2 和
  pairwise-quality@1；历史政策仍保持原语义与身份。结果绑定完整质量内容、冻结指标定义、
  明确阈值政策和资格来源摘要，角色/证据/政策变化不会复用旧通过结果
三后端离线 fixture/重启证据不代表真人复核、live-model、生产或平台原生验收完成。
