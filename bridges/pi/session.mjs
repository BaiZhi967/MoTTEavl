/**
 * Real SDK session wrapper for the Pi bridge (M4-T03).
 *
 * Wires the upstream `Agent` loop from @mariozechner/pi-agent-core with:
 *  - a scripted (faux) model provider registered through the SDK's own
 *    provider registry — no network, no real model call, ever, from here;
 *  - bridge-side workspace tools (tools.mjs) under the platform sandbox;
 *  - agent lifecycle events streamed to a caller-supplied sink.
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

const MAX_RESPONSE_STEPS = 64;
const MAX_TEXT_BLOCK_BYTES = 512 * 1024;

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
    this.responses = scriptedResponses(config.responses);
    this.quotas = config.workspace_quotas || {};

    const workspaceRoot = initPayload.workspace;
    if (typeof workspaceRoot !== "string" || !workspaceRoot.length) {
      throw new Error("init requires a workspace root path");
    }
    this.workspaceTools = buildWorkspaceTools(workspaceRoot, this.quotas);
    this.modelCallCount = 0;
    this.toolCallCount = 0;
    this.agentEvents = [];
    this.aborted = false;
  }

  _buildAgent() {
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
    const model = this.registration.getModel();

    const agent = new Agent({
      initialState: {
        model,
        systemPrompt: this.systemPrompt,
        tools: this.workspaceTools.select(this.toolNames),
      },
      streamFn: streamSimple,
      beforeToolCall: async (context) => {
        if (
          Number.isInteger(this.budgets.max_tool_calls) &&
          this.budgets.max_tool_calls > 0 &&
          this.toolCallCount >= this.budgets.max_tool_calls
        ) {
          this.emit({
            kind: "session_event",
            agent_event: { type: "budget_stop", reason: "max_tool_calls" },
          });
          this.aborted = true;
          this.budgetStopReason = "max_tool_calls";
          return { block: true, reason: "max_tool_calls budget exceeded" };
        }
        return undefined;
      },
    });
    agent.subscribe(async (event) => {
      this.agentEvents.push(event.type);
      await this._onAgentEvent(event);
    });
    return agent;
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
          this.modelCallCount += 1;
          if (this.maxSteps && this.modelCallCount >= this.maxSteps) {
            this.budgetStopReason = this.budgetStopReason || "max_steps";
            emit({ kind: "session_event", agent_event: { type: "budget_stop", reason: "max_steps" } });
          }
        }
        break;
      case "agent_end":
        emit({ kind: "session_event", agent_event: { type: "agent_end" } });
        break;
      case "tool_execution_start":
        this.toolCallCount += 1;
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
    try {
      await this.agent.prompt(promptText);
    } catch (error) {
      this.failure = `${error && error.message ? error.message : String(error)}`;
      return this._result("error");
    }
    if (this.aborted) {
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

  _result(status) {
    return {
      status,
      final_output: this._finalOutput(),
      steps: this.modelCallCount,
      tool_calls: this.toolCallCount,
      usage: {
        // Scripted model: no real metering exists. Reporting zeros as
        // observed usage would fabricate evidence — usage stays unreported.
        reported: false,
        source: "scripted-model",
        total_tokens: null,
      },
      failure: this.failure || null,
      budget_stop: this.budgetStopReason != null,
      budget_stop_reason: this.budgetStopReason || undefined,
    };
  }
}
