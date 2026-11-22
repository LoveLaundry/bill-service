"""Admin-defined linen scan legality, read from the shared company settings.

The state vocabulary and action -> target-status map live in the linens router.
Whether a particular ``from_status -> to_status`` is legal is operational policy
and belongs to the tenant admin, so it is stored in Company Settings (owned by
laundry-management) and read from the shared MAIN database at scan time.

Failures are deliberately soft: if settings are missing or unreadable we fall
back to the permissive default. This keeps scanning available during a settings
outage; admins opt into enforcement by saving their matrix.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..database.main_db import company_settings_collection
from ..tenancy import resolve_laundry_id, scoped_query

# Mirrors laundry-management's DEFAULT_SETTINGS so a fresh tenant behaves the
# same whichever service answers first.
DEFAULT_RULES: Dict[str, Any] = {
    "violation_mode": "block",
    "rules": [
        {"from_status": "IN_STOCK", "to_statuses": ["COLLECTED"]},
        {"from_status": "AT_CLIENT", "to_statuses": ["COLLECTED"]},
        {"from_status": "COLLECTED", "to_statuses": ["AT_LAUNDRY"]},
        {"from_status": "AT_LAUNDRY", "to_statuses": ["WASHING"]},
        {"from_status": "WASHING", "to_statuses": ["DRYING"]},
        {"from_status": "DRYING", "to_statuses": ["PRESSING"]},
        {"from_status": "PRESSING", "to_statuses": ["READY"]},
        {"from_status": "READY", "to_statuses": ["DELIVERED"]},
        {"from_status": "DELIVERED", "to_statuses": ["AT_CLIENT"]},
        {
            "from_status": "MISSING",
            "to_statuses": ["IN_STOCK", "AT_CLIENT", "DAMAGED", "RETIRED"],
        },
        {"from_status": "DAMAGED", "to_statuses": ["IN_STOCK", "RETIRED"]},
    ],
}


def normalize_policy(value: Any) -> Dict[str, Any]:
    """Coerce a stored settings value into a safe policy shape."""
    if not isinstance(value, dict):
        return {"violation_mode": "block", "rules": list(DEFAULT_RULES["rules"]), "configured": False}
    mode = str(value.get("violation_mode") or "block").strip().lower()
    if mode not in {"block", "warn"}:
        mode = "block"
    rules: List[Dict[str, Any]] = []
    for row in value.get("rules") or []:
        if not isinstance(row, dict):
            continue
        from_status = str(row.get("from_status") or "").strip().upper()
        to_statuses = [
            str(item).strip().upper()
            for item in (row.get("to_statuses") or [])
            if str(item).strip()
        ]
        if from_status and to_statuses:
            rules.append({"from_status": from_status, "to_statuses": to_statuses})
    return {
        "violation_mode": mode,
        "rules": rules if "rules" in value else list(DEFAULT_RULES["rules"]),
        "configured": bool(value.get("rules") is not None),
    }


async def load_transition_policy(current_user: Optional[dict]) -> Dict[str, Any]:
    """Read the tenant's linen transition policy (fail-soft to defaults)."""
    try:
        laundry_id = resolve_laundry_id(current_user)
        doc = await company_settings_collection.find_one(
            scoped_query({"key": "main"}, laundry_id)
        )
    except Exception:
        # A settings outage must not stop scanning. resolve_laundry_id can raise
        # HTTPException on a bad session; the scan endpoint still validates the
        # session itself, so defaults here are safe.
        return {**normalize_policy(DEFAULT_RULES), "configured": False}
    if not doc or "linen_transition_rules" not in doc:
        return {**normalize_policy(DEFAULT_RULES), "configured": False}
    return normalize_policy(doc.get("linen_transition_rules"))


def check_transition(
    policy: Dict[str, Any], from_status: str, to_status: str
) -> Optional[str]:
    """Return a human-readable reason when a transition is not allowed.

    ``None`` means allowed. A status with no configured row is unrestricted so
    admins can define only the transitions they care about.
    """
    allowed: Optional[List[str]] = None
    for row in policy.get("rules") or []:
        if str(row.get("from_status")) == from_status:
            allowed = [str(item) for item in row.get("to_statuses") or []]
            break
    if allowed is None or to_status in allowed:
        return None
    return f"Cannot move linen from {from_status} to {to_status}"


def violation_mode(policy: Dict[str, Any]) -> str:
    return "warn" if policy.get("violation_mode") == "warn" else "block"
