/** Pi bridge protocol-stub self-test (exit code is the result). */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const child = spawn(process.execPath, [path.join(here, "bridge.mjs")]);
let stdout = "";
let stderr = "";
child.stdout.on("data", (chunk) => (stdout += chunk));
child.stderr.on("data", (chunk) => (stderr += chunk));

const closed = new Promise((resolve, reject) => {
  const timeout = setTimeout(() => {
    child.kill();
    reject(new Error("bridge self-test timed out"));
  }, 2_000);
  child.on("error", (error) => {
    clearTimeout(timeout);
    reject(error);
  });
  child.on("close", (code) => {
    clearTimeout(timeout);
    resolve(code);
  });
});

child.stdin.write(`${JSON.stringify({ type: "probe" })}\n`);
child.stdin.write(
  `${JSON.stringify({ type: "prompt", id: "p1", text: "hello" })}\n`,
);
child.stdin.end();

try {
  assert.equal(await closed, 0, stderr);
  const events = stdout
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
  assert.deepEqual(events, [
    {
      type: "version",
      version: "0.1.0",
      protocol: "v1",
      execution_ready: false,
    },
    {
      type: "error",
      id: "p1",
      error: {
        code: "PI_BACKEND_UNAVAILABLE",
        message: "Pi execution backend is not configured",
      },
    },
  ]);
  console.log("pi bridge selftest: ok");
} catch (error) {
  console.error(`pi bridge selftest: FAILED\n${error.message}`);
  process.exitCode = 1;
}
