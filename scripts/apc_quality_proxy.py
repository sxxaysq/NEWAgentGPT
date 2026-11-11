"""Experiment-only model capture proxy; fixes sampling equally in all groups."""

import json
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

app = FastAPI()
ROOT = Path(os.environ["APC_EXPERIMENT_ROOT"])


@app.post("/v1/chat/completions")
async def complete(request: Request):
    body = await request.json()
    group = (ROOT / "current-group").read_text().strip()
    original_temperature = body.get("temperature")
    body.update(temperature=0, seed=42, cache_salt="apc-quality-" + group)
    started = time.time()
    async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
        response = await client.post("http://127.0.0.1:8010/v1/chat/completions", json=body)
    record = {"started": started, "elapsed_s": time.time() - started,
              "original_temperature": original_temperature, "request": body,
              "status": response.status_code, "response": response.json()}
    with (ROOT / group / "model-calls.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, ensure_ascii=False) + "\n")
    return Response(
        response.content, status_code=response.status_code, media_type="application/json"
    )
