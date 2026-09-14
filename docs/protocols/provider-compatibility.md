# Provider compatibility

Provider adapter 使用统一的 `ModelRequest`/`ModelResponse` 协议，支持 `openai_chat`、`openai_responses`、`anthropic_messages` 与 `openai_compatible`。不支持的参数必须在执行前报告 `unsupported`。
