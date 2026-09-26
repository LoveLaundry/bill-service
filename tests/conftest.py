"""Shared harness for DB-backed business-logic tests.

No real MongoDB instance is required. A ``mongomock-motor`` database stands
in for every MAIN / SECONDARY / LOCAL collection, patched onto the database
modules BEFORE the routers/services are (re)loaded, so their module-level
bindings (``from ..database.main_db import X``) resolve to the mock.

``asyncio_mode = auto`` (pytest.ini) lets tests and fixtures be plain
``async def`` with no decorators.
"""
import importlib

import pytest
from mongomock_motor import AsyncMongoMockClient

import bill_service.database.local_db as local_db
import bill_service.database.main_db as main_db
import bill_service.database.secondary_db as secondary_db

COLLECTION_NAMES = [
    "adjustments_collection",
    "balance_adjustments_collection",
    "audit_collection",
    "bills_collection",
    "deliveries_collection",
    "dispatch_jobs_collection",
    "gatepasses_collection",
    "idempotency_collection",
    "legacy_invoices_collection",
    "linen_events_collection",
    "linens_collection",
    "loyalty_collection",
    "payments_collection",
    "returns_collection",
    "shop_bills_collection",
    "sync_logs_collection",
    "sync_queue_collection",
    "sync_status_collection",
]

# Modules whose module-level collection bindings must point at the mock.
DEPENDENT_MODULES = [
    "bill_service.gatepass_balance",
    "bill_service.repositories.entity_registry",
    "bill_service.repositories.main_repository",
    "bill_service.repositories.secondary_repository",
    "bill_service.services.transaction_events",
    "bill_service.services.verification_service",
    "bill_service.services.synchronization_service",
    "bill_service.services.idempotency",
    "bill_service.services.bill_sync",
    "bill_service.routers.bills",
    "bill_service.routers.gatepasses",
    "bill_service.routers.deliveries",
    "bill_service.routers.returns",
    "bill_service.routers.adjustments",
    "bill_service.routers.balance_adjustments",
    "bill_service.routers.dashboard",
    "bill_service.routers.reconciliation",
]


@pytest.fixture(scope="session", autouse=True)
def mocked_db():
    """Swap every database collection for a mongomock-motor stand-in."""
    client = AsyncMongoMockClient()
    db = client["mock_main"]

    for name in COLLECTION_NAMES:
        coll = db.get_collection(name)
        setattr(main_db, name, coll)
        setattr(secondary_db, name, coll)
        setattr(local_db, name, coll)

    for module_name in DEPENDENT_MODULES:
        try:
            importlib.import_module(module_name)
            importlib.reload(importlib.import_module(module_name))
        except Exception:  # pragma: no cover - defensive against import noise
            pass

    _assert_no_live_clients(db)
    return db


def _assert_no_live_clients(mock_db) -> None:
    """Fail loudly if any synced module still points at a real MongoDB.

    Modules that cache collection handles at import time (``from ..database
    .main_db import sync_queue_collection``) escape the patching above unless
    they are also reloaded. A stale handle points at whatever ``.env`` names,
    which in development is the PRODUCTION Atlas cluster -- so a stray
    ``drain_due_jobs()`` in a test would mutate live data. Assert the handle a
    module actually holds belongs to the mongomock database.
    """
    import bill_service.services.synchronization_service as sync_service

    mock_prefix = f"{mock_db.name}."
    leaked = sorted(
        name
        for name in ("sync_queue_collection", "sync_logs_collection")
        if not str(getattr(sync_service, name, None).full_name).startswith(mock_prefix)
    )
    if leaked:
        details = ", ".join(
            f"{n} -> {getattr(sync_service, n).full_name}" for n in leaked
        )
        pytest.fail(
            "Refusing to run: synchronization_service still holds live "
            f"MongoDB collection(s) [{details}]. Tests would mutate a real "
            "database. Add the owning module to DEPENDENT_MODULES."
        )


@pytest.fixture(autouse=True)
async def clean_db(mocked_db):
    """Isolate tests: every mocked collection starts empty."""
    for name in COLLECTION_NAMES:
        await mocked_db[name].delete_many({})
    yield


def auth_user(name: str = "Alice", role: str = "ADMIN") -> dict:
    """A realistic current-user dict as FastAPI dependencies receive."""
    return {"auth_id": f"u-{name}", "user_name": name, "role": role}