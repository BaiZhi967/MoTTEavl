from fastapi import FastAPI
app=FastAPI(); runs={}
@app.get('/health')
def health(): return {'status':'ok'}
@app.post('/api/v1/runs',status_code=202)
def create_run(body:dict):
 rid=f"run-{len(runs)+1}"; runs[rid]={'id':rid,'status':'queued',**body}; return runs[rid]
