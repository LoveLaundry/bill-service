"""Shared loaders that turn stored records into canonical balances.

Every screen, report and router needs the same three things: the gate passes,
the movement documents (deliveries / returns), and the balance derived from
them. Before this module each router assembled those itself, and they drifted
apart — one ignored returns, one attributed a delivery to every gate pass it
mentioned, one trusted a stored status label. That drift is what let a
corrected delivery show a stale balance on one screen and a fresh one on the
next.

Everything here is a thin, read-only adapter over
:mod:`bill_service.services.balance_engine`. The engine stays pure; this
module owns the I/O.
"""
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from bson import ObjectId

from ..crypto_helper import decrypt_dict
from ..database.main_db import deliveries_collection, gatepasses_collection, returns_collection
from . import balance_engine as be

SENSITIVE_FIELDS = ["client_name", "items", "notes"]


def to_object_id(value: str) -> Optional[ObjectId]:
    """Best-effort ObjectId conversion; ``None`` when the value is not one."""
    if isinstance(value, ObjectId):
        return value
    if value and ObjectId.is_valid(str(value)):
        return ObjectId(str(value))
    return None


def serialize(doc: dict, sensitive_fields: Optional[List[str]] = None) -> dict:
    """Decrypt a document and swap ``_id`` for a string ``id``."""
    out = decrypt_dict(doc, sensitive_fields or SENSITIVE_FIELDS)
    out["id"] = str(out.get("_id"))
    out.pop("_id", None)
    return out


async def load_gate_passes(gate_pass_ids: Optional[Iterable[str]] = None) -> List[dict]:
    """Load + decrypt gate passes, optionally restricted to specific ids."""
    query: dict = {}
    ids = [to_object_id(g) for g in (gate_pass_ids or [])]
    ids = [g for g in ids if g is not None]
    if gate_pass_ids is not None:
        if not ids:
            return []
        query["_id"] = {"$in": ids}
    cursor = gatepasses_collection.find(query).sort("receiving_date", -1)
    out: List[dict] = []
    async for doc in cursor:
        try:
            gp = serialize(doc)
        except Exception:
            continue
        gp["gate_pass_id"] = gp["id"]
        out.append(gp)
    return out


async def load_gate_pass(gate_pass_id: str) -> Optional[dict]:
    """Load + decrypt a single gate pass, or ``None`` when it does not exist."""
    oid = to_object_id(gate_pass_id)
    if oid is None:
        return None
    doc = await gatepasses_collection.find_one({"_id": oid})
    if not doc:
        return None
    try:
        gp = serialize(doc)
    except Exception:
        return None
    gp["gate_pass_id"] = gp["id"]
    return gp


def source_filter(gate_pass_id: Optional[str]) -> dict:
    """Query filter matching every delivery that draws from a gate pass.

    ``source_gate_pass_ids`` is a denormalised, non-sensitive list of gate pass
    ids kept alongside the encrypted ``items`` payload precisely so deliveries
    remain queryable per pass. Legacy rows written before that field existed
    still match through their document-level ``gate_pass_id``.
    """
    if not gate_pass_id:
        return {}
    return {
        "$or": [
            {"source_gate_pass_ids": gate_pass_id},
            {"gate_pass_id": gate_pass_id, "source_gate_pass_ids": {"$exists": False}},
        ]
    }


async def load_movements(
    gate_pass_ids: Optional[Iterable[str]] = None,
) -> Tuple[List[dict], List[dict]]:
    """Load + decrypt the delivery and return documents for the given passes.

    ``gate_pass_ids=None`` loads every movement. Passing an empty iterable
    returns nothing (an empty selector must not silently become "all").
    """
    if gate_pass_ids is None:
        gp_filter: dict = {}
    else:
        ids = [g for g in (gate_pass_ids or []) if g]
        if not ids:
            return [], []
        # Flatten to a single top-level $or: nested $or is legal in MongoDB but
        # confusing to read and is not handled uniformly by test doubles.
        gp_filter = {"$or": [clause for g in ids for clause in source_filter(g)["$or"]]}

    deliveries: List[dict] = []
    async for doc in deliveries_collection.find(gp_filter):
        try:
            deliveries.append(serialize(doc))
        except Exception:
            continue

    returns: List[dict] = []
    async for doc in returns_collection.find({"gate_pass_id": {"$in": [g for g in (gate_pass_ids or []) if g]}} if gate_pass_ids is not None else {}):
        try:
            returns.append(serialize(doc))
        except Exception:
            continue

    return deliveries, returns


def delivered_by_gate_pass(deliveries: Iterable[dict]) -> Dict[str, Dict[str, int]]:
    """Group delivered quantities by the gate pass each line came from."""
    return be.compute_delivered_by_gate_pass(deliveries)


