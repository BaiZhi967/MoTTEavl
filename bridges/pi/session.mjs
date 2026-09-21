/**
 * Real SDK session wrapper for the Pi bridge (M4-T03).
 *
 * Wires the upstream `Agent` loop from @mariozechner/pi-agent-core with two
 * explicitly distinct model transports:
 *  - "scripted" (offline): a faux provider registered through the SDK's own
 *    provider registry — no network, no real model call, usage stays
 *    unreported;
 *  - "http" (real model): the model carries `api` + optional `baseUrl`, and
 *    `streamSimple` dispatches to the SDK's real streaming provider. The API
 *    key is read from the bridge process environment (name passed by the
 *    platform — never the secret itself crosses the protocol).
 *
 * Budgets are enforced, not notified: max_steps / max_tool_calls stop the
 * loop at the next model/tool action (M4 review R08). Bridge-side workspace
 * tools (tools.mjs) run under the platform sandbox; agent lifecycle events
 * stream to a caller-supplied sink.
 *
 * This module never falls back to echo/builtin behaviour: if the SDK is
 * missing, importing fails and the bridge reports PI_BACKEND_UNAVAILABLE.
 */
import {
  Agent,
} from "@mariozechner/pi-agent-core";
import {
  fauxAssistantMessage,
  fauxText,
  fauxThinking,
  fauxToolCall,
  registerFauxProvider,
  streamSimple,
} from "@mariozechner/pi-ai";
import { buildWorkspaceTools } from "./tools.mjs";
import { observeNativeUsage } from "./usage.mjs";

const MAX_RESPONSE_STEPS = 64;
const MAX_TEXT_BLOCK_BYTES = 512 * 1024;
const SUPPORTED_REAL_APIS = new Set([
  "openai-completions",
  "openai-responses",
  "anthropic-messages",
  "google-generative-ai",
  "mistral-conversations",
]);
const DEFAULT_BASE_URLS = {
  "openai-completions": "https://api.openai.com/v1",
  "openai-responses": "https://api.openai.com/v1",
  "anthropic-messages": "https://api.anthropic.com",
  "google-generative-ai": "https://generativelanguage.googleapis.com/v1beta",
  "mistral-conversations": "https://api.mistral.ai",
};

function contentBlockFrom(payload) {
  if (!payload || typeof payload !== "object") {
    throw new Error("response block must be an object");
  }
  if (payload.type === "text" && typeof payload.text === "string") {
    if (Buffer.byteLength(payload.text, "utf8") > MAX_TEXT_BLOCK_BYTES) {
      throw new Error("text block exceeds size limit");
    }
    return fauxText(payload.text);
  }
  if (payload.type === "thinking" && typeof payload.thinking === "string") {
    return fauxThinking(payload.thinking);
  }
  if (
    payload.type === "toolCall" &&
    typeof payload.name === "string" &&
    payload.arguments && typeof payload.arguments === "object" &&
    !Array.isArray(payload.arguments)
  ) {
    // fauxToolCall 保证 call id 存在：协议事件按 call_id 关联工具调用。
    return fauxToolCall(payload.name, payload.arguments, {
      id: typeof payload.id === "string" && payload.id ? payload.id : undefined,
    });
  }
  throw new Error("unsupported response block");
}

function scriptedResponses(spec) {
  if (!Array.isArray(spec) || spec.length === 0 || spec.length > MAX_RESPONSE_STEPS) {
    throw new Error(`responses must be a non-empty array of at most ${MAX_RESPONSE_STEPS} steps`);
  }
  return spec.map((step) => {
    const blocks = Array.isArray(step) ? step : [step];
    if (!blocks.length) {
      throw new Error("response step must contain at least one block");
    }
    const content = blocks.map(contentBlockFrom);
    return fauxAssistantMessage(content, {
      stopReason: step.stopReason === "toolUse" ? "toolUse" : "stop",
    });
  });
}

