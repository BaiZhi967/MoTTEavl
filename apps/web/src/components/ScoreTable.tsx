export function ScoreTable({scores}:{scores:any[]}) { return <table><caption>评分结果</caption><tbody>{scores.map((s,i)=><tr key={i}><td>{s.evaluator}</td><td>{s.value}</td></tr>)}</tbody></table> }
