import type { ReactNode } from "react";

/** 板面（REDESIGN-PLAN.md §5 P1）。
 *
 * 结构就是信息板的语法：吸附的表头带 + 固定列栅格 + 状态列永远在同一列位。
 * 列宽由页面用 Tailwind 工具类写在 th 上（table-layout: fixed 读首行宽度），
 * 共享语法（方角、2px 横线、行高、翻牌格）在 board.css。
 *
 * 用原生 table 而不是 Semi Table：板面需要精确的列宽控制、整幅高度下的内部滚动、
 * 以及"就地展开一行"三件事，Semi Table 自带的选择/分页/滚动 DOM 会与板面语法互相打架。
 * Semi 用在表单控件与外壳上——那才是原来真正出问题的地方。 */
export function Board({ label, head, children, testId }: {
  label: string;
  /** 表头带的一行 <th>，列宽写在这里 */
  head: ReactNode;
  children: ReactNode;
  /** 旧表带着 data-testid 时沿用，避免为了搬进板面而改动测试契约 */
  testId?: string;
}) {
  return (
    <div className="board">
      <div className="board-scroll">
        <table className="board-table" aria-label={label} data-testid={testId}>
          <thead className="board-band">
            <tr>{head}</tr>
          </thead>
          <tbody>{children}</tbody>
        </table>
      </div>
    </div>
  );
}
