/**
 * 一次性迁移脚本：把裸 <table> 块机械改写成板面 <Board>。
 *
 * 剩余 22 张表分布在 10 个文件里，手改既要读全表又要保证 thead/tbody 包裹与作用域不出错；
 * 脚本把这件事变成一次可审阅的机械变换。它做三件事：
 *  1. 保留 <thead> 的**完整内部**（只去掉 <tr> 包裹）——表头常是
 *     {arms.map((arm) => <th key={arm.runId}>…</th>)} 这种表达式，
 *     只抽 <th> 元素会丢掉外面的 map，arm 当场变成未定义（真踩过）；
 *  2. 保留 <tbody> 内容，删掉 thead/tbody 包裹（Board 自带 tbody，留旧的就是非法嵌套）；
 *  3. 沿用原表的 aria-label / data-testid 作为 Board 的 label / testId，不改测试契约。
 *
 * 用法：node apps/web/scripts/boardify-tables.mjs <文件…>
 */
import { readFileSync, writeFileSync } from "node:fs";

const TABLE_RE = /^([ \t]*)<table([^>]*)>([\s\S]*?)<\/table>/gm;

for (const file of process.argv.slice(2)) {
  let src = readFileSync(file, "utf8");
  let count = 0;
  src = src.replace(TABLE_RE, (full, indent, attrs, body) => {
    if (attrs.includes("board-table")) return full;
    const theadMatch = body.match(/<thead[^>]*>([\s\S]*?)<\/thead>/);
    if (!theadMatch) return full;
    let headInner = theadMatch[1].trim();
    headInner = headInner.replace(/^<tr[^>]*>/, "").replace(/<\/tr>$/, "").trim();
    if (!headInner) return full;
    const testId = (attrs.match(/data-testid="([^"]+)"/) || [])[1];
    const aria = (attrs.match(/aria-label="([^"]+)"/) || [])[1];
    const label = aria || testId || "数据板面";
    let inner = body.replace(/<thead[^>]*>[\s\S]*?<\/thead>/, "");
    inner = inner.replace(/^[ \t]*<tbody>[ \t]*\n?/m, "");
    inner = inner.replace(/^[ \t]*<\/tbody>[ \t]*\n?/m, "");
    inner = inner.replace(/^\n+/, "").replace(/\n[ \t]*$/, "");
    const headLines = headInner
      .split("\n")
      .map((l) => (l.trim() === "" ? "" : indent + "    " + l.trim()))
      .join("\n");
    const out = [];
    out.push(indent + "<Board");
    if (testId) out.push(indent + '  testId="' + testId + '"');
    out.push(indent + '  label="' + label + '"');
    out.push(indent + "  head={<>");
    out.push(headLines);
    out.push(indent + "  </>}");
    out.push(indent + ">");
    out.push(inner);
    out.push(indent + "</Board>");
    count++;
    return out.join("\n");
  });
  if (count > 0) {
    if (!src.includes("board/Board")) {
      src = src.replace(/^import[^\n]*\n/m, (m) => m + 'import { Board } from "../../board/Board";' + "\n");
    }
    writeFileSync(file, src, "utf8");
  }
  console.log((count > 0 ? "OK   " : "SKIP ") + file.replace("apps/web/src/", "") + " → " + count);
}
