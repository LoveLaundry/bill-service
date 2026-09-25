"""Tests for replica delete propagation and sync-queue id coercion.

Two regressions are covered here:

1. Deletes were hard-deleted from MAIN with no sync enqueue, so SECONDARY kept
   the record forever. Deletions now enqueue an ``operation: "DELETE"`` job that
   the worker replays against the replica.

2. ``sync_queue`` persists ``record_id`` as a string, but MongoDB matches ``_id``
   by exact BSON type. The worker looked the document up with the raw string,
   so every UPSERT job failed with "no longer exists in MAIN". ``to_record_id``
   coerces the stored value back to an ObjectId.
"""
from bson import ObjectId

from bill_service.repositories.entity_registry import to_record_id
from bill_service.repositories.main_repository import (
    OPERATION_DELETE,
    OPERATION_UPSERT,
    enqueue_delete,
    enqueue_sync,
)
from bill_service.services import synchronization_service


# --- id coercion ---------------------------------------------------------

def test_to_record_id_coerces_string_to_objectid():
    oid = ObjectId()
    assert to_record_id(oid) is oid
    assert to_record_id(str(oid)) == oid
    assert type(to_record_id(str(oid))) is ObjectId


def test_to_record_id_passes_through_non_objectid_values():
    assert to_record_id("not-an-object-id") == "not-an-object-id"
    assert to_record_id(7) == 7
    assert to_record_id(None) is None


async def test_upsert_job_finds_document_by_coerced_id(mocked_db):
    """The stored string id must still resolve the live MAIN document."""
    res = await mocked_db["bills_collection"].insert_one({"client_name": "Acme"})
    await enqueue_sync("bill", res.inserted_id, 1)

    job = await mocked_db["sync_queue_collection"].find_one({})
    assert job["record_id"] == str(res.inserted_id)
    assert job["operation"] == OPERATION_UPSERT

    # process_one would previously raise here; the coercion makes it resolve.
    resolved = await mocked_db["bills_collection"].find_one(
        {"_id": to_record_id(job["record_id"])}
    )
    assert resolved is not None
    assert resolved["client_name"] == "Acme"


# --- delete propagation --------------------------------------------------

async def test_enqueue_delete_records_delete_operation(mocked_db):
    res = await mocked_db["shop_bills_collection"].insert_one({"client_name": "Acme"})
    await enqueue_delete("shop_bill", res.inserted_id, 4)

    job = await mocked_db["sync_queue_collection"].find_one({})
    assert job["operation"] == OPERATION_DELETE
    assert job["entity"] == "shop_bill"
    assert job["record_id"] == str(res.inserted_id)
    assert job["version"] == 4
    assert job["status"] == "PENDING"


async def test_process_delete_job_removes_replica_and_is_terminal(mocked_db):
    """A DELETE job must not demand a version comparison - the doc is gone."""
    res = await mocked_db["shop_bills_collection"].insert_one({"client_name": "Acme"})
    await mocked_db["shop_bills_collection"].delete_one({"_id": res.inserted_id})
    await enqueue_delete("shop_bill", res.inserted_id, 2)

    job = await mocked_db["sync_queue_collection"].find_one({})
    result = await synchronization_service.process_one(job)

    assert result == synchronization_service.STATUS_DELETED
    assert await mocked_db["shop_bills_collection"].find_one(
        {"_id": to_record_id(job["record_id"])}
    ) is None


async def test_process_delete_job_is_idempotent(mocked_db):
    """Replaying a delete for an already-absent record must not raise."""
    res = await mocked_db["dispatch_jobs_collection"].insert_one({"ref": "JOB-1"})
    await mocked_db["dispatch_jobs_collection"].delete_one({"_id": res.inserted_id})
    await enqueue_delete("dispatch", res.inserted_id, 3)

    job = await mocked_db["sync_queue_collection"].find_one({})
    assert await synchronization_service.process_one(job) == synchronization_service.STATUS_DELETED
    assert await synchronization_service.process_one(job) == synchronization_service.STATUS_DELETED


async def test_drain_processes_delete_jobs_and_clears_queue(mocked_db):
    res = await mocked_db["linens_collection"].insert_one({"code": "LIN-1"})
    await mocked_db["linens_collection"].delete_one({"_id": res.inserted_id})
    await enqueue_delete("linen", res.inserted_id, 1)

    processed = await synchronization_service.drain_due_jobs()

    assert processed == 1
    assert await mocked_db["sync_queue_collection"].count_documents({}) == 0
    assert await mocked_db["linens_collection"].count_documents({}) == 0
