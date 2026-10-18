"""Back-sync of manually-created gate passes into the monthly operations grid.

Monthly operations are the *canonical input surface*: a day is entered, then
confirmed, which creates a real gate pass (starting DRAFT). This service handles
the reverse direction — when a gate pass is created *manually* through the normal
``POST /gatepasses`` endpoint (i.e. *not* generated from a monthly confirm), it
must be reflected in the matching monthly entry so the two views are always
consistent.

Rules
-----
* Only gate passes with ``origin=None`` (or ``origin.kind != "monthly"``) are
  synced here.  Monthly-generated passes are already linked.
* The monthly entry for ``(client_name, "receiving", year, month)`` is found or
  created.
* For the matching *day*:
  - EMPTY → create the day state in CONFIRMED status with quantities derived
    from the gate pass items.
  - DRAFT  → update quantities and references, promote to CONFIRMED.
  - CONFIRMED (same gate pass already in gate_pass_ids) → no-op.
  - CONFIRMED (different gate passes) → append this gate pass's ID to
    gate_pass_ids (a single day CAN have multiple passes).
  - CANCELLED → do nothing; log a warning.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from ..crypto_helper import encrypt_dict, get_search_token
from ..database.main_db import monthly_entries_collection

log = logging.getLogger(__name__)

MONTH_SENSITIVE_FIELDS = ["client_name", "notes"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _next_month_doc(client_name: str, year: int, month: int) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "client_name": client_name,
        "client_name_search": get_search_token(client_name),
        "kind": "receiving",
        "year": year,
        "month": month,
        "quotation_id": None,
        "days": {},
        "created_at": now,
        "updated_at": now,
    }


def _quantities_from_gp(gp: dict) -> tuple[dict, dict]:
    """Derive ``{item_key: qty}`` and ``{item_key: piece_count}`` from a gate pass."""
    quantities: dict[str, float] = {}
    piece_quantities: dict[str, int] = {}
    for item in gp.get("items") or []:
        name = item.get("item_name", "")
        spec = item.get("specification") or None
        key = f"{name}||{spec}" if spec else f"{name}||"
        qty = float(item.get("received_qty", 0) or 0)
        pc = int(item.get("piece_count", 0) or 0)
        if qty > 0:
            quantities[key] = quantities.get(key, 0.0) + qty
        if pc > 0:
            piece_quantities[key] = piece_quantities.get(key, 0) + pc
    return quantities, piece_quantities


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def sync_gate_pass_to_monthly(
    gate_pass: dict,
    *,
    actor_id: str = "system",
) -> bool:
    """Reflect a manually-created gate pass in the monthly receiving grid.

    Parameters
    ----------
    gate_pass:
        The serialized gate pass dict (as returned by ``_serialize``).
    actor_id:
        The auth_id of the user creating the gate pass, for audit trail.

    Returns
    -------
    bool
        ``True`` when the monthly entry was modified; ``False`` when this
        gate pass was already reflected or syncing was skipped.
    """
    # ── Guard: only sync manual gate passes, not monthly-generated ones ───
    origin = gate_pass.get("origin") or {}
    if isinstance(origin, dict) and origin.get("kind") == "monthly":
        # Already generated from monthly confirm — nothing to do.
        return False

    # ── Extract date components ────────────────────────────────────────────
    client_name: str = gate_pass.get("client_name") or ""
    gp_id: str = str(gate_pass.get("id") or "")
    receiving_date: Optional[datetime] = gate_pass.get("receiving_date")

    if not client_name or not gp_id or not receiving_date:
        log.warning(
            "sync_gate_pass_to_monthly: skipping gate pass %s — missing required fields",
            gp_id,
        )
        return False

    if isinstance(receiving_date, str):
        try:
            receiving_date = datetime.fromisoformat(receiving_date)
        except ValueError:
            log.warning("sync_gate_pass_to_monthly: could not parse receiving_date for %s", gp_id)
            return False

    year = receiving_date.year
    month = receiving_date.month
    day = receiving_date.day
    date_str = f"{year:04d}-{month:02d}-{day:02d}"

    # ── Find or create the monthly doc ─────────────────────────────────────
    month_doc = await monthly_entries_collection.find_one(
        {
            "client_name_search": get_search_token(client_name),
            "kind": "receiving",
            "year": year,
            "month": month,
        }
    )

    if month_doc is None:
        encrypted = encrypt_dict(
            _next_month_doc(client_name, year, month),
            MONTH_SENSITIVE_FIELDS,
        )
        result = await monthly_entries_collection.insert_one(encrypted)
        month_doc = await monthly_entries_collection.find_one({"_id": result.inserted_id})

    existing_day = (month_doc.get("days") or {}).get(str(day))
    day_status = (existing_day or {}).get("status", "EMPTY")

    # ── Skip if already linked ─────────────────────────────────────────────
    if day_status == "CONFIRMED" and gp_id in (existing_day.get("gate_pass_ids") or []):
        return False

    # ── Skip if CANCELLED day ─────────────────────────────────────────────
    if day_status == "CANCELLED":
        log.warning(
            "sync_gate_pass_to_monthly: day %s-%02d-%02d is CANCELLED; "
            "gate pass %s was NOT linked to monthly entry.",
            year, month, day, gp_id,
        )
        return False

    # ── Build the day state ────────────────────────────────────────────────
    quantities, piece_quantities = _quantities_from_gp(gate_pass)
    now = datetime.now(timezone.utc)

    # Merge with existing gate_pass_ids (CONFIRMED day with other GPs)
    existing_gp_ids: list = list(existing_day.get("gate_pass_ids") or []) if existing_day else []
    if gp_id not in existing_gp_ids:
        existing_gp_ids.append(gp_id)

    # Merge quantities: keep existing non-zero, add new (don't overwrite manual entries)
    merged_quantities: dict = dict(existing_day.get("quantities") or {}) if existing_day else {}
    for key, qty in quantities.items():
        if key not in merged_quantities or merged_quantities[key] == 0:
            merged_quantities[key] = qty

    merged_pieces: dict = dict(existing_day.get("piece_quantities") or {}) if existing_day else {}
    for key, pc in piece_quantities.items():
        if key not in merged_pieces or merged_pieces[key] == 0:
            merged_pieces[key] = pc

    day_state = {
        "day": day,
        "date": date_str,
        "status": "CONFIRMED",
        "total_qty": sum(merged_quantities.values()),
        "quantities": merged_quantities,
        "piece_quantities": merged_pieces,
        # Preserve existing references; fill from gate pass if absent
        "bill_number": (
            (existing_day or {}).get("bill_number")
            or gate_pass.get("manual_bill_number")
        ),
        "gate_pass_number": (
            (existing_day or {}).get("gate_pass_number")
            or gate_pass.get("manual_gate_pass_number")
            or gate_pass.get("gate_pass_number")
        ),
        "alrs_number": (
            (existing_day or {}).get("alrs_number")
            or gate_pass.get("alrs_number")
        ),
        "gate_pass_ids": existing_gp_ids,
        "delivery_ids": list(existing_day.get("delivery_ids") or []) if existing_day else [],
        "rewash_ids": list(existing_day.get("rewash_ids") or []) if existing_day else [],
        "confirmed_by": (existing_day or {}).get("confirmed_by") or actor_id,
        "confirmed_at": (existing_day or {}).get("confirmed_at") or now,
        "notes": (existing_day or {}).get("notes"),
        "updated_at": now,
        # Provenance: mark that this day was synced from a manual gate pass
        "synced_from_gate_pass_id": gp_id,
    }

    await monthly_entries_collection.update_one(
        {"_id": month_doc["_id"]},
        {
            "$set": {
                f"days.{day}": day_state,
                "updated_at": now,
            }
        },
    )

    log.info(
        "sync_gate_pass_to_monthly: linked gate pass %s to monthly entry "
        "%s receiving %d-%02d-%02d",
        gp_id, client_name, year, month, day,
    )
    return True


async def update_monthly_day_references(
    gate_pass: dict,
) -> bool:
    """Propagate reference field changes (bill_number, alrs_number) from an
    updated gate pass to its monthly entry day, if one exists.

    Called after a gate pass edit so the two records stay in sync.
    """
    origin = gate_pass.get("origin") or {}
    if isinstance(origin, dict) and origin.get("kind") == "monthly":
        return False  # monthly-generated; monthly is the source of truth

    client_name: str = gate_pass.get("client_name") or ""
    gp_id: str = str(gate_pass.get("id") or "")
    receiving_date: Optional[datetime] = gate_pass.get("receiving_date")

    if not client_name or not receiving_date:
        return False

    if isinstance(receiving_date, str):
        try:
            receiving_date = datetime.fromisoformat(receiving_date)
        except ValueError:
            return False

    year, month, day = receiving_date.year, receiving_date.month, receiving_date.day

    month_doc = await monthly_entries_collection.find_one(
        {
            "client_name_search": get_search_token(client_name),
            "kind": "receiving",
            "year": year,
            "month": month,
        }
    )
    if not month_doc:
        return False

    existing_day = (month_doc.get("days") or {}).get(str(day))
    if not existing_day:
        return False

    if gp_id not in (existing_day.get("gate_pass_ids") or []):
        return False  # gate pass not linked to this day

    now = datetime.now(timezone.utc)
    updates = {"updated_at": now}
    updates[f"days.{day}.updated_at"] = now

    new_bill = gate_pass.get("manual_bill_number")
    new_alrs = gate_pass.get("alrs_number")
    new_gp_num = gate_pass.get("manual_gate_pass_number") or gate_pass.get("gate_pass_number")

    if new_bill is not None:
        updates[f"days.{day}.bill_number"] = new_bill
    if new_alrs is not None:
        updates[f"days.{day}.alrs_number"] = new_alrs
    if new_gp_num is not None:
        updates[f"days.{day}.gate_pass_number"] = new_gp_num

    await monthly_entries_collection.update_one(
        {"_id": month_doc["_id"]},
        {"$set": updates},
    )
    return True
