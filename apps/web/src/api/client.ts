export async function getRun(id: string) { const r=await fetch(`/api/v1/runs/${id}`); return r.json(); }
