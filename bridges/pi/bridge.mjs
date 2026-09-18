#!/usr/bin/env node
/**
 * Versioned JSONL protocol stub for a future Pi execution backend.
 *
 * This package deliberately does not execute prompts. A probe reports that the
 * transport is present but execution is unavailable, and a prompt receives a
 * structured error. stdout is reserved exclusively for protocol messages.
 */
import readline from "node:readline";

const BRIDGE_VERSION = "0.1.0";
const PROTOCOL_VERSION = "v1";
const MAX_ID_LENGTH = 128;

function emit(event) {
  process.stdout.write(`${JSON.stringify(event)}\n`);
}

function isObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasOnlyKeys(value, allowed) {
  return Object.keys(value).every((key) => allowed.has(key));
}

function validId(value) {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= MAX_ID_LENGTH &&
    !/[\u0000-\u001f\u007f]/u.test(value)
  );
}

function emitError(id, code, message) {
  emit({ type: "error", id, error: { code, message } });
}

function handle(message) {
  if (message.type === "probe") {
    if (!hasOnlyKeys(message, new Set(["type"]))) {
      emitError(null, "INVALID_REQUEST", "invalid protocol message");
      return;
    }
    emit({
      type: "version",
      version: BRIDGE_VERSION,
      protocol: PROTOCOL_VERSION,
      execution_ready: false,
    });
    return;
  }

  if (message.type === "prompt") {
    const id = validId(message.id) ? message.id : null;
    if (
      id === null ||
      typeof message.text !== "string" ||
      !hasOnlyKeys(message, new Set(["type", "id", "text"]))
    ) {
      emitError(id, "INVALID_REQUEST", "invalid protocol message");
      return;
    }
    emitError(
      id,
      "PI_BACKEND_UNAVAILABLE",
      "Pi execution backend is not configured",
    );
    return;
  }

  const id = validId(message.id) ? message.id : null;
  emitError(id, "UNSUPPORTED_MESSAGE", "unsupported protocol message");
}

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    emitError(null, "INVALID_REQUEST", "invalid protocol message");
    return;
  }

  if (!isObject(message) || typeof message.type !== "string") {
    emitError(null, "INVALID_REQUEST", "invalid protocol message");
    return;
  }

  try {
    handle(message);
  } catch {
    const id = validId(message.id) ? message.id : null;
    emitError(id, "BRIDGE_INTERNAL_ERROR", "bridge request failed");
  }
});
