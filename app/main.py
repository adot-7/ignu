"""FastAPI application skeleton for the ignu pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from sse_starlette.sse import EventSourceResponse
from starlette.staticfiles import StaticFiles

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
