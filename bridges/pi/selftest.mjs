#!/usr/bin/env node
/**
 * Pi bridge self-test (exit code is the result).
 *
 * Drives the packaged bridge end-to-end against the REAL SDK: probe →
 * init (scripted model that calls write_file) → run → finished, then
 * asserts the tool actually wrote the file in the workspace. Fails if
 * the SDK is missing — a missing SDK must not be reported as passing.
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const workspace = await mkdtemp(path.join(tmpdir(), "pi-bridge-selftest-"));

const child = spawn(process.execPath, [path.join(here, "bridge.mjs")], {
  stdio: ["pipe", "pipe", "pipe"],
});
let stdout = "";
let stderr = "";
child.stdout.on("data", (chunk) => (stdout += chunk));
child.stderr.on("data", (chunk) => (stderr += chunk));

const events = [];
const closed = new Promise((resolve, reject) => {
  const timeout = setTimeout(() => {
    child.kill();
    reject(new Error("bridge self-test timed out"));
  }, 20_000);
  child.on("error", (error) => {
    clearTimeout(timeout);
    reject(error);
  });
  child.on("close", (code) => {
    clearTimeout(timeout);
    resolve(code);
  });
});

function send(message) {
  child.stdin.write(`${JSON.stringify(message)}\n`);
}

const readied = new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error("ready event never arrived")), 10_000);
  const poll = () => {
    if (events.some((event) => event.type === "ready")) {
      clearTimeout(timer);
      resolve();
    } else {
      setTimeout(poll, 20);
    }
  };
  poll();
});

child.stdout.on("data", () => {
  // events are parsed below in order; this hook only drives readied
});

// line-split collector
let buffer = "";
child.stdout.on("data", (chunk) => {
  buffer += chunk;
  let index;
  while ((index = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, index);
    buffer = buffer.slice(index + 1);
    if (line.trim()) {
      events.push(JSON.parse(line));
    }
  }
});

send({ type: "probe" });
// wait for version
await new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error("version event never arrived")), 10_000);
  const poll = () => {
    if (events.some((event) => event.type === "version")) {
      clearTimeout(timer);
      resolve();
    } else {
      setTimeout(poll, 20);
    }
  };
  poll();
});

send({
  type: "init",
  run_id: "selftest-run",
  case_id: "selftest-case",
  session_id: "selftest-session",
  operation_id: "selftest-operation",
  workspace,
  model: { id: "scripted-1", name: "Selftest scripted model" },
  budgets: { max_tool_calls: 8 },
  config: {
    system_prompt: "You write files when asked.",
    max_steps: 8,
    tools: ["read_file", "write_file", "list_files"],
    responses: [
      [
        { type: "toolCall", name: "write_file", arguments: { path: "hello.txt", content: "hello from scripted model" } },
      ],
      [{ type: "text", text: "Created hello.txt." }],
    ],
  },
});
await readied;

send({ type: "run", id: "run-1", text: "please create hello.txt" });
child.stdin.end();

try {
  assert.equal(await closed, 0, stderr);
  const version = events.find((event) => event.type === "version");
  assert.equal(version.protocol, "v2");
  assert.equal(version.execution_ready, true, "SDK must be installed for the selftest");
  assert.match(version.sdk_version || "", /^\d+\.\d+\.\d+$/);

  const ready = events.find((event) => event.type === "ready");
  assert.equal(ready.session_id, "selftest-session");

  const toolCall = events.find((event) => event.type === "tool_call");
  assert.ok(toolCall, "real SDK must emit a tool_call event");
  assert.equal(toolCall.name, "write_file");
  assert.equal(toolCall.run_id, "selftest-run");
  assert.equal(toolCall.case_id, "selftest-case");

  const finished = events.find((event) => event.type === "finished");
  assert.equal(finished.status, "completed");
  assert.equal(finished.result.tool_calls, 1);
  // scripted final output is echoed nowhere: the prompt must not appear as output
  const outputs = events.filter((event) => event.type === "output").map((event) => event.text);
  assert.ok(!outputs.some((text) => text.includes("please create hello.txt")));

  const written = await readFile(path.join(workspace, "hello.txt"), "utf8");
  assert.equal(written, "hello from scripted model");

  // seq strictly increasing on protocol events
  const seqs = events.map((event) => event.seq);
  assert.deepEqual(seqs, [...seqs].sort((a, b) => a - b));

  console.log("pi bridge selftest: ok (real SDK, scripted model, file written)");
} catch (error) {
  console.error(`pi bridge selftest: FAILED\n${error.message}`);
  process.exitCode = 1;
} finally {
  await rm(workspace, { recursive: true, force: true });
}
