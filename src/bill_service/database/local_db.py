"""
LOCAL database — admin-controlled replica.

Written ONLY when an authorized admin explicitly requests a local
synchronization (MAIN -> LOCAL). It is never a normal read source
and never writes back to MAIN.
"""
from motor.motor_asyncio import AsyncIOMotorCollection

from .connection_manager import ROLE_LOCAL, get_database

_db = get_database(ROLE_LOCAL)

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

# Mirror sync metadata so LOCAL sync can compare versions locally.
sync_status_collection: AsyncIOMotorCollection = _db.get_collection("sync_status")
