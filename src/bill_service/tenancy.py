"""Tenant context and database-per-tenant resolution.

Isolation model
---------------
Each laundry (tenant) owns a separate MongoDB database. The request tenant is
carried in the JWT `tenant` claim, stored in a ContextVar by TenantMiddleware,
and appended to the configured base database name by resolve_db_name().

Every collection in this service is obtained through get_database(), so a
request can only ever address its own tenant's database. Cross-tenant reads are
not expressible in the data layer rather than being a per-query convention.

Tenancy is opt-in (TENANCY_ENABLED). When it is off, or the token carries no
tenant, the configured base database name is used unchanged, so single-tenant
deployments keep working with no data migration.
"""
from __future__ import annotations

import os
import re
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware

TENANT_CLAIM = "tenant"
TENANT_SEPARATOR = "__"
_TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")

_tenant: ContextVar[str] = ContextVar("tenant", default="")


def tenancy_enabled() -> bool:
    return os.getenv("TENANCY_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def normalize_tenant(value) -> str:
    """Return a safe tenant slug, or "" when the value is unusable.

    The slug becomes part of a database name, so it is restricted to a
    conservative character set; anything else is rejected rather than escaped.
    """
    if not value:
        return ""
    slug = str(value).strip().lower()
    return slug if _TENANT_RE.match(slug) else ""


def default_tenant() -> str:
    return normalize_tenant(os.getenv("DEFAULT_TENANT", ""))


def current_tenant() -> str:
    return _tenant.get()


def set_tenant(value: str):
    return _tenant.set(value or "")


def reset_tenant(token) -> None:
    _tenant.reset(token)


def resolve_db_name(base: str) -> str:
    """Return the physical database name for the active tenant."""
    if not tenancy_enabled():
        return base
    slug = current_tenant() or default_tenant()
    if not slug:
        return base
    return f"{base}{TENANT_SEPARATOR}{slug}"


def _decode(token: str) -> dict:
    import jwt as _jwt

    secret = os.getenv("JWT_SECRET")
    if not secret:
        return {}
    try:
        return _jwt.decode(token, secret, algorithms=["HS256"])
    except Exception:
        return {}


class TenantMiddleware(BaseHTTPMiddleware):
    """Bind each request to the tenant named in its bearer token."""

    async def dispatch(self, request, call_next):
        auth = request.headers.get("authorization", "")
        tenant = ""
        if auth.lower().startswith("bearer "):
            payload = _decode(auth.split(" ", 1)[1].strip())
            tenant = normalize_tenant(payload.get(TENANT_CLAIM))
        ctx_token = set_tenant(tenant or default_tenant())
        try:
            return await call_next(request)
        finally:
            reset_tenant(ctx_token)
