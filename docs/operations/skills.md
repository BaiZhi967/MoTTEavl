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
- 同版本同内容幂等、异内容冲突；**唯一**允许的版本转换是 published→deprecated，
  且必须真正落库（三存储一致，见 tests/storage/test_scenario_skill_resources.py）。
- 弃用停止新选择，历史 Run 读到的 hash 与字节不变。
- 导入只做只读校验：不执行安装钩子、不下载依赖、不跑入口；路径穿越、符号链接、
  压缩炸弹、未声明执行文件、未固定依赖、凭据/虚拟环境/未知二进制一律拒绝。

## 3. 注入与权限

- 注入计划记录顺序、渲染 hash、冲突政策、资源落位与适配方式；**顺序改变 hash**。
- 有效权限 = 平台 ∩ Scenario ∩ Target 能力 ∩ Skill 请求，deny 优先：
  绝对路径与开放网络在契约层就不可声明，工具模式只能收紧（mock 不会变 real）。
- native-loader 无法观测实际加载内容 → observability=partial，不宣称完整生效。
- 指令 token 开销单列并标 estimated；不重复计入模型费用。

## 4. 命令

```bash
uv run pytest -q tests/skill
```
