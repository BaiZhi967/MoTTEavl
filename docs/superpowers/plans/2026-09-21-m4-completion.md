# M4 第二轮整改与完整交付 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.
**Goal:** 修复第二轮全部发现并完成 M4 T01–T11 的实现闭环、验证、合并推送，随后交付 M5 计划。
**Architecture:** 保持 RunDispatcher/CaseAttempt/Observation/ScoringPass 唯一主权；复用已存在的 runtime、命令及 Inspect 资源。
**Tech Stack:** Python 3.12 / uv / FastAPI / SQLite/PG / React / Node 24 / pi SDK 0.73.1。
**Spec:** docs/roadmap/M4-pi-and-external-harnesses.md；docs/superpowers/plans/2026-09-20-m4-kickoff.md；第二轮审查报告。
## Global Constraints
- 用户已明确授权修复、合并主干并推送；不得覆盖用户未提交工作。
- 默认测试不调用真实模型；live 需明确端点/模型/预算，缺证据不得宣称 passed。
- 先复现失败断言再改实现；不通过削弱测试或评分契约过门。
- 各子任务独占文件；共享契约由主代理协调；子代理不再派子代理，不自行提交其他人的改动。
- UI 遵循 AGENTS.md 和 apps/web/DESIGN.md；新接口同步 OpenAPI/TS。
## Review Focus
- 真实 SDK 的 usage/error 语义不等于桥接对象默认值。
- Windows 进程执行前即建立 Job 所有权，冻结前必须停止副作用。
- 已发布资源引用展开后仍检查秘密和预算。
- command ack 必须来自实际消费者，delivery_unknown 不重放。
- Inspect 来源身份、原始证据及评分引用不可变。
### Task 1: Pi 与 Provider
- [x] 修复 N01/N02/N10/N11/N12/N13，补真实 SDK + 本地 HTTP usage/error、多轮、预算边界与迟到写入回归。
- [x] 所有 Pi event 保留 operation_id；停止后再冻结；缺失计量保持 unknown。
- [x] 负责 bridges/pi、pi_runtime、motte_agent/pi、anthropic_messages 及对应测试/操作文档。
### Task 2: 公共边界、评分与比较
- [x] 主代理修复 N04/N08/N16；共享严格预算验证函数供两个 executor/资源入口调用。
- [x] 保留 no-forbidden-write 原语义，恢复轨迹不足的 insufficient。
- [x] 预算 allowlist/有限数/零值与比较 model-only 允许因子均须真实回归。
### Task 3: Harness 生命周期与证据
- [x] 修复 N06/N07/N09/N14 和 R12 终态后回调。
- [x] Windows 创建挂起进程、先归属再运行；失败拒绝；POSIX 正常路径继续保留。
- [x] CAS 跨进程互斥、实际 attempt_id、生产恢复扫描接线；不自动重放。
- [x] Codex 所有可见工具进入 Observation 或显式降完整度；原始证据独立命名空间。
### Task 4: 产品链路
- [x] 修复 N03/N05/N17/N18，发布与展开双重秘密/schema/预算校验。
- [x] 创建前静态预检；Web 实际版本 Profile 可选，显式边界确认，预算与任务配置、有效配置/模型/session/证据可查。
- [x] 同步 API/CLI/Web/类型/设计及测试。
### Task 5: 完成交互与 Inspect
- [x] 基于官方固定协议实现 codex-app-server 实际 transport/Worker command consumer，消息、审批、拒绝、取消、去重/过期/恢复及干预。
- [x] 修 N15；Inspect 固定 schema、身份去重与原文归档，接入 Run/Observation/ScoreSet 查询，导入绝不执行日志。
- [x] 命令与导入分别独立专项审查；共享契约联动后纳入统一集成提交，不冒充 live。
### Task 6: 交付与 M5
- [ ] 全量 Windows 与可用 Linux/PG CI 验证；独立整体审查，修复全部阻断。
- [ ] 更新 M4 G/T/A 矩阵真实证据，完成后合并 main 并推送（用户已授权）。
- [ ] 基于最终主干编写 M5 详细执行计划、kickoff/开发 agent 提示词，核对 G01–G22/T01–T12/A01–A18 与兼容边界。
