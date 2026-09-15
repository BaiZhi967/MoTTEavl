/** Pi bridge 自测：probe + prompt 全链路（node selftest.mjs，exit code 即结果）。 */
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const child = spawn(process.execPath, [path.join(here, "bridge.mjs")]);
let out = "";
child.stdout.on("data", (chunk) => (out += chunk));
const send = (message) => child.stdin.write(JSON.stringify(message) + "\n");

send({ type: "probe" });
send({ type: "prompt", id: "p1", text: "hello" });
setTimeout(() => {
  child.stdin.end();
  setTimeout(() => {
    const events = out.trim().split("\n").map((line) => JSON.parse(line));
    const types = events.map((event) => event.type);
    const ok =
      types[0] === "version" &&
      events[0].protocol === "v1" &&
      types.includes("started") &&
      types.includes("finished") &&
      events.at(-1).status === "completed" &&
      events.find((event) => event.type === "output" && event.text === "answer: HELLO");
    console.log(ok ? "pi bridge selftest: ok" : `pi bridge selftest: FAILED\n${out}`);
    process.exit(ok ? 0 : 1);
  }, 200);
}, 200);
