"""Reports derived from confirmed monthly-operation receiving entries."""
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..auth_helper import require_capability, require_role
from ..tenancy import resolve_laundry_id, scoped_query
from ..app_time import day_start, lkt
from ..crypto_helper import decrypt_dict
from ..database.connection_manager import get_client
from ..crypto_helper import get_search_token
from ..database.main_db import (
    audit_collection,
    gatepasses_collection,
    linen_category_mappings_collection,
)
from ..services.hotel_code import format_bill_number
from ..router_utils import log_audit

router = APIRouter(prefix="/reports", tags=["Reports"])

MONTHLY_SENSITIVE_FIELDS = ["client_name", "notes"]
QUOTATION_SENSITIVE_FIELDS = ["client_name", "quotation_title", "line_items"]
CATEGORY_UNCLASSIFIED = "Other / Unclassified Items"
STANDARD_CATEGORIES = [
    "Bedsheets", "Duvet Covers", "Pillowcases", "Bath Towels", "Hand Towels",
    "Bath Mats", "Pool Towels", "Bathrobes", "Blankets", "Tablecloths",
    "Face Towels", "Other / Unclassified Items",
]
DEFAULT_CATEGORY_ALIASES = {
    "Bedsheets": [
        "bedsheet", "bedsheets", "bed sheet", "bed sheets",
        "bedsheet - single", "bedsheet - double", "bedsheet - large", "bedsheet - king",
        "bed sheet - single", "bed sheet - double", "bed sheet - large", "bed sheet - king",
    ],
    "Duvet Covers": ["duvet cover", "duvet covers", "duvet cover - single", "duvet cover - double", "duvet cover - king"],
    "Pillowcases": ["pillowcase", "pillowcases", "pillow case", "pillow cases", "pillowcase - single", "pillowcase - double"],
    "Bath Towels": ["bath towel", "bath towels", "bath towel - standard", "bath towel - large", "large bath towel"],
    "Hand Towels": ["hand towel", "hand towels", "hand towel - small", "small hand towel"],
    "Bath Mats": ["bath mat", "bath mats", "bathmat", "bathmats"],
    "Pool Towels": ["pool towel", "pool towels", "pool towel - large"],
    "Bathrobes": ["bathrobe", "bathrobes", "bath robe", "bath robes"],
    "Blankets": ["blanket", "blankets", "blanket - single", "blanket - double", "blanket - king"],
    "Tablecloths": ["tablecloth", "tablecloths", "table cloth", "table cloths"],
    "Face Towels": ["face towel", "face towels", "face cloth", "face cloths"],
}
DEFAULT_CATEGORY_LOOKUP = {
    _alias: _category
    for _category, _aliases in DEFAULT_CATEGORY_ALIASES.items()
    for _alias in _aliases
}


class MonthlyOperationsReportRequest(BaseModel):
    start_date: date
    end_date: date
    hotel_name: Optional[str] = None
    selected_quotations: Dict[str, Optional[str]] = Field(default_factory=dict)
    quotation_snapshots: Dict[str, dict] = Field(default_factory=dict)


class LinenCategoryMappingPayload(BaseModel):
    item_name: str = Field(min_length=1, max_length=120)
    category: str


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


async def _quotation_prices(quotation_id: Optional[str], laundry_id: str) -> dict:
    if not quotation_id:
        return {}
    if not ObjectId.is_valid(quotation_id):
        return {}

    client = get_client("MAIN")
    quotation = None
    for db_name in ("quotations_db", "quotations", "laundry_db"):
        try:
            quotation = await client[db_name]["quotations"].find_one(
                scoped_query({"_id": ObjectId(quotation_id)}, laundry_id)
            )
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


def _business_date(value) -> Optional[date]:
    if isinstance(value, datetime):
        return lkt(value).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal("0")