function realModelFromSpec(modelSpec, providerSpec) {
  if (!modelSpec || typeof modelSpec.id !== "string" || !modelSpec.id) {
    throw new Error("http provider config requires model.id");
  }
  const api = providerSpec.api;
  if (!SUPPORTED_REAL_APIS.has(api)) {
    throw new Error(`unsupported real provider api: ${api}`);
  }
  const model = {
    id: modelSpec.id,
    name: typeof modelSpec.name === "string" && modelSpec.name ? modelSpec.name : modelSpec.id,
    api,
    provider: typeof providerSpec.provider === "string" && providerSpec.provider
      ? providerSpec.provider
      : "custom",
    baseUrl: providerSpec.base_url || DEFAULT_BASE_URLS[api],
    reasoning: false,
    input: ["text"],
    // SDK requires prices to normalize token usage. These placeholders are
    // never exposed as observed monetary cost by the platform.
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: 128000,
    maxTokens: 4096,
  };
  if (typeof providerSpec.base_url === "string" && providerSpec.base_url) {
    model.baseUrl = providerSpec.base_url;
  }
  // provider 实现按模态字段分流（如 image 输入）；未声明时默认纯文本。
  model.input = Array.isArray(modelSpec.input) ? modelSpec.input : ["text"];
  return model;
}

function realApiKeyFromSpec(providerSpec) {
  // key 只经环境名下发（协议里永不出现秘密原文）；未声明环境名时交给
  // SDK 的 provider 环境解析（如 provider=openai 读 OPENAI_API_KEY）。
  if (typeof providerSpec.api_key_env === "string" && providerSpec.api_key_env) {
    const key = process.env[providerSpec.api_key_env];
    if (!key) {
      throw new Error(
        `provider api key env ${providerSpec.api_key_env} is not set in the bridge process`,
      );
    }
    return key;
  }
  return undefined;
}

export class PiSession {
  constructor(initPayload, emit) {
    if (!initPayload || typeof initPayload !== "object") {
      throw new Error("init payload required");
    }
    const { runId, caseId, sessionId, operationId } = initPayload;
    if (![runId, caseId, sessionId, operationId].every((value) => typeof value === "string" && value.length > 0)) {
      throw new Error("init requires run/case/session/operation identities");
    }
    this.identities = { runId, caseId, sessionId, operationId };
    this.emit = emit;
    this.budgets = initPayload.budgets || {};
    this.modelSpec = initPayload.model || {};
    const config = initPayload.config || {};
    this.systemPrompt = typeof config.system_prompt === "string" ? config.system_prompt : "";
    this.configTokensPerSecond = Number(config.tokens_per_second);
    this.maxSteps = Number.isInteger(config.max_steps) && config.max_steps > 0
      ? Math.min(config.max_steps, MAX_RESPONSE_STEPS)
      : MAX_RESPONSE_STEPS;
    this.toolNames = Array.isArray(config.tools) ? config.tools : [];
    this.quotas = config.workspace_quotas || {};

    // 传输模式二选一，显式区分：scripted（离线）或 http（真实模型）。
    // 真实模型在构造期就解析：凭据缺失/协议名错误在 init 即失败（fail fast），
    // 不留到 run 中途。
    const providerSpec = config.provider;
    if (providerSpec != null) {
      if (config.responses != null) {
        throw new Error("init cannot carry both scripted responses and an http provider");
      }
      this.transportMode = "http";
      this.providerSpec = providerSpec;
      this.responses = null;
      this.realModel = realModelFromSpec(this.modelSpec, providerSpec);
      this.modelApiKey = realApiKeyFromSpec(providerSpec);
    } else {
      this.transportMode = "scripted";
      this.providerSpec = null;
      this.realModel = null;
      this.modelApiKey = undefined;
      this.responses = scriptedResponses(config.responses);
    }

    const workspaceRoot = initPayload.workspace;
    if (typeof workspaceRoot !== "string" || !workspaceRoot.length) {
      throw new Error("init requires a workspace root path");
    }
    this.workspaceTools = buildWorkspaceTools(workspaceRoot, this.quotas);
    this.modelCallCount = 0;
    this.toolCallCount = 0;
    this.agentEvents = [];
    this.aborted = false;
    this.budgetStopReason = null;
    // 真实模型的原生计量（scripted 保持未上报，不伪造）。
    this.nativeUsage = null;
    this.usageCalls = 0;
  }

