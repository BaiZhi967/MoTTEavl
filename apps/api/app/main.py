from fastapi import FastAPI
from fastapi.responses import StreamingResponse, JSONResponse
import json

app = FastAPI(title="MoTTEavl API", version="0.1.0")
runs: dict[str, dict] = {}

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

@app.post("/api/v1/runs", status_code=202)
def create_run(body: dict):
    scenario = body.get("scenario_version", "")
    if scenario.startswith("vision@"):
        return JSONResponse(status_code=422, content={"error": {"code": "MODEL_CAPABILITY_UNSUPPORTED", "message": "vision capability is unsupported"}})
    rid = f"run-{len(runs) + 1}"
    runs[rid] = {"id": rid, "status": "queued", **body}
    return runs[rid]

@app.get("/api/v1/runs/{run_id}")
def get_run(run_id: str):
    from fastapi import HTTPException
    if run_id not in runs:
        raise HTTPException(status_code=404, detail="run not found")
    return runs[run_id]

@app.get("/api/v1/runs/{run_id}/events")
def events(run_id: str):
    payload = json.dumps({"run_id": run_id, "type": "state", "status": runs.get(run_id, {}).get("status", "unknown")})
    return StreamingResponse(iter([f"data: {payload}\n\n"]), media_type="text/event-stream")
