"""Live linen channel — ``/ws/linen``.

A browser cannot set an ``Authorization`` header on a WebSocket handshake, so
the token travels in the query string of the *upgrade* request only. Two
consequences are handled here and in ``nginx/default.conf``:

* the socket path is served by a dedicated proxy block with ``access_log off``,
  so a bearer token never lands in a log line;
* the token is verified before a single byte of laundry data is sent, and the
  laundry comes from those claims — never from anything the client says.

Wire protocol (JSON text frames, both directions)
-------------------------------------------------
server → client
    {"type": "hello", "laundry_id", "clock", "channels": [], "server_time"}
    {"type": "subscribed", "channels": [...]}      reply to subscribe/unsubscribe
    {"type": "pong", "at", "clock"}
    {"type": "error", "detail"}
    {"type": "linen.event", "event", "laundry_id", "clock", "at",
     "action", "actor", "count", "linen" | null, "linen_ids"?}

client → server
    {"type": "subscribe",   "channels": ["laundry", "linen:LL-7K4P92"]}
    {"type": "unsubscribe", "channels": ["linen:LL-7K4P92"]}
    {"type": "ping"}
    {"type": "resync"}                              → pong carrying the clock

``clock`` is the same per-laundry counter ``GET /linens/live-clock`` exposes.
A client that reconnects compares it with the last value it saw and refetches
if it fell behind, so a dropped socket cannot leave a screen silently stale.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from ..auth_helper import verify_token
from ..live_clock import read_live_clock
from ..realtime import (
    LinenConnection,
    hub,
    normalise_channel,
)
from ..tenancy import resolve_laundry_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["realtime"])

# Application close codes (4000-4999 are reserved for the application).
CLOSE_UNAUTHORISED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_BAD_FRAME = 4400

# Starlette passes a websocket straight through BaseHTTPMiddleware, so the
# HTTP rate limiter does not apply here. A handshake is cheap and the socket is
# closed immediately when the token is bad, but a client that subscribes and
# then never reads still costs one queue — bounded by QUEUE_MAXSIZE and the
# drop-on-overflow rule in ``realtime.publish``.


def _allowed_origins() -> list:
    raw = os.getenv("ALLOWED_ORIGINS", "")
    return [origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()]


def _origin_allowed(websocket: WebSocket) -> bool:
    """Reject a cross-site handshake.

    A token in the query string is not sent by the browser automatically, so
    this is defence in depth rather than the primary control — but it costs one
    string compare and stops a page on another origin from driving a socket with
    a token it should not have. Same-origin is always allowed; anything else
    must be listed in ``ALLOWED_ORIGINS``.
    """
    origin = (websocket.headers.get("origin") or "").strip().rstrip("/")
    if not origin:
        # No Origin header: a native client, not a browser. There is nothing to
        # compare against, and the token still has to be valid.
        return True
    host = (origin.split("://", 1)[-1]).lower()
    request_host = (websocket.headers.get("host") or "").lower()
    if request_host and host == request_host:
        return True
    return origin.lower() in [allowed.lower() for allowed in _allowed_origins()]


async def _pump(connection: LinenConnection) -> None:
    """Drain this connection's queue onto the wire.

    Sending lives in its own task so a scan never awaits a browser: the write
    side is the only place that can block, and only this coroutine pays for it.
    """
    try:
        while True:
            message = await connection.queue.get()
            if message is None:
                break
            await connection.websocket.send_json(message)
            if connection.closed:
                break
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        pass
    except Exception:  # pragma: no cover - transport failure, already logged by uvicorn
        logger.debug("linen socket writer stopped", exc_info=True)
    finally:
        connection.closed = True


def _channels_from(message: dict) -> list:
    raw = message.get("channels")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    channels = []
    for candidate in raw:
        channel = normalise_channel(candidate)
        if channel and channel not in channels:
            channels.append(channel)
    return channels


@router.websocket("/ws/linen")
async def linen_socket(websocket: WebSocket, token: str = Query(default="")) -> None:
    if not _origin_allowed(websocket):
        await websocket.close(code=CLOSE_FORBIDDEN)
        return

    try:
        claims = verify_token(token)
        laundry_id = resolve_laundry_id(claims)
    except HTTPException as exc:
        # Accept first, then close with an application code. A `close()` before
        # `accept()` is answered by the transport as a plain HTTP 403, which the
        # browser reports as close code 1006 — indistinguishable from a network
        # failure, so the client could only retry blindly. Closing after the
        # upgrade delivers 4401/4403, which the client uses to stop retrying and
        # re-authenticate. Nothing is sent in between: no frame follows the
        # accept.
        await websocket.accept()
        # 401 for an unusable token. A token that verifies but carries no active
        # laundry (400/403 from `resolve_laundry_id`) is *forbidden*.
        await websocket.close(
            code=CLOSE_UNAUTHORISED if exc.status_code == 401 else CLOSE_FORBIDDEN
        )
        return
    except Exception:
        await websocket.accept()
        await websocket.close(code=CLOSE_UNAUTHORISED)
        return

    await websocket.accept()

    connection = LinenConnection(
        websocket=websocket,
        laundry_id=laundry_id,
        user_id=str(claims.get("user_id") or claims.get("auth_id") or ""),
    )
    hub.register(connection)
    writer = asyncio.create_task(_pump(connection))

    try:
        # Sent through the queue so the writer stays the only sender on this
        # socket: two coroutines writing frames would interleave.
        connection.queue.put_nowait(
            {
                "type": "hello",
                "laundry_id": laundry_id,
                "clock": await read_live_clock(laundry_id),
                "channels": [],
                "server_time": datetime.now(timezone.utc).isoformat(),
            }
        )

        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                connection.queue.put_nowait({"type": "error", "detail": "Malformed frame"})
                continue
            if not isinstance(message, dict):
                connection.queue.put_nowait({"type": "error", "detail": "Malformed frame"})
                continue

            kind = str(message.get("type") or "").lower()

            if kind == "subscribe":
                added = connection.subscribe(_channels_from(message))
                connection.queue.put_nowait(
                    {"type": "subscribed", "channels": sorted(connection.channels), "added": added}
                )
            elif kind == "unsubscribe":
                removed = connection.unsubscribe(_channels_from(message))
                connection.queue.put_nowait(
                    {"type": "subscribed", "channels": sorted(connection.channels), "removed": removed}
                )
            elif kind == "ping":
                connection.queue.put_nowait(
                    {
                        "type": "pong",
                        "at": datetime.now(timezone.utc).isoformat(),
                        "clock": await read_live_clock(laundry_id),
                    }
                )
            elif kind == "resync":
                connection.queue.put_nowait(
                    {
                        "type": "pong",
                        "at": datetime.now(timezone.utc).isoformat(),
                        "clock": await read_live_clock(laundry_id),
                    }
                )
            else:
                connection.queue.put_nowait(
                    {"type": "error", "detail": f"Unknown message type: {kind or 'empty'}"}
                )

            if connection.closed:
                break
    except WebSocketDisconnect:
        pass
    except Exception:  # pragma: no cover - transport level, nothing to answer
        logger.debug("linen socket handler stopped", exc_info=True)
    finally:
        connection.closed = True
        hub.unregister(connection)
        writer.cancel()
        try:
            await writer
        except (asyncio.CancelledError, Exception):
            pass