  _buildModel() {
    if (this.transportMode === "http") {
      return this.realModel;
    }
    const models = [{
      id: this.modelSpec.id || "scripted-1",
      name: this.modelSpec.name || "Scripted model",
    }];
    const registrationOptions = { models };
    if (
      Number.isFinite(this.configTokensPerSecond) &&
      this.configTokensPerSecond > 0
    ) {
      registrationOptions.tokensPerSecond = this.configTokensPerSecond;
    }
    this.registration = registerFauxProvider(registrationOptions);
    this.registration.setResponses(this.responses);
    return this.registration.getModel();
  }

  _buildAgent() {
    const model = this._buildModel();
    const session = this;
    // 真实传输：streamSimple 的 key 走 options.apiKey（模型对象上的 key
    // 不会被 provider wrapper 读取）；scripted 传输直接用 streamSimple。
    const streamFn = (modelArg, context, options) => {
      session.nativeUsageSeen = {};
      if (session.modelCallCount >= session.maxSteps) {
        session._budgetStop("max_steps");
        throw new Error("max_steps budget exceeded");
      }
      session.modelCallCount += 1;
      return streamSimple(modelArg, context, {
        ...options,
        ...(session.modelApiKey ? { apiKey: session.modelApiKey } : {}),
      });
    };
    const agent = new Agent({
      initialState: {
        model,
        systemPrompt: this.systemPrompt,
        tools: this.workspaceTools.select(this.toolNames),
      },
      streamFn,
      beforeToolCall: async () => {
        // SDK 先发 tool_execution_start 再调 beforeToolCall：预算计数必须
        // 在放行处自增（事件回调里的计数会把第一个工具就判超限）。
        if (
          Number.isInteger(session.budgets.max_tool_calls) &&
          session.budgets.max_tool_calls >= 0 &&
          session.toolCallCount >= session.budgets.max_tool_calls
        ) {
          session._budgetStop("max_tool_calls");
          return { block: true, reason: "max_tool_calls budget exceeded" };
        }
        session.toolCallCount += 1;
        return undefined;
      },
    });
    this.agent = agent;
    agent.subscribe(async (event) => {
      this.agentEvents.push(event.type);
      await this._onAgentEvent(event);
    });
    return agent;
  }

  _budgetStop(reason) {
    if (this.budgetStopReason == null) {
      this.budgetStopReason = reason;
    }
    this.emit({ kind: "session_event", agent_event: { type: "budget_stop", reason } });
    // 预算是强制边界：停掉 agent loop，后续模型/工具动作不再发生（R08）。
    if (this.agent) {
      this.agent.abort();
    }
  }

