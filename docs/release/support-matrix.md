# MoTTEavl 支持矩阵（M7 RC）

> 状态：draft（随 T11 证据回填）。取值：`tested`（本仓真实执行证据）＞ `supported`
> （tested 子集 + 无已知反例）＞ `experimental`（有实现、证据不足或已知限制）＞
> `blocked`（环境/授权缺失）＞ `not_run`（未执行）。skip 不计入 supported。
> 每行必须给出证据（测试 node / 命令记录 / 验证账本链接）；没有证据的行保持
> not_run，不因代码存在或 CI 总体通过而升级。

## 1. 操作系统 / 运行方式

| 环境 | 状态 | 证据 |
|---|---|---|
| Windows 11 + Git Bash（开发/离线测试） | tested | docs/verification/M7.md 命令账本（本机全量离线门禁） |
| Linux（CI ubuntu-latest + 真实 PG service） | tested | GitHub Actions（合并主干后 CI run 链接，见 release notes） |
| WSL2 | not_run | 无环境（README 声明为支持路径，未单独验证） |
| macOS | not_run | 无环境 |

## 2. 存储

| 后端 | 状态 | 证据 |
|---|---|---|
| SQLite（本地开发/单机） | tested | tests/storage/**、M7 平台表升级、备份/恢复集成测试 |
| PostgreSQL 16（CI service） | tested（CI） | CI 真实 PG 参数组（合并后回填 run 链接） |
| PostgreSQL（本机直连） | blocked | 本机无 docker/MOTTE_PG_DSN（docs/verification/M7.md §1） |

## 3. 执行后端 / Runtime

| 后端 | 状态 | 证据 |
|---|---|---|
| replay@1 / json_extract@1（确定性 fixture） | tested | 全量离线套件（零网络零费用） |
| direct-llm@1（含 v2 scorer） | tested | tests/api/test_direct_llm_api.py 等 |
| builtin-agent@1 | tested（离线） | tests/sdk/test_agent_task_scoring_gaps.py 等 |
| external-benchmark@1（C-Eval/CMMLU） | experimental | 离线 fixture 完整；真实外部执行未在 M7 复验 |
| terminal-bench@1（Harbor） | blocked（本机）/ experimental | 本机无 docker；离线 Harbor 测试全绿 |
| scenario/skill（M5） | tested（离线） | tests/scenario/**、tests/skill/** |

## 4. Provider / 模型 / Judge

| 项 | 状态 | 证据 |
|---|---|---|
| OpenAI-compatible / Responses / Anthropic 适配器 | tested（离线 fixture） | tests/provider/** |
| DeepSeek V4.1 Flash live | tested（M6 有界验收） | docs/verification/M6.md §5.1；M7 live smoke 见命令账本 |
| Judge（独立评分） | experimental | 无 ≥30 人工校准样本（M5 起已知边界，正式门禁 fail-closed） |

## 5. 操作能力（M7 验收面）

| 操作 | 状态 | 证据 |
|---|---|---|
| SDK 安装（clean venv/wheel） | tested | tests/packaging/test_clean_install.py 8 passed（e4d54bd，主审复跑同结果；CI packaging job 合并后补链接） |
| SDK 调用（Run/事件/报告/比较/Gate/导出） | tested | tests/sdk/test_client_contract.py + test_wait_and_events.py 27 passed（8389659/1db0a54，主审复跑同结果；内存幂等修复 133a2d5 附回归测试） |
| CLI local/server 双模式 | tested | tests/cli 97 passed 含 test_remote_parity.py 18 项（c4da983，主审复跑同结果；A04 断连零本地副作用）。tests/cli/test_terminalbench_cli.py 2 项为基线同样失败的 Windows O_DIRECTORY 环境族，非 M7 引入 |
| pytest 门禁读取 | tested | tests/sdk/test_pytest_and_exports.py 26 passed（287a3ca，主审复跑同结果；普通 pytest 零模型/零 Run 有网络 monkeypatch 反证） |
| 历史导入 dry-run/apply/resume/rollback（合成来源） | tested | tests/migration 19 passed/1 Windows-symlink skip（148e26a，主审复跑同结果）；真实旧导出 not_run |
| 备份/恢复（SQLite） | tested | tests/integration/test_backup_restore_consistency.py + tests/storage/test_maintenance.py 14 passed/1 PG skip（ec9be0a，主审复跑同结果） |
| 备份/恢复（PostgreSQL） | not_run | 无本机 PG；CI 未见 pg_dump 断言（如实登记） |
| GC/retention | tested | tests/security/test_gc_retention.py 4 passed（8c8cf3d）；trace DB 行裁剪 not_implemented（无事件时间戳，plan 如实报告） |
| 升级/回退演练（SQLite） | tested | tests/integration/test_release_smoke.py 4 passed（含旧形状升级、备份恢复路径、冻结退出码）；PG 降级阻断 not_run（无 DSN） |
| Docker Compose build/up | blocked | 本机无 docker；仅 compose config 通过 |
| 旧平台切换 | not_run | 需单独授权（docs/release/cutover.md） |

## 6. 阶段结论规则

只有上表所有声明 supported/tested 的行都有真实证据时，才可输出 `stable_supported`；
任一 not_run/blocked 存在时如实标注 `offline_verified` + `external_pending`。
