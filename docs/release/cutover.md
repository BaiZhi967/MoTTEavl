# 旧平台切换手册（cutover runbook）

> 状态：draft（M7-T12）。真实切换需要**单独授权**（操作人、窗口、范围、费用、
> 回退决定人），本文是执行清单不是授权记录。三类替代场景、迁移对账、备份恢复、
> 回退窗口全部齐备且支持矩阵无 not_run 阻断项之前，不得宣布 `cutover_ready`。

## 1. 冻结与前置检查

- [ ] 固定 MoTTEavl release candidate commit / wheel / Web dist / Docker image / Alembic head（见 release-notes.md RC 段）。
- [ ] 固定旧平台参考 commit 与导出包（manifest hash 登记进 ImportReport）。
- [ ] 支持矩阵中本次切换涉及的全部行均为 tested/supported；否则逐条列出缺口并暂停。
- [ ] 三类替代场景证据齐备（模型评测 / Agent Benchmark / 业务回归），任一缺失即不通过（A21）。

## 2. 停止旧平台增量

- [ ] 旧平台停止新增功能开发；停止发起新付费任务。
- [ ] 存量任务等待终态，或由操作者显式停止并记录结果（进行中任务**不迁移**：
      dry-run 中以 `in_flight_job_not_importable` 拒绝）。

## 3. 备份与只读 dry-run

- [ ] 旧库/旧 Artifact 只读备份（旧平台侧操作，保留 hash 清单）。
- [ ] MoTTEavl 侧 `motte backup --consistent`（维护屏障 + manifest v2 + hash 校验）。
- [ ] 导入 dry-run：核对 planned/reused/conflicted/rejected、missing artifacts、
      unknown fields、凭据 rebind 清单；**目标零修改**。
- [ ] 操作者逐项确认诊断；conflict/rejected 有处置决定。

## 4. 分批 apply 与对账

- [ ] 分批 `apply`（每批后核对 created+reused+rejected+conflicted == planned、
      artifact hash、引用完整性、分数/状态分布）。
- [ ] 确认 imported Run 均为终态 + `origin: imported` 且 Worker 从未领取。
- [ ] 凭据 rebind：按清单在新平台重建引用（环境变量名/credentials profile），
      不复制密钥值。
- [ ] 旧系统基线/门禁只作为历史结论保留，不进入新平台默认指针。

## 5. 新任务单路进入

- [ ] 新评测任务只从 MoTTEavl（API/CLI/Web）发起；旧平台入口只读。
- [ ] 三类替代场景在新平台各复跑一次作为切换验收。
- [ ] 观察窗口（建议 ≥ 1 个业务周期）：GSM8K/Direct LLM/Agent 套件无未解释差异。

## 6. 回退窗口

- [ ] 定义回退窗口长度与决定人。
- [ ] 回退动作（按序）：停止新远程执行写入口 → 停止导入 apply / GC apply →
      确认活动 Run/评分/备份状态 → 保留不可变历史、原始 Artifact、导入审计、
      tombstone、backup → 应用回退（优先回应用版本；schema 不可逆时从 staging/
      backup 恢复）。
- [ ] **不得**把新平台进行中的 Run 反向迁回旧执行器；**不得**自动归档旧仓库；
      **不得**删除历史来制造稳定假象。

## 7. 记录

切换完成后在 `docs/verification/M7.md` §8 登记：授权人、窗口、RC/旧参考
commit、每批 import_id 与对账数字、费用、回退窗口关闭时间、遗留 gap 分类。
