"""Smoke-test ``/ws/linen`` against a real uvicorn server.

Unit tests drive the handler directly and cannot catch the failures that only
appear on a live socket: a missing WebSocket protocol implementation in the
uvicorn install (``Unsupported upgrade request``), a proxy that strips
``Upgrade``, or a server that never flushes a frame. This script boots the
actual router with uvicorn on a local port, connects as a browser would, and
checks that a published event arrives.

    python scripts/ws_smoke.py

Everything runs in-process against an in-memory Mongo, so it touches no
database. Exit code 0 means the handshake, subscribe, fan-out and auth
rejection all behaved.
"""
from __future__ import annotations

import os
import sys
import threading
import time

os.environ.setdefault("JWT_SECRET", "smoke-only-jwt-secret-value-32-chars")
os.environ.setdefault("MASTER_KEY", "smoke-only-master-key")
os.environ.setdefault("ENCRYPTION_MODE", "single")

import uvicorn
from fastapi import FastAPI
from mongomock_motor import AsyncMongoMockClient
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

import bill_service.live_clock as live_clock
from bill_service.auth_helper import create_access_token
from bill_service.realtime import linen_channel, publish_linen_event
from bill_service.routers.realtime import router

LAUNDRY = "smoke-laundry"
PORT = 8791

# The router reads the clock on hello; point that at an in-memory collection
# before the app ever serves a request.
live_clock.live_clocks_collection = AsyncMongoMockClient()["smoke"]["live_clocks"]

app = FastAPI()
app.include_router(router)


@app.get("/_smoke/push")
async def push() -> dict:
    """Publish from inside the server's own loop — the same context a scan
    handler runs in, so the queue is written by the loop that reads it."""
    delivered = publish_linen_event(
        event="scan",
        laundry_id=LAUNDRY,
        clock=5,
        action="start_wash",
        linen={
            "id": "doc-1",
            "linen_id": "LL-SMOKE1",
            "status": "WASHING",
            "previous_status": "AT_LAUNDRY",
            "wash_count": 2,
        },
    )
    return {"delivered": delivered}


def _http_get_json(url: str) -> dict:
    """Plain stdlib GET — this script must run on a bare interpreter."""
    import json
    from urllib.request import urlopen

    with urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode())


def token_for(laundry_id: str = LAUNDRY) -> str:
    return create_access_token(
        {
            "auth_id": "smoke-user",
            "user_id": "smoke-user",
            "user_name": "Smoke Tester",
            "role": "ADMIN",
            "laundry_id": laundry_id,
            "laundries": [{"id": laundry_id, "role": "ADMIN", "status": "ACTIVE"}],
        }
    )


def start_server() -> uvicorn.Server:
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)

    def run() -> None:
        try:
            server.run()
        except BaseException:  # noqa: BLE001 - a boot failure must reach the operator
            import traceback

            traceback.print_exc()

    threading.Thread(target=run, daemon=True).start()

    # `server.started` is set once the listener is bound — an HTTP probe would
    # also depend on a route existing, which is not what is being tested here.
    deadline = time.time() + 15
    while time.time() < deadline:
        if server.started:
            return server
        time.sleep(0.1)
    raise RuntimeError("server did not start")


def main() -> int:
    server = start_server()
    url = f"ws://127.0.0.1:{PORT}/ws/linen"
    failures: list[str] = []

    try:
        # 1. A valid session is greeted with the laundry it is bound to.
        with connect(f"{url}?token={token_for()}", open_timeout=10) as socket_:
            hello = socket_.recv(timeout=5)
            print("hello:", hello)
            if '"hello"' not in hello or LAUNDRY not in hello:
                failures.append("hello frame missing or wrong laundry")

            # 2. Subscribe to one tag, then push an event for it.
            socket_.send(
                '{"type":"subscribe","channels":["laundry","%s"]}'
                % linen_channel("LL-SMOKE1")
            )
            subscribed = socket_.recv(timeout=5)
            print("subscribed:", subscribed)
            if "subscribed" not in subscribed:
                failures.append("no subscribed acknowledgement")

            pushed = _http_get_json(f"http://127.0.0.1:{PORT}/_smoke/push")
            print("push:", pushed)
            if pushed.get("delivered") != 1:
                failures.append("event not delivered to the subscriber")

            event = socket_.recv(timeout=5)
            print("event:", event)
            if '"type":"linen.event"' not in event.replace(" ", ""):
                failures.append("no linen.event frame")
            if "WASHING" not in event:
                failures.append("event did not carry the new status")

            # 3. Heartbeat keeps the connection server-side healthy.
            socket_.send('{"type":"ping"}')
            pong = socket_.recv(timeout=5)
            print("pong:", pong)
            if "pong" not in pong:
                failures.append("no pong")

        # 4. A bad token is closed with an application code the browser can read,
        #    so the app stops retrying instead of hammering the handshake.
        try:
            with connect(f"{url}?token=garbage", open_timeout=10) as socket_:
                socket_.recv(timeout=5)
                failures.append("invalid token was accepted")
        except ConnectionClosed as exc:
            print("rejected bad token with close code", exc.rcvd.code if exc.rcvd else None)
            if exc.rcvd is not None and exc.rcvd.code != 4401:
                failures.append(f"unexpected close code {exc.rcvd.code}")
        except InvalidStatus as exc:
            # Older Starlette refuses the upgrade instead; either is a rejection.
            print("rejected bad token with HTTP", exc.response.status_code)
    finally:
        server.should_exit = True

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(" -", failure)
        return 1

    print("\nOK — handshake, subscribe, fan-out and auth rejection all behaved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())