  async _onAgentEvent(event) {
    const emit = this.emit;
    switch (event.type) {
      case "message_start":
      case "message_update":
      case "message_end":
      case "turn_start":
      case "turn_end":
      case "agent_start":
        emit({ kind: "session_event", agent_event: { type: event.type } });
        if (event.type === "message_end" && event.message && event.message.role === "assistant") {
          const text = (event.message.content || [])
            .filter((block) => block.type === "text")
            .map((block) => block.text)
            .join("");
          if (text) {
            emit({ kind: "output", text });
          }
          if (event.message.stopReason === "error") {
            this.failure = event.message.errorMessage || "model stream failed";
          } else if (event.message.stopReason === "aborted") {
            this.aborted = true;
            this.failure = event.message.errorMessage || "model stream aborted";
          }
          const usage = event.message.usage;
          if (this.transportMode === "http" && this.nativeUsageSeen && usage && typeof usage === "object") {
            // 真实流的原生计量：只在字段可辨时如实上报。
            const input = Object.hasOwn(this.nativeUsageSeen, "input") ? this.nativeUsageSeen.input : null;
            const output = Object.hasOwn(this.nativeUsageSeen, "output") ? this.nativeUsageSeen.output : null;
            // SDK totalTokens may be synthesized from default-zero components.
            // Preserve an explicit native total even when its components are absent.
            const total = Object.hasOwn(this.nativeUsageSeen, "total") ? this.nativeUsageSeen.total
              : (input != null && output != null && Number.isFinite(usage.totalTokens) ? usage.totalTokens : null);
            if (input != null || output != null || total != null) {
              const prior = this.nativeUsage || { input_tokens: 0, output_tokens: 0, total_tokens: 0 };
              const add = (previous, current) => previous == null || current == null ? null : previous + current;
              this.nativeUsage = {
                input_tokens: add(prior.input_tokens, input),
                output_tokens: add(prior.output_tokens, output),
                total_tokens: add(prior.total_tokens, total),
              };
              this.usageCalls += 1;
            }
          }
        }
        break;
      case "agent_end":
        emit({ kind: "session_event", agent_event: { type: "agent_end" } });
        break;
      case "tool_execution_start":
        emit({
          kind: "tool_call",
          call_id: event.toolCallId,
          name: event.toolName,
          arguments: event.args,
        });
        break;
      case "tool_execution_end":
        if (event.isError) {
          const detail = event.result && event.result.content
            ? event.result.content.map((block) => (block && block.text ? block.text : "")).join("")
            : "";
          // 诊断只留首行且限长：validation 错误可能回显参数 JSON，不进 stderr。
          process.stderr.write(
            `[pi-bridge] tool error: ${detail.split("\n")[0].slice(0, 120)}\n`,
          );
        }
        emit({
          kind: "tool_result",
          call_id: event.toolCallId,
          name: event.toolName,
          is_error: Boolean(event.isError),
        });
        break;
      default:
        break;
    }
  }

  async run(promptText) {
    if (typeof promptText !== "string" || !promptText.length) {
      throw new Error("run requires non-empty prompt text");
    }
    if (this.agent) {
      throw new Error("session already ran; one run per operation");
    }
    this.agent = this._buildAgent();
    const restoreFetch = this.transportMode === "http"
      ? observeNativeUsage((field, value) => { this.nativeUsageSeen[field] = value; })
      : () => {};
    try {
      await this.agent.prompt(promptText);
    } catch (error) {
      if (this.interruptRequested || this.budgetStopReason != null) {
        // abort 触发的异常不是执行错误：按取消/预算收口。
        return this._result("cancelled");
      }
      this.failure = `${error && error.message ? error.message : String(error)}`;
      return this._result("error");
    } finally {
      restoreFetch();
    }
    if (this.interruptRequested || this.aborted) {
      return this._result("cancelled");
    }
    if (this.failure) {
      return this._result("error");
    }
    if (this.budgetStopReason) {
      return this._result("cancelled");
    }
    return this._result("completed");
  }

  abort() {
    this.aborted = true;
    if (this.agent) {
      this.agent.abort();
    }
  }

  _finalOutput() {
    if (!this.agent) {
      return null;
    }
    const messages = this.agent.state.messages || [];
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (message && message.role === "assistant") {
        const text = (message.content || [])
          .filter((block) => block.type === "text")
          .map((block) => block.text)
          .join("")
          .trim();
        if (text) {
          return text;
        }
      }
    }
    return null;
  }

  _usage() {
    if (this.transportMode === "http") {
      if (this.nativeUsage && this.usageCalls === this.modelCallCount) {
        return {
          reported: true,
          source: "native-model",
          input_tokens: this.nativeUsage.input_tokens,
          output_tokens: this.nativeUsage.output_tokens,
          total_tokens: this.nativeUsage.total_tokens,
        };
      }
      // 真实传输但流未回报计量：保持未上报，不伪造 0。
      return { reported: false, source: "native-model", total_tokens: null };
    }
    // Scripted model: no real metering exists. Reporting zeros as
    // observed usage would fabricate evidence — usage stays unreported.
    return { reported: false, source: "scripted-model", total_tokens: null };
  }

  _result(status) {
    return {
      status,
      final_output: this._finalOutput(),
      steps: this.modelCallCount,
      tool_calls: this.toolCallCount,
      usage: this._usage(),
      transport: this.transportMode,
      failure: this.failure || null,
      budget_stop: this.budgetStopReason != null,
      budget_stop_reason: this.budgetStopReason || undefined,
    };
  }
}
