import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .database.connection_manager import close_all
from .database.main_db import ensure_indexes
from .routers.admin_database import router as admin_database_router
from .routers.bills import router as bills_router
from .routers.deliveries import router as deliveries_router
from .routers.gatepasses import router as gatepasses_router
from .services import synchronization_service


import logging
import os
import sentry_sdk

# Configure logging first
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# CORS configuration - allow all origins for now to avoid blocking
try:
    ALLOWED_ORIGINS_ENV = os.getenv("ALLOWED_ORIGINS", "")
    if ALLOWED_ORIGINS_ENV:
        ALLOWED_ORIGINS = [origin.strip() for origin in ALLOWED_ORIGINS_ENV.split(",") if origin.strip()]
        ALLOW_CREDENTIALS = True
    else:
        # If not configured, allow all origins (development/production fallback)
        ALLOWED_ORIGINS = ["*"]
        ALLOW_CREDENTIALS = False

    logger.info(f"CORS configured with origins: {ALLOWED_ORIGINS}, credentials: {ALLOW_CREDENTIALS}")
except Exception as e:
    logger.warning(f"CORS configuration failed, using defaults: {e}")
    ALLOWED_ORIGINS = ["*"]
    ALLOW_CREDENTIALS = False

# Vercel sets VERCEL=1; background workers are unreliable in serverless.
ON_VERCEL = os.getenv("VERCEL") == "1"

SENTRY_DSN = os.getenv("SENTRY_DSN")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        traces_sample_rate=1.0,
    )

app = FastAPI(title="Bills, Receiving & Deliveries Service", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(bills_router)
app.include_router(gatepasses_router)
app.include_router(deliveries_router)
app.include_router(admin_database_router)

# Background sync worker: MAIN -> SECONDARY replication with retry/backoff.
_sync_task: asyncio.Task | None = None


@app.on_event("startup")
async def startup_event():
    try:
        await ensure_indexes()
    except Exception:
        logger.exception("Failed to ensure MongoDB indexes on startup")

    if settings.sync_enabled and not ON_VERCEL:
        global _sync_task
        _sync_task = asyncio.create_task(synchronization_service.run_worker())


@app.on_event("shutdown")
async def shutdown_event():
    global _sync_task
    if _sync_task is not None:
        _sync_task.cancel()
        try:
            await _sync_task
        except (asyncio.CancelledError, Exception):
            pass
        _sync_task = None
    await close_all()


@app.get("/health")
async def health():
    return {"status": "ok"}
