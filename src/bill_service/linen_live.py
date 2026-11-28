"""Live linen WebSocket for the scanner clients.

The scanner opens ``/ws/linen``, sends ``{subscribe: ["laundry"]}`` and periodic
``{ping: true}`` frames. A token may arrive on the query string or as the first
message so the credential does not have to appear in the URL.

The feed is read-only. It never mutates linen state: it watches the same
per-laundry live clock the REST endpoints bump, and only relays a change frame
after subscribe. Identity, tenant scope and the read capability are checked
before any frame is sent.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from .auth_helper import ROLE_CAPABILITIES, verify_token
from .config import settings
from .live_clock import read_live_clock

router = APIRouter(tags=["linen-live"])

POLL_INTERVAL_SECONDS = 1.0
CLOSE_AUTHENTICATION = 4401
CLOSE_AUTHORIZATION = 4403
ALLOWED_CHANNELS = {"laundry", "hotel", "linen"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _origin_allowed(websocket: WebSocket) -> bool:
    """Reject a browser cross-origin socket; non-browser clients send none."""

    origin = (
        websocket.headers.get("origin")
        or websocket.headers.get("sec-websocket-origin")
        or ""
    ).strip()
    if not origin:
        return True
    allowed = {str(value).rstrip("/") for value in settings.cors_origins}
    return origin.rstrip("/") in allowed


def _can_read(principal: dict) -> bool:
    role = str(principal.get("role") or "").upper()
    if principal.get("platform_admin") or role == "PLATFORM_ADMIN":
        return True
    return "linen:read" in ROLE_CAPABILITIES.get(role, [])


def _organizations(principal: dict) -> list[str] | None:
    """Tenants this principal may watch. ``None`` means platform-wide."""

    if principal.get("platform_admin") or str(principal.get("role") or "").upper() == "PLATFORM_ADMIN":
        return None
    organizations: list[str] = []
    direct = str(principal.get("laundry_id") or "").strip()
    if direct:
        organizations.append(direct)
    for row in principal.get("laundries") or []:
        value = (
            str(row.get("laundry_id") or row.get("id") or "")
            if isinstance(row, dict)
            else str(row)
        ).strip()
        if value and value not in organizations:
            organizations.append(value)
    return organizations


async def _clock(organization_id: str) -> int:
    try:
        return await read_live_clock(organization_id) if organization_id else 0
    except Exception:
        return 0


def _change_frame(organization_id: str, clock: int) -> dict[str, Any]:
    return {
        "type": "linen.event",
        "event": "linen.changed",
        "at": _now(),
        "organization_id": organization_id or None,
        "linen_ids": [],
        "data": {"clock": clock},
    }


@router.websocket("/ws/linen")
async def linen_live_feed(
    websocket: WebSocket, token: str | None = Query(default=None)
) -> None:
    # Accept first so the client receives the explicit 44xx close code and can
    # stop its reconnect loop. No application frame is sent before checks pass.
    await websocket.accept()
    if not _origin_allowed(websocket):
        await websocket.close(code=CLOSE_AUTHORIZATION, reason="Origin not allowed")
        return

    supplied = (token or "").strip()
    handshake: dict | None = None
    if not supplied:
        try:
            received = await websocket.receive_json()
        except (WebSocketDisconnect, ValueError):
            received = None
        if isinstance(received, dict):
            handshake = received
            supplied = str(received.get("token") or "").strip()
    if not supplied:
        await websocket.close(code=CLOSE_AUTHENTICATION, reason="Authentication required")
        return

    try:
        principal = verify_token(supplied)
    except Exception:
        await websocket.close(code=CLOSE_AUTHENTICATION, reason="Invalid authentication token")
        return
    if not _can_read(principal):
        await websocket.close(code=CLOSE_AUTHORIZATION, reason="Insufficient permissions")
        return
    organizations = _organizations(principal)
    if organizations is not None and not organizations:
        await websocket.close(code=CLOSE_AUTHORIZATION, reason="Organization scope is required")
        return

    subscribed = bool(handshake and handshake.get("subscribe"))
    if subscribed:
        await websocket.send_json({"type": "subscribed", "channels": ["laundry"]})

    watch = organizations if organizations is not None else [""]
    clocks = {organization_id: await _clock(organization_id) for organization_id in watch}
    try:
        while True:
            for organization_id in list(clocks):
                current = await _clock(organization_id)
                if current == clocks[organization_id]:
                    continue
                clocks[organization_id] = current
                if subscribed:
                    await websocket.send_json(_change_frame(organization_id, current))

            try:
                message = await asyncio.wait_for(
                    websocket.receive_json(), timeout=POLL_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("subscribe"):
                subscribed = True
                channels = [
                    str(channel)
                    for channel in (message.get("subscribe") or [])
                    if str(channel) in ALLOWED_CHANNELS
                ]
                await websocket.send_json(
                    {"type": "subscribed", "channels": channels or ["laundry"]}
                )
            elif message.get("ping"):
                await websocket.send_json({"type": "pong", "at": _now()})
    except WebSocketDisconnect:
        return
    except Exception:
        await websocket.close(code=1011, reason="Linen feed failed")
