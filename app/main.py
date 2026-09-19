"""FastAPI application skeleton for the ignu pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from sse_starlette.sse import EventSourceResponse
from starlette.staticfiles import StaticFiles

from . import pipeline
from .api_state import build_state
from .db import init_db
from .events import subscribe
from .llm import verify_models

logger = logging.getLogger(__name__)

app = FastAPI(title="ignu", version="0.1.0")
_static_dir = Path(__file__).parent / "static"

# The dashboard lane supplies index.html later.  check_dir=False keeps the
# foundation importable and bootable from a clean checkout in the meantime.
app.mount("/static", StaticFiles(directory=str(_static_dir), check_dir=False), name="static")


@app.on_event("startup")
def startup() -> None:
    init_db()
    verify_models()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


async def _event_stream() -> AsyncIterator[dict[str, str]]:
    """Stream persisted/future pipeline events with a ten-second heartbeat."""

    event_iterator = subscribe().__aiter__()
    # Send an initial heartbeat so curl/browser clients know the stream is
    # live immediately, while the timeout below maintains the ten-second SLA.
    yield {
        "event": "heartbeat",
        "data": json.dumps({"ts": datetime.now(timezone.utc).isoformat()}),
    }
    try:
        while True:
            try:
                event = await asyncio.wait_for(event_iterator.__anext__(), timeout=10.0)
            except asyncio.TimeoutError:
                yield {
                    "event": "heartbeat",
                    "data": json.dumps({"ts": datetime.now(timezone.utc).isoformat()}),
                }
                continue
            except StopAsyncIteration:
                return
            yield {"event": "pipeline", "data": event.model_dump_json()}
    finally:
        await event_iterator.aclose()


@app.get("/events")
async def events() -> EventSourceResponse:
    return EventSourceResponse(_event_stream(), ping=None)


@app.get("/api/state")
def api_state() -> dict[str, Any]:
    return build_state()


def _run_request(payload: dict[str, Any], run_id: str) -> None:
    """Execute an API-triggered run outside the request thread."""

    settings = pipeline.get_settings()
    source = payload.get("file") or settings.registrations_file
    mapping = payload.get("mapping") or settings.mapping_file
    event = payload.get("event") or settings.event_name
    previous = payload.get("prev")
    ids_value = payload.get("ids")
    requested_ids = (
        [str(value).strip() for value in ids_value if str(value).strip()]
        if isinstance(ids_value, list)
        else [value.strip() for value in str(ids_value).split(",") if value.strip()]
        if ids_value
        else None
    )
    n_value = payload.get("n")
    try:
        n = int(n_value) if n_value is not None else None
    except (TypeError, ValueError):
        n = None
    try:
        slow = max(0.0, float(payload.get("slow") or 0.0))
    except (TypeError, ValueError):
        slow = 0.0

    if requested_ids is None and n is None:
        pipeline.run(source, mapping, event, run_id=run_id, slow=slow, prev_file=previous)
        return

    people = pipeline.list_people()
    if not people and Path(source).exists():
        # Prepare the local person index without doing any external work; the
        # following slice run performs the requested evidence/profile stages.
        pipeline.run(source, mapping, event, person_ids=[], run_id=run_id, prev_file=previous)
        people = pipeline.list_people()
    selected = pipeline.select_demo_ids(
        people,
        n if n is not None else len(people),
        requested_ids,
    )
    pipeline.run(
        source,
        mapping,
        event,
        person_ids=selected,
        run_id=run_id,
        slow=slow,
        prev_file=previous,
    )


@app.post("/api/run", status_code=202)
def api_run(payload: dict[str, Any] | None = None) -> dict[str, str]:
    """Start a full or selected local pipeline run in a background thread."""

    body = payload or {}
    run_id = f"api-{uuid.uuid4().hex[:12]}"
    thread = threading.Thread(
        target=_run_request,
        args=(body, run_id),
        name=f"ignu-pipeline-{run_id}",
        daemon=True,
    )
    thread.start()
    return {"status": "started", "run_id": run_id}


@app.post("/api/reset")
def api_reset(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Restore the latest full-prerun snapshot used by the demo reset button."""

    event = (payload or {}).get("event")
    return pipeline.reset_demo(str(event) if event else None)


@app.get("/", response_model=None)
def dashboard() -> FileResponse | HTMLResponse:
    index = _static_dir / "index.html"
    if index.exists():
        return FileResponse(index)
    return HTMLResponse(
        "<!doctype html><html><head><title>ignu</title></head>"
        "<body><h1>ignu</h1><p>Dashboard assets are not installed yet.</p></body></html>"
    )


__all__ = ["app"]
