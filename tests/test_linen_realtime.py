"""The live linen channel: fan-out, isolation, and what a scan actually sends.

Specified behaviour (see `bill_service/realtime.py` and `routers/realtime.py`):

- an event only reaches sockets registered for the laundry that published it;
- a socket following one tag hears about that tag and nothing else;
- a scan publishes the post-scan row, so a watching screen patches instead of
  refetching;
- a token is required, and the laundry comes from its claims.
"""
import asyncio

import pytest

from bill_service.auth_helper import create_access_token
from bill_service.models import LinenCreate, LinenScanAction
from bill_service.realtime import (
    CHANNEL_LAUNDRY,
    LinenConnection,
    hub,
    linen_channel,
    linen_snapshot,
    normalise_channel,
    publish_linen_event,
)
from bill_service.routers import linens
from bill_service.routers.realtime import linen_socket
from conftest import LAUNDRY_A, LAUNDRY_B, tenant_user


class FakeSocket:
    """Stands in for Starlette's WebSocket.

    Frames arrive from a queue rather than a list so the test can keep the
    handler alive while it asserts on what was sent — the real handler blocks in
    ``receive_text`` and a list would end the loop before the writer task ran.
    """

    def __init__(self, headers=None):
        self.headers = headers or {}
        self.sent = []
        self.closed_code = None
        self.accepted = False
        self.inbox = asyncio.Queue()

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000):
        self.closed_code = code

    async def send_json(self, message):
        self.sent.append(message)

    async def receive_text(self):
        payload = await self.inbox.get()
        if payload is None:
            from fastapi import WebSocketDisconnect

            raise WebSocketDisconnect()
        return payload

    # ── test helpers ───────────────────────────────────────────────────────
    def push(self, payload):
        if isinstance(payload, str):
            self.inbox.put_nowait(payload)
        else:
            import json

            self.inbox.put_nowait(json.dumps(payload))

    def disconnect(self):
        self.inbox.put_nowait(None)

    def of_type(self, kind):
        return [message for message in self.sent if message.get("type") == kind]


