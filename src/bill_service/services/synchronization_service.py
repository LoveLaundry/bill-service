"""
Synchronization service - replicates MAIN changes to the SECONDARY
database using a durable queue, and verifies each replication.

Design:
    - Writes land in MAIN and enqueue a job in MAIN.sync_queue.
    - A background worker claims due jobs, copies the raw encrypted
      document from MAIN to SECONDARY, then verifies versions.
    - Failed jobs are retried with exponential backoff up to a limit,
      then marked FAILED permanently (still diagnosable via sync_logs).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..config import settings
from ..database.main_db import sync_logs_collection, sync_queue_collection
from ..repositories.entity_registry import effective_version, get_main_collection
from ..repositories.secondary_repository import upsert_document
from . import verification_service

logger = logging.getLogger(__name__)

SYNC_OPERATION = "MAIN_TO_SECONDARY"
MAX_ATTEMPTS = 5


async def enqueue(entity: str, record_id: Any, version: int) -> None:
    """Durable enqueue - upserts a PENDING job in MAIN.sync_queue."""
    now = datetime.now(timezone.utc)
    await sync_queue_collection.update_one(
        {"entity": entity, "record_id": str(record_id)},
        {
            "$set": {
                "entity": entity,
                "record_id": str(record_id),
                "version": version,
                "status": "PENDING",
                "attempts": 0,
                "next_attempt_at": now,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


async def write_sync_log(
    operation: str,
    entity: Optional[str],
    record_id: Optional[str],
    status: str,
    started_at: datetime,
    completed_at: Optional[datetime] = None,
    error: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    entry = {
        "operation": operation,
        "entity": entity,
        "record_id": record_id,
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "error": error,
        "created_at": datetime.now(timezone.utc),
    }
    if extra:
        entry.update(extra)
    await sync_logs_collection.insert_one(entry)


async def process_one(job: dict) -> str:
    """
    Replicate a single queued change from MAIN to SECONDARY and verify it.

    Returns "VERIFIED", "PENDING" (version mismatch), or raises so the
    caller can retry.
    """
    entity = job["entity"]
    record_id = job["record_id"]

    # 1. Read raw encrypted doc from MAIN
    main_collection = get_main_collection(entity)
    main_doc = await main_collection.find_one({"_id": record_id})
    if main_doc is None:
        raise RuntimeError(f"Record {entity}/{record_id} no longer exists in MAIN")

    # 2. Copy to SECONDARY (raw copy preserves decryption metadata)
    await upsert_document(entity, main_doc)

    # 3. Verify by version comparison
    main_version = effective_version(main_doc)
    return await verification_service.verify_against_secondary(
        entity, record_id, main_version
    )


async def attempt_job(job: dict) -> None:
    """Run/retry one job with backoff. Marks FAILED when attempts run out."""
    job_id = job.get("_id")
    entity = job["entity"]
    record_id = job["record_id"]
    attempts = int(job.get("attempts") or 0) + 1
    started_at = datetime.now(timezone.utc)

    await verification_service.mark_syncing(entity, record_id, job.get("version") or 0)

    try:
        result_status = await process_one(job)

        await sync_queue_collection.delete_one({"_id": job_id})
        await write_sync_log(
            operation=SYNC_OPERATION,
            entity=entity,
            record_id=record_id,
            status="SUCCESS",
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
            error=None if result_status == "VERIFIED" else "Verified with version mismatch",
            extra={"result_status": result_status},
        )
    except Exception as exc:
        logger.warning("Sync failed for %s/%s (attempt %d): %s", entity, record_id, attempts, exc)

        if attempts >= MAX_ATTEMPTS:
            await sync_queue_collection.update_one(
                {"_id": job_id},
                {"$set": {"status": "FAILED", "attempts": attempts, "updated_at": datetime.now(timezone.utc)}},
            )
            await verification_service.mark_failed(
                entity, record_id, job.get("version") or 0, str(exc)
            )
            await write_sync_log(
                operation=SYNC_OPERATION,
                entity=entity,
                record_id=record_id,
                status="FAILED",
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                error=str(exc),
                extra={"attempts": attempts},
            )
        else:
            backoff_seconds = settings.sync_retry_base_delay_seconds * (2 ** (attempts - 1))
            await sync_queue_collection.update_one(
                {"_id": job_id},
                {
                    "$set": {
                        "status": "PENDING",
                        "attempts": attempts,
                        "next_attempt_at": datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds),
                        "error": str(exc),
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )


async def drain_due_jobs(limit: int = 50) -> int:
    """Process all jobs whose next_attempt_at has passed. Returns count processed."""
    cursor = (
        sync_queue_collection.find(
            {"status": "PENDING", "next_attempt_at": {"$lte": datetime.now(timezone.utc)}}
        )
        .sort("next_attempt_at", 1)
        .limit(limit)
    )

    processed = 0
    async for job in cursor:
        await attempt_job(job)
        processed += 1
    return processed


async def run_worker(stop_event: Optional[asyncio.Event] = None) -> None:
    """
    Background worker loop. Polls the durable queue and drains due jobs.
    Runs until stop_event is set (used by tests / shutdown).
    """
    poll_seconds = settings.sync_worker_poll_seconds
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            await drain_due_jobs()
        except Exception as exc:
            logger.exception("Sync worker drain failed: %s", exc)
        await asyncio.sleep(poll_seconds)
