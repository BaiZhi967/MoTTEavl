from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from motte_sdk.service import RunService
from motte_storage.repositories import SQLiteRepository


def create_app(repository=None) -> FastAPI:
    if repository is None:
        path = Path(os.environ.get("MOTTE_DB_PATH", "var/runs.db"))
        path.parent.mkdir(parents=True, exist_ok=True)
        repository = SQLiteRepository(path)
    service = RunService(repository)
    application = FastAPI(title="MoTTEavl API", version="0.1.0")
    application.state.run_service = service

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/api/v1/runs", status_code=202)
    def create_run(body: dict):
        scenario = body.get("scenario_version", "")
        if scenario.startswith("vision@"):
            return JSONResponse(status_code=422, content={"error": {"code": "MODEL_CAPABILITY_UNSUPPORTED", "message": "vision capability is unsupported"}})
        return service.create_run(scenario, body.get("manifest", {}))

    @application.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str):
        try:
            return service.get_run(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error

    @application.post("/api/v1/runs/{run_id}/cancel")
    def cancel_run(run_id: str):
        return service.cancel(run_id)

    @application.post("/api/v1/runs/{run_id}/rescore")
    def rescore_run(run_id: str):
        return service.rescore(run_id)

    @application.get("/api/v1/runs/{run_id}/events")
    def events(run_id: str):
        payloads = [json.dumps(event) for event in service.events(run_id)]
        return StreamingResponse((f"data: {payload}\n\n" for payload in payloads), media_type="text/event-stream")

    return application


app = create_app()
