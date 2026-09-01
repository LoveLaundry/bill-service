import asyncio
import traceback

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .database.connection_manager import close_all
from .database.main_db import ensure_indexes
from .routers.admin_database import router as admin_database_router
from .routers.bills import router as bills_router
from .routers.deliveries import router as deliveries_router
from .routers.dispatch import router as dispatch_router
from .routers.loyalty import router as loyalty_router
from .routers.dashboard import router as dashboard_router
from .routers.gatepasses import router as gatepasses_router
from .routers.linens import router as linens_router
from .routers.returns import router as returns_router
from .services import synchronization_service


import logging
import os
import sentry_sdk

# Configure logging first
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# CORS configuration
try:
    ALLOWED_ORIGINS_ENV = os.getenv("ALLOWED_ORIGINS", "")
    if ALLOWED_ORIGINS_ENV:
        ALLOWED_ORIGINS = [origin.strip() for origin in ALLOWED_ORIGINS_ENV.split(",") if origin.strip()]
        ALLOW_CREDENTIALS = True
    else:
        # Production fallback — restrict to known domains
        ALLOWED_ORIGINS = [
            "https://lovelaundry-manager.vercel.app",
            "https://public.lovelaundry.lk",
            "http://localhost:5173",
            "http://localhost:3000",
        ]
        ALLOW_CREDENTIALS = True

    logger.info(f"CORS configured with origins: {ALLOWED_ORIGINS}, credentials: {ALLOW_CREDENTIALS}")
except Exception as e:
    logger.warning(f"CORS configuration failed, using defaults: {e}")
    ALLOWED_ORIGINS = ["http://localhost:5173"]
    ALLOW_CREDENTIALS = True

# Vercel sets VERCEL=1; background workers are unreliable in serverless.
ON_VERCEL = os.getenv("VERCEL") == "1"

SENTRY_DSN = os.getenv("SENTRY_DSN")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        traces_sample_rate=0.1,
    )

app = FastAPI(title="Bills, Receiving & Deliveries Service", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    origin = request.headers.get("origin", "")
    cors_headers = {}
    if origin in ALLOWED_ORIGINS:
        cors_headers = {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods": "*",
            "Access-Control-Allow-Headers": "*",
        }

    if isinstance(exc, HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=cors_headers,
        )

    logger.error(f"Unhandled exception: {exc}\n{traceback.format_exc()}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error. Please try again later."},
        headers=cors_headers,
    )

app.include_router(bills_router)
app.include_router(gatepasses_router)
app.include_router(deliveries_router)
app.include_router(dispatch_router)
app.include_router(loyalty_router)
app.include_router(dashboard_router)
app.include_router(linens_router)
app.include_router(returns_router)
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
