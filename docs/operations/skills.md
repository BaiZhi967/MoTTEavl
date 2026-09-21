# Skill 运维说明（M5）

状态：T06（版本与安全导入）已由子包实现并提交；T07（注入与权限交集）已实现并
有测试。三臂对照（T08）只有比较条件实现，执行尚未接通。

## 1. 三种形态

| kind | entrypoint | 验证范围 | 能证明什么 | 不能证明什么 |
|---|---|---|---|---|
| instruction | 不需要 | static | manifest/指令/schema/权限声明有效 | 指令能完成任务 |
| instruction_with_resources | 不需要 | static | 资源 hash 与依赖 pin 固定 | 指令能完成任务 |
| executable | **必须** | executable-fixture | 入口在受控输入下的输出与副作用 | 所有模型都会正确使用 |

固定 Agent/模型/任务下的实际效果是第三种范围 agent-behaviour，只有真的跑了行为
测试才能这样标注。

## 2. 发布与不可变

- 资源字节进入内容寻址存储；发布再次核验字节与依赖 pin。
- **资源清单非空时，没有可读的内容存储就必须拒绝发布**（`SkillResourceStoreRequired`）：
  不能因为 `resource_store=None` 就跳过字节校验。缺失字节、size/hash 漂移、payload
  损坏分别报 `SkillResourceMissing` / `SkillResourceMismatch`。
- 依赖必须精确固定版本；配置 `dependency_resolver`（可调用对象，接收 `SkillDependency`
  返回该版本是否存在）时，解析不到或解析失败都拒绝发布（`SkillDependencyUnavailable`）。
- 同版本同内容幂等、异内容冲突；**唯一**允许的版本转换是 published→deprecated，
  且必须真正落库（三存储一致，见 tests/storage/test_scenario_skill_resources.py）。
- 弃用**只能**改 lifecycle/deprecated_*：published_at、content_hash、资源清单等发布
  元数据保持原值，否则是冲突（F20）。历史 Run 读到的 hash 与字节不变。
- 导入只做只读校验：不执行安装钩子、不下载依赖、不跑入口；路径穿越、符号链接、
  压缩炸弹、未声明执行文件、未固定依赖、凭据/虚拟环境/未知二进制一律拒绝。
- 版本读取返回隔离副本：改 `SkillRegistry.resolve()/latest()/cached()` 结果的嵌套
  schema 不会污染缓存，也不会影响相邻调用或其它 Run 的加载（F19）。

## 3. 内容存储（API 与 Worker 共享）

协议只有两个方法：`put(bytes) -> "sha256:<hex>"` 与 `get(ref) -> bytes | None`
（`motte_skill.content_store.ContentStore`）。发布与执行只依赖 `get`；写入由导入流程
的 `ingest_resources` 负责。

| 实现 | 用途 | 持久性 |
|---|---|---|
| `ContentAddressedMemoryStore` | 单元测试、草稿预览 | 仅进程内，**不可**作为发布端保证 |
| `FileContentStore(root)` | API 与 Worker 的生产路径 | 落盘，跨进程/重启可读 |

- 落盘布局：`<root>/sha256/<前两位>/<64 位 hex>`，临时目录 `<root>/tmp`。
  写入是"临时文件 → fsync → 原子 rename"，进程中途被打断不会留下可读的半个 blob；
  读取时重算 sha256，损坏的 payload 抛 `ContentStoreCorruption` 并让发布失败
  （再次 `put` 同一字节会用真实内容修复该 blob）。
- 同 hash 并发写安全：多个写者各自原子提交同一份字节，结果只有一个 blob。
- **配置**：`MOTTE_SKILL_CONTENT_ROOT`（默认 `var/skill-content`，与 `MOTTE_DB_PATH`
  同级惯例）。API 与 Worker 必须指向同一个目录（容器/主机共享卷），否则发布期核验
  通过的字节在 Worker 侧读不到。`motte_skill.content_store.create_content_store()`
  读取该变量；`SQLiteResourceStore/PostgresResourceStore/InMemoryResourceStore` 的
  `content_store=` 参数把同一个存储接到资源仓库上，`ResourceStore.publish_skill()`
  与 `motte_skill.publish_skill()` 共享同一套字节与依赖保证。

## 4. executable 入口的 argv 语法

入口是固定 `interpreter + argv`（列表，永不经 shell），且**必须绑定已发布、已核验的
资源文件**：第一个位置参数就是入口文件，必须在 `resource_manifest` 里；其余位置参数
里凡带脚本后缀或路径分隔符的 token 也必须已声明。纯指令 Skill 不要求入口。

- 内联代码与按名加载模块一律拒绝，含 `-cprint(42)`、`-econsole.log(1)`、`-p1+1`、
  `-mpdb`、`-r ./preload`、`--eval=...` 等连写/长写法，以及从 stdin 读程序的 `-`。
- 已列语法的解释器家族：python（`-c` 内联、`-m` 按名；`-X/-W/-Q` 取一个值）、
  node（`-e/-p`、`-r/--require`、`--eval`；`-C/--conditions` 等取值）、ruby、perl、
  lua、php。取值开关后面的值不会被误判成入口文件（`python -X dev run.py` 的入口是
  `run.py`）。
- 未列出语法的解释器使用保守规则：入口必须是第一个 token，任何前置开关都会使入口
  判定失败（fail closed）。
- 这些规则在契约校验、`validated_command()` 与 `publish_skill()`（发布前重新校验一次）
  三处都生效，绕过 pydantic 构造或就地改字段同样被拒绝。

## 5. 注入与权限

- 注入计划记录顺序、渲染 hash、冲突政策、资源落位与适配方式；**顺序改变 hash**。
- 有效权限 = 平台 ∩ Scenario ∩ Target 能力 ∩ Skill 请求，deny 优先：
  绝对路径与开放网络在契约层就不可声明，工具模式只能收紧（mock 不会变 real）。
- native-loader 无法观测实际加载内容 → observability=partial，不宣称完整生效。
- 指令 token 开销单列并标 estimated；不重复计入模型费用。

## 6. 命令

```bash
uv run pytest -q -m "not live" tests/skill tests/storage/test_scenario_skill_resources.py \
    tests/storage/test_resource_store.py
uv run ruff check .
```

回归证据：tests/skill/test_skill_versions.py（发布边界、argv 语法、缓存隔离、持久内容
存储）、tests/storage/test_scenario_skill_resources.py（Memory/SQLite 两后端的资源字节
与弃用语义）。
