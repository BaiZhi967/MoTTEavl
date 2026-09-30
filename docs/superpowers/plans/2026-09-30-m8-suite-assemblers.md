# M8 类型化 suite 装配实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将已具备冻结与可强制预算语义的 standalone suite 接入实验，未证明的外部组合继续明确拒绝。
**Architecture:** 每 suite 用薄 assembler 复用 standalone builder，输出统一冻结 Cell 输入。
Preview/create 共用编译结果，allocate/retry 不再解析当前资源。无新 scheduler，无任意 manifest 注入。
**Tech Stack:** Python/Pydantic，现有 ExperimentService/RunService/Worker，Memory/SQLite/PG。
**Spec:** `docs/superpowers/specs/2026-09-30-m8-persistent-closure-design.md` §D。

## Global Constraints
- 原有 supported suite 语义、严格 preview binding、legacy guarantee boundary 不变。
- 只有实际执行路径可证明且强制的模型调用上界可用；包括每一层重试；不接受客户端自报证明。
- 未支持 factors/Runtime/Profile 在任何 Spec/Cell/Run/Job 写入前拒绝。
- Skill 臂只有 `factors.skill_version` 一条矩阵轴：字面值 `no-skill` 和恰好两个互异、固定、已发布的
  `skill-id@version` 引用；不添加隐式臂维度，不另增 Run，仍为现有 factor 笛卡尔积 × repeats。
- 合成 Runner/Provider 证明软件路径，不证明真实官方 suite 验收。
- 无真实模型调用、官方私有数据访问或生产操作。

## Review Focus
- 结构化配置与旧 scalar controlled_conditions 混用必须报错，不能丢弃条件。
- Case 数、workflow turn 数、transport retry、experiment repeat、Harbor Trial 不是同一单位。
- preview 后 latest/资源内容漂移不得进入同一冻结 Cell。
- 恢复/重试不得重新启动已知外部 Job 或复用旧 Trial identity。
- 上游重试配置未知时不得以零或一次作为默认事实。
- Skill 的三臂计划不是三个额外 Cell；臂标签、实际 Skill ref、Cell assignment 必须一一对应。

## Task 1: 类型化配置和内部装配边界
**Files:** `packages/contracts/motte_contracts/experiment.py`；新建
`packages/sdk-python/motte_sdk/experiment_assemblers.py`；`motte_sdk/experiments.py`；
`tests/contract/test_experiment_suite_config.py`；`tests/sdk/test_experiment_assemblers.py`。
**Interfaces:** ExperimentSpec 新增可选判别式 `suite_config`：
`CevalExperimentConfig(kind='ceval', dataset_revision, scope, split, few_shot, few_shot_split, seed, execution_profile)`；
`ScenarioExperimentConfig(kind='scenario', workflow_ref, cases, agent_mode, execution_budget, target='builtin-agent')`；
`SkillExperimentConfig(kind='skill', workflow_ref, cases, agent_mode, execution_budget, budget_policy)`。
字段类型复用 standalone 请求合同；未知字段 forbid。现有 Direct/GSM/Agent 仍使用旧配置。
Skill 首版只允许 `model_profile` 和 `skill_version` 因子；两者必须显式提供。
`skill_version` 的三个唯一值包含且只包含一个 `no-skill`，剔除该字面值后按**声明顺序**取两个引用：
第一个映射现有 planner 的 `skill-v1`，第二个映射 `skill-v2`；标签不按资源版本字符串大小推断。
缺臂、额外臂、重复引用、浮动或未发布引用，以及任何旧臂列表配置均在预检拒绝。
现有 `ExperimentSpec.cell_count()`、`_expand_matrix(spec)`、`compute_cell_id(...)` 不改计数/身份算法。
`assemble_experiment_cell(spec, assignment, *, resources, store, skill_group=None) -> AssembledExperimentCell`，
其中 `skill_group: SkillGroupInputs | None` 是 Task2 编译期内部上下文，非公共配置；Skill 分支必须提供。
结果含 scenario_version、requested_manifest、manifest、ordered case_ids、resource_hashes、call_bound。
call_bound 只由受支持执行实现产生，包含 max_calls、执行边界及受控重试配置；不是公共输入字段。
- [ ] RED：新配置合法shape可解析；错suite、任意manifest、未知字段、缺revision/预算拒绝；旧Spec快照不变。
- [ ] RED `test_skill_axis_requires_one_no_skill_and_two_distinct_published_refs`：精确三值通过；
      缺/重复 `no-skill`、重复引用、一/三个非空引用、未固定/未发布引用拒绝，零Spec/Cell/Run。
- [ ] `uv run pytest -q tests/contract/test_experiment_suite_config.py tests/sdk/test_experiment_assemblers.py` 确认缺合同失败。
- [ ] 实现合同/分发边界；原支持路径委派原函数，不更改预算公式；未实现分支给具名错误。
- [ ] 重跑新测试及 `tests/sdk/test_m8_native_agent_experiments.py`，全通过后中文提交。

