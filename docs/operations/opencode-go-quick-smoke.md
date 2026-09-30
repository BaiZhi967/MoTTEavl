# OpenCode Go / Space Bunny Free：小规模编码冒烟

官方依据：[OpenCode Go](https://opencode.ai/docs/go/)（2026-09-30 核对）。
模型 ID 为 `space-bunny-free`，Chat Completions 地址为
`https://opencode.ai/zen/go/v1/chat/completions`。
官方标注**限时免费**；每次真实运行前请在官方控制台确认当前仍免费。
模型 ID 固定不能保证服务未来永远免费；脚本不会切换或回退到付费模型。

## 兼容范围

- 继续使用 `openai_compatible`，`base_url` 为 `https://opencode.ai/zen/go/v1`
- 访问官方 Go 路径时发送真实的 `User-Agent: MoTTEavl/0.1.0`
- `x-opencode-session` 来自当前 `BuiltinSession`，工具往返、多轮对话、重试及流式请求保持一致；新会话换 ID
- `CaseDrivenProvider.invoke` 和旧单次 `live-smoke` 各次调用视为独立会话
- 直接调用 `provider.complete/stream(ModelRequest(...))` 时，必须提供
  `metadata={"session_id": "your-stable-conversation-id"}`；同一对话复用该值，
  不同对话用不同值。未提供时在网络请求前拒绝，不按 provider 全局伪造一个 ID
- 保留 system/user/assistant/tool 历史、规范 function 工具声明、tool_calls、
  tool_call_id；session 不混入请求 JSON。非 OpenCode 服务不接收这个专用头
- 未冒充 OpenCode/Codex 等已验证客户端。本项目不在官方已验证客户端名单内，
  本地兼容测试不代表服务已认可或真实调用已成功
- 该接入用于正常编码代理流量，不应拿 Go 跑通用问答或整套基准测试

## 默认只预览，不读取密钥、不联网

在已完成 `uv sync` 的项目环境执行：

```sh
uv run python -m motte_cli.opencode_smoke
```

这不是全部测试集，也不会启动 benchmark/experiment。固定三个独立合成编码案例：
加法符号修复、range 边界修复、None 默认值修复。每例使用内存中的 `solution.py`，
真实经过项目的 native-tool Agent → Provider → HTTP transport；工具只读写这个小文件。
不会读取或上传真实仓库文件，不会执行模型生成的代码。

## 用户自行安全配置后再真实运行

将 `OPENCODE_GO_API_KEY` 作为秘密环境变量配置在实际执行此命令的环境里。
不要贴到聊天、命令参数、源码、run manifest、报告或 Git。脚本只在显式 `--live`
后读取这个变量，默认不调用凭据文件解析、不保存密钥；不需要持久凭据文件。
云执行环境应由用户在环境的秘密配置界面输入；本地终端可由用户直接输入隐藏变量：

```sh
read -rsp 'OpenCode Go API key: ' OPENCODE_GO_API_KEY; echo
export OPENCODE_GO_API_KEY
uv run python -m motte_cli.opencode_smoke --live --confirm-free
unset OPENCODE_GO_API_KEY
```

如用户希望使用项目已有的凭据 profile，可在用户亲自操作的交互终端执行：

```sh
uv run python -m motte_cli credentials set opencode-go
uv run python -m motte_cli.opencode_smoke --live --confirm-free --credentials opencode-go
```

第一个命令通过 `getpass` 隐藏输入，写入 0600 凭据文件。若提示不能隐藏输入，请取消。
第二个命令仅在显式 `--live --confirm-free` 后读取指定 profile；profile 不存在或损坏立即
退出，不回退到其他环境变量或凭据。默认 dry-run 即使带 `--credentials` 也不读取凭据。
如配置了 `MOTTE_CREDENTIALS_PATH`，两个命令必须指向同一个用户授权的私有文件。

`--confirm-free` 表示操作者已在自己的官方控制台确认当前 Space Bunny Free 可免费使用。
密钥缺失或未确认时退出码 2，零请求。不要为了此冒烟自动订阅、充值或接受新协议。

### 硬边界与判定

- 串行 3 个案例，每例默认最多 6 个模型回合；`--max-steps 1..6` 可下调，不能上调。
  直接读→写→最终回答共 9 次请求；允许自然复读自检，最多 18 次请求
- HTTP 发送处总上限 20（包括任何意外重试）；配置重试为 0，重定向禁用
- 固定唯一 endpoint/model；每次输出最多 512 tokens；单次 socket/Agent 期限 20 秒，整轮期限 180 秒
- 期限到了停止后续派发；底层已在途的 HTTP 请求由 socket 超时回收，不能声称服务端已取消
- 任一错误/超时/认证失败/限流/模型身份不匹配/案例未通过立即停止，无备用服务或模型
- 判定要求 read_file 与 write_file 确实发生、Agent 正常最终回答，并将 Python AST
  与简单目标实现比较。不执行模型代码；这是接入烟测，不是泛化质量评估
- 报告仅含案例、终止原因、HTTP 次数和错误分类，不包含 API key 或原始错误正文
- 输出 token 上限是请求参数；实际服务端计量是否正确仍需真实服务验证

退出码：0 = 三例通过；1 = 运行/案例失败；2 = 缺少运行前条件。
默认 dry-run 返回 0 仅表示计划可展示，不能当作 live pass。

## 离线回归（不会使用真实服务）

```sh
uv run pytest -q tests/provider/test_opencode_go.py tests/cli/test_opencode_smoke.py
```

这些测试使用 HTTP 假响应检查真实序列化、重试/session、流式、工具回灌、分案例隔离、
密钥脱敏和总请求计数。真实验收状态应单独记录；不能把 mock 通过写成 live 通过。

### 为什么默认放宽到 6 步

2026-09-30 首轮真实 3 案例、9 请求全部通过；第二轮的 range 案例在读→写后
主动再读文件复核，原来的 3 步限制阻止它给出最终回答。该失败保留为预算边界证据，
不能改写成通过。默认改为 6 步、整轮 20 次 HTTP 硬上限，保留同样的 token/时间
限制及零重试策略；没有通过提示词压制模型正常自检，也没有运行全部基准。