def _currency(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _empty_aggregate() -> dict:
    return {"quantity": Decimal("0"), "value": Decimal("0"), "rates": set(), "variants": {}, "has_missing_rate": False}


def _add_variant(aggregate: dict, item_name: str, specification: str, quantity: Decimal, value: Decimal, rate: Optional[Decimal]):
    aggregate["quantity"] += quantity
    aggregate["value"] += value
    if rate is not None:
        aggregate["rates"].add(rate)
    else:
        aggregate["has_missing_rate"] = True
    variant_key = (_normalized(item_name), _normalized(specification))
    variant = aggregate["variants"].setdefault(variant_key, {
        "item_name": item_name,
        "specification": specification,
        "quantity": Decimal("0"),
        "value": Decimal("0"),
        "rates": set(),
        "has_missing_rate": False,
    })
    variant["quantity"] += quantity
    variant["value"] += value
    if rate is not None:
        variant["rates"].add(rate)
    else:
        variant["has_missing_rate"] = True


def _finish_category(aggregate: dict, category: str) -> dict:
    rates = sorted(aggregate["rates"])
    quantity = aggregate["quantity"]
    value = _currency(aggregate["value"])
    variants = []
    for variant in sorted(aggregate["variants"].values(), key=lambda row: (row["item_name"].casefold(), row["specification"].casefold())):
        variant_rates = sorted(variant["rates"])
        variants.append({
            "item_name": variant["item_name"],
            "specification": variant["specification"],
            "quantity": float(variant["quantity"]),
            "unit_price": float(variant_rates[0]) if len(variant_rates) == 1 else None,
            "rate_label": "Missing price" if variant.get("has_missing_rate") or not variant_rates else f"LKR {variant_rates[0]:.2f}" if len(variant_rates) == 1 else "Multiple Rates",
            "value": float(_currency(variant["value"])) if variant_rates else None,
            "value_complete": not variant.get("has_missing_rate") and bool(variant_rates),
        })
    return {
        "category": category,
        "quantity": float(quantity),
        "unit_price": float(rates[0]) if len(rates) == 1 else None,
        "rate_label": "Incomplete: missing rate" if aggregate.get("has_missing_rate") else f"LKR {rates[0]:.2f}" if len(rates) == 1 else "Multiple Rates" if rates else "Missing price",
        "weighted_average_price": float(_currency(value / quantity)) if quantity and rates else None,
        "value": float(value) if rates else None,
        "value_complete": not aggregate.get("has_missing_rate") and bool(rates),
        "variants": variants,
    }


async def _load_category_lookup() -> dict:
    lookup = {_normalized(alias): category for alias, category in DEFAULT_CATEGORY_LOOKUP.items()}
    async for mapping in linen_category_mappings_collection.find({}):
        item_name = _normalized(mapping.get("normalized_item") or mapping.get("item_name", ""))
        category = mapping.get("category")
        if item_name and category in STANDARD_CATEGORIES:
            lookup[item_name] = category
    return lookup


def _gate_pass_price_snapshot(gate_pass: dict) -> dict:
    snapshot = gate_pass.get("quotation_price_snapshot") or {}
    prices = snapshot.get("prices") or {}
    return {_normalized(key.partition("||")[0]) + "||" + _normalized(key.partition("||")[2]): _decimal(value)
            for key, value in prices.items()}


def _build_gatepass_report(
    gate_passes: list,
    draft_passes: list,
    cancelled_count: int,
    start_date: date,
    end_date: date,
    selected_ids: dict,
    selected_prices: dict,
    category_lookup: dict,
) -> dict:
    daily_all: dict = {}
    daily_by_hotel: dict = {}
    period_all: dict = {}
    period_by_hotel: dict = {}
    hotel_totals: dict = {}
    item_totals: dict = {}
    daily_details = []
    unpriced_items = []
    unclassified_items: dict = {}
    transaction_dates = set()
    max_updated_at = None
    current_quote_fallback_count = 0
    total_quantity = Decimal("0")
    total_value = Decimal("0")
    selected_quotations = {}

    for gp in gate_passes:
        if "encryption_metadata" in gp:
            try:
                gp = decrypt_dict(gp, ["client_name", "items", "notes"])
            except Exception:
                pass
        client_name = (gp.get("client_name") or "Unknown").strip() or "Unknown"
        hotel_key = _normalized(client_name)
        gp_id = str(gp.get("_id") or gp.get("id") or "")
        gp_quote_id = str(gp.get("quotation_id") or "") or None
        hotel_has_explicit_selection = hotel_key in selected_ids
        selected_quote_id = selected_ids.get(hotel_key)
        selected_quotations[client_name] = selected_quote_id or gp_quote_id
        receiving_date = _business_date(gp.get("receiving_date"))
        if not receiving_date or not start_date <= receiving_date <= end_date:
            continue

        hotel = hotel_totals.setdefault(client_name, {
            "client_name": client_name,
            "quantity": Decimal("0"),
            "value": Decimal("0"),
            "gate_pass_count": 0,
            "transaction_dates": set(),
        })
        hotel["gate_pass_count"] += 1
        hotel["transaction_dates"].add(receiving_date.isoformat())
        transaction_dates.add(receiving_date.isoformat())
        updated_at = gp.get("updated_at")
        if updated_at and (max_updated_at is None or updated_at > max_updated_at):
            max_updated_at = updated_at

        historic_prices = _gate_pass_price_snapshot(gp)
        selected_quote = selected_prices.get(selected_quote_id) if selected_quote_id else {}
        current_quote = (
            selected_quote
            if hotel_has_explicit_selection
            else selected_quote or selected_prices.get(gp_quote_id) or {}
        )
        details = {
            "date": receiving_date.isoformat(),
            "client_name": client_name,
            "gate_pass_id": gp_id,
            "gate_pass_number": gp.get("gate_pass_number"),
            "quotation_id": selected_quote_id or gp_quote_id,
            "items": [],
        }

        for source_item in gp.get("items") or []:
            if source_item.get("rewashed"):
                continue
            item_name = (source_item.get("item_name") or "").strip()
            specification = (source_item.get("specification") or "").strip()
            quantity = _decimal(source_item.get("received_qty"))
            piece_count = int(_number(source_item.get("piece_count")))
            if not item_name or (quantity <= 0 and piece_count <= 0):
                continue

            item_key = _normalized(item_name) + "||" + _normalized(specification)
            historical_rate = historic_prices.get(item_key)
            if historical_rate is None and specification:
                historical_rate = historic_prices.get(_normalized(item_name) + "||")
            if historical_rate is not None:
                unit_price = historical_rate
                price_source = "gate_pass_price_snapshot"
            else:
                current_rate = current_quote.get((_normalized(item_name), _normalized(specification)))
                if current_rate is None and specification:
                    current_rate = current_quote.get((_normalized(item_name), ""))
                unit_price = _decimal(current_rate) if current_rate is not None else None
                price_source = "selected_current_quotation" if unit_price is not None else "missing"
                if unit_price is not None:
                    current_quote_fallback_count += 1

            if unit_price is not None and unit_price <= 0:
                unit_price = None
                price_source = "missing"

            category = category_lookup.get(_normalized(item_name), CATEGORY_UNCLASSIFIED)
            line_value = _currency(quantity * unit_price) if unit_price is not None else Decimal("0")
            total_quantity += quantity
            total_value += line_value
            hotel["quantity"] += quantity
            hotel["value"] += line_value
            if category == CATEGORY_UNCLASSIFIED:
                unknown = unclassified_items.setdefault(_normalized(item_name), {
                    "item_name": item_name,
                    "quantity": Decimal("0"),
                    "value": Decimal("0"),
                })
                unknown["quantity"] += quantity
                unknown["value"] += line_value

            for target, key in (
                (period_all, category),
                (period_by_hotel, (client_name, category)),
                (daily_all, (receiving_date.isoformat(), category)),
                (daily_by_hotel, (receiving_date.isoformat(), client_name, category)),
            ):
                aggregate = target.setdefault(key, _empty_aggregate())
                _add_variant(aggregate, item_name, specification, quantity, line_value, unit_price)

            item_key_global = (category, _normalized(item_name), _normalized(specification))
            item = item_totals.setdefault(item_key_global, {
                "category": category,
                "item_name": item_name,
                "specification": specification,
                "quantity": Decimal("0"),
                "value": Decimal("0"),
                "rates": set(),
                "hotel_names": set(),
            })
            item["quantity"] += quantity
            item["value"] += line_value
            if unit_price is not None:
                item["rates"].add(unit_price)
            item["hotel_names"].add(client_name)

            if unit_price is None:
                unpriced_items.append({
                    "date": receiving_date.isoformat(),
                    "client_name": client_name,
                    "gate_pass_number": gp.get("gate_pass_number"),
                    "item_name": item_name,
                    "specification": specification,
                    "quantity": float(quantity),
                    "quotation_id": selected_quote_id or gp_quote_id,
                    "reason": "No historical rate snapshot or usable selected quotation price.",
                })
            details["items"].append({
                "category": category,
                "item_name": item_name,
                "specification": specification,
                "quantity": float(quantity),
                "unit": source_item.get("unit") or ("kg" if "curtain" in item_name.casefold() else "pcs"),
                "piece_count": piece_count,
                "unit_price": float(unit_price) if unit_price is not None else None,
                "rate_source": price_source,
                "value": float(line_value) if unit_price is not None else None,
            })
        daily_details.append(details)

    active_daily = []
    date_count = (end_date - start_date).days + 1
    for offset in range(date_count):
        current_day = (start_date + timedelta(days=offset)).isoformat()
        categories = []
        day_quantity = Decimal("0")
        day_value = Decimal("0")
        for (day_key, category), aggregate in daily_all.items():
            if day_key != current_day:
                continue
            result = _finish_category(aggregate, category)
            categories.append(result)
            day_quantity += Decimal(str(result["quantity"]))
            if result["value"] is not None:
                day_value += Decimal(str(result["value"]))
        categories.sort(key=lambda row: row["category"].casefold())
        active_daily.append({
            "date": current_day,
            "transaction_count": len({row["gate_pass_id"] for row in daily_details if row["date"] == current_day}),
            "quantity": float(day_quantity),
            "value": float(_currency(day_value)),
            "categories": categories,
        })

    period_categories = [_finish_category(aggregate, category) for category, aggregate in period_all.items()]
    period_categories.sort(key=lambda row: row["category"].casefold())
    hotel_rows = [{
        "client_name": row["client_name"],
        "quantity": float(row["quantity"]),
        "value": float(_currency(row["value"])),
        "gate_pass_count": row["gate_pass_count"],
        "transaction_days": len(row["transaction_dates"]),
        "categories": [
            _finish_category(aggregate, category)
            for (hotel_name, category), aggregate in period_by_hotel.items()
            if hotel_name == row["client_name"]
        ],
    } for row in hotel_totals.values()]
    hotel_rows.sort(key=lambda row: row["client_name"].casefold())
    for row in hotel_rows:
        row["categories"].sort(key=lambda category: category["category"].casefold())

    item_rows = [{
        "category": row["category"],
        "item_name": row["item_name"],
        "specification": row["specification"],
        "quantity": float(row["quantity"]),
        "value": float(_currency(row["value"])),
        "rate_label": f"LKR {next(iter(row['rates'])):.2f}" if len(row["rates"]) == 1 else "Multiple Rates" if row["rates"] else "Missing price",
        "hotel_count": len(row["hotel_names"]),
    } for row in item_totals.values()]
    item_rows.sort(key=lambda row: (row["category"].casefold(), row["item_name"].casefold(), row["specification"].casefold()))

    draft_rows = []
    for draft in draft_passes:
        try:
            doc = decrypt_dict(draft, ["client_name", "items", "notes"])
        except Exception:
            doc = draft
        draft_rows.append({
            "gate_pass_id": str(draft.get("_id")),
            "gate_pass_number": doc.get("gate_pass_number"),
            "client_name": doc.get("client_name"),
            "date": (_business_date(doc.get("receiving_date")) or start_date).isoformat(),
            "quantity": round(sum(_number(item.get("received_qty")) for item in doc.get("items", []) if not item.get("rewashed")), 2),
        })

    return {
        "source_of_truth": "active_gate_pass_received_items",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "calendar_days": date_count,
        "transaction_days": len(transaction_dates),
        "total_quantity": float(total_quantity),
        "total_income": float(_currency(total_value)),
        "hotel_summary": hotel_rows,
        "period_summary": period_categories,
        "daily_summary": active_daily,
        "daily_details": sorted(daily_details, key=lambda row: (row["date"], row["client_name"].casefold(), row["gate_pass_number"] or "")),
        "item_details": item_rows,
        "unmapped_items": [{
            "item_name": row["item_name"],
            "category": CATEGORY_UNCLASSIFIED,
            "quantity": float(row["quantity"]),
            "value": float(_currency(row["value"])),
        } for row in unclassified_items.values()],
        "unpriced_items": unpriced_items,
        "unpriced_count": len(unpriced_items),
        "draft_receiving": draft_rows,
        "cancelled_gate_pass_count": cancelled_count,
        "current_quotation_fallback_count": current_quote_fallback_count,
        "selected_quotations": selected_quotations,
        "last_source_update": max_updated_at.isoformat() if hasattr(max_updated_at, "isoformat") else None,
    }


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


@router.get("/linen-categories")
async def list_linen_categories(
    current_user: dict = Depends(require_capability("report:read")),
):
    mappings = await linen_category_mappings_collection.find({}).sort("item_name", 1).to_list(length=None)
    return {
        "categories": STANDARD_CATEGORIES,
        "default_aliases": DEFAULT_CATEGORY_ALIASES,
        "mappings": [{
            "item_name": row.get("item_name"),
            "category": row.get("category"),
            "updated_at": row.get("updated_at"),
            "updated_by": row.get("updated_by"),
        } for row in mappings],
    }


@router.put("/linen-categories/mappings")
async def upsert_linen_category_mapping(
    payload: LinenCategoryMappingPayload,
    current_user: dict = Depends(require_role(["ADMIN"])),
):
    item_name = " ".join(payload.item_name.split())
    category = payload.category.strip()
    if category not in STANDARD_CATEGORIES or category == CATEGORY_UNCLASSIFIED:
        raise HTTPException(status_code=400, detail="Choose a standard linen category.")
    now = datetime.now(timezone.utc)
    document = {
        "item_name": item_name,
        "normalized_item": _normalized(item_name),
        "category": category,
        "updated_at": now,
        "updated_by": str(current_user.get("auth_id") or ""),
    }
    await linen_category_mappings_collection.update_one(
        {"normalized_item": document["normalized_item"]},
        {"$set": document, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    await log_audit(
        current_user.get("auth_id", ""),
        "LINEN_CATEGORY_MAPPING_UPSERT",
        "linen_category_mapping",
        document["normalized_item"],
        {"item_name": item_name, "category": category},
    )
    return document


@router.delete("/linen-categories/mappings/{item_name}")
async def delete_linen_category_mapping(
    item_name: str,
    current_user: dict = Depends(require_role(["ADMIN"])),
):
    normalized_item = _normalized(item_name)
    result = await linen_category_mappings_collection.delete_one({"normalized_item": normalized_item})
    if not result.deleted_count:
        raise HTTPException(status_code=404, detail="Category mapping not found.")
    await log_audit(
        current_user.get("auth_id", ""),
        "LINEN_CATEGORY_MAPPING_DELETE",
        "linen_category_mapping",
        normalized_item,
    )
    return {"deleted": True, "item_name": item_name}


@router.post("/monthly-operations")
async def monthly_operations_report(
    payload: MonthlyOperationsReportRequest,
    include_prefix: bool = Query(
        False, description="Include hotel-code prefix in bill / gate-pass numbers"
    ),
    current_user: dict = Depends(require_capability("report:read")),
):
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date.")
    laundry_id = resolve_laundry_id(current_user)
    query: dict = {
        "receiving_date": {
            "$gte": day_start(payload.start_date),
            "$lt": day_start(payload.end_date + timedelta(days=1)),
        },
        "status": {"$nin": ["DRAFT", "CANCELLED"]},
    }
    all_gate_passes = await gatepasses_collection.find(
        scoped_query(query, laundry_id)
    ).sort("receiving_date", 1).to_list(length=None)
    available_hotels = set()
    for gate_pass in all_gate_passes:
        try:
            decoded = decrypt_dict(gate_pass, ["client_name", "items", "notes"])
        except Exception:
            decoded = gate_pass
        hotel_name = (decoded.get("client_name") or "").strip()
        if hotel_name:
            available_hotels.add(hotel_name)
    gate_passes = all_gate_passes
    if payload.hotel_name:
        hotel_key = _normalized(payload.hotel_name)
        gate_passes = [
            gate_pass for gate_pass in all_gate_passes
            if _normalized((decrypt_dict(gate_pass, ["client_name", "items", "notes"]).get("client_name") or "")) == hotel_key
        ]
    draft_query = {
        "receiving_date": query["receiving_date"],
        "status": "DRAFT",
    }
    if payload.hotel_name:
        draft_query["client_name_search"] = get_search_token(payload.hotel_name)
    draft_passes = await gatepasses_collection.find(
        scoped_query(draft_query, laundry_id)
    ).to_list(length=None)
    cancelled_count = await gatepasses_collection.count_documents(scoped_query({
        "receiving_date": query["receiving_date"],
        "status": "CANCELLED",
        **({"client_name_search": get_search_token(payload.hotel_name)} if payload.hotel_name else {}),
    }, laundry_id))

    selected_ids = {
        _normalized(name): str(quotation_id) if quotation_id else None
        for name, quotation_id in payload.selected_quotations.items()
    }
    explicit_hotel_keys = set(selected_ids)
    for gate_pass in gate_passes:
        try:
            decoded = decrypt_dict(gate_pass, ["client_name", "items", "notes"])
        except Exception:
            decoded = gate_pass
        hotel_key = _normalized(decoded.get("client_name", ""))
        quote_id = str(decoded.get("quotation_id") or "") or None
        if hotel_key and hotel_key not in explicit_hotel_keys and quote_id:
            selected_ids[hotel_key] = quote_id

    quote_ids = {quote_id for quote_id in selected_ids.values() if quote_id}
    quote_ids.update(str(gp.get("quotation_id")) for gp in gate_passes if gp.get("quotation_id"))
    selected_prices = {}
    for quotation_id in quote_ids:
        selected_prices[quotation_id] = await _quotation_prices(quotation_id, laundry_id)
        snapshot = payload.quotation_snapshots.get(str(quotation_id))
        if not selected_prices[quotation_id] and snapshot and str(snapshot.get("id")) == str(quotation_id):
            selected_prices[quotation_id] = _quotation_price_map(snapshot.get("line_items", []))

    report = _build_gatepass_report(
        gate_passes,
        draft_passes,
        cancelled_count,
        payload.start_date,
        payload.end_date,
        selected_ids,
        selected_prices,
        await _load_category_lookup(),
    )
    report["hotel_options"] = sorted(available_hotels, key=str.casefold)
    report["selected_hotel"] = payload.hotel_name
    report["hotel_summary"] = [
        {**hotel, "suggested_quotation_id": selected_ids.get(_normalized(hotel["client_name"]))}
        for hotel in report["hotel_summary"]
    ]
    report["hotels"] = report["hotel_summary"]

    # Apply prefix formatting to daily details for printing
    for detail in report.get("daily_details", []):
        if detail.get("gate_pass_number"):
            detail["gate_pass_number"] = format_bill_number(
                detail["gate_pass_number"], include_prefix
            )

    return report