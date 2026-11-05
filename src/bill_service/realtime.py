"""Linen realtime fan-out over WebSocket.

Every linen mutation (scan, create, update, delete, bulk) publishes one event to
the laundry it belongs to. Screens hold a socket open and either follow the
whole laundry or a single linen's channel, so the status they are looking at
changes the moment another device scans that tag.

Three rules shape this module:

* **Publishing never blocks a write.** A scan handler must not wait on a slow
  or wedged browser socket, so ``publish_*`` is a synchronous, in-memory
  ``put_nowait`` into a bounded per-connection queue. A consumer that cannot
  keep up is dropped instead of stalling the laundry.
* **Tenant isolation is a channel property, not a filter.** A connection is
  registered under exactly one ``laundry_id`` taken from the signed token, and
  only receives events published for that laundry. Nothing a client sends can
  widen that.
* **The event carries the new state.** A linen snapshot travels with the event
  so clients patch their cache instead of refetching on every scan; a busy
  pressing station would otherwise turn one scan into a refetch storm across
  every open screen.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# Channel names accepted from a client.
#   ``laundry``       — every linen event in the caller's laundry
#   ``linen:LL-7K4P92`` — only that tag (a status screen follows one item)
CHANNEL_LAUNDRY = "laundry"
CHANNEL_LINEN_PREFIX = "linen:"

# A screen that opens a filtered list and a detail panel at once legitimately
# holds a handful of channels. The cap stops a client from turning a subscription
# list into an unbounded allocation.
MAX_CHANNELS_PER_CONNECTION = 64
MAX_CHANNEL_LENGTH = 64

# Per-connection backlog. A browser that has not read 256 events is gone, not
# slow; dropping it is what keeps one frozen tab from delaying a scan response.
QUEUE_MAXSIZE = 256


def linen_channel(code: str) -> str:
    """Channel name for one linen tag."""
    return f"{CHANNEL_LINEN_PREFIX}{str(code or '').strip().upper()}"


def normalise_channel(raw: Any) -> Optional[str]:
    """Validate a client-supplied channel name, or return ``None``.

    Only the two documented shapes are accepted. Lower-casing the tag keeps
    ``linen:ll-7k4p92`` — what a scanner may read from a badly printed label —
    on the same channel as the stored ``LL-7K4P92``.
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value or len(value) > MAX_CHANNEL_LENGTH:
        return None
    if value == CHANNEL_LAUNDRY:
        return CHANNEL_LAUNDRY
    if value.lower().startswith(CHANNEL_LINEN_PREFIX):
        code = value[len(CHANNEL_LINEN_PREFIX):].strip().upper()
        if not code:
            return None
        return linen_channel(code)
    return None


