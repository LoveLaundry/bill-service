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
    "bill_templates_collection",
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
    "bill_service.repositories.entity_registry",
    "bill_service.repositories.main_repository",
    "bill_service.repositories.secondary_repository",
    "bill_service.services.transaction_events",
    "bill_service.services.verification_service",
    "bill_service.services.synchronization_service",
    "bill_service.services.idempotency",
    "bill_service.services.bill_sync",
    "bill_service.services.balance_engine",
    "bill_service.services.operations_context",
    "bill_service.routers.bills",
    "bill_service.routers.gatepasses",
    "bill_service.routers.deliveries",
    "bill_service.routers.returns",
    "bill_service.routers.adjustments",
    "bill_service.routers.day_close",
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

    # Import every bill_service module FIRST, then reload the ones that cache a
    # collection handle. Reloading before the module has ever been imported is a
    # no-op for our purposes, and a module already in sys.modules (imported by a
    # test module at collection time) would otherwise keep the LIVE handle it
    # captured before patching.
    for module_name in DEPENDENT_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception:  # pragma: no cover - defensive against import noise
            pass
    for module_name in DEPENDENT_MODULES:
        try:
            importlib.reload(importlib.import_module(module_name))
        except Exception:  # pragma: no cover - defensive against import noise
            pass

    _assert_no_live_clients(db)
    return db


def _assert_no_live_clients(mock_db) -> None:
    """Fail loudly if ANY imported module still points at a real MongoDB.

    Modules that cache collection handles at import time
    (``from ..database.main_db import sync_queue_collection``) escape the
    patching above unless they are also reloaded. A stale handle points at
    whatever ``.env`` names, which in development is the PRODUCTION Atlas
    cluster -- so a stray ``drain_due_jobs()`` in a test would mutate live
    data.

    This checks every ``bill_service.*`` module currently in ``sys.modules``,
    not a hand-picked list, so a newly added module holding a collection
    handle is caught on the next run instead of quietly writing to
    production.
    """
    import sys

    mock_prefix = f"{mock_db.name}."

    # The database modules are where the LIVE handles originate -- they are
    # the patching target, not a consumer of it, so they are not scanned.
    db_modules = {
        "bill_service.database.main_db",
        "bill_service.database.secondary_db",
        "bill_service.database.local_db",
    }

    leaked = []
    for module_name, module in sorted(sys.modules.items()):
        if not module_name.startswith("bill_service") or module is None:
            continue
        if module_name in db_modules:
            continue
        for attr_name, value in sorted(vars(module).items()):
            if attr_name.startswith("_"):
                continue
            if not hasattr(value, "full_name"):
                continue
            # Only collection-like objects, not a Motor client/DB handle.
            if not isinstance(getattr(value, "name", None), str):
                continue
            if not str(value.full_name).startswith(mock_prefix):
                leaked.append(f"{module_name}.{attr_name} -> {value.full_name}")

    if leaked:
        details = "\n  ".join(leaked)
        pytest.fail(
            "Refusing to run: these modules still hold live MongoDB "
            f"collection(s):\n  {details}\n"
            "Tests would read from / write to a real database. Add the owning "
            "module to DEPENDENT_MODULES so it is reloaded after patching."
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