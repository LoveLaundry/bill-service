"""Frontend-facing linen detail and live-feed response contracts."""
import pytest
from bson import ObjectId
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bill_service.auth_helper import create_access_token

LAUNDRY = "acme"


@pytest.fixture()
def client(mocked_db):
    from bill_service.main import app

    return TestClient(app)


def _headers() -> dict:
    token = create_access_token(
        {
            "auth_id": "contract-scanner",
            "user_name": "Contract Scanner",
            "role": "ADMIN",
            "laundry_id": LAUNDRY,
            "laundries": [{"id": LAUNDRY, "role": "ADMIN", "status": "ACTIVE"}],
        }
    )
    return {"Authorization": f"Bearer {token}"}


def _token() -> str:
    return _headers()["Authorization"].split(" ", 1)[1]


async def _make_linen(mocked_db) -> str:
    oid = ObjectId()
    await mocked_db["linens_collection"].insert_one(
        {
            "_id": oid,
            "laundry_id": LAUNDRY,
            "linen_id": "LINEN-CONTRACT",
            "category": "BEDSHEET",
            "item_type": "Sheet",
            "status": "IN_STOCK",
        }
    )
    await mocked_db["linen_events_collection"].insert_one(
        {
            "laundry_id": LAUNDRY,
            "linen_id": "LINEN-CONTRACT",
            "action": "register",
            "from_status": "",
            "to_status": "IN_STOCK",
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
    )
    return str(oid)


async def test_linen_detail_keeps_flat_and_named_shapes(client: TestClient, mocked_db) -> None:
    linen_id = await _make_linen(mocked_db)
    response = client.get(f"/linens/{linen_id}", headers=_headers())
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body["profile"], dict)
    assert isinstance(body["history"], list)
    assert body["linen_id"] == body["profile"]["linen_id"] == "LINEN-CONTRACT"
    assert body["status"] == body["profile"]["status"] == "IN_STOCK"


async def test_linen_scanner_alias_uses_the_same_projection(client: TestClient, mocked_db) -> None:
    linen_id = await _make_linen(mocked_db)
    response = client.get(f"/linen/items/{linen_id}", headers=_headers())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["profile"]["linen_id"] == "LINEN-CONTRACT"
    assert body["history"]


def test_linen_live_feed_handles_subscribe_and_ping(client: TestClient) -> None:
    with client.websocket_connect(f"/ws/linen?token={_token()}") as socket:
        socket.send_json({"subscribe": ["laundry"]})
        assert socket.receive_json()["type"] == "subscribed"
        socket.send_json({"ping": True})
        assert socket.receive_json()["type"] == "pong"


def test_linen_live_feed_rejects_invalid_token(client: TestClient) -> None:
    with client.websocket_connect("/ws/linen?token=invalid") as socket:
        with pytest.raises(WebSocketDisconnect) as exc:
            socket.receive_json()
    assert exc.value.code == 4401
