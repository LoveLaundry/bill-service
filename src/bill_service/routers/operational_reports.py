"""Reports derived from confirmed monthly-operation receiving entries."""
import calendar
from datetime import date
from typing import Dict, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth_helper import require_capability
from ..crypto_helper import decrypt_dict
from ..database.connection_manager import get_client
from ..database.main_db import monthly_entries_collection

router = APIRouter(prefix="/reports", tags=["Reports"])

MONTHLY_SENSITIVE_FIELDS = ["client_name", "notes"]
QUOTATION_SENSITIVE_FIELDS = ["client_name", "quotation_title", "line_items"]


class MonthlyOperationsReportRequest(BaseModel):
    start_date: date
    end_date: date
    selected_quotations: Dict[str, Optional[str]] = Field(default_factory=dict)
    quotation_snapshots: Dict[str, dict] = Field(default_factory=dict)


def _number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _normalized(value: str) -> str:
    return " ".join((value or "").casefold().split())


def _quotation_price_map(line_items: list) -> dict:
    prices = {}
    for line in line_items or []:
        if not isinstance(line, dict):
            continue
        item_name = _normalized(line.get("item_name", ""))
        if not item_name:
            continue
        base_price = _number(line.get("unit_price") or line.get("price"))
        specifications = line.get("specifications") or []
        if specifications:
            for specification in specifications:
                if not isinstance(specification, dict):
                    continue
                spec_name = _normalized(specification.get("specification", ""))
                prices[(item_name, spec_name)] = _number(
                    specification.get("unit_price") or base_price
                )
        else:
            prices[(item_name, "")] = base_price
    return prices


async def _quotation_prices(quotation_id: Optional[str]) -> dict:
    if not quotation_id:
        return {}
    if not ObjectId.is_valid(quotation_id):
        return {}

    client = get_client("MAIN")
    quotation = None
    for db_name in ("quotations_db", "quotations", "laundry_db"):
        try:
            quotation = await client[db_name]["quotations"].find_one({"_id": ObjectId(quotation_id)})
        except Exception:
            continue
        if quotation:
            break
    if not quotation:
        return {}

    try:
        decoded = decrypt_dict(quotation, QUOTATION_SENSITIVE_FIELDS)
    except Exception:
        decoded = quotation

    return _quotation_price_map(decoded.get("line_items", []))


def _monthly_day_rows(documents: list, start_date: date, end_date: date, price_map: dict) -> dict:
    hotel_totals: dict = {}
    item_totals: dict = {}
    daily_details = []
    unpriced_items = []
    total_income = 0.0
    total_quantity = 0.0
    quotation_by_hotel: dict = {}
    suggested_quotation_by_hotel: dict = {}

    for raw_doc in documents:
        if raw_doc.get("kind") != "receiving":
            continue
        try:
            doc = decrypt_dict(raw_doc, MONTHLY_SENSITIVE_FIELDS)
        except Exception:
            doc = raw_doc
        client_name = (doc.get("client_name") or "Unknown").strip() or "Unknown"
        normalized_client = _normalized(client_name)
        if doc.get("quotation_id"):
            suggested_quotation_by_hotel[normalized_client] = doc.get("quotation_id")
        quote_id = price_map.get("selected_ids", {}).get(normalized_client)
        quotation_by_hotel[client_name] = quote_id
        prices = price_map.get(quote_id, {}) if quote_id else {}
        for day_state in (doc.get("days") or {}).values():
            if (day_state.get("status") or "").upper() != "CONFIRMED":
                continue
            try:
                receiving_date = date.fromisoformat(str(day_state.get("date") or ""))
            except ValueError:
                continue
            if not start_date <= receiving_date <= end_date:
                continue

            hotel = hotel_totals.setdefault(client_name, {
                "client_name": client_name,
                "quotation_id": quote_id,
                "income": 0.0,
                "quantity": 0.0,
                "confirmed_days": 0,
            })
            hotel["confirmed_days"] += 1
            day_items = []
            quantities = day_state.get("quantities") or {}
            piece_quantities = day_state.get("piece_quantities") or {}
            for item_key in quantities.keys() | piece_quantities.keys():
                item_name, separator, specification = str(item_key).partition("||")
                item_name = item_name.strip()
                specification = specification.strip() if separator else ""
                quantity = _number(quantities.get(item_key))
                piece_count = int(_number(piece_quantities.get(item_key)))
                if not item_name or (quantity <= 0 and piece_count <= 0):
                    continue

                unit_price = prices.get((_normalized(item_name), _normalized(specification)))
                if unit_price is None and specification:
                    unit_price = prices.get((_normalized(item_name), ""))
                if unit_price is None or unit_price <= 0:
                    unpriced_items.append({
                        "date": receiving_date.isoformat(),
                        "client_name": client_name,
                        "item_name": item_name,
                        "specification": specification,
                        "quantity": round(quantity, 2),
                        "quotation_id": quote_id,
                    })
                    unit_price = 0.0

                line_income = round(quantity * unit_price, 2)
                total_income += line_income
                total_quantity += quantity
                hotel["income"] += line_income
                hotel["quantity"] += quantity

                item_key_normalized = (_normalized(item_name), _normalized(specification))
                item = item_totals.setdefault(item_key_normalized, {
                    "item_name": item_name,
                    "specification": specification,
                    "income": 0.0,
                    "quantity": 0.0,
                    "hotel_names": set(),
                })
                item["income"] += line_income
                item["quantity"] += quantity
                item["hotel_names"].add(client_name)
                day_items.append({
                    "item_name": item_name,
                    "specification": specification,
                    "quantity": round(quantity, 2),
                    "unit": "kg" if "curtain" in item_name.casefold() else "pcs",
                    "piece_count": piece_count,
                    "unit_price": round(unit_price, 2),
                    "income": line_income,
                })

            daily_details.append({
                "date": receiving_date.isoformat(),
                "client_name": client_name,
                "quotation_id": quote_id,
                "items": day_items,
            })

    for row in hotel_totals.values():
        row["income"] = round(row["income"], 2)
        row["quantity"] = round(row["quantity"], 2)
    hotels = sorted(hotel_totals.values(), key=lambda row: row["client_name"].casefold())
    items = [{
        "item_name": row["item_name"],
        "specification": row["specification"],
        "income": round(row["income"], 2),
        "quantity": round(row["quantity"], 2),
        "hotel_count": len(row["hotel_names"]),
    } for row in item_totals.values()]
    items.sort(key=lambda row: (-row["income"], row["item_name"].casefold(), row["specification"].casefold()))
    daily_details.sort(key=lambda row: (row["date"], row["client_name"].casefold()))
    return {
        "source_of_truth": "monthly_operations_confirmed_receiving",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "total_income": round(total_income, 2),
        "total_quantity": round(total_quantity, 2),
        "unpriced_count": len(unpriced_items),
        "unpriced_items": unpriced_items,
        "hotel_summary": hotels,
        "item_summary": items,
        "daily_details": daily_details,
        "selected_quotations": quotation_by_hotel,
        "suggested_quotations": suggested_quotation_by_hotel,
    }


