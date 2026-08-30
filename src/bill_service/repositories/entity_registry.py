"""
Entity registry — maps business entity names to their collections in each
database role. This is the single place that knows which collections
participate in synchronization.
"""
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
