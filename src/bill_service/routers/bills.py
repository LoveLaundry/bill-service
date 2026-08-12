from datetime import datetime, timezone, timedelta
from typing import List, Optional
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth_helper import get_current_user, require_capability
from ..crypto_helper import decrypt_dict, encrypt_dict, get_search_token
from ..database.main_db import (
    audit_collection,
    bills_collection,
    deliveries_collection,
    payments_collection,
)
from ..repositories.main_repository import bump_version, enqueue_sync
from ..services.verification_service import attach_verification_to
from ..models import (
    BillCreate,
    BillModel,
    BillListResponse,
    PaymentCreate,
    PaymentModel,
)

router = APIRouter(prefix="/bills", tags=["bills"])

SENSITIVE_FIELDS = [
    "client_name",
    "quotation_title",
    "notes",
    "items",
]

PAYMENT_SENSITIVE_FIELDS = ["client_name", "notes"]
DELIVERY_SENSITIVE_FIELDS = ["client_name", "items", "notes"]
QUOTATION_SENSITIVE_FIELDS = ["client_name", "quotation_title", "line_items"]


def _serialize(doc: dict) -> dict:
    try:
        decrypted = decrypt_dict(doc, SENSITIVE_FIELDS)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to decrypt document: {str(e)}"
        )

    decrypted["id"] = str(decrypted["_id"])
    del decrypted["_id"]
    return decrypted


def _serialize_payment(doc: dict) -> dict:
    try:
        decrypted = decrypt_dict(doc, PAYMENT_SENSITIVE_FIELDS)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to decrypt payment: {str(e)}"
        )
    decrypted["id"] = str(decrypted["_id"])
    del decrypted["_id"]
    return decrypted


def _parse_object_id(bill_id: str) -> ObjectId:
    try:
        return ObjectId(bill_id)
    except InvalidId:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid bill ID"
        )


async def log_audit(user_id: str, action: str, entity: str, entity_id: str):
    await audit_collection.insert_one(
        {
            "user_id": user_id,
            "action": action,
            "entity": entity,
            "entity_id": entity_id,
            "timestamp": datetime.now(timezone.utc),
        }
    )


async def get_quotation_prices(quotation_id: str) -> dict:
    """Fetch quotation pricing via the MAIN cluster client (read-only)."""
    from ..database.connection_manager import get_client

    motor_client = get_client("MAIN")

    try:
        q_oid = ObjectId(quotation_id)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid quotation_id format.",
        )

    # Search known quotation DB names (mirrors quotation-service config defaults)
    db_names = ["quotations_db", "quotations", "laundry_db"]
    quotation_doc = None
    for db_name in db_names:
        try:
            coll = motor_client[db_name]["quotations"]
            quotation_doc = await coll.find_one({"_id": q_oid})
            if quotation_doc:
                break
        except Exception:
            continue

    if not quotation_doc:
        # Return empty price map — prices will fall back to manual item prices
        return {}

    try:
        q_decrypted = decrypt_dict(quotation_doc, QUOTATION_SENSITIVE_FIELDS)
    except Exception:
        return {}

    prices = {}
    for item in q_decrypted.get("line_items", []):
        prices[item["item_name"]] = {
            "price": item.get("unit_price") or item.get("price") or 0.0,
            "category": item.get("category"),
        }
    return prices


