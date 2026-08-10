from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .database import ensure_indexes
from .routers.bills import router as bills_router
from .routers.gatepasses import router as gatepasses_router
from .routers.deliveries import router as deliveries_router
from .routers.dashboard import router as dashboard_router

app = FastAPI(title="Bills, Receiving & Deliveries Service", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(bills_router)
app.include_router(gatepasses_router)
app.include_router(deliveries_router)
app.include_router(dashboard_router)


@app.on_event("startup")
async def startup_event():
    await ensure_indexes()


@app.get("/health")
async def health():
    return {"status": "ok"}