async def _until(predicate, timeout=2.0):
    """Wait for an async condition without assuming a fixed frame order."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def socket_for(laundry_id, *, channels=(CHANNEL_LAUNDRY,), user="u-1"):
    connection = LinenConnection(
        websocket=FakeSocket(),
        laundry_id=laundry_id,
        user_id=user,
        channels=set(channels),
    )
    return connection


@pytest.fixture(autouse=True)
def clean_hub():
    hub.reset()
    yield
    hub.reset()


# ── channel names ─────────────────────────────────────────────────────────
def test_channel_names_are_normalised():
    assert normalise_channel("laundry") == CHANNEL_LAUNDRY
    assert normalise_channel(" linen:ll-7k4p92 ") == "linen:LL-7K4P92"
    assert normalise_channel("linen:") is None
    assert normalise_channel("everyone") is None
    assert normalise_channel("") is None
    assert normalise_channel(None) is None
    assert normalise_channel("linen:" + "X" * 100) is None


def test_snapshot_serialises_datetimes_and_ids():
    from datetime import datetime, timezone

    now = datetime(2026, 11, 5, 10, 30, tzinfo=timezone.utc)
    snapshot = linen_snapshot(
        {
            "_id": "abc123",
            "linen_id": "LL-7K4P92",
            "status": "WASHING",
            "wash_count": 4,
            "updated_at": now,
        },
        extra={"previous_status": "AT_LAUNDRY"},
    )
    assert snapshot["id"] == "abc123"
    assert snapshot["linen_id"] == "LL-7K4P92"
    assert snapshot["previous_status"] == "AT_LAUNDRY"
    assert snapshot["updated_at"] == now.isoformat()


# ── fan-out ───────────────────────────────────────────────────────────────
def test_event_reaches_only_its_own_laundry():
    mine = socket_for(LAUNDRY_A)
    theirs = socket_for(LAUNDRY_B)
    hub.register(mine)
    hub.register(theirs)

    delivered = publish_linen_event(
        event="scan",
        laundry_id=LAUNDRY_A,
        clock=7,
        linen={"linen_id": "LL-AAAAAA", "status": "WASHING"},
    )

    assert delivered == 1
    assert mine.queue.qsize() == 1
    assert theirs.queue.qsize() == 0


def test_single_linen_channel_hears_only_that_tag():
    watcher = socket_for(LAUNDRY_A, channels=[linen_channel("LL-WATCH1")])
    hub.register(watcher)

    publish_linen_event(
        event="scan",
        laundry_id=LAUNDRY_A,
        linen={"linen_id": "LL-OTHER1", "status": "READY"},
    )
    assert watcher.queue.qsize() == 0

    publish_linen_event(
        event="scan",
        laundry_id=LAUNDRY_A,
        linen={"linen_id": "LL-WATCH1", "status": "PRESSING"},
    )
    assert watcher.queue.qsize() == 1


def test_bulk_event_reaches_a_watcher_of_one_of_its_tags():
    watcher = socket_for(LAUNDRY_A, channels=[linen_channel("LL-INBULK")])
    hub.register(watcher)

    publish_linen_event(
        event="bulk_scan",
        laundry_id=LAUNDRY_A,
        count=3,
        linen_ids=["LL-INBULK", "LL-B", "LL-C"],
    )

    assert watcher.queue.qsize() == 1


def test_unsubscribed_socket_hears_nothing():
    silent = socket_for(LAUNDRY_A, channels=[])
    hub.register(silent)

    assert publish_linen_event(event="scan", laundry_id=LAUNDRY_A) == 0
    assert silent.queue.qsize() == 0


def test_publish_without_listeners_is_a_no_op():
    assert publish_linen_event(event="scan", laundry_id="nobody-home", clock=1) == 0


def test_a_wedged_socket_is_dropped_instead_of_blocking_the_writer():
    stuck = socket_for(LAUNDRY_A)
    stuck.queue = asyncio.Queue(maxsize=1)
    hub.register(stuck)

    publish_linen_event(event="scan", laundry_id=LAUNDRY_A, linen={"linen_id": "LL-1"})
    publish_linen_event(event="scan", laundry_id=LAUNDRY_A, linen={"linen_id": "LL-2"})

    assert stuck.closed is True
    assert hub.count(LAUNDRY_A) == 1  # still registered; the handler unregisters


# ── the socket endpoint ───────────────────────────────────────────────────
def _token(laundry_id=LAUNDRY_A):
    user = tenant_user("live-user", laundry_id, "ADMIN")
    return create_access_token(
        {
            "auth_id": user["auth_id"],
            "user_id": user["auth_id"],
            "user_name": user["user_name"],
            "role": user["role"],
            "laundry_id": laundry_id,
            "laundries": user["laundries"],
        }
    )


async def test_bad_token_is_closed_with_an_application_code():
    """The upgrade is accepted so the browser can read a close code.

    A `close()` before `accept()` reaches the client as HTTP 403, which the
    WebSocket API reports as 1006 — the same code as a dropped network — and the
    client would retry a token that can never work.
    """
    socket = FakeSocket()
    await linen_socket(socket, token="not-a-token")
    assert socket.accepted is True
    assert socket.closed_code == 4401
    assert socket.sent == []  # nothing was streamed before the refusal


async def test_token_without_a_laundry_is_forbidden():
    token = create_access_token({"auth_id": "u-x", "role": "ADMIN", "laundries": []})
    socket = FakeSocket()
    await linen_socket(socket, token=token)
    assert socket.closed_code == 4403
    assert socket.sent == []


async def test_hello_then_subscribe_then_receive_a_scan(mocked_db):
    socket = FakeSocket()
    task = asyncio.create_task(linen_socket(socket, token=_token()))

    assert await _until(lambda: socket.of_type("hello")), "no hello frame"
    hello = socket.of_type("hello")[0]
    assert hello["laundry_id"] == LAUNDRY_A
    assert hello["clock"] == 0
    assert socket.accepted is True

    socket.push({"type": "subscribe", "channels": ["laundry", "linen:LL-7K4P92", "bogus"]})
    assert await _until(lambda: socket.of_type("subscribed"))
    assert socket.of_type("subscribed")[0]["channels"] == ["laundry", "linen:LL-7K4P92"]

    publish_linen_event(
        event="scan",
        laundry_id=LAUNDRY_A,
        clock=3,
        action="start_wash",
        linen={"linen_id": "LL-7K4P92", "status": "WASHING"},
    )
    assert await _until(lambda: socket.of_type("linen.event"))
    event = socket.of_type("linen.event")[0]
    assert event["action"] == "start_wash"
    assert event["linen"]["status"] == "WASHING"

    socket.push({"type": "resync"})
    assert await _until(lambda: socket.of_type("pong"))

    socket.disconnect()
    await asyncio.wait_for(task, timeout=2)
    assert hub.count(LAUNDRY_A) == 0


async def test_malformed_frame_is_answered_not_dropped():
    socket = FakeSocket()
    task = asyncio.create_task(linen_socket(socket, token=_token()))

    assert await _until(lambda: socket.of_type("hello"))
    socket.push("{not json")
    assert await _until(lambda: socket.of_type("error"))

    socket.disconnect()
    await asyncio.wait_for(task, timeout=2)


async def test_foreign_origin_is_refused():
    socket = FakeSocket(headers={"origin": "https://evil.example", "host": "bills.local"})
    await linen_socket(socket, token=_token())
    assert socket.accepted is False
    assert socket.closed_code == 4403


# ── what a real mutation publishes ────────────────────────────────────────
async def test_scan_publishes_the_post_scan_row(mocked_db):
    user = tenant_user("rt-scan", LAUNDRY_A, "ADMIN")
    watcher = socket_for(LAUNDRY_A)
    hub.register(watcher)

    made = await linens.create_linen(
        LinenCreate(category="Bed", item_type="Sheet", client_name="Hotel X"), user
    )
    # Drain the create event so this test only inspects the scan.
    while not watcher.queue.empty():
        watcher.queue.get_nowait()

    await linens.scan_linen(made["id"], LinenScanAction(action="collect", location="Bay 2"), user)

    assert watcher.queue.qsize() == 1
    event = watcher.queue.get_nowait()
    assert event["event"] == "scan"
    assert event["action"] == "collect"
    assert event["linen"]["linen_id"] == made["linen_id"]
    assert event["linen"]["status"] == "COLLECTED"
    assert event["linen"]["previous_status"] == "IN_STOCK"
    assert event["linen"]["location"] == "Bay 2"
    assert event["clock"] > 0
    assert event["actor"]["user_id"] == "rt-scan"


async def test_scan_is_not_published_to_another_laundry(mocked_db):
    user = tenant_user("rt-scan-a", LAUNDRY_A, "ADMIN")
    outsider = socket_for(LAUNDRY_B)
    hub.register(outsider)

    made = await linens.create_linen(
        LinenCreate(category="Bed", item_type="Sheet", client_name="Hotel X"), user
    )
    await linens.scan_linen(made["id"], LinenScanAction(action="collect"), user)

    assert outsider.queue.qsize() == 0


async def test_bulk_scan_publishes_a_summary_and_each_item(mocked_db):
    user = tenant_user("rt-bulk", LAUNDRY_A, "ADMIN")
    watcher = socket_for(LAUNDRY_A, channels=[CHANNEL_LAUNDRY])
    # A screen following one tag should still hear about it via the summary and
    # via that tag's own item event.
    follower = socket_for(LAUNDRY_A, channels=[linen_channel("PLACEHOLDER")])
    hub.register(watcher)

    made = [
        await linens.create_linen(
            LinenCreate(category="Bed", item_type="Sheet", client_name="Hotel X"), user
        )
        for _ in range(2)
    ]
    follower.channels = {linen_channel(made[1]["linen_id"])}
    hub.register(follower)

    while not watcher.queue.empty():
        watcher.queue.get_nowait()

    await linens.bulk_scan_linens(
        [item["linen_id"] for item in made], action="collect", location=None, user=None,
        current_user=user,
    )

    received = _drain(watcher)
    summary = [m for m in received if m["event"] == "bulk_scan"]
    items = [m for m in received if m["event"] == "scan"]
    assert summary and summary[0]["count"] == 2
    assert {m["linen"]["linen_id"] for m in items} == {item["linen_id"] for item in made}
    assert all(m["action"] == "collect" for m in items)

    follower_events = _drain(follower)
    assert any(
        m["event"] == "scan" and m["linen"]["linen_id"] == made[1]["linen_id"]
        for m in follower_events
    )


def _drain(connection):
    drained = []
    while not connection.queue.empty():
        item = connection.queue.get_nowait()
        if item is not None:
            drained.append(item)
    return drained