@router.post("", response_model=BillModel, status_code=status.HTTP_201_CREATED)
async def create_bill(
    payload: BillCreate,
    current_user: dict = Depends(require_capability("bill:write")),
):
    # 1. Fetch quotation prices
    price_map = await get_quotation_prices(payload.quotation_id)

    # 2. Prevent Double Billing & Auto populate or validate quantities
    bill_items_to_save = []
    del_ids_to_save = payload.delivery_ids or []

    if del_ids_to_save:
        # Map item_name -> total delivered quantity in inputs
        delivered_map = {}
        for d_id in del_ids_to_save:
            d_oid = ObjectId(d_id)
            d_doc = await deliveries_collection.find_one({"_id": d_oid})
            if not d_doc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Referenced delivery record '{d_id}' not found.",
                )

            d_dec = decrypt_dict(d_doc, DELIVERY_SENSITIVE_FIELDS)
            for item in d_dec.get("items", []):
                name = item["item_name"]
                delivered_map[name] = delivered_map.get(name, 0) + item["quantity"]

        # Map item_name -> already billed quantity for these deliveries
        already_billed_map = {}
        prev_bills_cursor = bills_collection.find(
            {
                "delivery_ids": {"$in": del_ids_to_save},
                "payment_status": {"$ne": "CANCELLED"},
            }
        )
        async for pb_doc in prev_bills_cursor:
            pb_dec = decrypt_dict(pb_doc, SENSITIVE_FIELDS)
            for item in pb_dec.get("items", []):
                name = item["item_name"]
                already_billed_map[name] = (
                    already_billed_map.get(name, 0) + item["quantity"]
                )

        # Compute remaining billable quantities
        billable_map = {}
        for name, del_qty in delivered_map.items():
            billed = already_billed_map.get(name, 0)
            billable_map[name] = max(0, del_qty - billed)

        if payload.items:
            # Validate input quantities against remaining billable balance
            for input_item in payload.items:
                name = input_item.item_name
                qty = input_item.quantity
                allowed = billable_map.get(name, 0)

                if qty > allowed:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Double-Billing constraint violation. "
                            f"Item '{name}' requested: {qty}, but only {allowed} remaining is billable."
                        ),
                    )

                price_info = price_map.get(name, {"price": input_item.unit_price, "category": input_item.category})
                unit_price = price_info["price"]
                line_total = round(unit_price * qty, 2)
                bill_items_to_save.append(
                    {
                        "item_name": name,
                        "category": price_info.get("category") or input_item.category,
                        "unit_price": unit_price,
                        "quantity": qty,
                        "line_total": line_total,
                    }
                )
        else:
            # Auto-aggregate remaining billable
            for name, billable_qty in billable_map.items():
                if billable_qty <= 0:
                    continue
                price_info = price_map.get(name)
                if not price_info:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Pricing rule for item '{name}' not defined in the quotation.",
                    )
                unit_price = price_info["price"]
                line_total = round(unit_price * billable_qty, 2)
                bill_items_to_save.append(
                    {
                        "item_name": name,
                        "category": price_info["category"],
                        "unit_price": unit_price,
                        "quantity": billable_qty,
                        "line_total": line_total,
                    }
                )
    else:
        # Create manually from items list in payload
        if not payload.items:
            raise HTTPException(
                status_code=400,
                detail="Must provide either delivery_ids or custom items payload.",
            )

        for input_item in payload.items:
            # Use price from quotation if available, otherwise pay what they passed
            name = input_item.item_name
            price_info = price_map.get(name, {"price": input_item.unit_price, "category": input_item.category})
            unit_price = price_info["price"]
            line_total = round(unit_price * input_item.quantity, 2)
            bill_items_to_save.append(
                {
                    "item_name": name,
                    "category": price_info.get("category"),
                    "unit_price": unit_price,
                    "quantity": input_item.quantity,
                    "line_total": line_total,
                }
            )

    if not bill_items_to_save:
        raise HTTPException(
            status_code=400,
            detail="No billable items found. Everything in these deliveries might have already been billed.",
        )

    # 3. Calculate Totals
    total_quantity = sum(x["quantity"] for x in bill_items_to_save)
    total_amount = round(sum(x["line_total"] for x in bill_items_to_save), 2)

    discounts = payload.discounts or 0.0
    transport_fee = payload.transport_fee or 0.0
    taxes = payload.taxes or 0.0
    additional_charges = payload.additional_charges or 0.0

    grand_total = round(
        total_amount - discounts + transport_fee + taxes + additional_charges, 2
    )
    if grand_total < 0:
        grand_total = 0.0

    now = datetime.now(timezone.utc)
    doc = {
        "quotation_id": payload.quotation_id,
        "client_name": payload.client_name,
        "quotation_title": payload.quotation_title,
        "items": bill_items_to_save,
        "total_quantity": total_quantity,
        "total_amount": total_amount,
        "discounts": discounts,
        "transport_fee": transport_fee,
        "taxes": taxes,
        "additional_charges": additional_charges,
        "grand_total": grand_total,
        "payment_status": "DRAFT",
        "paid_amount": 0.0,
        "outstanding_amount": grand_total,
        "delivery_ids": del_ids_to_save,
        "notes": payload.notes,
        "created_at": now,
        "updated_at": now,
    }

    encrypted_doc = encrypt_dict(doc, SENSITIVE_FIELDS)
    result = await bills_collection.insert_one(encrypted_doc)
    created = await bills_collection.find_one({"_id": result.inserted_id})

    serialized = _serialize(created)

    # Main DB write succeeded -> bump version and enqueue replication
    new_version = await bump_version("bill", result.inserted_id)
    await enqueue_sync("bill", result.inserted_id, new_version)
    serialized = await attach_verification_to("bill", result.inserted_id, serialized)

    await log_audit(
        current_user.get("auth_id", "system"),
        "BILL_CREATE",
        "bill",
        serialized["id"],
    )
    return serialized


