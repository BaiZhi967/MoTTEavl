import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import uiCss from "../src/ui.css?raw";
import themeCss from "../src/theme.css?raw";
import boardCss from "../src/board/board.css?raw";

// vitest 在 apps/web 下运行（pnpm --dir apps/web test），src 是相对工作目录的固定位置
const SRC_DIR = resolve(process.cwd(), "src");

/** 断言只看规则：注释里允许（也应该）写下迁移史与取舍理由。 */
function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

function walk(dir: string, collected: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, collected);
    else collected.push(full);
  }
  return collected;
}

/* 全局 CSS 完整性：这些断言对应审计报告里"一处代码坏，全站跟着坏"的缺陷，
 * 防止工具类重复定义、全局规则缺失、旧世界令牌回流等问题回归。
 *
 * 载入顺序（main.tsx）：semi.css → tailwind.css → ui.css → theme.css
 * 令牌只在 tailwind.css 的 @theme 与 theme.css 里定义（AGENTS.md 治理第 1 条）。 */
describe("全局样式完整性", () => {
  it("全局 box-sizing: border-box", () => {
    expect(uiCss).toMatch(/\*,\s*\*::before,\s*\*::after\s*\{\s*box-sizing:\s*border-box;?\s*\}/);
  });

  it("冗余的局部 box-sizing 已收敛到全局一条", () => {
    expect(uiCss.match(/box-sizing:\s*border-box/g)).toHaveLength(1);
  });

  it("导航链接无下划线", () => {
    const tabStart = uiCss.indexOf(".tab {");
    const tabEnd = uiCss.indexOf(".tab:hover");
    expect(uiCss.slice(tabStart, tabEnd)).toContain("text-decoration: none");
  });

  it("[hidden] 不被作者样式（display:block）复活", () => {
    expect(uiCss).toMatch(/\[hidden\]\s*\{\s*display:\s*none\s*!important;/);
  });

  it("ModelPicker 行内布局由 .model-picker-item 自己声明，不靠后代选择器", () => {
    const rule = uiCss.slice(
      uiCss.indexOf(".model-picker-item {"),
      uiCss.indexOf(".model-picker-item:hover"),
    );
    expect(rule).toContain("display: flex");
    // 旧世界靠 .operate-card .model-picker-item 的 0-2-0 去压 .operate-card label 的 display:block
    expect(uiCss).not.toContain(".operate-card");
  });

  it("工具类只定义一次", () => {
    expect(uiCss.match(/^\.actions \{/gm)).toHaveLength(1);
    expect(uiCss.match(/^\.run-progress \{/gm)).toHaveLength(1);
  });

  it("签发台字段栅格是显式原语：控件样式由 .field-grid 给定，不靠 form 这种隐式祖先", () => {
    expect(boardCss).toContain(".field-grid {");
    expect(boardCss).toContain(".field-grid input:not([type=\"checkbox\"]):not([type=\"radio\"])");
    expect(boardCss).toContain(".issue-bar {");
    // 旧世界的 12 列操作卡栅格已删除（签发台改用 FieldGrid）
    expect(uiCss).not.toContain(".operate-grid");
  });

  it("主按钮由 .primary 语义类驱动", () => {
    expect(uiCss).toContain("button.primary {");
    expect(uiCss).not.toContain('button[type="submit"]');
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

  it("token 纪律（CSS 侧）：十六进制色值只允许出现在 tailwind.css 的 @theme 与 theme.css", () => {
    const offenders: string[] = [];
    for (const file of walk(SRC_DIR)) {
      if (!file.endsWith(".css")) continue;
      const rel = file.replace(SRC_DIR, "src").replace(/\\/g, "/");
      if (rel === "src/tailwind.css" || rel === "src/theme.css") continue;
      const matches = readFileSync(file, "utf8").match(/#[0-9a-fA-F]{3,8}\b/g);
      if (matches) offenders.push(rel + " → " + matches.join(", "));
    }
    expect(offenders).toEqual([]);
  });

  it("迁移桥已拆除：旧世界令牌名不再出现在任何样式规则里", () => {
    const legacy = /--(bg-canvas|bg-surface|bg-subtle|bg-inset|text-primary|text-secondary|text-faint|on-accent|accent-hover|font-mono)\b/;
    for (const [name, css] of [["ui.css", uiCss], ["theme.css", themeCss], ["board.css", boardCss]] as const) {
      // 注释里可以（也应该）写下迁移史，断言只看规则本身
      expect(stripComments(css), name).not.toMatch(legacy);
    }
  });

  it("旧世界的圆角（6/8/9999px）已收敛到 --radius-cell / --radius-panel", () => {
    expect(stripComments(uiCss)).not.toMatch(/border-radius:\s*(6px|8px|9999px)/);
  });

  it("M5 能力提示与分段切换样式已登记（DESIGN.md 第 5 节）", () => {
    expect(uiCss).toContain(".notice {");
    expect(uiCss).toContain(".notice-info {");
    expect(uiCss).toContain(".tabs-list {");
    expect(uiCss).toContain(".tabs-trigger {");
  });
});