## Task 2: Scenario 与 Skill 装配
**Files:** `experiment_assemblers.py`、`experiments.py`；复用 `resolve.py`/`skill_ablation.py`；
`tests/sdk/test_experiment_scenario_skill.py`、`tests/integration/test_experiment_scenario_skill.py`。
**Interfaces:** 新增 `assemble_scenario_cell(spec, assignment, *, resources, store)` 和
`assemble_skill_cell(spec, assignment, *, resources, store, skill_group: SkillGroupInputs)`，均返回 Task1 统一结果。
新增 `prepare_skill_groups(spec, assignments, *, resources, store) -> dict[str, SkillGroupInputs]`，
`assignments: Sequence[FactorAssignment]` 从既有 `_expand_matrix(spec)` 返回的
`(assignment, repeat_index)` 对中抽取，允许其中包含各 repeat 的重复配置。
`SkillGroupInputs` 只含 `plan: AblationPlan`、`arm_id_by_value: dict[str, str]` 和
`requested_manifests: dict[str, dict[str, Any]]`；不可触发创建或持久化。
分组键是删除 `skill_version` 后、保留其它因素的 `FactorAssignment.canonical_payload()` 的 canonical hash，
不含 repeat_index。每次 preview/create 的单次编译，对每个固定非Skill因素组只构造一次完整三臂计划，
各 repeat 复用该编译结果，不做跨编译缓存。两模型即两个组；不跨模型做纯Skill归因。

复用的**现有实际签名**（不修改 standalone planner 的臂数或语义）：
- `prepare_run(scenario_version, manifest, case_ids, resources)` → `(resolved_manifest, case_ids)`，
  负责 Workflow/Fixture/Target/Skill/evaluator 冻结；Target 只允许 builtin-agent。
- `plan_skill_ablation(*, base_manifest, experiment_ref, arms: Sequence[ArmSpec], budget_policy, case_keys)`
  → `AblationPlan`；这里的 `arms` 是内部生成的函数参数，不是第二条公共配置或矩阵轴。
- `arm_manifests(base_manifest, plan)` → 以现有 `no-skill`/`skill-v1`/`skill-v2` 为键的 manifest 字典。

每组基于同一未解析的 `base_manifest`（包含固定 workflow/model/预算/cases）调用现有 planner 和
`arm_manifests`；给出的 experiment_ref 包含实验id/version与分组hash。内部构造完整三条 `ArmSpec`：
`no-skill` 的 skills=()；另两条分别 skills=(固定ref,)，并携带已发布资源的身份/内容hash。
必须在解析 Skill ref 或访问资源仓库之前识别 `no-skill`，它不是待查询的资源名。
从实际 Cell assignment 的 skill_version 经 `arm_id_by_value` 选择**一条** requested manifest，
再用 prepare_run 冻结；不将已含保留快照字段的 manifest 再送 prepare_run。
比较最终非Skill冻结条件以拒绝同组漂移；Skill快照只在 `resource_snapshots.skill_injection` 中，
不添加不受 ResolvedManifest 支持的顶层 skill_snapshot。
预算政策只复用现有 `same-total-budget`/`same-execution-budget`，不调用 `run_skill_ablation`，不自行分配 Run。

- [ ] RED `test_skill_matrix_has_twelve_existing_cell_identities`：模型 `[model-a, model-b]`、
      skill_version=`[no-skill, cancel-guard@1, cancel-guard@2]`、repeats=2，断言
      `spec.cell_count()==preview.cell_count==len(preview.cells)==12`；全部ID逐项等于既有
      `compute_cell_id(experiment_id, version, assignment, repeat_index)`，12个唯一ID、每模型/臂恰好两个repeat。
- [ ] RED `test_skill_matrix_budget_counts_each_cell_once`：上述矩阵、每Cell两个Case、session max_steps=8、
      provider max_retries=1，断言初始矩阵 `max_potential_calls==12*2*8*2==384`；上限384通过，383拒绝；
      max_cells=11拒绝；全部拒绝路径零Spec/Cell/Run/Job。不得再因planner返回三条臂而乘3。
- [ ] RED `test_skill_group_plan_and_selected_arm_match_standalone`：每次编译两个非Skill组，
      真实planner每组只处理完整三臂一次；12个Cell逐个对照同组 standalone planner→arm_manifests→prepare_run
      的结果、Case IDs、scorer和Skill注入hash。no-skill为skills=[]且skill_arm=no-skill，无对应资源查询；
      两固定ref分别得到skill-v1/v2标签，真正进入各自模型请求；同组非Skill条件逐字段相同。
- [ ] RED `test_skill_ref_order_has_deterministic_mapping_and_preview_identity`：`no-skill` 位于列表任意位置
      都先作为哨兵处理，其余ref按声明顺序映射；交换两个ref时planner标签同步交换、preview_hash改变；
      不可把新映射写进已发布的同一实验版本。无重复ref或额外隐式臂。