@router.get("", response_model=BillListResponse)
async def list_bills(
    search: Optional[str] = Query(None),
    client_name: Optional[str] = Query(None),
    quotation_id: Optional[str] = Query(None),
    payment_status: Optional[str] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_capability("bill:read")),
):
    query: dict = {}

    if client_name:
        query["client_name_search"] = get_search_token(client_name)

    if quotation_id:
        query["quotation_id"] = quotation_id

    if payment_status:
        query["payment_status"] = payment_status

    if date_from or date_to:
        date_filter: dict = {}
        if date_from:
            date_filter["$gte"] = date_from.replace(tzinfo=timezone.utc)
        if date_to:
            date_filter["$lte"] = date_to.replace(tzinfo=timezone.utc)
        query["created_at"] = date_filter

    if search:
        # For general search, we filter client by token
        query["client_name_search"] = get_search_token(search)

    total = await bills_collection.count_documents(query)
    cursor = (
        bills_collection.find(query)
        .sort("created_at", -1)
        .skip(skip)
        .limit(limit)
    )

    docs = []
    async for doc in cursor:
        try:
            serialized = _serialize(doc)
            docs.append(await attach_verification_to("bill", doc["_id"], serialized))
        except HTTPException:
            pass

    return {"items": docs, "total": total}


@router.get("/{bill_id}", response_model=BillModel)
async def get_bill(
    bill_id: str,
    current_user: dict = Depends(require_capability("bill:read")),
):
    oid = _parse_object_id(bill_id)
    doc = await bills_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bill not found"
        )
    serialized = _serialize(doc)
    return await attach_verification_to("bill", oid, serialized)


