"""Shop-raised orders and the laundry's confirmation of receipt.

A shop order is an ordinary gate pass in DRAFT: the shop states what it is
sending (client_qty), the laundry counts what arrived (received_qty), and the
existing DRAFT -> RECEIVED transition is what closes the gap. These tests pin
that sequence and the isolation around it.
"""
from datetime import datetime, timezone

import pytest

from bill_service.crypto_helper import encrypt_dict, get_search_token
from bill_service.database import main_db
from bill_service.routers import client_shop_orders, gatepasses
from bill_service.routers.client_shop_orders import (
    resolve_shop_scope,
    ShopOrderCreate,
    ShopOrderLine,
    create_shop_order,
    get_shop_bill,
    get_shop_order,
    list_shop_orders,
)

LAUNDRY = "lovelaundry"
OTHER_LAUNDRY = "rivalwash"
SHOP_A = "Raman Stores"
SHOP_B = "Perera Traders"


def shop_session(shop=SHOP_A, laundry=LAUNDRY):
    return {
        "auth_id": "raman",
        "user_name": shop,
        "role": "SHOP_ADMIN",
        "platform_admin": False,
        "token_kind": "client",
        "client_scope": {
            "laundry_id": laundry,
            "client_name": shop,
            "client_type": "SHOP",
        },
    }


def hotel_session():
    session = shop_session()
    session["client_scope"]["client_type"] = "HOTEL"
    return session


def staff_session(laundry=LAUNDRY):
    return {
        "auth_id": "asha",
        "role": "ADMIN",
        "laundry_id": laundry,
        "laundries": [
            {"id": laundry, "name": "Love Laundry", "role": "ADMIN", "status": "ACTIVE"}
        ],
    }


def order_payload():
    return ShopOrderCreate(
        items=[
            ShopOrderLine(item_name="Bed sheet", quantity=40, unit="pcs"),
            ShopOrderLine(item_name="Bath towel", quantity=25, unit="pcs"),
        ],
        notes="Weekly collection",
    )


async def test_shop_can_raise_an_order_that_starts_as_a_draft(mocked_db):
    created = await create_shop_order(order_payload(), session=shop_session())

    assert created["status"] == "DRAFT"
    assert created["pieces"] == 65
    assert created["line_count"] == 2
    # The shop's figure is what it sent; the laundry has counted nothing yet.
    assert all(line["received_qty"] == 0 for line in created["items"])

    doc = await main_db.gatepasses_collection.find_one({})
    assert doc["laundry_id"] == LAUNDRY
    assert doc["origin"]["kind"] == "shop_order"


async def test_a_hotel_cannot_raise_a_shop_order(mocked_db):
    from fastapi import HTTPException

    # The side check lives in the route's dependency; calling the handler
    # directly would skip it, so the dependency itself is asserted here.
    with pytest.raises(HTTPException) as caught:
        resolve_shop_scope(hotel_session())
    assert caught.value.status_code == 403


async def test_laundry_confirms_receipt_and_the_shop_sees_it(mocked_db):
    created = await create_shop_order(order_payload(), session=shop_session())

    # The laundry side activates the draft, which is the existing guarded
    # transition — no bespoke "confirm" endpoint was invented for shops.
    await gatepasses.update_gate_pass_status(
        created["id"], status_update="RECEIVED", current_user=staff_session()
    )

    listing = await list_shop_orders(session=shop_session(), skip=0, limit=50)
    assert listing["total"] == 1
    assert listing["items"][0]["status"] == "RECEIVED"


async def test_one_shop_never_sees_another_shops_orders(mocked_db):
    await create_shop_order(order_payload(), session=shop_session(SHOP_A))
    await create_shop_order(order_payload(), session=shop_session(SHOP_B))

    mine = await list_shop_orders(session=shop_session(SHOP_A), skip=0, limit=50)
    assert mine["total"] == 1
    assert mine["items"][0]["status"] == "DRAFT"

    others = await list_shop_orders(session=shop_session(SHOP_B), skip=0, limit=50)
    assert others["total"] == 1
    assert others["items"][0]["id"] != mine["items"][0]["id"]


async def test_the_same_shop_name_at_another_laundry_is_separate(mocked_db):
    await create_shop_order(order_payload(), session=shop_session(SHOP_A, LAUNDRY))
    await create_shop_order(order_payload(), session=shop_session(SHOP_A, OTHER_LAUNDRY))

    here = await list_shop_orders(session=shop_session(SHOP_A, LAUNDRY), skip=0, limit=50)
    assert here["total"] == 1


async def test_another_shops_order_reads_as_not_found(mocked_db):
    from fastapi import HTTPException

    created = await create_shop_order(order_payload(), session=shop_session(SHOP_A))
    with pytest.raises(HTTPException) as caught:
        await get_shop_order(created["id"], session=shop_session(SHOP_B))
    assert caught.value.status_code == 404


async def test_shop_can_open_its_own_bill_only(mocked_db):
    from fastapi import HTTPException

    async def seed(shop, total):
        doc = {
            "laundry_id": LAUNDRY,
            "bill_number": f"SB-{shop[:3]}",
            "created_at": datetime.now(timezone.utc),
            "grand_total": total,
            "paid_amount": 0.0,
            "outstanding_amount": total,
            "payment_status": "PENDING",
            "status": "ISSUED",
            "items": [{"item_name": "Bed sheet", "quantity": 10}],
            "client_name": shop,
        }
        result = await main_db.shop_bills_collection.insert_one(
            encrypt_dict(doc, client_shop_orders.BILL_SENSITIVE)
        )
        return result.inserted_id

    mine = await seed(SHOP_A, 1200.0)
    await seed(SHOP_B, 9000.0)

    bill = await get_shop_bill(str(mine), session=shop_session(SHOP_A))
    assert bill["grand_total"] == 1200.0
    assert bill["balance"] == 1200.0

    with pytest.raises(HTTPException) as caught:
        await get_shop_bill(str(mine), session=shop_session(SHOP_B))
    assert caught.value.status_code == 404

async def test_owner_can_open_own_order(mocked_db):
    created = await create_shop_order(order_payload(), session=shop_session(SHOP_A))

    opened = await get_shop_order(created["id"], session=shop_session(SHOP_A))
    assert opened["id"] == created["id"]
    assert opened["status"] == "DRAFT"
    assert opened["pieces"] == 65