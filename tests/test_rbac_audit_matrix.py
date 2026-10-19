"""RBAC + audit matrix for bill_service.

Generated data-driven matrix that asserts, for every (role, capability) pair,
whether the role is admitted by ``require_capability``; plus the
``require_role`` precedence rules, JWT verification behaviour, a structural
"every mutating route is guarded" sweep, and the audit-writer schema
contract.

Runs entirely against the mongomock patched collections from conftest.py and
the in-process ASGI app: no live MongoDB is touched.
"""
import inspect

import jwt
import pytest
from fastapi import HTTPException

from bill_service import auth_helper
from bill_service.auth_helper import (
    ROLE_CAPABILITIES,
    create_access_token,
    require_capability,
    require_role,
    verify_token,
)

# ---------------------------------------------------------------------------
# Capability matrix
# ---------------------------------------------------------------------------
ROLES = ["ADMIN", "MANAGER", "STAFF", "PLATFORM_ADMIN"]

# The complete capability universe exercised by the service.
CAPABILITIES = sorted({cap for caps in ROLE_CAPABILITIES.values() for cap in caps})

# Documented intent: which roles may perform which capability.
CAPABILITY_MATRIX = {
    "ADMIN": {c for c in CAPABILITIES},
    "MANAGER": {c for c in CAPABILITIES},
    "STAFF": {
        "gatepass:read", "gatepass:write",
        "delivery:read", "delivery:write",
        "dispatch:read", "dispatch:write",
        "loyalty:read",
        "bill:read",
        "dashboard:read", "report:read",
        "linen:read", "linen:scan",
    },
    "PLATFORM_ADMIN": {c for c in CAPABILITIES},
}


def _run_capability(user: dict, capability: str):
    dep = require_capability(capability)
    return dep(user)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("capability", CAPABILITIES)
def test_capability_matrix(role, capability):
    """Every role/capability pair resolves exactly as documented."""
    user = {"auth_id": f"u-{role}", "role": role}
    should_allow = capability in CAPABILITY_MATRIX[role]
    if should_allow:
        assert _run_capability(user, capability) is user
    else:
        with pytest.raises(HTTPException) as exc:
            _run_capability(user, capability)
        assert exc.value.status_code == 403


def test_lockstep_with_module_table():
    """The hand-written matrix must not drift from the shipped table."""
    for role, advertised in CAPABILITY_MATRIX.items():
        assert set(ROLE_CAPABILITIES[role]) == advertised, role


@pytest.mark.parametrize("role", ["", "ADMIN ", None, "SUPERUSER", "GUEST"])
def test_unknown_roles_are_denied(role):
    user = {"role": role} if role is not None else {}
    with pytest.raises(HTTPException) as exc:
        _run_capability(user, "bill:read")
    assert exc.value.status_code == 403


def test_role_is_case_normalised():
    # Roles are upper-cased before lookup, so a lower-case role resolves.
    assert _run_capability({"role": "admin"}, "bill:read")


# ---------------------------------------------------------------------------
# require_role precedence
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("allowed", [["ADMIN"], ["admin"], ["ADMIN", "MANAGER"]])
@pytest.mark.parametrize("role", ["ADMIN", "MANAGER"])
def test_require_role_allows_listed(allowed, role):
    if role.upper() not in [r.upper() for r in allowed]:
        pytest.skip("not in list")
    assert require_role(allowed)({"role": role})


@pytest.mark.parametrize("allowed", [["ADMIN"], ["ADMIN", "MANAGER"]])
def test_require_role_platform_admin_inherits_admin(allowed):
    """PLATFORM_ADMIN satisfies any ADMIN-shaped list, mirroring user-service."""
    assert require_role(allowed)({"role": "PLATFORM_ADMIN"})


@pytest.mark.parametrize("allowed", [["STAFF"], ["STAFF", "MANAGER"]])
def test_require_role_platform_admin_not_in_narrow_list(allowed):
    """A narrower list (no ADMIN) stays exact; PLATFORM_ADMIN is not implied."""
    with pytest.raises(HTTPException) as exc:
        require_role(allowed)({"role": "PLATFORM_ADMIN"})
    assert exc.value.status_code == 403


@pytest.mark.parametrize("role", ["STAFF", "MANAGER", "", "NONE"])
def test_require_role_denies_unlisted(role):
    if role.upper() == "ADMIN":
        pytest.skip("admin is allowed")
    with pytest.raises(HTTPException) as exc:
        require_role(["ADMIN"])({"role": role})
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# JWT verification
# ---------------------------------------------------------------------------
def test_roundtrip_token():
    token = create_access_token({"auth_id": "u-1", "role": "ADMIN"})
    payload = verify_token(token)
    assert payload["auth_id"] == "u-1"
    assert payload["role"] == "ADMIN"


def test_expired_token_rejected():
    expired = create_access_token({"auth_id": "u-1"}, expires_in_hours=-1)
    with pytest.raises(HTTPException) as exc:
        verify_token(expired)
    assert exc.value.status_code == 401


def test_wrong_signature_rejected():
    forged = jwt.encode({"auth_id": "u-1"}, "not-the-secret", algorithm="HS256")
    with pytest.raises(HTTPException) as exc:
        verify_token(forged)
    assert exc.value.status_code == 401


def test_garbage_token_rejected():
    with pytest.raises(HTTPException) as exc:
        verify_token("not.a.jwt")
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Structural sweep: every route declares an auth dependency
# ---------------------------------------------------------------------------
AUTH_MARKERS = ("require_capability", "require_role", "get_current_user", "security")

# Health/probe endpoints that intentionally require no token.
PUBLIC_PATHS = {"/", "/health", "/api/health", "/docs", "/openapi.json", "/redoc"}


