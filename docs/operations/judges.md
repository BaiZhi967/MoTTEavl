# Judge 运维说明（M5）

状态：T09a（契约与零成本预检）已提交；T09b（持久作业与调用账本）、T09c（原子
发布）、T10（校准与人工修订）仍在实现中。本文只描述已经落地的边界与不可协商的
约束，未实现部分明确标注。

## 1. 已固定的契约边界（T09a）

- JudgeSpec 固定 judge profile、prompt/rubric 版本与 hash、criteria、输出 schema、
  参数、输入证据选择、缺失政策、校准版本与预算。
- 输入只来自选定 Observation 的白名单字段与归属校验过的 Artifact，并保存
  input evidence digest。候选内容只作为**数据**：不给业务工具，不给网络。
- 输出逐 criterion 校验；不属于输入集合的证据引用被拒绝。
- 拒答/畸形/超时/缺证据保持 evaluator_error 或 insufficient，**不补 0、不判通过**。
- preflight 返回用途、模型、样本数、最大调用次数、tokens 上限、价格覆盖与预算
  可执行性；无授权即零调用；价格未知时不声称精确货币硬上限。
- pairwise 与 single 分开：两个 candidate_id 固定同一任务，presentation_order 与
  候选身份无关；正反序 fingerprint 不同、各计一次费用。

## 2. 尚未交付（不得当作已有能力）

- 持久 ScoringJob、用途为 judge 的调用账本、取消与 CAS、原子发布 ScoreSet/pass。
- 崩溃窗口逐项行为（prepared / dispatching / 响应已落库 / 事务中断 / 提交后通知）。
- 校准集、混淆/分歧/换序统计、experimental 标记与人工修订。
- API/CLI 的提交、只读查询与幂等取消入口。

## 3. 不可协商的约束

- GET、compare、gate、历史切换与普通离线 rescore **永远零模型调用**。
- Judge 是独立授权作业：在 Worker 中执行，不在 API 请求内付费运行。
- 待执行/失败/部分证据只留在 job，不进入终态 pass 表；终态 ScoreSet + 预分配
  ScoringPass + job receipt + current 指针在同一事务 CAS 提交。
- 不确定时不自动重发：原始 subject Run 的终态与证据不因 Judge 故障改写。
- 人工修订只追加新 pass 并保留依据；新 rubric 不继承旧校准，未校准 Judge 为
  experimental，不进阻断门禁。

## 4. 接线前必须解决的前提（已核实的阻断点）

T09 给出的接线面是：WorkerLoop 里构造 ScoringJobService、恢复时调
recover_interrupted()、无 Run 可领时调 claim_and_run()。已核实两个事实：

1. `ScoringJobService(store, *, provider_factory=None, artifact_reader=None)` 的
   provider_factory 是可选的，缺省时作业仍可提交/领取/恢复，但**无法真正调用
   Judge 模型**——"能用但空转"不算接线完成。
2. `RunService.__init__` 只持有 `store`，**没有资源仓库**（model profile /
   provider connection / price table）。因此 Worker 侧今天拿不到按 JudgeSpec
   固定模型解析连接的入口，写不出正确的 provider_factory。

因此接线前必须先做一个显式设计决定，二选一：

- **A（推荐，与 Run 的冻结语义一致）**：在**提交期**由 API 用资源仓库解析出
  Judge 的 provider 快照并冻结进请求/作业，Worker 只按快照构造 Provider，
  provider_factory 退化为"从快照构建"，Worker 不需要资源仓库。
- **B**：把资源仓库注入 WorkerLoop，使 Worker 在领取时解析模型档案。

选 A 时，Judge 的模型/连接/价格表与 Run 一样在创建期固定，历史作业不随资源
改名漂移；选 B 时必须在作业上再固定解析结果，否则同一作业在两次领取间可能
解析到不同 provider。无论选哪种，都必须保持"GET 与普通离线 rescore 零调用"
以及"pending/failed job 不进终态 pass 表"。
