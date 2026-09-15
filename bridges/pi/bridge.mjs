#!/usr/bin/env node
/**
 * Pi bridge：MoTTEavl 与 Pi Agent 之间的版本化 JSONL 桥接进程。
 *
 * 协议 v1（与 motte_agent/protocol.py 对齐，逐行 JSON）：
 *   入站 {"type":"probe"}                        -> {"type":"version","version":"0.1.0","protocol":"v1"}
 *   入站 {"type":"prompt","id":..,"text":..}      -> {"type":"started","id"}
 *                                                 -> {"type":"output","id","text"}
 *                                                 -> {"type":"finished","id","status":"completed"}
 *   非法行                                       -> {"type":"error","error":"malformed protocol message"}
 *
 * 默认 echo 模式（确定性，供测试与冒烟）；真实 Pi runtime 接入时替换 runAgent()。
 */
import readline from "node:readline";

const BRIDGE_VERSION = "0.1.0";
const PROTOCOL_VERSION = "v1";

async function runAgent(text, emit) {
  emit({ type: "output", text });
  return text.toUpperCase();
}

function handle(message, emit) {
  if (message.type === "probe") {
    emit({ type: "version", version: BRIDGE_VERSION, protocol: PROTOCOL_VERSION });
    return;
  }
  if (message.type === "prompt") {
    const id = message.id ?? null;
    emit({ type: "started", id });
    runAgent(String(message.text ?? ""), (event) => emit({ id, ...event })).then((answer) => {
      emit({ type: "output", id, text: `answer: ${answer}` });
      emit({ type: "finished", id, status: "completed" });
    });
    return;
  }
  emit({ type: "error", error: `unsupported message type: ${message.type}` });
}

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  const emit = (event) => process.stdout.write(JSON.stringify(event) + "\n");
  let message;
  try {
    message = JSON.parse(line);
    if (typeof message !== "object" || message === null || Array.isArray(message)) throw new Error("not an object");
  } catch {
    emit({ type: "error", error: "malformed protocol message" });
    return;
  }
  handle(message, emit);
});
