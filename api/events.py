"""Live updates over Server-Sent Events, with durable catch-up.

GET /events streams "changes" events. Each carries the operations that
changed and, as its SSE id, the feed horizon. A client that reconnects
sends Last-Event-ID (or ?after=) and receives everything committed
since, in order, never skipping a late commit. Events are hints to
re-read over REST; PostgreSQL stays the source of truth.

- No cursor: a "reset" event with the current horizon; read the views
  over REST, then follow.
- "truncated": too many changes to list; read the views again.
- Every poll re-checks the session in PostgreSQL, so logout, expiry or
  disablement end the stream with a "session_ended" event.
- A stream lives at most sse_max_seconds, then ends with "reconnect";
  one session may hold at most sse_max_streams streams.
"""

import asyncio
import hashlib
import json
import threading
import time
from collections import Counter

import anyio
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from api.actions import schema, secret
from api.errors import ApiError, error_body
from storage import product_api as product


router = APIRouter(tags=["events"])

CURSOR = r"^[0-9]{1,20}$"


class StreamLimiter:
    """Open streams per session, in this process."""

    def __init__(self):
        self._lock = threading.Lock()
        self._open = Counter()

    def acquire(self, key, limit):
        with self._lock:
            if self._open[key] >= limit:
                return False
            self._open[key] += 1
            return True

    def release(self, key):
        with self._lock:
            self._open[key] -= 1
            if self._open[key] <= 0:
                del self._open[key]


def frame(event, data, event_id=None):
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(data, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"


@router.get(
    "/events",
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def events(
    request: Request,
    after: str | None = Query(None, pattern=CURSOR),
):
    settings = request.app.state.settings
    session = secret(request)
    cursor = request.headers.get("last-event-id") or after
    if cursor is not None and not cursor.isdigit():
        raise ApiError(422, "invalid_request", "The request is not valid.")

    def read(current):
        with request.app.state.connection_scope() as connection:
            with connection.transaction():
                return product.changes(
                    connection, schema(request), secret=session, after=current,
                )

    # Authenticate before the stream starts, so a bad session is a plain
    # 401 and never an open stream.
    first = await anyio.to_thread.run_sync(read, cursor)

    limiter = request.app.state.stream_limiter
    key = hashlib.sha256((session or "").encode()).hexdigest()
    if not limiter.acquire(key, settings.sse_max_streams):
        return JSONResponse(
            error_body("too_many_streams",
                       "Too many live views are open for this session."),
            status_code=429,
        )

    async def stream():
        try:
            started = time.monotonic()
            beat = started
            result = first
            while True:
                horizon = result["horizon"]
                if result["reset"] or result["truncated"]:
                    yield frame(
                        "reset", {"truncated": result["truncated"]}, horizon
                    )
                elif result["changes"]:
                    yield frame(
                        "changes", {"changes": result["changes"]}, horizon
                    )

                now = time.monotonic()
                if now - started >= settings.sse_max_seconds:
                    yield frame("reconnect", {}, horizon)
                    return
                if now - beat >= settings.sse_heartbeat_seconds:
                    beat = now
                    yield ": keep-alive\n\n"

                await asyncio.sleep(settings.sse_poll_seconds)
                if await request.is_disconnected():
                    return
                try:
                    result = await anyio.to_thread.run_sync(read, horizon)
                except product.Unauthenticated:
                    yield frame("session_ended", {})
                    return
        finally:
            limiter.release(key)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
