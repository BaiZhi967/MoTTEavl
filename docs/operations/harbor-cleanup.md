# Harbor 取消、清理与残留核查 runbook

> 状态：**所有权、清理报告与残留清单已实现并有测试**；真实 Harbor 任务容器
> 级的取消演练未执行（**not_run**，见 [M3 验证记录](../verification/M3.md)）。
> 本文只描述本平台实际拥有的资源与操作顺序；不覆盖 Harbor 内部状态机。

## 1. 所有权模型

本 Run 启动的每个资源都带该 Job 的 `launch_token`（每次启动新生成、不可复用）：

| 资源 | 归属凭证 |
|---|---|
| Runner 进程树 | argv/env 中的 launch token + 启动身份（pid、host、started_at）；跨会话凭 argv 核验（Linux `/proc/<pid>/cmdline`、macOS `ps -p <pid> -o command=`） |
| 任务容器 | 资源账本记录的容器标签 `motte.job=<job_id>` |
| Job 工作目录 | `work_dir` 路径 + 账本中的 `owner_token` |
| Harbor Job 目录 | 工作目录内的 `harbor/job-location.json`（越界即拒绝） |

adapter 在 `start`（唯一产生副作用的操作）时刷新资源账本：

```json
{"owner_token": "<launch_token>", "job_id": "<job_id>", "run_id": "<run_id>",
 "container_label": "motte.job=<job_id>",
 "resources": [{"kind": "job_dir", "path": "<work_dir>", "owner_token": "<launch_token>"}]}
```

账本随句柄持久化；清理与中断只依据它，**不推断**未登记的资源。

## 2. 取消顺序与规则

取消（操作员 `RunService.cancel` 或超时）固定按此顺序：

1. 先持久化取消请求（跨进程可见；API 与 Worker 分进程时另一实例落库的取消也能
   在有限时间内生效）；
2. 中断**本 Job 拥有**的进程树：TERM → 宽限 → KILL（按进程组）；
3. 尽力采集部分工件并保留；终态不因迟到输出复活。

硬规则：

- **只动所有权可核验的资源**：token 不可核验时绝不发信号（PID 被无关进程复用
  不会误杀）；不执行全局 `docker prune`；不 `kill` 非本 Job 的进程；不删除
  未知状态的工作目录。
- **清理失败不覆盖已有评分**：清理先于/独立于评分证据保存，清理失败只记录
  残留，不修改已落盘的分数与证据。
- **迟到结果只审计**：晚到的 failed/indeterminate 观察不覆盖 `cancelled`，
  只写审计事件（`external_job_late_results`），不新增评分、不复活终态 Run。
- **所有权不可证明 → 保守 `needs_review`**：Run 保持待人工核查，**绝不自动重启**；
  禁止仅凭"没有结果文件"重新付费执行。
- **恢复只观察**：崩溃恢复不启动第二个 Job；Runner 入口发现同名 Job 目录已存在
  时只登记位置并退出 0（`reused_existing_job_dir: true`），绝不重复执行。

完成标记（`<work_dir>/.motte-job-complete`，原子写）是"跑完了"与"进程消失"的
唯一判据：exit 0 → settled、非零 → failed、**没有标记只有部分输出 →
indeterminate**。

## 3. 验证步骤

取消或异常后，按以下步骤核查并**逐 Run 记录残留**：

```bash
# 1) 本 Job 的容器（标签来自资源账本）
docker ps -a --filter label=motte.job=<job_id>

# 2) 本 Job 的工作目录（harbor/job-location.json 指向 Harbor Job 目录）
ls -la <work_dir>
ls -la <work_dir>/harbor

# 3) 进程（token 可核验时才判定归属）
ps -p <pid> -o pid=,command=
```

清理报告（`cleanup()`）字段：`state`（`clean` / `residual`）、`owner_token`、
`job_id`、`known_resources`、`job_dir`、`job_dir_present`、`leftovers`。

必须记录的内容（每条 Run 一行，进入运行记录/交接记录）：

| 字段 | 取值 |
|---|---|
| run_id / job_id | 本次 Run 与 Job |
| launch_token | 本次启动身份（用于判定"是否本 Job 资源"） |
| state | `clean` 或 `residual` |
| leftovers | 残留条目（kind + path/pid + 归属判定依据）；无残留写空列表 |
| job_dir_present | 工作目录是否仍存在（存在不等于残留，需注明是否含未知内容） |
| 核查命令与时间 | 上述命令原文与执行时间 |

**未核查不得宣称已清理**；`residual` 必须写进交接记录，且不得据此删除不确定
资源（保留待人工处理）。

## 4. 回退

关闭 Harbor 新建能力（不注册 adapter / 移除固定 wrapper）后，创建入口在提交前
拒绝（`RUNNER_NOT_CONNECTED`）；随后停止或确认存量 Job（只观察、不重启），
保留 Trial、Artifact 与 ScoringPass 历史。禁止删除所有权不确定的 Job 目录。