def _iso(value: Any) -> Optional[str]:
    """Serialise a Mongo datetime for JSON without losing UTC meaning."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if value is None:
        return None
    return str(value)


def linen_snapshot(doc: Optional[dict], *, extra: Optional[dict] = None) -> Optional[dict]:
    """The subset of a linen a client needs to patch its cache in place.

    Deliberately small: an event that carried the whole document would be as
    expensive to ship as the refetch it replaces.
    """
    if not isinstance(doc, dict):
        return None
    snapshot = {
        "id": str(doc.get("_id") or doc.get("id") or ""),
        "linen_id": doc.get("linen_id"),
        "status": doc.get("status"),
        "previous_status": None,
        "category": doc.get("category"),
        "item_type": doc.get("item_type"),
        "client_name": doc.get("client_name"),
        "condition": doc.get("condition"),
        "location": doc.get("location"),
        "wash_count": doc.get("wash_count"),
        "last_washed_date": _iso(doc.get("last_washed_date")),
        "last_scanned_date": _iso(doc.get("last_scanned_date")),
        "retirement_date": _iso(doc.get("retirement_date")),
        "updated_at": _iso(doc.get("updated_at")),
    }
    if extra:
        snapshot.update(extra)
    return snapshot


def build_event(
    *,
    event: str,
    laundry_id: str,
    clock: int = 0,
    linen: Optional[dict] = None,
    action: Optional[str] = None,
    actor: Optional[dict] = None,
    count: Optional[int] = None,
    linen_ids: Optional[Iterable[str]] = None,
    at: Optional[datetime] = None,
) -> dict:
    """One wire message. Every event has the same shape, so a client has one
    parser and one reducer rather than one branch per mutation."""
    payload: dict = {
        "type": "linen.event",
        "event": event,
        "laundry_id": laundry_id,
        "clock": int(clock or 0),
        "at": _iso(at or datetime.now(timezone.utc)),
        "action": action,
        "actor": actor,
        "count": count,
        "linen": linen,
    }
    if linen_ids is not None:
        payload["linen_ids"] = [str(code) for code in linen_ids if code]
    return payload


@dataclass(eq=False)
class LinenConnection:
    """One open browser socket, bound to one laundry."""

    websocket: Any
    laundry_id: str
    user_id: str = ""
    channels: set = field(default_factory=set)
    queue: "asyncio.Queue" = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_MAXSIZE))
    closed: bool = False

    def wants(self, event: dict) -> bool:
        """True when this connection subscribed to something the event touches."""
        if CHANNEL_LAUNDRY in self.channels:
            return True
        if not self.channels:
            return False
        linen = event.get("linen") or {}
        code = linen.get("linen_id")
        if code and linen_channel(code) in self.channels:
            return True
        # A bulk event may touch a hundred tags; the fan-out list lets a screen
        # that follows just one of them still hear about it.
        for candidate in event.get("linen_ids") or []:
            if linen_channel(candidate) in self.channels:
                return True
        return False

    def subscribe(self, channels: Iterable[str]) -> list:
        added = []
        for channel in channels:
            if channel in self.channels:
                continue
            if len(self.channels) >= MAX_CHANNELS_PER_CONNECTION:
                break
            self.channels.add(channel)
            added.append(channel)
        return added

    def unsubscribe(self, channels: Iterable[str]) -> list:
        removed = []
        for channel in channels:
            if channel in self.channels:
                self.channels.discard(channel)
                removed.append(channel)
        return removed


class LinenHub:
    """Registry of open sockets, keyed by laundry.

    In-process by design: the service runs as a single uvicorn worker
    (see ``Dockerfile``), so the producer and every consumer share one hub.
    A multi-worker deployment would need a shared broker in front of this
    class; the publish call sites would not change, only ``publish`` itself.
    """

    def __init__(self) -> None:
        self._connections: dict[str, set[LinenConnection]] = {}

    # ── registry ───────────────────────────────────────────────────────────
    def register(self, connection: LinenConnection) -> None:
        self._connections.setdefault(connection.laundry_id, set()).add(connection)
        logger.info(
            "linen socket open: laundry=%s user=%s connections=%d",
            connection.laundry_id,
            connection.user_id or "-",
            self.count(connection.laundry_id),
        )

    def unregister(self, connection: LinenConnection) -> None:
        bucket = self._connections.get(connection.laundry_id)
        if bucket is not None:
            bucket.discard(connection)
            if not bucket:
                self._connections.pop(connection.laundry_id, None)
        logger.info(
            "linen socket closed: laundry=%s connections=%d",
            connection.laundry_id,
            self.count(connection.laundry_id),
        )

    def count(self, laundry_id: Optional[str] = None) -> int:
        if laundry_id is None:
            return sum(len(bucket) for bucket in self._connections.values())
        return len(self._connections.get(laundry_id) or ())

    def reset(self) -> None:
        """Drop every registration. Test hook; never called by the app."""
        for bucket in list(self._connections.values()):
            for connection in bucket:
                connection.closed = True
        self._connections.clear()

    # ── fan-out ────────────────────────────────────────────────────────────
    def publish(self, event: dict) -> int:
        """Queue ``event`` for every interested socket in the event's laundry.

        Synchronous and non-blocking: called from the request path of a scan,
        which must answer the operator immediately. Returns the number of
        sockets the event was queued for (useful in tests and for logging).
        """
        laundry_id = event.get("laundry_id")
        bucket = self._connections.get(laundry_id)
        if not bucket:
            return 0
        delivered = 0
        for connection in list(bucket):
            if connection.closed or not connection.wants(event):
                continue
            try:
                connection.queue.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                # Wedged consumer. Drop it rather than block (or grow without
                # bound) on behalf of a client that stopped reading.
                logger.warning(
                    "linen socket backlog full — dropping connection: laundry=%s user=%s",
                    laundry_id,
                    connection.user_id or "-",
                )
                connection.closed = True
                try:
                    connection.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    connection.queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
        return delivered


hub = LinenHub()


def publish_linen_event(
    *,
    event: str,
    laundry_id: str,
    clock: int = 0,
    linen: Optional[dict] = None,
    action: Optional[str] = None,
    actor: Optional[dict] = None,
    count: Optional[int] = None,
    linen_ids: Optional[Iterable[str]] = None,
    at: Optional[datetime] = None,
) -> int:
    """Build and publish in one call, so a mutation site stays one line."""
    return hub.publish(
        build_event(
            event=event,
            laundry_id=laundry_id,
            clock=clock,
            linen=linen,
            action=action,
            actor=actor,
            count=count,
            linen_ids=linen_ids,
            at=at,
        )
    )


def actor_of(current_user: Optional[dict]) -> dict:
    """Who did it, for the ``scanned by`` line on every other screen."""
    user = current_user or {}
    return {
        "user_id": str(user.get("user_id") or user.get("auth_id") or ""),
        "name": str(user.get("user_name") or user.get("name") or ""),
        "role": str(user.get("role") or ""),
    }
