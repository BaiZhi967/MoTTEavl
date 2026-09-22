import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import css from "../src/index.css?raw";

// vitest 在 apps/web 下运行（pnpm --dir apps/web test），src 是相对工作目录的固定位置
const SRC_DIR = resolve(process.cwd(), "src");

function walk(dir: string, collected: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, collected);
    else collected.push(full);
  }
  return collected;
}

/* 全局 CSS 完整性：这些断言对应审计报告里"一处代码坏，全站跟着坏"的缺陷，
 * 防止工具类重复定义、全局规则缺失等问题回归。 */
describe("全局样式完整性", () => {
  it("全局 box-sizing: border-box", () => {
    expect(css).toMatch(/\*,\s*\*::before,\s*\*::after\s*\{\s*box-sizing:\s*border-box;?\s*\}/);
  });

  it("冗余的局部 box-sizing 已收敛到全局一条", () => {
    expect(css.match(/box-sizing:\s*border-box/g)).toHaveLength(1);
  });

  it("导航链接无下划线", () => {
    const tabStart = css.indexOf(".tab {");
    const tabEnd = css.indexOf(".tab:hover");
    expect(css.slice(tabStart, tabEnd)).toContain("text-decoration: none");
  });

  it("[hidden] 不被作者样式（display:block）复活", () => {
    expect(css).toMatch(/\[hidden\]\s*\{\s*display:\s*none\s*!important;/);
  });

  it("ModelPicker 行内布局压过 .operate-card label 的 display:block", () => {
    expect(css).toContain(".operate-card .model-picker-item {");
  });

  it("工具类只定义一次", () => {
    expect(css.match(/\.actions\s*\{/g)).toHaveLength(1);
    expect(css.match(/\.run-progress\s*\{/g)).toHaveLength(1);
  });

  it("操作卡走 12 列栅格并按语义跨列（不再等宽平分）", () => {
    const start = css.indexOf(".operate-grid {");
    const end = css.indexOf(".operate-card {");
    const grid = css.slice(start, end);
    expect(grid).toContain("grid-template-columns: repeat(12, minmax(0, 1fr))");
    // 行内卡片同高 → 行与行之间没有参差的底边
    expect(grid).toContain("align-items: stretch");
    expect(css).toContain(".operate-card.wide {");
    expect(css).toContain(".operate-card.rail {");
    expect(css).toContain(".operate-card.full {");
  });

  it("主按钮由 .primary 语义类驱动", () => {
    expect(css).toContain("button.primary {");
    expect(css).not.toContain('button[type="submit"]');
  });

  it("token 纪律：组件与脚本不直接写十六进制色值（唯一来源是 :root）", () => {
    const offenders: string[] = [];
    for (const file of walk(SRC_DIR)) {
      if (!/\.(ts|tsx)$/.test(file)) continue;
      const matches = readFileSync(file, "utf8").match(/#[0-9a-fA-F]{3,8}\b/g);
      if (matches) offenders.push(file.replace(SRC_DIR, "src") + " → " + matches.join(", "));
    }
    expect(offenders).toEqual([]);
  });

  it("M5 能力提示与分段切换样式已登记（DESIGN.md 第 4 节）", () => {
    expect(css).toContain(".notice {");
    expect(css).toContain(".notice-info {");
    expect(css).toContain(".tabs-list {");
    expect(css).toContain(".tabs-trigger {");
  });
});
