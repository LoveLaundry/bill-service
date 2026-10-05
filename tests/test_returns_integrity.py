"""Route regressions for return source relationships and movement quantities."""
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException

from bill_service.crypto_helper import decrypt_dict, encrypt_dict
from bill_service.models import ReturnCreate, ReturnItem, ReturnUpdate
from bill_service.routers import returns

from conftest import auth_user

SENSITIVE_FIELDS = ["client_name", "items", "notes"]


def _now():
    return datetime.now(timezone.utc)


async def seed_gate_pass(mocked_db, *, client="Hotel A", quantity=4, status="DELIVERED"):
    doc = {
        "gate_pass_number": f"GP-{ObjectId()}",
        "client_name": client,
        "items": [
            {
                "item_name": "Towel",
                "specification": "Large",
                "client_qty": quantity,
                "received_qty": quantity,
            }
        ],
        "status": status,
        "receiving_date": _now(),
        "created_at": _now(),
        "updated_at": _now(),
    }
    result = await mocked_db["gatepasses_collection"].insert_one(
        encrypt_dict(doc, SENSITIVE_FIELDS)
    )
    return str(result.inserted_id)


async def seed_delivery(
    mocked_db, *, gate_pass_id, client="Hotel A", quantity=4, status="DELIVERED"
):
    doc = {
        "gate_pass_id": gate_pass_id,
        "source_gate_pass_ids": [gate_pass_id],
        "client_name": client,
        "items": [
            {
                "item_name": "Towel",
                "specification": "Large",
                "quantity": quantity,
                "gate_pass_id": gate_pass_id,
            }
        ],
        "status": status,
        "delivery_date": _now(),
        "created_at": _now(),
    }
    result = await mocked_db["deliveries_collection"].insert_one(
        encrypt_dict(doc, SENSITIVE_FIELDS)
    )
    return str(result.inserted_id)


def return_item(quantity, *, action="RECEIVE_BACK"):
    return ReturnItem(
        item_name="Towel",
        specification="Large",
        returned_qty=quantity,
        reason="DAMAGED",
        action=action,
    )


async def test_create_return_rejects_wrong_client_or_source_gate_pass(mocked_db):
    gate_pass_id = await seed_gate_pass(mocked_db)
    other_gate_pass_id = await seed_gate_pass(
        mocked_db, client="Hotel B", status="RECEIVED"
    )
    other_delivery_id = await seed_delivery(
        mocked_db, gate_pass_id=other_gate_pass_id, client="Hotel B"
    )

    with pytest.raises(HTTPException) as client_error:
        await returns.create_return(
            ReturnCreate(
                gate_pass_id=gate_pass_id,
                client_name="Hotel B",
                items=[return_item(1)],
            ),
            current_user=auth_user(),
        )
    assert client_error.value.status_code == 400

    with pytest.raises(HTTPException) as source_error:
        await returns.create_return(
            ReturnCreate(
                gate_pass_id=gate_pass_id,
                delivery_id=other_delivery_id,
                client_name="Hotel A",
                items=[return_item(1)],
            ),
            current_user=auth_user(),
        )
    assert source_error.value.status_code == 400
    assert await mocked_db["returns_collection"].count_documents({}) == 0


async def test_create_and_update_cannot_exceed_active_delivery_quantity(mocked_db):
    gate_pass_id = await seed_gate_pass(mocked_db, quantity=4)
    delivery_id = await seed_delivery(
        mocked_db, gate_pass_id=gate_pass_id, quantity=3
    )
    created = await returns.create_return(
        ReturnCreate(
            gate_pass_id=gate_pass_id,
            delivery_id=delivery_id,
            client_name="Hotel A",
            items=[return_item(2)],
        ),
        current_user=auth_user(),
    )

    with pytest.raises(HTTPException) as create_error:
        await returns.create_return(
            ReturnCreate(
                gate_pass_id=gate_pass_id,
                delivery_id=delivery_id,
                client_name="Hotel A",
                items=[return_item(2)],
            ),
            current_user=auth_user(),
        )
    assert create_error.value.status_code == 400

    with pytest.raises(HTTPException) as update_error:
        await returns.update_return(
            created["return_id"],
            ReturnUpdate(items=[return_item(4)]),
            current_user=auth_user(),
        )
    assert update_error.value.status_code == 400


async def test_pending_return_and_resend_recompute_gate_pass_status(mocked_db):
    gate_pass_id = await seed_gate_pass(mocked_db, quantity=4)
    delivery_id = await seed_delivery(
        mocked_db, gate_pass_id=gate_pass_id, quantity=4
    )
    created = await returns.create_return(
        ReturnCreate(
            gate_pass_id=gate_pass_id,
            delivery_id=delivery_id,
            client_name="Hotel A",
            items=[return_item(2, action="RE_WASH")],
        ),
        current_user=auth_user(),
    )

    gp = await mocked_db["gatepasses_collection"].find_one(
        {"_id": ObjectId(gate_pass_id)}
    )
    assert gp["status"] == "PARTIALLY_DELIVERED"

    await returns.update_return(
        created["return_id"],
        ReturnUpdate(items=[return_item(2, action="DISCARD")]),
        current_user=auth_user(),
    )
    gp = await mocked_db["gatepasses_collection"].find_one(
        {"_id": ObjectId(gate_pass_id)}
    )
    assert gp["status"] == "DELIVERED"

    await returns.update_return(
        created["return_id"],
        ReturnUpdate(items=[return_item(2, action="RE_WASH")]),
        current_user=auth_user(),
    )
    gp = await mocked_db["gatepasses_collection"].find_one(
        {"_id": ObjectId(gate_pass_id)}
    )
    assert gp["status"] == "PARTIALLY_DELIVERED"
    stored_return = await mocked_db["returns_collection"].find_one(
        {"return_id": created["return_id"]}
    )
    assert decrypt_dict(stored_return, SENSITIVE_FIELDS)["status"] == "PENDING"

    resent = await returns.mark_item_resent(
        created["return_id"],
        item_name="Towel",
        specification="Large",
        current_user=auth_user(),
    )
    assert resent["items"][0]["resend_status"] == "SENT"
    gp = await mocked_db["gatepasses_collection"].find_one(
        {"_id": ObjectId(gate_pass_id)}
    )
    assert gp["status"] == "DELIVERED"
