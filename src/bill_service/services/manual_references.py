"""Reserve human-facing references across monthly operations and generated documents.

Bill numbers are scoped per hotel (client): the same number may appear for
different hotels, because the stored value always carries the hotel-code prefix
(e.g. ``RH-1001`` for hotel ``RH``).  All other reference types (gate-pass
numbers, ALRS numbers, legacy invoice numbers, generated shop-bill IDs) remain
globally unique.
"""
import re
from uuid import uuid4

from pymongo.errors import DuplicateKeyError

from ..database.main_db import (
    bills_collection,
    gatepasses_collection,
    legacy_invoices_collection,
    manual_references_collection,
    shop_bills_collection,
)
from ..crypto_helper import get_search_token


REFERENCE_FIELDS = ("bill_number", "gate_pass_number", "alrs_number")

# Reference types whose uniqueness is scoped to a single hotel/client.
# All other types are globally unique.
HOTEL_SCOPED_FIELDS = frozenset({"bill_number"})


def normalize_reference(value: str) -> str:
    return " ".join(value.split()).casefold()


async def _existing_document_reference(
    value: str,
    *,
    client_name: str | None = None,
) -> bool:
    """Check whether *value* already exists in any live document.

    For bill-number fields the check is scoped to ``client_name`` (same hotel).
    All other reference fields are checked globally.
    """
    parts = value.split()
    pattern = r"^\s*" + r"\s+".join(re.escape(part) for part in parts) + r"\s*$"

    # Gate-pass / ALRS / GP-number checks are always global.
    global_checks = (
        (gatepasses_collection, "gate_pass_number"),
        (gatepasses_collection, "manual_gate_pass_number"),
        (gatepasses_collection, "alrs_number"),
        (bills_collection, "manual_gate_pass_number"),
        (bills_collection, "alrs_number"),
        (legacy_invoices_collection, "invoice_number"),
    )
    for collection, field in global_checks:
        if await collection.find_one({field: {"$regex": pattern, "$options": "i"}}):
            return True

    # Bill-number checks: scope to hotel when client_name is provided.
    bill_number_checks = (
        (gatepasses_collection, "manual_bill_number"),
        (bills_collection, "manual_bill_number"),
        (shop_bills_collection, "bill_number"),
    )
    for collection, field in bill_number_checks:
        query: dict = {field: {"$regex": pattern, "$options": "i"}}
        if client_name:
            query["client_name_search"] = get_search_token(client_name)
        if await collection.find_one(query):
            return True

    return False


