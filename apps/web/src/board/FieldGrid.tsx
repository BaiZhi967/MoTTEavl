import type { ReactNode } from "react";

/** 签发台的字段栅格（REDESIGN-PLAN.md §5 P3）。
 *
 * 两个作用，缺一不可：
 *  1. 排版：单列 measure ≤ 420px、≥1100px 两列、标签在控件上方——不再出现 1200px 宽的下拉框；
 *  2. 结构性防错：**控件样式由 .field-grid 这个显式原语按上下文给定**，
 *     所以"忘了写 className 就掉回浏览器默认样式"这条老毛病在新系统里不可能发生。
 *
 * 旧世界的问题不是"用了上下文选择器"，而是 form input 这种**隐式**上下文——任何地方的一个 form 都算数。
 * 这里是显式原语：放进 FieldGrid 就一定有样式，不放就没有。 */
export function FieldGrid({ children, columns = 2 }: { children: ReactNode; columns?: 1 | 2 | 3 }) {
  return <div className="field-grid" data-columns={columns}>{children}</div>;
}

export function Field({ label, hint, children, wide }: {
  label: string;
  /** 字段下方的说明（写清后果与单位，不写装饰性文案） */
  hint?: ReactNode;
  children: ReactNode;
  /** 跨满整行（长文本域、JSON 内容等） */
  wide?: boolean;
}) {
  return (
    <label className={wide ? "field field-wide" : "field"}>
      <span className="field-name">{label}</span>
      {children}
      {hint ? <span className="field-hint">{hint}</span> : null}
    </label>
  );
}

/** 主动作条：签发台底部唯一的 primary + 后果说明（吸底）。 */
export function IssueBar({ children, note }: { children: ReactNode; note?: ReactNode }) {
  return (
    <div className="issue-bar">
      {children}
      {note ? <span className="issue-note">{note}</span> : null}
    </div>
  );
}
