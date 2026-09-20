#!/usr/bin/env node
/**
 * Pi execution bridge — protocol v2 (M4-T03).
 *
 * Drives the REAL upstream SDK (@mariozechner/pi-agent-core Agent loop) with
 * a scripted (faux) model provider and bridge-side workspace tools. stdout
 * carries versioned JSONL protocol messages only; diagnostics go to a
 * bounded, sanitized stderr. There is no echo and no builtin fallback: if
 * the SDK is unavailable, init/run fail with PI_BACKEND_UNAVAILABLE.
 *
 * Control messages (Python -> bridge, one JSON object per line):
 *   {"type":"probe"}
 *   {"type":"init","run_id","case_id","session_id","operation_id","workspace",
 *    "model":{"id","name"},"budgets":{...},"config":{"system_prompt","max_steps",
 *    "tools":[...],"responses":[[blocks]],"workspace_quotas":{...}}}
 *   {"type":"run","id","text"}
 *   {"type":"interrupt","id"}
 *
 * Events (bridge -> Python): version / ready / session_event / output /
 * tool_call / tool_result / finished / interrupted / error — every event
 * carries run/case/session/operation identity and a monotonic seq.
 */
import readline from "node:readline";
import { mkdir } from "node:fs/promises";

const BRIDGE_VERSION = "2.0.0";
const PROTOCOL_VERSION = "v2";
const MAX_ID_LENGTH = 128;
const MAX_PROMPT_BYTES = 1_000_000;
const STDERR_LIMIT_BYTES = 64 * 1024;

let stderrWritten = 0;
const originalStderrWrite = process.stderr.write.bind(process.stderr);
process.stderr.write = (chunk, ...rest) => {
  if (stderrWritten < STDERR_LIMIT_BYTES) {
    stderrWritten += chunk.length;
    originalStderrWrite(chunk, ...rest);
  }
  return true;
};

let sdk = null;
let sdkError = null;
let sdkVersion = null;

async function loadSdk() {
  if (sdk || sdkError) {
    return sdk;
  }
  try {
    const sessionModule = await import("./session.mjs");
    const pkg = await import(
      /* @vite-ignore */ "@mariozechner/pi-agent-core/package.json",
      { with: { type: "json" } }
    ).catch(() => null);
    sdkVersion = pkg && pkg.default && pkg.default.version ? pkg.default.version : null;
    sdk = sessionModule;
    return sdk;
  } catch (error) {
    sdkError = error;
    originalStderrWrite(`[pi-bridge] SDK load failed: ${error && error.code ? error.code : ""}\n`);
    return null;
  }
}

let seq = 0;
let session = null;
let activeRun = null;

function emitEvent(object) {
  process.stdout.write(`${JSON.stringify(object)}\n`);
}

function emitError(id, code, message) {
  emitEvent({ type: "error", id, seq: (seq += 1), error: { code, message } });
}

function withIdentity(payload) {
  if (!session) {
    return payload;
  }
  const { runId, caseId, sessionId, operationId } = session.identities;
  return {
    run_id: runId,
    case_id: caseId,
    session_id: sessionId,
    operation_id: operationId,
    ...payload,
  };
}

function sessionEmit(event) {
  const next = (seq += 1);
  const base = withIdentity({ type: event.kind, seq: next });
  if (event.kind === "session_event") {
    emitEvent({ ...base, agent_event: event.agent_event });
  } else if (event.kind === "output") {
    emitEvent({ ...base, text: event.text });
  } else if (event.kind === "tool_call") {
    emitEvent({ ...base, call_id: event.call_id, name: event.name, arguments: event.arguments });
  } else if (event.kind === "tool_result") {
    emitEvent({ ...base, call_id: event.call_id, name: event.name, is_error: event.is_error });
  } else {
    emitError(null, "BRIDGE_INTERNAL_ERROR", "unknown session event kind");
  }
}

function validId(value) {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= MAX_ID_LENGTH &&
    !/[\u0000-\u001f\u007f]/u.test(value)
  );
}

function hasOnlyKeys(value, allowed) {
  return Object.keys(value).every((key) => allowed.has(key));
}

async function handleProbe(message) {
  if (!hasOnlyKeys(message, new Set(["type"]))) {
    emitError(null, "INVALID_REQUEST", "invalid protocol message");
    return;
  }
  const loaded = await loadSdk();
  emitEvent({
    type: "version",
    version: BRIDGE_VERSION,
    protocol: PROTOCOL_VERSION,
    execution_ready: loaded !== null,
    sdk_version: sdkVersion,
    seq: (seq += 1),
  });
}

