import type { Score } from "../api/client";

export function ScoreTable({ scores }: { scores: Score[] }) {
  const passed = scores.filter((score) => score.passed).length;
  return (
    <section className="panel">
      <h2>评分结果</h2>
      <p className="summary">
        共 {scores.length} 项，通过 {passed}，未通过 {scores.length - passed}
        {scores.length > 0 && `（通过率 ${Math.round((passed / scores.length) * 100)}%）`}
      </p>
      <table>
        <thead>
          <tr>
            <th>Case</th>
            <th>结果</th>
          </tr>
        </thead>
        <tbody>
          {scores.map((score) => (
            <tr key={score.case_id}>
              <td>{score.case_id}</td>
              <td className={score.passed ? "pass" : "fail"}>{score.passed ? "通过" : "未通过"}</td>
            </tr>
          ))}
          {scores.length === 0 && (
            <tr>
              <td colSpan={2} className="empty">
                暂无评分
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}
