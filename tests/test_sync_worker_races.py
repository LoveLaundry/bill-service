import asyncio

from bill_service.repositories.main_repository import enqueue_delete, enqueue_sync
from bill_service.services import synchronization_service


async def test_enqueue_during_processing_survives_successful_ack(mocked_db, monkeypatch):
    started = asyncio.Event()
    continue_processing = asyncio.Event()

    async def pause_before_success(job):
        started.set()
        await continue_processing.wait()
        return "VERIFIED"

    monkeypatch.setattr(synchronization_service, "process_one", pause_before_success)
    await enqueue_sync("bill", "race-record", 1)

    worker = asyncio.create_task(synchronization_service.drain_due_jobs())
    await asyncio.wait_for(started.wait(), timeout=2)

    # Replace the claimed UPSERT with a newer DELETE while process_one is live.
    await enqueue_delete("bill", "race-record", 2)
    continue_processing.set()
    assert await worker == 1

    queued = await mocked_db["sync_queue_collection"].find_one({})
    assert queued["version"] == 2
    assert queued["operation"] == "DELETE"
    assert queued["status"] == "PENDING"
    assert queued["attempts"] == 0

    # The retained work can be claimed and acknowledged normally.
    monkeypatch.setattr(
        synchronization_service,
        "process_one",
        lambda job: _async_status(synchronization_service.STATUS_DELETED),
    )
    assert await synchronization_service.drain_due_jobs() == 1
    assert await mocked_db["sync_queue_collection"].count_documents({}) == 0


async def test_enqueue_during_failed_attempt_does_not_inherit_attempt_count(
    mocked_db, monkeypatch
):
    started = asyncio.Event()
    continue_processing = asyncio.Event()

    async def pause_before_failure(job):
        started.set()
        await continue_processing.wait()
        raise RuntimeError("obsolete operation failed")

    monkeypatch.setattr(synchronization_service, "process_one", pause_before_failure)
    await enqueue_sync("bill", "race-record", 1)

    worker = asyncio.create_task(synchronization_service.drain_due_jobs())
    await asyncio.wait_for(started.wait(), timeout=2)
    await enqueue_sync("bill", "race-record", 2)
    continue_processing.set()
    assert await worker == 1

    queued = await mocked_db["sync_queue_collection"].find_one({})
    assert queued["version"] == 2
    assert queued["status"] == "PENDING"
    assert queued["attempts"] == 0
    assert "claim_token" not in queued
    assert "lease_until" not in queued


async def test_competing_workers_claim_a_pending_job_only_once(mocked_db, monkeypatch):
    started = asyncio.Event()
    continue_processing = asyncio.Event()
    calls = 0

    async def pause_before_success(job):
        nonlocal calls
        calls += 1
        started.set()
        await continue_processing.wait()
        return "VERIFIED"

    monkeypatch.setattr(synchronization_service, "process_one", pause_before_success)
    await enqueue_sync("bill", "single-claim", 1)

    first_worker = asyncio.create_task(synchronization_service.drain_due_jobs())
    await asyncio.wait_for(started.wait(), timeout=2)
    assert await synchronization_service.drain_due_jobs() == 0
    assert calls == 1

    continue_processing.set()
    assert await first_worker == 1
    assert await mocked_db["sync_queue_collection"].count_documents({}) == 0


async def _async_status(status):
    return status
