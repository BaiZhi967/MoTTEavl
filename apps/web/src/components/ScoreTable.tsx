import type { Score } from "../api/client";
import { Board } from "../board/Board";
import { OutcomeFlap } from "../board/OutcomeFlap";
import { EmptyBoard } from "../board/EmptyBoard";

/** 评分结果：板面语法。判定列用结果格（通过 / 未通过 / 未判定），
 *  词表与运行状态词表分开——评分判定不该污染状态语义的唯一来源。 */
export function ScoreTable({ scores, embedded = false }: { scores: Score[]; embedded?: boolean }) {
  const passed = scores.filter((score) => score.passed === true).length;
  const failed = scores.filter((score) => score.passed === false).length;
  const unjudged = scores.length - passed - failed;
  const judged = passed + failed;
  return (
    <section className={embedded ? undefined : "panel"}>
      <div className="panel-head">
        <h2 className={embedded ? "embed-title" : undefined}>评分结果</h2>
        <p className="panel-summary">
          共 {scores.length} 项，通过 {passed}，未通过 {failed}
          {unjudged > 0 ? "，未判定 " + unjudged : ""}
          {judged > 0 ? "（通过率 " + Math.round((passed / judged) * 100) + "%）" : ""}
        </p>
      </div>
      <Board
        label="评分板面"
        head={
          <>
            <th className="w-[240px]">Case</th>
            <th className="w-[120px]">结果</th>
          </>
        }
      >
        {scores.map((score, index) => {
          const label = score.passed === true ? "通过" : score.passed === false ? "未通过" : "未判定";
          const tone = score.passed === true ? "success" : score.passed === false ? "error" : "neutral";
          return (
            <tr key={score.case_id ?? score.evaluator ?? index}>
              <td className="data board-id">{score.case_id ?? score.evaluator ?? "—"}</td>
              <td><OutcomeFlap label={label} tone={tone} /></td>
            </tr>
          );
        })}
        {scores.length === 0 && (
          <tr>
            <td colSpan={2} style={{ height: "auto", padding: "16px" }}>
              <EmptyBoard reason="暂无评分" next="跑完一次运行并评分后，这里会出现逐题判定" />
            </td>
          </tr>
        )}
      </Board>
    </section>
  );
}
