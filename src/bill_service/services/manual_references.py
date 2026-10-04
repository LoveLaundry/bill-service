"""Reserve human-facing references across monthly operations and generated documents."""
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


REFERENCE_FIELDS = ("bill_number", "gate_pass_number", "alrs_number")


def normalize_reference(value: str) -> str:
    return " ".join(value.split()).casefold()


async def _existing_document_reference(value: str) -> bool:
    parts = value.split()
    pattern = r"^\s*" + r"\s+".join(re.escape(part) for part in parts) + r"\s*$"
    checks = (
        (gatepasses_collection, "gate_pass_number"),
        (gatepasses_collection, "manual_bill_number"),
        (gatepasses_collection, "manual_gate_pass_number"),
        (gatepasses_collection, "alrs_number"),
        (bills_collection, "manual_bill_number"),
        (bills_collection, "manual_gate_pass_number"),
        (bills_collection, "alrs_number"),
        (shop_bills_collection, "bill_number"),
        (legacy_invoices_collection, "invoice_number"),
    )
    for collection, field in checks:
        if await collection.find_one({field: {"$regex": pattern, "$options": "i"}}):
            return True
    return False


async def reserve_references(owner: str, references: dict[str, str | None]) -> None:
    values: dict[str, str] = {}
    for field in REFERENCE_FIELDS:
        value = (references.get(field) or "").strip()
        if value:
            normalized = normalize_reference(value)
            previous = values.get(normalized)
            if previous is not None:
                raise ValueError(f"{field.replace('_', ' ').title()} conflicts with {previous.replace('_', ' ')}.")
            values[normalized] = field

    for normalized, field in values.items():
        reservation = await manual_references_collection.find_one({"normalized": normalized})
        if reservation and reservation.get("owner") != owner:
            raise ValueError(
                f"'{references[field]}' is already used as a {reservation.get('reference_type', 'document reference')}."
            )
        if not reservation and await _existing_document_reference(references[field]):
            raise ValueError(f"'{references[field]}' is already used by another document.")

    existing_owned = {
        entry["normalized"]
        async for entry in manual_references_collection.find({"owner": owner})
    }
    try:
        for normalized, field in values.items():
            await manual_references_collection.update_one(
                {"normalized": normalized, "owner": owner},
                {
                    "$set": {
                        "normalized": normalized,
                        "value": references[field].strip(),
                        "owner": owner,
                        "reference_type": field,
                    }
                },
                upsert=True,
            )
    except DuplicateKeyError as exc:
        newly_reserved = [
            normalized
            for normalized in values
            if normalized not in existing_owned
        ]
        if newly_reserved:
            await manual_references_collection.delete_many(
                {"owner": owner, "normalized": {"$in": newly_reserved}}
            )
        reservation = await manual_references_collection.find_one({"normalized": normalized})
        used_by = reservation.get("reference_type", "document reference") if reservation else "another document"
        raise ValueError(f"'{references[field]}' is already used as a {used_by}.") from exc


async def release_references(owner: str, references: dict[str, str | None]) -> None:
    normalized = [
        normalize_reference(value.strip())
        for value in references.values()
        if value and value.strip()
    ]
    if normalized:
        await manual_references_collection.delete_many(
            {"owner": owner, "normalized": {"$in": normalized}}
        )


async def reserve_generated_reference(value: str, reference_type: str) -> bool:
    """Atomically claim an auto-generated value; False means choose another."""
    normalized = normalize_reference(value)
    owner = f"generated:{reference_type}:{uuid4().hex}"
    if await _existing_document_reference(value):
        return False
    try:
        await manual_references_collection.insert_one(
            {
                "normalized": normalized,
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
    if await _existing_document_reference(value):
        return False
    try:
        await manual_references_collection.insert_one(
            {
                "normalized": normalized,
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
