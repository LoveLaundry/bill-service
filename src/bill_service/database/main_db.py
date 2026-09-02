"""
MAIN database — production source of truth.

All normal application reads and writes MUST go through these
collections. Never read or write the Secondary/Local databases
from business logic.
"""
from motor.motor_asyncio import AsyncIOMotorCollection

from .connection_manager import ROLE_MAIN, get_database

_db = get_database(ROLE_MAIN)

bills_collection: AsyncIOMotorCollection = _db.get_collection("bills")
gatepasses_collection: AsyncIOMotorCollection = _db.get_collection("gatepasses")
deliveries_collection: AsyncIOMotorCollection = _db.get_collection("deliveries")
dispatch_jobs_collection: AsyncIOMotorCollection = _db.get_collection("dispatch_jobs")
payments_collection: AsyncIOMotorCollection = _db.get_collection("payments")
loyalty_collection: AsyncIOMotorCollection = _db.get_collection("loyalty_accounts")
audit_collection: AsyncIOMotorCollection = _db.get_collection("audit_logs")
linens_collection: AsyncIOMotorCollection = _db.get_collection("linens")
linen_events_collection: AsyncIOMotorCollection = _db.get_collection("linen_events")
returns_collection: AsyncIOMotorCollection = _db.get_collection("returns")
shop_bills_collection: AsyncIOMotorCollection = _db.get_collection("shop_bills")

# Sync infrastructure collections live alongside business data in MAIN.
sync_status_collection: AsyncIOMotorCollection = _db.get_collection("sync_status")
sync_queue_collection: AsyncIOMotorCollection = _db.get_collection("sync_queue")
sync_logs_collection: AsyncIOMotorCollection = _db.get_collection("sync_logs")


async def ensure_indexes():
    """Create all required indexes on the MAIN database."""
    # Bills indexes
    await bills_collection.create_index("created_at")
    await bills_collection.create_index("client_name_search")
    await bills_collection.create_index("quotation_id")

    # Gatepasses indexes
    await gatepasses_collection.create_index("gate_pass_number", unique=True)
    await gatepasses_collection.create_index("client_name_search")
    await gatepasses_collection.create_index("status")
    await gatepasses_collection.create_index("created_at")

    # Deliveries indexes
    await deliveries_collection.create_index("gate_pass_id")
    await deliveries_collection.create_index("client_name_search")
    await deliveries_collection.create_index("created_at")

    # Dispatch jobs indexes
    await dispatch_jobs_collection.create_index("client_name_search")
    await dispatch_jobs_collection.create_index("status")
    await dispatch_jobs_collection.create_index("assigned_to")
    await dispatch_jobs_collection.create_index("scheduled_at")

    # Payments indexes
    await payments_collection.create_index("bill_id")
    await payments_collection.create_index("client_name_search")
    await payments_collection.create_index("created_at")

    # Loyalty indexes
    await loyalty_collection.create_index("client_name_search", unique=True)

    # Audit logs indexes
    await audit_collection.create_index("timestamp")
    await audit_collection.create_index("user_id")
    await audit_collection.create_index("entity_id")

    # Linens indexes
    await linens_collection.create_index("linen_id", unique=True)
    await linens_collection.create_index("category")
    await linens_collection.create_index("status")
    await linens_collection.create_index("client_name")
    await linens_collection.create_index("condition")
    await linens_collection.create_index("created_at")
    await linens_collection.create_index("last_scanned_date")

    # Linen events indexes
    await linen_events_collection.create_index("linen_id")
    await linen_events_collection.create_index("timestamp")
    await linen_events_collection.create_index([("linen_id", 1), ("timestamp", -1)])

    # Returns indexes
    await returns_collection.create_index("return_id", unique=True)
    await returns_collection.create_index("gate_pass_id")
    await returns_collection.create_index("client_name_search")
    await returns_collection.create_index("status")
    await returns_collection.create_index("created_at")

    # Shop Bills indexes
    await shop_bills_collection.create_index("bill_number", unique=True)
    await shop_bills_collection.create_index("client_name_search")
    await shop_bills_collection.create_index("status")
    await shop_bills_collection.create_index("payment_status")
    await shop_bills_collection.create_index("created_at")

    # Sync infrastructure indexes
    await sync_status_collection.create_index([("entity", 1), ("record_id", 1)], unique=True)
    await sync_queue_collection.create_index([("status", 1), ("next_attempt_at", 1)])
    await sync_queue_collection.create_index([("entity", 1), ("record_id", 1)], unique=True)
    await sync_logs_collection.create_index([("operation", 1), ("started_at", -1)])
