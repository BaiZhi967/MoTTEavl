/** 结果格：板面上的"通过 / 未通过 / 未判定"。
 *
 * 刻意不复用 StatusFlap——StatusFlap 的用词来自 STATUS_META 的封闭状态词表，
 * 而这里是评分判定，词表不同（"未通过"不是"失败"）。两者共用同一套翻牌格语法，
 * 但不共用词表：状态语义的唯一来源不能被评分判定污染。
 *
 * tone 必填：调用方必须显式给出语气，不做猜测式回退。
 * 词直接是根元素的文本子节点（不套 .flap-word）——结果格没有翻牌动画，
 * 不需要那层包装；少一层包装也让"按文本找元素"的查询保持直白。 */
export function OutcomeFlap({ label, tone }: {
  label: string;
  tone: "success" | "error" | "warning" | "info" | "neutral";
}) {
  return (
    <span className="flap" data-tone={tone}>
      <span className="flap-dot" aria-hidden />
      {label}
    </span>
  );
}
