"""Today sheet: one commit creates the day's receives and FIFO deliveries."""
import pytest
from fastapi import HTTPException

from bill_service.models import (
    TodaySheetCommit,
    TodaySheetDelivery,
    TodaySheetDeliveryLine,
    TodaySheetReceive,
    TodaySheetReceiveItem,
)
from bill_service.routers.today_sheet import commit_today_sheet, today_sheet


def user(name="Alice"):
    from conftest import tenant_user

    return tenant_user(f"u-{name}", "lovelaundry")


DAY = "2026-11-06"


def receive_payload(client="Sunny Hotel", qty=10):
    return TodaySheetReceive(
        client_name=client,
        items=[TodaySheetReceiveItem(item_name="Bedsheet", quantity=qty, unit="pcs")],
    )


async def test_commit_creates_receive_then_fifo_delivery(mocked_db):
    payload = TodaySheetCommit(
        date=DAY,
        receives=[receive_payload()],
        deliveries=[
            TodaySheetDelivery(
                client_name="Sunny Hotel",
                items=[TodaySheetDeliveryLine(item_name="Bedsheet", quantity=4)],
            )
        ],
    )
    out = await commit_today_sheet(payload, user())
    assert all(r["ok"] for r in out["results"]), out["results"]
    receive = next(r for r in out["results"] if r["section"] == "receive")
    delivery = next(r for r in out["results"] if r["section"] == "delivery")
    assert receive["gate_pass_number"].startswith("GP-")
    assert delivery["id"]

    sheet = await today_sheet(date=DAY, current_user=user())
    row = next(c for c in sheet["clients"] if c["client_name"] == "Sunny Hotel")
    assert len(row["receives"]) == 1
    assert len(row["deliveries"]) == 1
    # 10 received, 4 delivered -> 6 still deliverable on the pass.
    assert row["available_qty"] == 6


async def test_commit_rejects_uncovered_delivery(mocked_db):
    payload = TodaySheetCommit(
        date=DAY,
        receives=[receive_payload(qty=2)],
        deliveries=[
            TodaySheetDelivery(
                client_name="Sunny Hotel",
                items=[TodaySheetDeliveryLine(item_name="Bedsheet", quantity=5)],
            )
        ],
    )
    out = await commit_today_sheet(payload, user())
    delivery = next(r for r in out["results"] if r["section"] == "delivery")
    assert delivery["ok"] is False
    assert "Not enough received stock" in delivery["error"]


async def test_commit_rejects_future_day(mocked_db):
    payload = TodaySheetCommit(date="2999-01-01", receives=[receive_payload()])
    with pytest.raises(HTTPException) as exc:
        await commit_today_sheet(payload, user())
    assert exc.value.status_code == 409


async def test_commit_rejects_empty(mocked_db):
    with pytest.raises(HTTPException) as exc:
        await commit_today_sheet(TodaySheetCommit(date=DAY), user())
    assert exc.value.status_code == 400
