"""
Entity registry — maps business entity names to their collections in each
database role. This is the single place that knows which collections
participate in synchronization.
"""
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorCollection

from ..database import local_db, main_db, secondary_db

# entity name -> (MAIN collection, SECONDARY collection, LOCAL collection)
COLLECTION_MAP: dict[str, tuple[AsyncIOMotorCollection, AsyncIOMotorCollection, AsyncIOMotorCollection]] = {
    "bill": (main_db.bills_collection, secondary_db.bills_collection, local_db.bills_collection),
    "gatepass": (main_db.gatepasses_collection, secondary_db.gatepasses_collection, local_db.gatepasses_collection),
    "delivery": (main_db.deliveries_collection, secondary_db.deliveries_collection, local_db.deliveries_collection),
    "dispatch": (main_db.dispatch_jobs_collection, secondary_db.dispatch_jobs_collection, local_db.dispatch_jobs_collection),
    "payment": (main_db.payments_collection, secondary_db.payments_collection, local_db.payments_collection),
    "audit_log": (main_db.audit_collection, secondary_db.audit_collection, local_db.audit_collection),
    "linen": (main_db.linens_collection, secondary_db.linens_collection, local_db.linens_collection),
    "shop_bill": (main_db.shop_bills_collection, secondary_db.shop_bills_collection, local_db.shop_bills_collection),
    "bill_template": (main_db.bill_templates_collection, secondary_db.bill_templates_collection, local_db.bill_templates_collection),
    "legacy_invoice": (main_db.legacy_invoices_collection, secondary_db.legacy_invoices_collection, local_db.legacy_invoices_collection),
}


def get_collections(entity: str) -> tuple[AsyncIOMotorCollection, AsyncIOMotorCollection, AsyncIOMotorCollection]:
    """Return (main, secondary, local) collections for an entity name."""
    if entity not in COLLECTION_MAP:
        raise ValueError(f"Unknown sync entity: {entity!r}")
    return COLLECTION_MAP[entity]


def get_main_collection(entity: str) -> AsyncIOMotorCollection:
    return get_collections(entity)[0]


def get_secondary_collection(entity: str) -> AsyncIOMotorCollection:
    return get_collections(entity)[1]


def get_local_collection(entity: str) -> AsyncIOMotorCollection:
    return get_collections(entity)[2]


def all_entities() -> list[str]:
    """Return every syncable entity name."""
    return list(COLLECTION_MAP.keys())


def effective_version(doc: dict) -> int:
    """Return the sync version of a stored document, defaulting legacy docs to 1."""
    return int(doc.get("sync_version") or 1)


def to_record_id(value):
    """Coerce a stored queue `record_id` back into the type used by `_id`.

    `sync_queue` persists `record_id` as a string, but MongoDB `_id` values are
    `ObjectId`s and are compared by exact BSON type. Re-coercing here keeps the
    sync worker able to find the document it is replicating.
    """
    if isinstance(value, ObjectId):
        return value
    if isinstance(value, str) and ObjectId.is_valid(value):
        return ObjectId(value)
    return value
