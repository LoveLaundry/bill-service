"""
SECONDARY database — verification/replica database.

This database may ONLY be written by the synchronization service and
read by the verification service. It must NEVER be used as the normal
frontend data source.
"""
from motor.motor_asyncio import AsyncIOMotorCollection

from .connection_manager import ROLE_SECONDARY, get_database

_db = get_database(ROLE_SECONDARY)

bills_collection: AsyncIOMotorCollection = _db.get_collection("bills")
gatepasses_collection: AsyncIOMotorCollection = _db.get_collection("gatepasses")
deliveries_collection: AsyncIOMotorCollection = _db.get_collection("deliveries")
dispatch_jobs_collection: AsyncIOMotorCollection = _db.get_collection("dispatch_jobs")
payments_collection: AsyncIOMotorCollection = _db.get_collection("payments")
audit_collection: AsyncIOMotorCollection = _db.get_collection("audit_logs")
linens_collection: AsyncIOMotorCollection = _db.get_collection("linens")
linen_events_collection: AsyncIOMotorCollection = _db.get_collection("linen_events")
shop_bills_collection: AsyncIOMotorCollection = _db.get_collection("shop_bills")
bill_templates_collection: AsyncIOMotorCollection = _db.get_collection("bill_templates")

# Sync metadata is mirrored too, so verification can compare records
# entirely within the Secondary database.
sync_status_collection: AsyncIOMotorCollection = _db.get_collection("sync_status")
