import { describe, expect, it } from "vitest";
import css from "../src/index.css?raw";

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

  it("操作卡不强行等高", () => {
    const start = css.indexOf(".operate-grid {");
    const end = css.indexOf(".operate-card {");
    expect(css.slice(start, end)).toContain("align-items: flex-start");
  });
});