def _all_routes():
    """Yield (path, methods, dependant) for every mutating route.

    Walks the included router objects directly rather than ``app.routes``:
    under the reload-heavy conftest the mounted ``APIRoute`` wrappers lose
    their ``dependant``, while the original router entries keep theirs.
    """
    from fastapi.routing import APIRoute

    import bill_service.main as main

    routers = [
        main.bills_router, main.gatepasses_router, main.adjustments_router,
        main.deliveries_router, main.events_router, main.dispatch_router,
        main.loyalty_router, main.dashboard_router, main.day_close_router,
        main.day_money_router, main.linens_router, main.monthly_router,
        main.returns_router, main.reconciliation_router,
        main.notifications_router, main.operational_reports_router,
        main.shop_bills_router, main.admin_database_router,
    ]
    seen = set()
    for router in routers:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            path = route.path
            if path in PUBLIC_PATHS:
                continue
            methods = route.methods or set()
            if not methods & {"POST", "PUT", "PATCH", "DELETE"}:
                continue
            key = (path, tuple(sorted(methods)))
            if key in seen:
                continue
            seen.add(key)
            yield path, methods, route.dependant


def _dependency_names(dependant) -> str:
    """Flatten every dependency call name into one searchable string.

    Conftest reloads router modules, so the same dependency can appear under
    several function objects; matching on names (not identities) keeps the
    sweep stable across reloads.
    """
    names = []
    stack = [dependant]
    while stack:
        current = stack.pop()
        call = getattr(current, "call", None)
        if call is not None:
            names.append(getattr(call, "__name__", ""))
        stack.extend(getattr(current, "dependencies", []) or [])
    return " ".join(names)


def test_every_mutating_route_is_guarded():
    """No state-changing endpoint may be reachable without an auth dependency."""
    unguarded = []
    for path, methods, dependant in _all_routes():
        names = _dependency_names(dependant)
        if not any(marker in names for marker in AUTH_MARKERS):
            unguarded.append(f"{sorted(methods)} {path}")
    assert not unguarded, "Unguarded mutating routes:\n  " + "\n  ".join(unguarded)


def test_at_least_one_guarded_mutating_route():
    """Sanity: the sweep actually found guarded routes (not a vacuous pass)."""
    guarded = 0
    for path, methods, dependant in _all_routes():
        names = _dependency_names(dependant)
        if any(marker in names for marker in AUTH_MARKERS):
            guarded += 1
    assert guarded > 100


# ---------------------------------------------------------------------------
# Audit-writer schema contract
# ---------------------------------------------------------------------------
async def test_log_audit_writes_canonical_and_alias_fields(mocked_db):
    from bill_service.router_utils import log_audit

    ok = await log_audit("u-1", "BILL_VOID", "shop_bill", "abc", {"reason": "x"})
    assert ok is True

    doc = await mocked_db["audit_collection"].find_one({"entity_id": "abc"})
    assert doc is not None
    # canonical
    assert doc["action"] == "BILL_VOID"
    assert doc["entity_type"] == "shop_bill"
    assert doc["entity_id"] == "abc"
    assert doc["details"] == {"reason": "x"}
    assert doc.get("timestamp") is not None
    # legacy aliases the readers key on
    assert doc["user_id"] == "u-1"
    assert doc["entity"] == "shop_bill"
    assert doc.get("created_at") is not None

async def test_log_audit_stamps_laundry_id(mocked_db):
    from bill_service.router_utils import log_audit

    await log_audit("u-1", "X", "shop_bill", "1", laundry_id="acme")
    doc = await mocked_db["audit_collection"].find_one({"entity_id": "1"})
    assert doc["laundry_id"] == "acme"


async def test_log_audit_returns_false_on_failure():
    from bill_service import router_utils

    class Boom:
        async def insert_one(self, _):
            raise RuntimeError("db down")

    ok = await router_utils.log_audit("u", "A", "e", "1", audit_collection=Boom())
    assert ok is False


# ---------------------------------------------------------------------------
# Audit visibility helper used by the dashboard reader
# ---------------------------------------------------------------------------
async def test_audit_visible_accepts_both_schemas(mocked_db):
    from bill_service.routers.dashboard import _audit_visible

    # Stamped entries match on laundry_id regardless of which alias names the
    # entity.
    assert await _audit_visible({"entity_type": "shop_bill", "laundry_id": "acme"}, "acme", False) is True
    assert await _audit_visible({"entity": "shop_bill", "laundry_id": "acme"}, "acme", False) is True


async def test_audit_visible_blocks_foreign_laundry(mocked_db):
    from bill_service.routers.dashboard import _audit_visible

    assert await _audit_visible({"entity_type": "shop_bill", "laundry_id": "other"}, "acme", False) is False


async def test_audit_visible_platform_sees_all(mocked_db):
    from bill_service.routers.dashboard import _audit_visible

    assert await _audit_visible({"entity_type": "shop_bill", "laundry_id": "other"}, "acme", True) is True


# ---------------------------------------------------------------------------
# Delegation: local writers funnel into the canonical schema
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "module_name,func_name",
    [
        ("bill_service.routers.bills", "log_audit"),
        ("bill_service.routers.deliveries", "log_audit"),
        ("bill_service.routers.dispatch", "log_audit"),
        ("bill_service.routers.gatepasses", "log_audit"),
        ("bill_service.routers.linens", "_log_audit"),
    ],
)
def test_local_audit_helpers_exist(module_name, func_name):
    import importlib

    module = importlib.import_module(module_name)
    assert inspect.iscoroutinefunction(getattr(module, func_name))


def test_capability_import_is_generator_free():
    """require_capability must return a plain callable dependency."""
    dep = require_capability("bill:read")
    assert callable(dep)
    assert not inspect.isgeneratorfunction(dep)
