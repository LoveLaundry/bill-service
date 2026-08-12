"""
Admin-only database management endpoints.

GET  /admin/database/status         -> health + sync status of MAIN/SECONDARY/LOCAL
POST /admin/database/sync-local     -> run incremental MAIN -> LOCAL sync
POST /admin/database/sync-secondary -> drain the pending sync queue
GET  /admin/database/sync-logs      -> recent synchronization history
"""
from datetime import datetime

from fastapi import APIRouter, Depends

from ..auth_helper import require_role
from ..database.connection_manager import ping
from ..services import local_sync_service, synchronization_service

router = APIRouter(prefix="/admin/database", tags=["admin-database"])

admin_only = Depends(require_role(["ADMIN"]))


def _format_log(entry: dict) -> dict:
    """Serialize a sync_logs entry for the admin UI."""
    out = dict(entry)
    out["id"] = str(out.pop("_id", ""))
    for key in ("started_at", "completed_at", "created_at"):
        if isinstance(out.get(key), datetime):
            out[key] = out[key].isoformat()
    return out


@router.get("/status")
async def database_status(_user: dict = admin_only):
    """Report ONLINE/OFFLINE and sync status for each database role."""
    main_online = await ping("MAIN")
    secondary_online = await ping("SECONDARY")
    local_online = await ping("LOCAL")

    last_local = await local_sync_service.last_local_sync()

    secondary_sync_status = "SYNCHRONIZED"
    if not secondary_online:
        secondary_sync_status = "UNKNOWN"
    else:
        pending = await synchronization_service.sync_queue_collection.count_documents(
            {"status": "PENDING"}
        )
        failed = await synchronization_service.sync_queue_collection.count_documents(
            {"status": "FAILED"}
        )
        if failed:
            secondary_sync_status = "FAILED"
        elif pending:
            secondary_sync_status = "SYNCING"

    return {
        "main": {"status": "ONLINE" if main_online else "OFFLINE"},
        "secondary": {
            "status": "ONLINE" if secondary_online else "OFFLINE",
            "sync_status": secondary_sync_status,
        },
        "local": {
            "status": "ONLINE" if local_online else "OFFLINE",
            "last_sync": last_local["completed_at"].isoformat() if last_local and last_local.get("completed_at") else None,
            "last_sync_status": last_local.get("status") if last_local else None,
        },
    }


@router.post("/sync-local")
async def sync_local(_user: dict = admin_only):
    """Run the admin-triggered incremental MAIN -> LOCAL synchronization."""
    report = await local_sync_service.sync_local_database()
    return report


@router.post("/sync-secondary")
async def sync_secondary(_user: dict = admin_only):
    """Manually drain the pending MAIN -> SECONDARY sync queue."""
    processed = await synchronization_service.drain_due_jobs(limit=200)
    return {
        "message": "Secondary synchronization triggered",
        "jobs_processed": processed,
        "pending": await synchronization_service.sync_queue_collection.count_documents(
            {"status": "PENDING"}
        ),
        "failed": await synchronization_service.sync_queue_collection.count_documents(
            {"status": "FAILED"}
        ),
    }


@router.get("/sync-logs")
async def sync_logs(limit: int = 50, _user: dict = admin_only):
    """Return recent synchronization log entries, newest first."""
    cursor = (
        synchronization_service.sync_logs_collection.find()
        .sort("started_at", -1)
        .limit(max(1, min(limit, 200)))
    )
    logs = []
    async for entry in cursor:
        logs.append(_format_log(entry))
    return {"logs": logs}