- [ ] RED：跨多个workflow sends 的 max_steps 为session累计上界；不得按turn重复分配预算。
- [ ] RED：资源漂移、未知Target、隐藏gold进入subject、未消费factor、缺预算均零持久化拒绝。
- [ ] 实现按非Skill因素分组的纯编译和单臂选择，由统一preflight逐Cell累计真实max_calls；
      冻结结果保存prepared_run；一次已有factor×repeat展开同时决定preview、Cell ID、分配数与预算。
- [ ] RED `test_skill_matrix_api_cli_preview_create_recovery_and_retry`：同一2×3×2请求经API/CLI预览一致，
      预览零Run；create分配12个initial Run，重复create/allocate及服务重建后恢复仍为12个，ID和各臂不变。
      显式retry其中一Cell只增加1个superseding Run（总13），原12个Cell与preview的初始矩阵计数/预算不变，
      子Run保留该Cell冻结的Skill身份、Case IDs和全部执行条件及parent引用；不重建三臂、不查询当前资源。
- [ ] API/CLI创建→Worker完成、取消、重启恢复、显式重试与原Cell审计不变；断言每Cell恰好一个initial Run，
      每个非Skill配置组恰好一份完整三臂计划，repeat不产生额外隐式计划维度。
- [ ] 跑新测试和现有scenario/skill_ablation/预算边界测试，独立审查后中文提交。

## Task 3: C-Eval 装配及硬上界先决条件
**Files:** `experiment_assemblers.py`、必要时 `benchmark_catalog.py`/固定OpenCompass adapter配置；
`tests/sdk/test_experiment_ceval.py`、`tests/integration/test_experiment_ceval.py`。
**Interfaces:** `assemble_ceval_cell(...)` 复用 validate_external_run_request 和
prepare_external_run_inputs，精确 benchmark_datasets.get('ceval', revision)，禁止latest。
冻结 scope/split/fewshot/seed/model/RunnerProfile/样本内容摘要/scorer/parser；仅 model_profile 可变。
- [ ] 先核验固定上游版本的真实 transport/operator/runner 重试边界，登记支持Profile与可执行公式；
      无法证明则本Task仅保留明确unsupported和负例，不能放行。
- [ ] RED：已支持Profile的独立builder与实验冻结输入一致；repeat生成不同Cell但相同受控条件。
- [ ] RED：未知重试、缺固定revision、预算不足、非model factor、latest漂移均零写入拒绝。
- [ ] 实现已证明Profile路径；不要将builder已冻结manifest再送prepare_run造成保留字段拒绝。
- [ ] API/CLI两模型→Worker/外部Job脚本运行、计分身份、取消/恢复/重复启动负例；离线测试明确标注。
- [ ] 跑新的及现有C-Eval integration/runner选择测试，审查公式包含全部重试后中文提交。

## Task 4: Harbor/外部 Runtime 的拒绝边界与未来接入口
**Files:** `experiment_assemblers.py`，`tests/sdk/test_experiment_external_budget_guards.py`，支持矩阵文档。
**Interfaces:** 未证明Profile返回 `EXPERIMENT_BUDGET_UNPROVABLE` 或现有unsupported具名错误；
保留 build_run_inputs/refreeze_for_run 接口供以后真实可执行Profile使用，不开放任意manifest。
- [ ] RED：Claude/Codex timeout、Harbor max_turns/max_budget、Task×Trial、Pi streamFn数量本身均非transport硬上界。
- [ ] 对缺内部retry证明的配置测试 preview/create/retry 均拒绝且零新Run/Job/调用。
- [ ] 只有实测受控Profile存在时才实现正路径及Trial身份重建；oracle不标为真实Agent支持。
- [ ] 文档逐Profile区分“代码拒绝/离线验证/真实Runner验收”；全量后中文提交。

## Task 5: 公共契约与整体验证
**Files:** `apps/api/app/main.py`、SDK client/CLI现有实验入口、`api/openapi.json`、Web生成schema、协议文档。
- [ ] RED：API/CLI类型化请求真实流入同一装配器；previewHash包含新配置及实际prepared输入。
- [ ] 复用现有创建/取消/重试接口，不新建平行执行服务；不新增Web页面。
- [ ] `make openapi` 后 `make check`；缺环境阶段独立记录，不能冒称全绿。
- [ ] 一次稳定树独立复核全部新suite与旧Direct/GSM/native回归；当前PR同分支追加提交并跟精确HEAD CI。

## 审阅与执行
本计划供用户审阅；批准计划前不实施新增接口。推荐子任务隔离实施并逐项独立审查。
C-Eval/Harbor没有证明可执行硬调用上界时仍未闭合，不能因保留拒绝测试通过就声明支持。
