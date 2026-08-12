"""
Local sync service - admin-triggered incremental synchronization from the
MAIN database into the LOCAL database.

Direction is strictly MAIN -> LOCAL. Local never overwrites Main.
The diff is computed on `sync_version` so documents that already match
are skipped.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..database.main_db import sync_logs_collection
from ..repositories.entity_registry import all_entities, effective_version, get_local_collection, get_main_collection
from ..services import synchronization_service

SYNC_OPERATION = "MAIN_TO_LOCAL"


async def build_diff_plan(entity: str) -> dict:
    """
    Compute new/changed/deleted records for one entity by comparing
    MAIN and LOCAL documents via their _id and sync_version.

    Returns {"insert": [...], "update": [...], "delete": [...], "unchanged": n}
    where insert/update entries are the raw MAIN documents.
    """
    main_coll = get_main_collection(entity)
    local_coll = get_local_collection(entity)

    inserts: list[dict] = []
    updates: list[dict] = []
    unchanged = 0

    local_versions: dict = {}
    async for doc in local_coll.find({}, {"_id": 1, "sync_version": 1}):
        local_versions[str(doc["_id"])] = effective_version(doc)

    async for main_doc in main_coll.find():
        main_id = str(main_doc["_id"])
        main_version = effective_version(main_doc)
        local_version = local_versions.get(main_id)

        if local_version is None:
            inserts.append(main_doc)
        elif local_version != main_version:
            updates.append(main_doc)
        else:
            unchanged += 1

    # Records present in LOCAL but absent in MAIN are deleted (respecting
    # soft-deletion semantics - these are hard deletions of the replica
    # copy only, never affecting MAIN).
    main_ids = set(local_versions.keys())
    async for main_doc in main_coll.find({}, {"_id": 1}):
        main_ids.discard(str(main_doc["_id"]))

    deletes = [local_versions[k] for k in main_ids]

    return {
        "insert": inserts,
        "update": updates,
        "delete": deletes,
        "unchanged": unchanged,
    }


async def sync_local_database() -> dict:
    """
    Run a full incremental MAIN -> LOCAL synchronization across all
    entities. Returns a report suitable for the admin UI.
    """
    started_at = datetime.now(timezone.utc)
    stats = {
        "records_checked": 0,
        "records_inserted": 0,
        "records_updated": 0,
        "records_deleted": 0,
        "records_unchanged": 0,
        "errors": 0,
    }
    entity_stats: list[dict] = []

    for entity in all_entities():
        try:
            plan = await build_diff_plan(entity)
            stats["records_checked"] += (
                plan["unchanged"] + len(plan["insert"]) + len(plan["update"])
            )

            # Apply inserts/updates
            local_coll = get_local_collection(entity)
            for doc in plan["insert"]:
                await local_coll.replace_one({"_id": doc["_id"]}, doc, upsert=True)
            for doc in plan["update"]:
                await local_coll.replace_one({"_id": doc["_id"]}, doc, upsert=True)

            # Deletes: remove local copies that no longer exist in MAIN
            main_ids = set()
            main_coll = get_main_collection(entity)
            async for main_doc in main_coll.find({}, {"_id": 1}):
                main_ids.add(str(main_doc["_id"]))

            local_ids_to_delete = []
            async for local_doc in local_coll.find({}, {"_id": 1}):
                if str(local_doc["_id"]) not in main_ids:
                    local_ids_to_delete.append(local_doc["_id"])
            for local_id in local_ids_to_delete:
                await local_coll.delete_one({"_id": local_id})

            inserted = len(plan["insert"])
            updated = len(plan["update"])
            deleted = len(local_ids_to_delete)
            unchanged = plan["unchanged"]

            stats["records_inserted"] += inserted
            stats["records_updated"] += updated
            stats["records_deleted"] += deleted
            stats["records_unchanged"] += unchanged

            entity_stats.append(
                {
                    "entity": entity,
                    "inserted": inserted,
                    "updated": updated,
                    "deleted": deleted,
                    "unchanged": unchanged,
                }
            )
        except Exception as exc:
            stats["errors"] += 1
            entity_stats.append({"entity": entity, "error": str(exc)})

    completed_at = datetime.now(timezone.utc)
    duration_seconds = round((completed_at - started_at).total_seconds(), 3)

    await synchronization_service.write_sync_log(
        operation=SYNC_OPERATION,
        entity=None,
        record_id=None,
        status="SUCCESS" if stats["errors"] == 0 else "PARTIAL",
        started_at=started_at,
        completed_at=completed_at,
        error=None if stats["errors"] == 0 else f"{stats['errors']} entity sync error(s)",
        extra={
            "records_checked": stats["records_checked"],
            "records_inserted": stats["records_inserted"],
            "records_updated": stats["records_updated"],
            "records_deleted": stats["records_deleted"],
            "records_unchanged": stats["records_unchanged"],
            "duration_seconds": duration_seconds,
        },
    )

    return {
        "status": "SUCCESS" if stats["errors"] == 0 else "PARTIAL",
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_seconds": duration_seconds,
        "stats": stats,
        "entities": entity_stats,
    }


async def last_local_sync() -> Optional[dict]:
    """Return the most recent MAIN_TO_LOCAL sync log entry, if any."""
    return await sync_logs_collection.find_one(
        {"operation": SYNC_OPERATION}, sort=[("started_at", -1)]
    )
