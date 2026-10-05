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
from uuid import uuid4

from pymongo import ReturnDocument
from ..config import settings
from ..database.main_db import sync_logs_collection, sync_queue_collection
from ..repositories.entity_registry import effective_version, get_main_collection, to_record_id
from ..repositories.main_repository import (
    OPERATION_DELETE,
    OPERATION_UPSERT,
    enqueue_sync as enqueue_repository_sync,
)
from ..repositories.secondary_repository import delete_document, upsert_document
from . import verification_service

logger = logging.getLogger(__name__)

SYNC_OPERATION = "MAIN_TO_SECONDARY"
MAX_ATTEMPTS = 5
SYNC_CLAIM_LEASE_SECONDS = 300

# Returned by process_one for a propagated deletion. There is no replica
# document left to version-compare, so it is terminal rather than PENDING.
STATUS_DELETED = "DELETED"


async def enqueue(entity: str, record_id: Any, version: int) -> None:
    """Durable enqueue - upserts a PENDING job in MAIN.sync_queue."""
    await enqueue_repository_sync(entity, record_id, version)


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
    operation = job.get("operation") or OPERATION_UPSERT
    version = int(job.get("version") or 0)

    if operation == OPERATION_DELETE:
        # The record is intentionally gone from MAIN, so there is nothing to
        # compare versions against. Propagate the deletion and record it.
        await delete_document(entity, record_id)
        await verification_service.mark_verified(entity, record_id, version)
        return STATUS_DELETED

    # 1. Read raw encrypted doc from MAIN
    main_collection = get_main_collection(entity)
    main_doc = await main_collection.find_one({"_id": to_record_id(record_id)})
    if main_doc is None:
        raise RuntimeError(f"Record {entity}/{record_id} no longer exists in MAIN")

    # 2. Copy to SECONDARY (raw copy preserves decryption metadata)
    await upsert_document(entity, main_doc)

    # 3. Verify by version comparison
    main_version = effective_version(main_doc)
    return await verification_service.verify_against_secondary(
        entity, record_id, main_version
    )


def _claim_filter(job: dict) -> dict:
    """Match only the exact queued version/operation owned by this claim."""
    query = {
        "_id": job["_id"],
        "status": "SYNCING",
        "claim_token": job["claim_token"],
        "version": job.get("version"),
    }
    operation = job.get("operation")
    if operation is None:
        query["$or"] = [
            {"operation": {"$exists": False}},
            {"operation": None},
        ]
    else:
        query["operation"] = operation
    return query


def _claim_owner_filter(job: dict) -> dict:
    """Match a live lease without restricting it to the claimed data version."""
    return {
        "_id": job["_id"],
        "status": "SYNCING",
        "claim_token": job["claim_token"],
    }


async def _release_claim(job: dict) -> None:
    """Make a superseded job available again without overwriting its payload."""
    await sync_queue_collection.update_one(
        _claim_owner_filter(job),
        {
            "$set": {"status": "PENDING", "updated_at": datetime.now(timezone.utc)},
            "$unset": {"claim_token": "", "lease_until": ""},
        },
    )


async def _renew_claim(job: dict) -> None:
    """Keep long-running async copies from expiring their exclusive claim."""
    while True:
        await asyncio.sleep(SYNC_CLAIM_LEASE_SECONDS / 3)
        result = await sync_queue_collection.update_one(
            _claim_owner_filter(job),
            {
                "$set": {
                    "lease_until": datetime.now(timezone.utc)
                    + timedelta(seconds=SYNC_CLAIM_LEASE_SECONDS),
                }
            },
        )
        if not result.matched_count:
            return


async def attempt_job(job: dict) -> None:
    """Run/retry one job with backoff. Marks FAILED when attempts run out."""
    entity = job["entity"]
    record_id = job["record_id"]
    attempts = int(job.get("attempts") or 0) + 1
    started_at = datetime.now(timezone.utc)

    heartbeat = asyncio.create_task(_renew_claim(job))
    try:
        await verification_service.mark_syncing(entity, record_id, job.get("version") or 0)
        result_status = await process_one(job)
        acknowledged = await sync_queue_collection.delete_one(_claim_filter(job))
        if not acknowledged.deleted_count:
            # An enqueue replaced this version/operation during processing.
            # Retain that newer work instead of deleting it with this ack.
            await _release_claim(job)
        await write_sync_log(
            operation=SYNC_OPERATION,
            entity=entity,
            record_id=record_id,
            status="SUCCESS" if acknowledged.deleted_count else "SUPERSEDED",
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
            error=None if result_status == "VERIFIED" else "Verified with version mismatch",
            extra={
                "result_status": result_status,
                "superseded": not bool(acknowledged.deleted_count),
            },
        )
    except Exception as exc:
        logger.warning("Sync failed for %s/%s (attempt %d): %s", entity, record_id, attempts, exc)

        retry_at = datetime.now(timezone.utc)
        if attempts >= MAX_ATTEMPTS:
            status = "FAILED"
        else:
            backoff_seconds = settings.sync_retry_base_delay_seconds * (2 ** (attempts - 1))
            retry_at += timedelta(seconds=backoff_seconds)
            status = "PENDING"

        updated = await sync_queue_collection.update_one(
            _claim_filter(job),
            {
                "$set": {
                    "status": status,
                    "attempts": attempts,
                    "next_attempt_at": retry_at,
                    "error": str(exc),
                    "updated_at": datetime.now(timezone.utc),
                },
                "$unset": {"claim_token": "", "lease_until": ""},
            },
        )
        if updated.matched_count:
            if status == "FAILED":
                await verification_service.mark_failed(
                    entity, record_id, job.get("version") or 0, str(exc)
                )
            await write_sync_log(
                operation=SYNC_OPERATION,
                entity=entity,
                record_id=record_id,
                status=status,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                error=str(exc),
                extra={"attempts": attempts},
            )
        else:
            # A newer enqueue changed the version/operation. Do not carry the
            # stale attempt count onto that new work.
            await _release_claim(job)
            await write_sync_log(
                operation=SYNC_OPERATION,
                entity=entity,
                record_id=record_id,
                status="SUPERSEDED",
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                error=str(exc),
                extra={"attempts": attempts},
            )
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass


async def drain_due_jobs(limit: int = 50) -> int:
    """Process due queue rows once per drain. Returns the number of attempts."""
    processed = 0
    processed_ids: set[Any] = set()
    for _ in range(max(0, limit)):
        now = datetime.now(timezone.utc)
        claim_token = str(uuid4())
        job = await sync_queue_collection.find_one_and_update(
            {
                "_id": {"$nin": list(processed_ids)},
                "$or": [
                    {
                        "status": "PENDING",
                        "next_attempt_at": {"$lte": now},
                    },
                    {
                        "status": "SYNCING",
                        "lease_until": {"$lte": now},
                    },
                ]
            },
            {
                "$set": {
                    "status": "SYNCING",
                    "claim_token": claim_token,
                    "lease_until": now + timedelta(seconds=SYNC_CLAIM_LEASE_SECONDS),
                    "updated_at": now,
                }
            },
            sort=[("next_attempt_at", 1)],
            return_document=ReturnDocument.AFTER,
        )
        if job is None:
            break
        processed_ids.add(job["_id"])
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