@router.patch("/{bill_id}/status", response_model=BillModel)
async def update_bill_status(
    bill_id: str,
    status_update: str = Query(...),
    current_user: dict = Depends(require_capability("bill:write")),
):
    valid_statuses = [
        "DRAFT",
        "PENDING",
        "ISSUED",
        "PARTIALLY_PAID",
        "PAID",
        "CANCELLED",
    ]
    if status_update not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of {valid_statuses}",
        )

    oid = _parse_object_id(bill_id)
    doc = await bills_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bill not found"
        )

    await bills_collection.update_one(
        {"_id": oid},
        {
            "$set": {
                "payment_status": status_update,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )

    updated_doc = await bills_collection.find_one({"_id": oid})
    serialized = _serialize(updated_doc)

    new_version = await bump_version("bill", oid)
    await enqueue_sync("bill", oid, new_version)
    serialized = await attach_verification_to("bill", oid, serialized)

    await log_audit(
        current_user.get("auth_id", "system"),
        "BILL_STATUS_UPDATE",
        "bill",
        serialized["id"],
    )
    return serialized


@router.delete("/{bill_id}")
async def delete_bill(
    bill_id: str,
    current_user: dict = Depends(require_capability("payment:write")),
):
    oid = _parse_object_id(bill_id)
    # Prefer Cancellation / status CANCELLED instead of hard delete
    result = await bills_collection.update_one(
        {"_id": oid},
        {
            "$set": {
                "payment_status": "CANCELLED",
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )
    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bill not found"
        )

    new_version = await bump_version("bill", oid)
    await enqueue_sync("bill", oid, new_version)

    await log_audit(
        current_user.get("auth_id", "system"), "BILL_CANCEL", "bill", bill_id
    )
    return {"message": "Bill cancelled successfully"}


# --- Payment APIs ---
@router.post(
    "/{bill_id}/payments",
    response_model=PaymentModel,
    status_code=status.HTTP_201_CREATED,
)
async def create_payment(
    bill_id: str,
    payload: PaymentCreate,
    current_user: dict = Depends(require_capability("payment:write")),
):
    oid = _parse_object_id(bill_id)
    doc = await bills_collection.find_one({"_id": oid})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bill not found"
        )

    dec_bill = _serialize(doc)
    if dec_bill["payment_status"] == "CANCELLED":
        raise HTTPException(
            status_code=400, detail="Cannot record payments on a cancelled bill."
        )

    outstanding = dec_bill["outstanding_amount"]
    if payload.amount > outstanding + 0.01:  # Allow minor tolerance for rounding
        raise HTTPException(
            status_code=400,
            detail=f"Payment amount of {payload.amount} exceeds outstanding block of {outstanding}.",
        )

    # 1. Create Payment record
    now = datetime.now(timezone.utc)
    payment_doc = {
        "bill_id": bill_id,
        "client_name": dec_bill["client_name"],
        "amount": payload.amount,
        "payment_method": payload.payment_method,
        "payment_date": payload.payment_date.replace(tzinfo=timezone.utc),
        "reference": payload.reference,
        "notes": payload.notes,
        "created_at": now,
    }

    encrypted_pay = encrypt_dict(payment_doc, PAYMENT_SENSITIVE_FIELDS)
    pay_result = await payments_collection.insert_one(encrypted_pay)

    payment_version = await bump_version("payment", pay_result.inserted_id)
    await enqueue_sync("payment", pay_result.inserted_id, payment_version)

    # 2. Update Bill totals
    new_paid = round(dec_bill["paid_amount"] + payload.amount, 2)
    new_outstanding = round(dec_bill["grand_total"] - new_paid, 2)
    if new_outstanding < 0.01:
        new_outstanding = 0.0
        new_status = "PAID"
    else:
        new_status = "PARTIALLY_PAID"

    await bills_collection.update_one(
        {"_id": oid},
        {
            "$set": {
                "paid_amount": new_paid,
                "outstanding_amount": new_outstanding,
                "payment_status": new_status,
                "updated_at": now,
            }
        },
    )

    created_pay = await payments_collection.find_one({"_id": pay_result.inserted_id})
    serialized_pay = _serialize_payment(created_pay)

    # The bill changed too -> enqueue its replication as well
    bill_new_version = await bump_version("bill", oid)
    await enqueue_sync("bill", oid, bill_new_version)

    serialized_pay = await attach_verification_to("payment", pay_result.inserted_id, serialized_pay)

    await log_audit(
        current_user.get("auth_id", "system"),
        "PAYMENT_CREATE",
        "payment",
        serialized_pay["id"],
    )
    return serialized_pay


@router.get("/{bill_id}/payments", response_model=List[PaymentModel])
async def list_payments_for_bill(
    bill_id: str,
    current_user: dict = Depends(require_capability("payment:read")),
):
    cursor = payments_collection.find({"bill_id": bill_id}).sort("payment_date", -1)
    results = []
    async for doc in cursor:
        try:
            serialized = _serialize_payment(doc)
            results.append(await attach_verification_to("payment", doc["_id"], serialized))
        except HTTPException:
            pass
    return results