async def reserve_references(
    owner: str,
    references: dict[str, str | None],
    *,
    client_name: str | None = None,
) -> None:
    """Claim reference values for *owner*, raising ``ValueError`` on conflict.

    Parameters
    ----------
    owner:
        Opaque ownership token (e.g. ``"monthly:..."``, ``"gatepass:<uuid>"``).
    references:
        Mapping ``{field_name: value}`` of the references to reserve.
    client_name:
        The hotel/client the reservation belongs to.  Required for
        bill-number reservations so that uniqueness is scoped per hotel.
    """
    values: dict[str, str] = {}
    for field in REFERENCE_FIELDS:
        value = (references.get(field) or "").strip()
        if value:
            normalized = normalize_reference(value)
            previous = values.get(normalized)
            if previous is not None:
                raise ValueError(
                    f"{field.replace('_', ' ').title()} conflicts with "
                    f"{previous.replace('_', ' ')}."
                )
            values[normalized] = field

    # Choose the scope key used inside manual_references_collection.
    # For hotel-scoped fields, include the hotel token so the same
    # normalized value can coexist for different hotels.
    def _scope_key(normalized: str, field: str) -> str:
        if field in HOTEL_SCOPED_FIELDS and client_name:
            return f"{normalized}|hotel:{get_search_token(client_name)}"
        return normalized

    for normalized, field in values.items():
        scope = _scope_key(normalized, field)
        reservation = await manual_references_collection.find_one({"scope_key": scope})
        if reservation and reservation.get("owner") != owner:
            raise ValueError(
                f"'{references[field]}' is already used as a "
                f"{reservation.get('reference_type', 'document reference')}."
            )
        if not reservation and await _existing_document_reference(
            references[field],
            client_name=client_name if field in HOTEL_SCOPED_FIELDS else None,
        ):
            raise ValueError(
                f"'{references[field]}' is already used by another document."
            )

    existing_owned_scopes = {
        entry["scope_key"]
        async for entry in manual_references_collection.find({"owner": owner})
        if "scope_key" in entry
    }

    try:
        for normalized, field in values.items():
            scope = _scope_key(normalized, field)
            await manual_references_collection.update_one(
                {"scope_key": scope, "owner": owner},
                {
                    "$set": {
                        "normalized": normalized,
                        "scope_key": scope,
                        "value": references[field].strip(),
                        "owner": owner,
                        "reference_type": field,
                        "client_name_search": (
                            get_search_token(client_name)
                            if field in HOTEL_SCOPED_FIELDS and client_name
                            else None
                        ),
                    }
                },
                upsert=True,
            )
    except DuplicateKeyError as exc:
        newly_reserved = [
            _scope_key(normalized, field)
            for normalized, field in values.items()
            if _scope_key(normalized, field) not in existing_owned_scopes
        ]
        if newly_reserved:
            await manual_references_collection.delete_many(
                {"owner": owner, "scope_key": {"$in": newly_reserved}}
            )
        reservation = await manual_references_collection.find_one(
            {"scope_key": _scope_key(normalized, field)}
        )
        used_by = (
            reservation.get("reference_type", "document reference")
            if reservation
            else "another document"
        )
        raise ValueError(
            f"'{references[field]}' is already used as a {used_by}."
        ) from exc


async def release_references(
    owner: str,
    references: dict[str, str | None],
    *,
    client_name: str | None = None,
) -> None:
    normalized_list = [
        normalize_reference(value.strip())
        for value in references.values()
        if value and value.strip()
    ]
    if not normalized_list:
        return

    # Build scope keys matching the ones used during reservation.
    scope_keys = []
    for field, value in references.items():
        if not value or not value.strip():
            continue
        norm = normalize_reference(value.strip())
        if field in HOTEL_SCOPED_FIELDS and client_name:
            scope_keys.append(f"{norm}|hotel:{get_search_token(client_name)}")
        else:
            scope_keys.append(norm)  # legacy entries without scope_key also covered below

    if scope_keys:
        await manual_references_collection.delete_many(
            {
                "owner": owner,
                "$or": [
                    {"scope_key": {"$in": scope_keys}},
                    {"normalized": {"$in": normalized_list}},  # backwards compat
                ],
            }
        )


async def reserve_generated_reference(value: str, reference_type: str) -> bool:
    """Atomically claim an auto-generated value; ``False`` means choose another."""
    normalized = normalize_reference(value)
    scope_key = normalized  # auto-generated references are always global
    owner = f"generated:{reference_type}:{uuid4().hex}"
    if await _existing_document_reference(value):
        return False
    try:
        await manual_references_collection.insert_one(
            {
                "normalized": normalized,
                "scope_key": scope_key,
                "value": value,
                "owner": owner,
                "reference_type": reference_type,
            }
        )
    except DuplicateKeyError:
        return False
    return True


async def reserve_generated_bill_number(value: str) -> bool:
    """Reserve generated shop-bill numbers in the same global namespace."""
    normalized = normalize_reference(value)
    scope_key = normalized
    if await _existing_document_reference(value):
        return False
    try:
        await manual_references_collection.insert_one(
            {
                "normalized": normalized,
                "scope_key": scope_key,
                "value": value,
                "owner": f"generated:shop_bill:{uuid4().hex}",
                "reference_type": "generated shop bill number",
            }
        )
    except DuplicateKeyError:
        return False
    return True


async def release_generated_reference(value: str, reference_type: str) -> None:
    normalized = normalize_reference(value)
    await manual_references_collection.delete_one(
        {
            "normalized": normalized,
            "reference_type": reference_type,
            "owner": {"$regex": f"^generated:{re.escape(reference_type)}:"},
        }
    )