async function handleInit(message) {
  const allowed = new Set([
    "type", "run_id", "case_id", "session_id", "operation_id",
    "workspace", "model", "budgets", "config",
  ]);
  if (!hasOnlyKeys(message, allowed)) {
    emitError(null, "INVALID_REQUEST", "init carries unknown fields");
    return;
  }
  if (session) {
    emitError(null, "SESSION_ALREADY_INITIALIZED", "one session per bridge process");
    return;
  }
  const loaded = await loadSdk();
  if (!loaded) {
    emitError(
      null,
      "PI_BACKEND_UNAVAILABLE",
      "pi SDK is not installed; the bridge never falls back to echo or builtin execution",
    );
    return;
  }
  if (typeof message.workspace !== "string" || !message.workspace.length) {
    emitError(null, "INVALID_REQUEST", "init requires a workspace path");
    return;
  }
  try {
    await mkdir(message.workspace, { recursive: true });
    session = new loaded.PiSession(
      {
        runId: message.run_id,
        caseId: message.case_id,
        sessionId: message.session_id,
        operationId: message.operation_id,
        workspace: message.workspace,
        budgets: message.budgets || {},
        model: message.model || {},
        config: message.config || {},
      },
      sessionEmit,
    );
  } catch (error) {
    session = null;
    emitError(null, "SESSION_INIT_FAILED", "session configuration rejected");
    originalStderrWrite(`[pi-bridge] init rejected: ${error && error.message}\n`);
    return;
  }
  const next = (seq += 1);
  emitEvent({
    type: "ready",
    seq: next,
    run_id: message.run_id,
    case_id: message.case_id,
    session_id: message.session_id,
    operation_id: message.operation_id,
    sdk_version: sdkVersion,
  });
}

async function handleRun(message) {
  const allowed = new Set(["type", "id", "text"]);
  const id = validId(message.id) ? message.id : null;
  if (
    id === null ||
    typeof message.text !== "string" ||
    !hasOnlyKeys(message, allowed) ||
    Buffer.byteLength(message.text, "utf8") > MAX_PROMPT_BYTES
  ) {
    emitError(id, "INVALID_REQUEST", "invalid protocol message");
    return;
  }
  if (!session) {
    emitError(id, "SESSION_NOT_INITIALIZED", "run requires init first; no echo path exists");
    return;
  }
  if (activeRun) {
    emitError(id, "RUN_ACTIVE", "a run is already active on this session");
    return;
  }
  activeRun = (async () => {
    const result = await session.run(message.text);
    // interrupted 事件先于 finished：客户端以 finished 收口，中断标记不能迟到
    if (session.interruptRequested) {
      const ack = (seq += 1);
      emitEvent(withIdentity({ type: "interrupted", seq: ack, id, status: "cancelled" }));
    }
    const next = (seq += 1);
    emitEvent(withIdentity({ type: "finished", seq: next, id, status: result.status, result }));
    activeRun = null;
  })();
  try {
    await activeRun;
  } catch (error) {
    activeRun = null;
    emitError(id, "RUN_FAILED", "run raised an internal error");
    originalStderrWrite(`[pi-bridge] run failed: ${error && error.stack ? error.stack : error}\n`);
  }
}

async function handleInterrupt(message) {
  const allowed = new Set(["type", "id", "reason"]);
  const id = validId(message.id) ? message.id : null;
  if (id === null || !hasOnlyKeys(message, allowed)) {
    emitError(id, "INVALID_REQUEST", "invalid protocol message");
    return;
  }
  if (!session || !activeRun) {
    emitError(id, "NOTHING_TO_INTERRUPT", "no active run to interrupt");
    return;
  }
  session.interruptRequested = true;
  session.abort();
  await activeRun.catch(() => {});
}

async function handleMessage(message) {
  switch (message.type) {
    case "probe":
      await handleProbe(message);
      return;
    case "init":
      await handleInit(message);
      return;
    case "run":
      await handleRun(message);
      return;
    case "interrupt":
      await handleInterrupt(message);
      return;
    default:
      break;
  }
  const id = validId(message.id) ? message.id : null;
  emitError(id, "UNSUPPORTED_MESSAGE", "unsupported protocol message");
}

const rl = readline.createInterface({ input: process.stdin });
// 控制消息严格串行：init 的 async 工作完成前不处理后续 run/interrupt。
let processing = Promise.resolve();
rl.on("line", (line) => {
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    emitError(null, "INVALID_REQUEST", "invalid protocol message");
    return;
  }
  if (
    typeof message !== "object" ||
    message === null ||
    Array.isArray(message) ||
    typeof message.type !== "string"
  ) {
    emitError(null, "INVALID_REQUEST", "invalid protocol message");
    return;
  }
  processing = processing.then(() => handleMessage(message)).catch(() => {
    const id = validId(message.id) ? message.id : null;
    emitError(id, "BRIDGE_INTERNAL_ERROR", "bridge request failed");
  });
});