async def movement_maps(
    gate_pass_ids: Optional[Iterable[str]] = None,
) -> Tuple[Dict[str, Dict[str, int]], Dict[str, Dict[str, int]]]:
    """``(delivered_by_gp, returned_by_gp)`` for the given passes.

    The single replacement for the load-and-group loop that was copy-pasted
    into every dashboard, report and reconciliation handler. Each of those
    copies derived the map slightly differently, so the same delivery could be
    attributed to the wrong pass on one screen and the right one on another.
    """
    deliveries, returns = await load_movements(gate_pass_ids)
    return (
        be.compute_delivered_by_gate_pass(deliveries),
        _returns_by_gate_pass(returns),
    )


def returned_by_gate_pass(returns: Iterable[dict]) -> Dict[str, Dict[str, int]]:
    """Group pending return quantities by gate pass."""
    return _returns_by_gate_pass(returns)


def _returns_by_gate_pass(returns: Iterable[dict]) -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {}
    for ret in returns or []:
        gp_id = str(ret.get("gate_pass_id") or "")
        if not gp_id:
            continue
        bucket = out.setdefault(gp_id, {})
        for key, qty in be.compute_returned_by_item([ret]).items():
            bucket[key] = bucket.get(key, 0) + qty
    return out


def availability_maps(
    gate_passes: List[dict],
    delivered: Dict[str, Dict[str, int]],
    returned: Optional[Dict[str, Dict[str, int]]] = None,
) -> Dict[str, Dict[str, int]]:
    """{gate_pass_id: {item_key: still-deliverable quantity}}."""
    returned = returned or {}
    out: Dict[str, Dict[str, int]] = {}
    for gp in gate_passes or []:
        gp_id = str(gp.get("gate_pass_id") or gp.get("id") or "")
        if not gp_id:
            continue
        out[gp_id] = be.compute_available(
            be.gate_pass_received_map(gp.get("items", [])),
            delivered.get(gp_id, {}),
            returned.get(gp_id, {}),
        )
    return out


def balances_for(
    gate_passes: List[dict],
    delivered: Dict[str, Dict[str, int]],
    returned: Optional[Dict[str, Dict[str, int]]] = None,
) -> Dict[str, dict]:
    """{gate_pass_id: canonical balance document}."""
    returned = returned or {}
    out: Dict[str, dict] = {}
    for gp in gate_passes or []:
        gp_id = str(gp.get("gate_pass_id") or gp.get("id") or "")
        if not gp_id:
            continue
        out[gp_id] = be.compute_gate_pass_balance(
            gp.get("items", []),
            delivered.get(gp_id, {}),
            returned.get(gp_id, {}),
            marked_delivered=bool(gp.get("marked_delivered")),
        )
    return out


def derived_status(gp: dict, balance: dict) -> str:
    return be.derive_gate_pass_status(balance, gp.get("status") or "RECEIVED")


async def refresh_gate_pass_statuses(gate_pass_ids: Iterable[str]) -> Dict[str, str]:
    """Recompute and persist every affected gate pass's status from quantities.

    The status is a *derived* value, so any flow that changes a quantity must
    call this — otherwise a corrected delivery leaves the pass still showing
    DELIVERED while its balance says otherwise. Returns the ids whose stored
    status actually changed.
    """
    ids = [g for g in dict.fromkeys(gate_pass_ids or []) if g]
    if not ids:
        return {}

    gate_passes = await load_gate_passes(ids)
    deliveries, returns = await load_movements(ids)
    delivered = delivered_by_gate_pass(deliveries)
    returned = returned_by_gate_pass(returns)
    balances = balances_for(gate_passes, delivered, returned)

    changed: Dict[str, str] = {}
    now = datetime.now(timezone.utc)
    for gp in gate_passes:
        gp_id = str(gp.get("gate_pass_id") or gp.get("id") or "")
        balance = balances.get(gp_id)
        if not balance:
            continue
        new_status = derived_status(gp, balance)
        if new_status == (gp.get("status") or ""):
            continue
        changed[gp_id] = new_status
        # ``status`` is a plain, unencrypted field: a targeted $set keeps the
        # encrypted payload (and its wrapped DEK) untouched.
        await gatepasses_collection.update_one(
            {"_id": to_object_id(gp_id)},
            {"$set": {"status": new_status, "updated_at": now}},
        )
    return changed


async def sync_gate_passes(gate_pass_ids: Iterable[str]) -> None:
    """Bump sync versions and enqueue replication for the given gate passes."""
    from ..repositories.main_repository import bump_version, enqueue_sync

    for gp_id in dict.fromkeys(gate_pass_ids or []):
        oid = to_object_id(gp_id)
        if oid is None:
            continue
        try:
            version = await bump_version("gatepass", oid)
            await enqueue_sync("gatepass", oid, version)
        except Exception:
            # A gate pass deleted between the read and the write must not break
            # the caller's response; the balance was still computed correctly.
            import logging

            logging.getLogger("bill_service").warning(
                "gate pass %s vanished before sync enqueue", gp_id, exc_info=True
            )


def movements_for_delivery(deliveries: Iterable[dict], delivery_id: str) -> bool:
    """True when the given delivery is present in the movement set."""
    target = str(delivery_id)
    return any(str(d.get("id") or d.get("_id")) == target for d in deliveries or [])
