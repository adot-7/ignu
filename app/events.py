"""Persisted, thread-safe pipeline event bus."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from .db import EventRow, get_session, init_db
from .models import PipelineEvent

logger = logging.getLogger(__name__)

_Subscriber = tuple[asyncio.AbstractEventLoop, asyncio.Queue[PipelineEvent]]
_subscribers: list[_Subscriber] = []
_subscribers_lock = threading.RLock()


def _persist(event: PipelineEvent) -> None:
    """Write an event without allowing a transient DB problem to stop a stage."""

    try:
        init_db()
        with get_session() as session:
            session.add(
                EventRow(
                    ts=event.ts,
                    run_id=event.run_id,
                    stage=event.stage,
                    person_id=event.person_id,
                    status=event.status,
                    msg=event.msg,
                    data=event.data,
                )
            )
    except Exception:  # pragma: no cover - defensive path for degraded runs
        logger.exception("could not persist pipeline event", extra={"stage": event.stage})


def _discard(subscriber: _Subscriber) -> None:
    with _subscribers_lock:
        try:
            _subscribers.remove(subscriber)
        except ValueError:
            pass


def _ignore_future_error(future: Any) -> None:
    try:
        future.result()
    except Exception:
        logger.debug("event subscriber closed before receiving event", exc_info=True)


def emit(event: PipelineEvent) -> PipelineEvent:
    """Persist and fan out an event.

    ``emit`` is intentionally synchronous: pipeline stages can call it from
    ordinary worker threads.  Delivery to subscribers is scheduled onto each
    subscriber's event loop, so no asyncio queue is mutated from the worker
    thread directly.
    """

    _persist(event)
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    with _subscribers_lock:
        subscribers = list(_subscribers)
    for loop, queue in subscribers:
        if loop.is_closed():
            _discard((loop, queue))
            continue
        try:
            if current_loop is loop:
                queue.put_nowait(event)
            else:
                future = asyncio.run_coroutine_threadsafe(queue.put(event), loop)
                future.add_done_callback(_ignore_future_error)
        except RuntimeError:
            _discard((loop, queue))
    return event


async def subscribe() -> AsyncIterator[PipelineEvent]:
    """Yield future events until the consuming request disconnects."""

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[PipelineEvent] = asyncio.Queue()
    subscriber = (loop, queue)
    with _subscribers_lock:
        _subscribers.append(subscriber)
    try:
        while True:
            yield await queue.get()
    finally:
        _discard(subscriber)


async def heartbeat(interval: float = 10.0) -> AsyncIterator[dict[str, str]]:
    """Yield SSE-compatible heartbeat payloads at a bounded interval."""

    interval = max(0.01, float(interval))
    while True:
        await asyncio.sleep(interval)
        yield {
            "event": "heartbeat",
            "data": json.dumps({"ts": datetime.now(timezone.utc).isoformat()}),
        }


__all__ = ["emit", "heartbeat", "subscribe"]
