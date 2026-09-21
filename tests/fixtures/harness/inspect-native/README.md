# Inspect 0.3.266 原生日志收据

`../inspect-ai-0.3.266-native-mock.json` 是官方 `inspect-ai==0.3.266` 的真实
`eval()` runner 写出的 EvalLog v2，使用官方 `mockllm/model`、内置 `generate()`
和本地确定性评分器。两个样本分数为 1.0、0.0。分类为 **actual-native-output +
mock-model**，不是实际模型 live 验证。零 token 计数是显式 mock 数据，不代表计费测量。

原始文件未重写：24,546 字节，SHA-256
`9dec3bb4c91175688bd5f93e066232da81cfdc321999a6753c2febb756479aea`。
`.gitattributes` 禁止其换行转换。原生日志中的平台路径仅是历史元数据；导入和测试
不依赖这些路径存在。来源、官方 wheel 哈希及版本见相邻 provenance JSON。

`original-capture.py.txt` 保存原始采集脚本字节，仅供审计；`generate.py` 是整理后的
可执行复现脚本，带 main guard，导入时不运行。provenance 分别记录两个脚本的哈希。
新运行会产生新时间戳和原生 ID，因此日志哈希自然不同，样本及分数语义应一致。

从仓库根目录执行以下 PowerShell 命令（使用新的 scratch 目录）：

```powershell
$probe = '.superpowers/sdd/inspect-reproduce'
New-Item -ItemType Directory -Path $probe
Copy-Item tests/fixtures/harness/inspect-native/generate.py "$probe/generate.py"
uv venv "$probe/.venv" --python 3.12
uv pip install --python "$probe/.venv/Scripts/python.exe" 'inspect-ai==0.3.266'
& "$probe/.venv/Scripts/python.exe" -X utf8 "$probe/generate.py" "$probe/output"
```

POSIX 环境将解释器路径换为 `.venv/bin/python`。脚本要求输出目录尚不存在，生成
`logs/*.json` 和 `generation-receipt.json`。它隔离 HOME/应用数据/临时目录，使用空
`.env`，不继承凭据，拒绝外部 socket 连接。`-X utf8` 避免 Windows 本地编码影响
上游元数据读取；官方 `log_realtime=False` 关闭实时 SQLite 缓冲，完整样本 JSON
仍然保留。无需个人登录或外部模型账户；根项目不需要安装 Inspect。

验证既有收据无需安装 Inspect：

```powershell
uv run pytest -q tests/harness/test_inspect_native_fixture.py
```

该测试验证文件与脚本哈希、原生来源身份、真实 fixture → SQLite 导入、重开 store
查询、原生分数、幂等重导、原文及 Artifact 字节一致、零执行 attempt。

官方来源：[固定版 PyPI](https://pypi.org/project/inspect-ai/0.3.266/)、
[EvalLog 文档](https://inspect.aisi.org.uk/eval-logs.html)。
