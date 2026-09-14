export function RunTimeline({events}:{events:any[]}) { return <section><h2>运行时间线</h2><ol>{events.map((e,i)=><li key={e.seq??i}>{e.type}</li>)}</ol></section> }