@router.post("/monthly-operations")
async def monthly_operations_report(
    payload: MonthlyOperationsReportRequest,
    current_user: dict = Depends(require_capability("report:read")),
):
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date.")

    selected_ids = {
        _normalized(name): quotation_id
        for name, quotation_id in payload.selected_quotations.items()
    }
    explicitly_selected_hotels = set(selected_ids)
    documents = []
    cursor = monthly_entries_collection.find({
        "kind": "receiving",
        "year": {"$gte": payload.start_date.year, "$lte": payload.end_date.year},
    })
    async for document in cursor:
        try:
            month = int(document.get("month", 0))
            year = int(document.get("year", 0))
            if not 1 <= month <= 12:
                continue
            month_start = date(year, month, 1)
            month_end = date(year, month, calendar.monthrange(year, month)[1])
        except (TypeError, ValueError):
            continue
        if month_start <= payload.end_date and month_end >= payload.start_date:
            documents.append(document)

    documents.sort(key=lambda doc: (int(doc.get("year", 0)), int(doc.get("month", 0))))
    for raw_doc in documents:
        if not raw_doc.get("quotation_id"):
            continue
        try:
            decoded = decrypt_dict(raw_doc, MONTHLY_SENSITIVE_FIELDS)
        except Exception:
            decoded = raw_doc
        hotel_key = _normalized(decoded.get("client_name", ""))
        if hotel_key and hotel_key not in explicitly_selected_hotels:
            selected_ids[hotel_key] = raw_doc["quotation_id"]

    prices = {}
    snapshot_quote_ids = set()
    for quotation_id in set(selected_ids.values()):
        if quotation_id:
            prices[quotation_id] = await _quotation_prices(quotation_id)
            snapshot = payload.quotation_snapshots.get(str(quotation_id))
            selected_hotel_names = {
                hotel_name
                for hotel_name, selected_id in selected_ids.items()
                if str(selected_id) == str(quotation_id)
            }
            snapshot_matches_hotel = (
                snapshot
                and _normalized(snapshot.get("client_name", "")) in selected_hotel_names
            )
            if not prices[quotation_id] and snapshot_matches_hotel and str(snapshot.get("id")) == str(quotation_id):
                prices[quotation_id] = _quotation_price_map(snapshot.get("line_items", []))
                if prices[quotation_id]:
                    snapshot_quote_ids.add(quotation_id)
    prices["selected_ids"] = selected_ids

    report = _monthly_day_rows(documents, payload.start_date, payload.end_date, prices)
    for hotel in report["hotel_summary"]:
        hotel["suggested_quotation_id"] = report["suggested_quotations"].get(_normalized(hotel["client_name"]))
    report["hotels"] = report["hotel_summary"]
    report["quotation_price_sources"] = {
        str(quotation_id): "quotation_api_snapshot" if quotation_id in snapshot_quote_ids else "main_database"
        for quotation_id in set(selected_ids.values()) if quotation_id
    }
    return report