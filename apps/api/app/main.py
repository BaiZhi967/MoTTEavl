from fastapi import FastAPI
from fastapi.responses import StreamingResponse, JSONResponse
import json
from motte_sdk.service import RunService
from motte_storage.repositories import InMemoryRepository

app = FastAPI(title="MoTTEavl API", version="0.1.0")
_repository = InMemoryRepository()
_run_service = RunService(_repository)
runs = _repository._items


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/runs", status_code=202)
def create_run(body: dict):
    scenario = body.get("scenario_version", "")
    if scenario.startswith("vision@"):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "MODEL_CAPABILITY_UNSUPPORTED",
                    "message": "vision capability is unsupported",
                }
            },
        )
    return _run_service.create_run(scenario, body.get("manifest", {}))


@app.get("/api/v1/runs/{run_id}")
def get_run(run_id: str):
    from fastapi import HTTPException

    if run_id not in runs:
        raise HTTPException(status_code=404, detail="run not found")
    return _run_service.get_run(run_id)


@app.post("/api/v1/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    return _run_service.cancel(run_id)


@app.post("/api/v1/runs/{run_id}/rescore")
def rescore_run(run_id: str):
    return _run_service.rescore(run_id)


@app.get("/api/v1/runs/{run_id}/events")
def events(run_id: str):
    payloads = [json.dumps(event) for event in _run_service.events(run_id)]
    return StreamingResponse((f"data: {payload}\n\n" for payload in payloads), media_type="text/event-stream")
