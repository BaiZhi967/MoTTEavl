# M4 live 验收执行卡

状态：**执行方案，付费模型 live 尚未执行、未通过**；本卡本身不构成付费调用授权。安装固定版本、创建临时目录、选择端口和核对代码状态等普通准备按已有授权推进。主干合入已另行完成；本卡不证明 M4 的真实模型验收完成。执行前登记最终代码 SHA、实际迁移 head 和环境；未运行项保持 `not_run`，真实依赖缺失保持 `blocked`。本次编写仅核对源码与文档，没有读取凭据、调用模型、启动 CLI 任务或修改现有用户文件。

范围来自 [M4 kickoff T09/T10/T11 与退出门](../superpowers/plans/2026-09-20-m4-kickoff.md)：每个声明支持的后端需要同一文件小任务、实际执行中取消和失败证据；交互通道另验审批、steer、interrupt；Inspect 原生日志只读导入单列，不冒充模型 live。实现状态以 [M4 完成复核记录](../verification/M4-completion-review-2026-09-21.md) 为准。

交接状态：开发分支代码 `270bfad` 已推送，[最终 Linux/PG CI 35556224363](https://github.com/BaiZhi967/MoTTEavl/actions/runs/35556224363) 全绿：Python **1963 passed、29 skipped、1 deselected**，Web **239 passed**；完整 make check、audit 和生成契约检查通过。用户已于 2026-09-21 明确要求先合入；main 已快进到 adf468a 并推送，该提交完整 CI 亦通过。此合入不改变 live 待验结论。真实 Codex 0.155.1 曾以空凭据完成 initialize→initialized→thread/start 零模型探针，回报 readOnly/network=false，未发送 turn/start，关闭无残留。该证据只归类 protocol/integration，不能记为模型 live、真实认证或模型取消通过。真实 Inspect 使用 mock 模型产生的原生收据已通过导入验收，原生格式证据可用，详见第 8 节。本卡四种创建 body 与 13 组预算已由独立 reviewer 经临时库/真实 API 作零 spawn、零 Worker 校验；执行时仍重新登记代码和实际环境。

## 1. 执行范围和停止条件

付费模型 dispatch 前，补齐第 10 节真正缺失的模型、端点、凭据引用和费用/调用边界；准备工作可继续，不为每项普通准备重复询问。记录本批所覆盖的后端，不从“允许开发”推断允许付费，不复用操作者个人订阅登录态。已有 Inspect 只读导入证据不需要另加真人日志门。

- 只访问本卡指定模型服务端点；被测任务不得上网、安装依赖、调用 MCP、读取环境变量、扫描主机、发信、支付或修改仓库。
- 每个 Run 只选一个合成 Case、只启动一次；串行运行。禁止自动 retry、second-chance 或反复试到成功。原生 CLI/SDK 内部请求或重试可能不可完全观测，另记 `unknown`。
- 模型、原生版本、超时、目录、凭据**引用名称**和允许文件固定后再创建 Run。提交失败或 HTTP 响应丢失时先查已有 Run，不能盲目重新 POST 创建另一条付费运行。
- 发现越界动作、版本漂移、调用上限无法满足授权、取消后仍写入、清理归属不明或费用超出操作者控制阈值，立即停止后续提交。结果不确定时保留 `needs_review` 和证据，不重放。
- 只运行确定性评分，不启用 Judge。GET 报告/历史/产物、比较和 Inspect 导入不调用模型。

## 2. 临时环境、文件和网络边界

执行者创建并记录**全新、归本次验收所有的绝对临时根目录** `<LIVE_ROOT>`，位于仓库和既有业务目录之外；无需用户另行指定目录或确认创建。拒绝 symlink/junction/reparse-point 目录链，拒绝复用已有任务目录。临时目录限定本次允许的文件动作，不代表已建立宿主 OS 沙箱。

计划目录：

```text
<LIVE_ROOT>/
  state/runs.db             独立 SQLite，禁止指向日常数据库
  artifacts/               保留冻结产物和脱敏原始事件
  sessions/                本次 runtime session 证据
  workspaces/              各后端/run/case 独立工作区
  os-temp/                 原生隔离 HOME/config 与其他临时文件
  evidence/                授权记录、请求 JSON、报告、校验 hash
  inspect-input/           可选补充的只读原生日志副本
```

API、Worker、CLI 指向同一份专用配置；只在这些验收进程中设置，不覆盖用户全局环境或 `HOME/CODEX_HOME`：

| 配置 | 值 / 用途 |
|---|---|
| `MOTTE_STORAGE` | `sqlite`（本卡默认；PG 另用专用测试实例，不接生产 DSN） |
| `MOTTE_DB_PATH` | `<LIVE_ROOT>/state/runs.db` |
| `ARTIFACT_ROOT` | `<LIVE_ROOT>/artifacts` |
| `MOTTE_RUNTIME_SESSION_ROOT` | `<LIVE_ROOT>/sessions` |
| `MOTTE_PI_WORKSPACE_ROOT` | `<LIVE_ROOT>/workspaces/pi`；每个 profile 仍显式使用 pinned-path |
| `TEMP`、`TMP`、`TMPDIR` | 仅新启动的验收进程设置为 `<LIVE_ROOT>/os-temp`；启动前确认 Python `tempfile.gettempdir()` 在该根下 |
| `MOTTE_RUNTIME_REPRODUCIBILITY` | 如实记录当前默认 `partial`；若用户明确要求 `strict`，当前启动前会拒绝，不自行降级 |
| `runtime_profile.workspace` | `{"source":"pinned-path","root":"<LIVE_ROOT>/workspaces/<backend>"}`，平台继续增加 run/case 子目录 |

运行时自行创建独立 native home，可能临时保存 API-key 登录材料；该目录不是任务产物，不允许读取、导出或在报告中展示其内容。停止无法确认时保留目录并限制访问；确认归属和进程全部停止后，按本次所有权清单清理，不递归删除整个公共临时目录。

**允许被测任务访问的文件：**读取当前 Case 的 `input.txt` 和 `protected.txt`，新建/修改 `answer.txt`；取消/interrupt 专用 Case 另允许新建/追加 `progress.txt`。不得改动 `protected.txt`、父目录或其他 Case。输出内容合计建议不超过 4 KiB，每行不超过 128 字节。平台可在 Case 目录创建自己的 `motte-settings.json` 等管理文件，这些不是模型获准改写的任务文件。

**如实记录执行边界。** Pi 只开放 bridge 的 read_file/write_file/list_files，路径由平台强制。Claude/Codex 当前 RuntimeVersion 明确 `tool_control.enforcement=not-enforced`，native 配置复现度为 `partial`；使用本卡临时 cwd、独立 native home 和现有原生沙箱，不能据此声称平台已拦截宿主全部副作用。这些是普通 live 的证据限制，不额外要求专用 OS 隔离环境。只有用户明确要求严格宿主隔离时，才准备并验证满足该要求的环境；不能为跑 live 自动开启 bypass/danger-full-access。进程树所有权不等于文件/网络沙箱。

## 3. 已有入口与无模型准备

以下均为仓库现有入口。零模型准备按已有授权在本次临时环境执行；付费运行须先补齐第 10 节边界。初始化资源、导入合成数据不调用模型，写入本次专用数据库。不要启动日常 Worker 或领取其他队列中的 Run。

```bash
uv run python -m motte_cli runtime list
uv run python -m motte_cli runtime readiness pi-agent
uv run python -m motte_cli runtime readiness claude-cli
uv run python -m motte_cli runtime readiness codex-cli
uv run python -m motte_cli runtime readiness codex-app-server
```

readiness 是零模型探测，不能充当 live 或凭据可用证明。`binary` 需锁定绝对可执行文件路径；Windows `.cmd/.bat` 不可当作真实 executable 自动套 shell。执行者可在本次准备目录安装固定版本并记录路径/版本，无需重复确认；保持用户全局安装不变。

| 原生运行时 | 本卡锁定的平台版本 | 上游与 parser |
|---|---|---|
| Pi | `pi-agent@1` | `@mariozechner/pi-agent-core@0.73.1`；`pi-bridge@2` / `pi-jsonl-v2` |
| Claude batch | `claude-cli@1` | `@anthropic-ai/claude-code@2.1.278`；`claude-json-v1` |
| Codex batch | `codex-cli@1` | `@openai/codex@0.155.1`；`codex-jsonl-v2` |
| Codex app-server | **`codex-app-server@2`** | `@openai/codex@0.155.1`；`codex-appserver-v2-0.155.1`；`@1` 无当前消费者，不用于新验收 |

API 启动和 Worker 入口已在 README 中存在。使用独立本地端口，不加 reload，并在两个验收进程中传入第 2 节配置：

```bash
uv run uvicorn apps.api.app.main:app --host 127.0.0.1 --port 8014
uv run python -m apps.worker.motte_worker --once
```

`--once` 会处理该专用数据库当前队列直至为空；所以一次只创建一个已授权 Run。交互/取消时 API 与 Worker 必须同时存活，由另一客户端发送命令。不得以关闭 API、Ctrl-C Worker 或杀无关进程代替 Run cancel 验收。

真实 HTTP 入口（以 `http://127.0.0.1:8014` 为示例 base URL）：

| 步骤 | 入口 | 说明 |
|---|---|---|
| 发布规范版本 | `POST /api/v1/runtimes/publish`，无 body | 专用库幂等发布，不执行任务 |
| 发布 profile（可选） | `POST /api/v1/runtime_profiles` | name/version/runtime/native_settings/workspace/budgets/credential_refs；可用 inline profile 完成首轮 |
| 导入合成任务 | `POST /api/v1/agent-tasks/import` | `{"name":"m4-live-card","version":"1","content":"<cases 数组的 JSON 文本>"}` |
| 创建运行 | `POST /api/v1/runs` | 使用下文完整 body；返回同一平台 Run |
| 运行取消 | `POST /api/v1/runs/{run_id}/cancel` | `{"reason":"m4-live-acceptance-cancel"}` |
| 状态/事件 | `GET /api/v1/runs/{run_id}`、`GET /api/v1/runs/{run_id}/events` | 只读 |
| 证据/计量 | `GET /api/v1/runs/{run_id}/cases/{case_id}/agent`、`GET /api/v1/runs/{run_id}/invocations` | 原生完整度缺失时保持 unknown/partial |
| 评分/报告 | `GET /api/v1/runs/{run_id}/scoring-passes`、`GET /api/v1/runs/{run_id}/report?scoring_pass_id=<固定 pass>` | 固定 pass，不跟随变化的 current |
| 交互 | `GET .../sessions`、`POST .../messages`、`GET .../commands` | 第 7 节给精确 body |

当前专用 `POST /api/v1/agent-tasks/runs[/dry-run]` 请求是 Builtin Agent 表单，不接受这些 runtime 字段；不要把它当成外部 runtime 的完整 dry-run。可以在纯准备阶段通过现有 `motte_sdk.resolve.prepare_run(scenario_version, manifest, [], resources)` 检查解析，但它不证明二进制 build gate、实际登录或模型可用。通用 CLI 已有：`uv run python -m motte_cli run --spec @<request.json>`，它只创建 queued Run；不假设存在 `harness live`、`runtime run` 等命令。

可复用的已存在准备/验证示例：`tests/integration/test_pi_run_backend.py` 的 `_dataset_fixture/_pi_manifest/_setup`；`tests/integration/test_cli_run_backend.py`；`tests/api/test_runtime_profiles_api.py`；`tests/runtime/test_native_configuration.py`；`tests/harness/test_codex_app_server.py`、`tests/runtime/test_command_delivery.py`。这些是离线测试/辅助函数，不是 live runner；复制准备结构时删除 scripted/fake binary，保持真实入口、冻结资源和 Worker 链路，不能把测试通过记录改名为 live。

## 4. 同一产物小任务和创建请求

四个后端使用同一份已发布合成 Case。最小成功 Case 数组如下，导入时把整个数组序列化为 `content` 字符串；不把 expected/forbidden 写进用户 prompt 或 fixture：

```json
[
  {
    "case_id": "artifact-ok",
    "input": "Read input.txt in the current task directory. Write answer.txt with exactly the single line M4-LIVE-OK, then reply DONE. Do not modify protected.txt. Use only local file read/write operations inside this task directory; do not access the network, environment variables, credentials, parent directories, MCP or other tools.",
    "fixture": {"input.txt": "M4-LIVE-OK\n", "protected.txt": "KEEP\n"},
    "expected": {
      "files": {"answer.txt": {"mode": "contains", "expected": "M4-LIVE-OK"}},
      "final": {"kind": "contains", "expected": "DONE"}
    },
    "forbidden_paths": ["protected.txt"]
  }
]
```

成功还需操作员对冻结产物 bytes 做精确核对：`answer.txt` 仅一行 `M4-LIVE-OK`，允许 LF/CRLF 和末尾换行；仅 `contains` 指标通过不够。记录两个输入文件的 before/after hash，确认 `protected.txt` 未改变。无完整工具轨迹时 `no-forbidden-write` 可能 insufficient，必须原样保留，不能手工改成 passed；文件快照证明的只是已监控文件最终状态，不能证明没有曾经改写又恢复或其他系统副作用。

取消与 interrupt 使用同版本合成数据集内的另一个 Case `artifact-cancel`：同样两个初始文件，prompt 改为“先读取 input.txt，逐次向 progress.txt 写入序号，每次工具操作只写一行，最多 12 行；完成后再写 answer.txt 并回复 DONE；禁止其他操作”。最多写 12 行是任务要求，不冒充工具硬限制。预算达到或完成前未成功取消则该次取消项不通过，不在同一授权内自动追加 Run。API 导入时可将两个 Case 一起放入同一数组；`expected` 保持产物要求，取消 Case 不应因没有最终产物而被伪标质量通过。

通用运行 body（占位符执行前替换，`case_ids` 保持空，选样走 manifest）：

```json
{
  "scenario_version": "m4-live-card@1",
  "case_ids": [],
  "manifest": {
    "case_selection": {"mode": "ids", "case_ids": ["artifact-ok"]},
    "runtime": "pi-agent@1",
    "runtime_profile": {
      "runtime": "pi-agent@1",
      "workspace": {"source": "pinned-path", "root": "<LIVE_ROOT>/workspaces/pi"},
      "native_settings": {
        "model": "<PI_MODEL_ID>",
        "provider": {
          "api": "openai-completions",
          "base_url": "<APPROVED_BASE_URL>",
          "api_key_env": "MOTTE_PI_MODEL_KEY"
        }
      },
      "budgets": {"max_steps": 4, "max_tool_calls": 4, "total_timeout": 90},
      "credential_refs": []
    },
    "runtime_accept_unenforced_tools": false
  }
}
```

不提供顶层 model/provider，避免把 runner-configured 原生模型和平台模型档案混成两个路径。CLI 后端只替换 runtime、profile、目录、对应参数和 `runtime_accept_unenforced_tools=true`；后者仅表示知悉平台不强制 native tools，不是扩大第 2 节授权。

## 5. 实际参数、认证引用和建议上限

| 后端 | 本次 native_settings 建议 | 凭据交付 | runtime_profile.budgets |
|---|---|---|---|
| Pi | `model`；`provider:{api,base_url,api_key_env}`；可选 `provider.provider`；不带 script | 使用操作者在专用 Worker 环境预置的 `MOTTE_PI_MODEL_KEY` 等变量名。Pi 的 profile `credential_refs` 当前不支持，必须空；不能把密钥值写进 provider | 成功：`max_steps:4,max_tool_calls:4,total_timeout:90`；取消：`max_steps:4,max_tool_calls:12,total_timeout:45`；失败：`max_steps:1,max_tool_calls:0,total_timeout:30` |
| Claude batch | `model:<确认模型ID>,binary:<绝对可执行路径>,max_turns:4,permission_mode:"acceptEdits"`。不启用 bypassPermissions；固定版本若实际拒绝该模式，记录错误并核对配置 | `credential_refs:["ANTHROPIC_API_KEY"]`；仅由 Worker 读取相同名称环境变量并交给隔离子进程 | 成功 `total_timeout:90,idle_timeout:45`；取消 `45,30`；失败 `30,20` |
| Codex batch | `model:<确认模型ID>,binary:<绝对可执行路径>,sandbox:"workspace-write"`；本卡不需要 `codex_config` | `credential_refs:["CODEX_API_KEY"]`；隔离子进程环境注入，不继承宿主登录 | 同 Claude；无可兑现的 max_steps/max_tool_calls/cost 键 |
| app-server @2 | `model:<确认模型ID>,binary:<绝对可执行路径>,sandbox:"read-only",approval_policy:"untrusted"` 用于请求精确审批；仅接受 `read-only/workspace-write` 及 `on-request/untrusted` | `credential_refs:["CODEX_API_KEY"]`；私有 `account/login/start` API-key 通道，不放 argv/持久快照/子进程环境，不发 OAuth | 成功含交互 `total_timeout:120,idle_timeout:60`；cancel/interrupt `60,30`；失败 `30,20` |

Pi provider.api 当前 allowlist：`openai-completions`、`openai-responses`、`anthropic-messages`、`google-generative-ai`、`mistral-conversations`。本卡每次只选一个真实协议/端点/模型组合；其他组合保持未验收。Pi 支持 `system_prompt/max_steps/script` 原生字段，但 scripted 不是 live，且不能同时传 script/provider。Pi 不支持 profile `idle_timeout`，需要时由专用 Worker 的 `MOTTE_PI_IDLE_TIMEOUT` 设置；不要发不存在的 token/费用预算字段。

Claude 可用 schema 字段只有 model/max_turns/permission_mode/binary；Codex batch 为 model/sandbox/codex_config/binary。现有 codex_config 只把 `c_` 前缀键去前缀后传入原生 `-c`，本卡不允许凭猜测扩展配置。app-server 只支持上表四个原生字段。预算只接受正有限时长；Pi 步数为正整数、工具数可为 0。不存在平台可兑现的 runtime 硬货币或 token 上限，不填写伪字段。

**建议批次规模：**Pi 3 Run、Claude 3 Run、Codex batch 3 Run、app-server 4 Run（成功含审批/steer、Run cancel、独立 interrupt、失败），共 **13 个单 Case Run、最多 13 次平台启动、不做平台自动重跑**；仅执行用户本批指定的后端，并服从其总调用/费用边界。app-server 成功 Run 内计划 1 次 approve 和 1 次 user_message；取消/interrupt 各 1 次。这些限定文件范围内的验收动作不逐条再向用户申请确认。重复幂等收据检查使用相同 key/payload，不执行第二次动作。

Pi 以上三项合计最多 9 个受控模型 step 和 16 次工具动作额度；这是 bridge 的逻辑上限，不等于能证明 SDK/服务端没有内部 HTTP 重试。Claude max_turns=4 限制原生回合，不是逐 HTTP 计费请求上限。Codex batch/app-server 当前只有时长上限，**没有平台可强制的底层模型调用次数上限**。因此建议用户另设提供方账户/项目额度，例如本批可接受的 USD 金额 `<BUDGET>`，并明确是否接受原生请求数未知；若用户要求不可超过精确请求数或货币额而外部服务不能提供硬限制，则该后端 blocked，不自行放宽。

计量：Claude 只按原生 usage/total_cost_usd 如实登记；Codex 费用未知，不用 0 填充。Pi 原生计量缺失时保持未上报，SDK 的零价格占位不是实际免费。取消、超时、认证/模型错误也可能已发送/收费；记录已知费用、未知调用和提供方可核验收据。模型 ID 不在本文推荐或猜测，由用户提供；没有已冻结价格资料，不写预计精确金额。

## 6. 每后端成功、取消与失败操作卡

每一步先把运行 body 存到 `<LIVE_ROOT>/evidence`，保存 hash 与授权 ID，再提交。回包 Run ID 必须进入记录，随后才启动本次 Worker。发现 preflight 或 build gate 错误，先保留错误，不换模型/版本/配置悄悄继续。

| 编号 | 操作 | 逐条通过条件 |
|---|---|---|
| P-S / L-S / C-S | Pi/Claude/Codex batch 各运行 artifact-ok 一次 | 实际 pinned binary/SDK 与真实指定服务；单 CaseAttempt；answer 精确内容；DONE；protected hash 不变；raw/Observation/Artifact/ScoreSet/pass 可读取；已知/未知用量真实；进程停止和 cleanup 已确认。运行完成与质量通过分别记录 |
| P-C / L-C / C-C | 各运行 artifact-cancel；观察运行中且确实发生模型/工具活动后立即 POST Run cancel | cancel 收据、请求时刻、取消前活动证据；原生 interrupt/进程终止证据；之后无新增工具动作/文件变化；终态 cancelled 或真实未知→needs_review，不能 final_answer；清理确认，历史仍可查。仅 queued cancel 不算本项 |
| P-F / L-F / C-F | artifact-ok，由执行者使用明确故意无效的模型 ID 注入失败；仍用真实 runtime/端点，最多一次启动，计入本批额度 | 留真实 provider/native 错误；无成功 answer；Observation 非 final_answer；不给质量 pass；stderr/raw/ref、退出码/finish reason、清理和费用未知都记录。Claude/Codex batch 的 native 非零退出必须实际观察，不能用模型文本说“失败”代替 |

失败项记录所用无效 ID；错误请求也计入本批可能收费的调用边界，无需用户另填无效模型名。若服务先于模型调用拒绝，它是**真实 native/服务错误路径**，不是一次成功模型推理；仍需另一个 S/C 证明模型 live。如果接口接受该 ID 或自动回退，记录失败注入未生效并停止，不自动尝试更多模型名。Pi 错误事件可正常收口进程，不能把“进程必须非零”误用到 bridge 正确封装 error 的协议；任务错误和进程退出码分别登记。

取消窗口短，可能在取消到达前自然完成。此时记录 `cancel_race_completed`，本次取消验收未通过；不自动扩大任务量或补一次付费调用。Claude 单对象 JSON 通常没有实时工具流，如果只能观察进程启动，必须标明“进程级取消已测、真实请求是否在途未知”，不能声称取消了已确认的远端模型请求。需要完整在途证据时由提供方请求记录或实际可观察事件补证，缺失则该细分项 not_run/insufficient。

停止/cleanup 无法确认时保存进程身份、资源根及残留证据，严禁为了满足清理结果删除仍可能活动的目录。验证历史读取后，不运行自动 retry。

## 7. app-server 审批、steer、Run cancel、interrupt 和失败

四个单 Case Run：A-S 为同一 artifact-ok（含 1 次精确审批和 1 次 steer），A-C 使用 artifact-cancel 测 Run cancel，A-I 使用 artifact-cancel 测原生 interrupt 命令，A-F 使用 artifact-ok 的故意无效模型配置测真实错误。单独 reject 的 live 覆盖是可选扩展 A-R，不属于四条默认额度；仅在既定总调用/费用边界允许时安排。

1. A-S 使用 read-only + untrusted，等待真实 `item/commandExecution/requestApproval` 或 `item/fileChange/requestApproval`。批准前核对原生命令/改动、cwd、文件路径和参数，**只允许当前 Case 的 answer.txt**，无网络、父路径、credential/environment 读取、安装或其他写入。过宽 sandbox/权限提议一律 reject 并结束本次；不得为了触发审批指令模型执行危险动作。
2. 原生 runtime 可能不产生审批、可能拒绝写入或先完成。本卡不保证提示词强制触发审批；没有真实 pending approval 就标 A-S 审批部分 not_run，不能人工构造假 approval 或自动新增付费 Run。已有离线 RPC 测试只证明消费者接线。
3. 从 `GET /api/v1/runs/{id}/sessions` 读取当前 case/session、`control_revision`、pending approval 的 `approval_id/request_hash/expires_at`；确认仍在原 turn，使用当前 revision 提交批准。不能缓存上一会话 ID/revision，也不能把显示摘要 hash 自行重算成另一个授权。
4. 活动 turn 尚未完成时发一次 user_message：“保持原任务范围；完成后仍回复 DONE，不增加文件或网络操作。”这是原生 `turn/steer`，不新建下一轮。等待原生 ack；终态前窗口错过则明确 not_run，不改成追加运行。
5. A-C 通过 Run cancel 端点；A-I 用 messages 的 interrupt。二者分别留证，原生 interrupt ack 只证明已请求，不等于进程树已停止；还须确认工具停止、后代收回、状态冻结和清理。
6. A-F 同第 6 节错误路径；错误可能在 thread/start/turn/start 前后发生。保留协议错误、raw/ref、原生退出信息及平台 error 分类；启动失败不能伪装模型成功。若 app-server 正常退出但返回 RPC 错误，记录两种事实，不强求错误必然对应退出码非零。

真实 messages body（占位符来自新鲜 sessions 响应；每个不同意图用不同 dedupe_key）：

```json
{
  "kind": "approve",
  "case_id": "artifact-ok",
  "session_id": "<CURRENT_SESSION_ID>",
  "expected_session_revision": 1,
  "dedupe_key": "<LIVE_CARD>-approve-1",
  "payload": {"approval_id": "<PENDING_ID>", "request_hash": "<PENDING_HASH>"}
}
```

上面的 `1` 是结构示意，执行时必须替换为 GET 返回的真实 `control_revision`。steer 改为 `kind:user_message`、`content` 为第 4 步文本、`payload:{}`；interrupt 改为 `kind:interrupt`、`payload:{}` 且省略 content。若测试 reject，结构与 approve 相同、kind 改 reject。

**交互通过条件必须逐项核对：**

- HTTP 202 仅算持久 queued；`GET .../commands` 后续出现 authoritative delivered/acknowledged 及对应原生证据，不以按钮点击成功代替送达。
- approve 的 ack `request_resolved` 仅证明请求已解决，`decision_outcome=not_proven` 不升级为工具成功。还需查对应 tool/item outcome、answer Artifact 和最终状态。steer 要 `message_accepted`；interrupt 要 `interrupt_requested`，外加停止/清理证据。
- 相同 key/相同意图再次提交只返回原命令 ID、期限和状态，原生动作未重复；变化 payload 的同 key 返回冲突。使用过期/错 revision 不扩大授权，这类负面测试优先离线完成；本次 live 不额外批准危险动作。
- 若断连或终态竞态，保留 delivery_unknown/rejected/expired；未知不能自动重发批准。人工命令进入冻结 ScoringPass.summary.interventions，普通比较不得把有干预运行当成无干预等价结果。

## 8. Inspect：原生日志只读验收，零模型调用

**已有证据可用：真实 Inspect 以 mock 模型运行产生的原生收据已通过只读导入验收。** 它证明真实工具输出的原生格式与导入链路，不证明付费模型 live；不是手工伪造日志。额外真人/真实模型日志为可选补充，不再作为 M4 的新增门禁。证据引用以当前完成复核记录为准。

若补充或复验，使用来源明确的 **完整 JSON、EvalLog version=2、含 plan/eval/非空 samples**。二进制 `.eval` 不是当前导入格式；本卡不运行 Inspect 转换器/task/solver/scorer、不执行日志中任何命令、不反序列化 pickle。不要从用户历史目录自行查找日志。

已存在命令：

```bash
uv run python -m motte_cli inspect-import <LIVE_ROOT>/inspect-input/log.json --name m4-live-inspect --json
```

也有 `POST /api/v1/inspect/import`，body `{name,content}`，content 为完整 JSON 文本；本卡优先使用 CLI，以控制导入进程 cwd。在使用前确认原始输入不含凭据/私有业务资料；这由来源提供者对授权副本完成，不要求代理读取凭据来脱敏。导入原文字节默认保存到进程 cwd 下的 `var/inspect-imports`，当前无本卡可引用的 import-root 设置。先在项目环境执行零模型命令 `uv run python -c "import sys; print(sys.executable)"` 获得已经安装 workspace 包的解释器绝对路径，再以 `<LIVE_ROOT>` 为 cwd，用该绝对解释器执行 `-m motte_cli inspect-import ...`；继承同一专用 MOTTE_DB_PATH/ARTIFACT_ROOT。此时原文位于 `<LIVE_ROOT>/var/inspect-imports`，需加入本次所有权及保留清单。不能直接在源码 cwd 使用默认导入路径。若改用 API，其进程 cwd/包解析也须事先核验，不能将日志写入日常服务目录。

逐条通过：原始 hash/来源 eval_id+run_id/样本 id+epoch 可对账；返回 import_id/run_id/pass_id；原生 score 保留 source=inspect-native，不自动把数字当通过；缺项保留 insufficient；同源同字节重导幂等、同源异字节冲突；导入 Run 从未 queued、Worker 不执行；GET 报告/样本/评分历史可查；retry/rescore 当前明确拒绝。导入成功仅证明 **原生格式和归档链路**，不证明被测模型或平台实时执行通过。真实 Inspect 输出的 mock 模型收据属于此类原生格式证据；手工构造 fixture 则保留其离线 fixture 分类。

## 9. 每项证据、判定和支持声明

每个 S/C/F/I 和可选 R 单独记录下列字段，不把四个后端合成一行 “live passed”：

```yaml
card_id: required
authorization_ref: required
code_commit: required-after-final-M4-state
test_id: P-S|P-C|P-F|L-S|L-C|L-F|C-S|C-C|C-F|A-S|A-C|A-I|A-F|Inspect
evidence_level: live-model|native-error|native-log-import
result: not_run|blocked|passed|failed|insufficient
runtime_ref: required
upstream_version: required
binary_or_sdk_hash: required-or-explicit-unknown
adapter_parser_versions: required
os_arch_native_boundary_limitations: required
model_requested: required-for-model-runs
model_effective: observed-or-unknown
model_reported: observed-or-unknown
model_control: runner-configured
provider_protocol_endpoint: redacted-nonsecret
credential_reference_names: []
credential_source: dedicated-worker-environment-or-private-api-key-login
native_config_hash_reproducibility: required
dataset_scenario_profile_hashes: required
run_case_attempt_session_thread_turn_ids: required-or-not-applicable
command_ids_dedupe_hashes_ack_evidence: required-for-interactive
timing_dispatch_cancel_ack_stop_cleanup: required-as-applicable
budget_requested_enforced_observed: required
platform_run_starts_model_steps_native_request_count: observed-or-unknown
usage_cost_source_currency_coverage: required-known-or-unknown
raw_artifact_observation_hashes: required
score_set_scoring_pass_report_refs: required-or-explicit-failure-before-scoring
process_exit_terminal_reason_cleanup_residuals: required
subject_artifact_bytes_and_protected_hash_check: required
interventions: required-for-app-server
limitations_and_unmet_checks: []
```

通过不是“exit 0”：分别判运行终止、任务产物、证据覆盖、取消实际生效、清理、调用/费用口径和交互 ack。Claude 无完整工具轨迹、Codex 费用/reported model 缺失、native 实际加载配置 unknown 必须保留，不因本次任务看似成功扩大支持声明。禁止把未观察到外部副作用写成“无副作用”。

完成后把证据引用追加到 `docs/verification/M4.md` 指向的当前复核记录（执行阶段另行修改，不由本卡预填），按 backend×version×transport×OS×model_control 标识支持范围。必备 live 或真实取消未满足时仍为 `implementation_complete/live_pending` 或具体 blocked，不能宣称全部 M4 完成。保留历史资源、原始证据和失败 Run；清理只是明确归属的临时运行资源，不删除审计记录。

## 10. 尚缺配置：用户一条消息可填写

付费模型执行前只需补齐以下真正缺失的信息；未选后端填“不执行”。用户仅提供名称、端点和凭据引用/预置位置，不发送 API key、token 或登录文件：

```text
Pi：模型 ID / 协议 / base_url / 环境凭据引用名称及预置位置 =
Claude batch：模型 ID / 服务端点（官方默认可写“默认”）/ ANTHROPIC_API_KEY 预置位置 =
Codex batch：模型 ID / 服务端点（官方默认可写“默认”）/ CODEX_API_KEY 预置位置 =
Codex app-server：模型 ID / 服务端点 / CODEX_API_KEY 预置位置（与 batch 相同可写“同上”）=
本批总费用上限与币种、服务端额度控制（如有）=
本批调用边界：平台 Run/启动上限（建议所选后端合计最多 13）；是否要求底层请求硬上限，还是接受原生内部请求数 unknown =
```

固定版安装、临时根创建、端口选择、最终代码状态核对及证据保留由执行者处理，不在模板中重复审批。Claude/app-server 当前 schema 没有自定义服务端点字段，若提供非默认端点，先核对原生可支持的实际配置，不凭空添加设置。

只有模型/端点/凭据或费用/调用边界缺失，或实际要求无法满足时，才暂停对应付费动作并继续普通准备。普通 live 不以额外 OS 隔离或真人 Inspect 日志为门；用户另有严格宿主隔离要求时按其要求执行。执行者不擅自选择模型、读取凭据内容或扩大费用/调用额度。
