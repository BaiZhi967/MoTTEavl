# Runtime 事件协议（归一化目标）

外部 runtime 事件在平台侧归一化为统一 envelope；本文是 `motte_agent`
bridge 协议与 `motte_harness` parser 事件的共享语义说明（M4-G08）。

## Bridge 协议 v2（pi-agent，stdout 仅协议）

控制消息（Python → bridge，一行一个 JSON）：

| 消息 | 说明 |
|---|---|
| `{"type":"probe"}` | → version 事件（execution_ready/sdk_version） |
| `{"type":"init", run_id, case_id, session_id, operation_id, workspace, model, budgets, config}` | → ready 事件 |
| `{"type":"run","id","text"}` | 事件流 → finished |
| `{"type":"interrupt","id"}` | → interrupted（先于 finished）→ finished(cancelled) |

事件（bridge → Python）：`version` / `ready` / `session_event` /
`output` / `tool_call` / `tool_result` / `interrupted` / `finished` /
`error`。每个事件携带 run/case/session/operation 身份与单调 `seq`；
字段 allowlist 严格校验；未知事件类型/多余字段/身份不匹配/seq 回退全部
fail closed（PI_PROTOCOL_INVALID）。错误保留 code、不透传 bridge 文本。

## CLI parser 事件（归一化 envelope 字段）

`parsers/claude.py`（claude-json-v1）与 `parsers/codex.py`
（codex-jsonl-v1）输出共享字段：

```json
{
  "parser_version": "…-v1",
  "status": "final | error | insufficient",
  "final_output": "…",
  "usage": {"reported": bool, "input_tokens": int|null, "output_tokens": int|null},
  "cost_usd": float|null,
  "model": "observed|null",
  "coverage": "complete | partial",
  "unknown_fields": ["…"],
  "raw_ref": "原始证据引用"
}
```

- `insufficient`：缺 final/未知 schema/缺终态——不冒充成功；
- `coverage=partial`：未知字段/截断/缺事件——评分与展示层按证据不完整
  处理；
- usage/cost/model 只取原生回报；缺失保持 null（不填 0/请求值）。

## 平台持久化（trace_events）

runtime 事件经 `RunService.emit_run_event` 落 `trace_events`：
`{run_id, seq, type, case_id, session_id, source_seq, parser_version,
coverage, …payload}`（`pi_*` / `<backend>_stderr` 等类型前缀）。
事件身份（run/case/session）在写库前核验；跨 Run 事件提前隔离。

## 与 Provider 流式（M4-T08 横向项）的一致性

`BaseHTTPProvider.stream` 产出受控类型 `text_delta` / `tool_delta` /
`usage` / `finish` / `error`；终态语义与 bridge `finished`、parser
`final/insufficient` 对齐：增量 → 终态收口，缺 usage 如实省略
（`tests/provider/test_streaming_contracts.py` 断言一致性）。
