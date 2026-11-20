import logging
import os
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPBearer
import jwt
from pymongo import MongoClient

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load .env before reading secrets. config.Settings uses pydantic-settings, which
# parses .env into the model WITHOUT exporting to os.environ — so without this,
# the service refused to start unless JWT_SECRET was exported by the host shell.
load_dotenv()

_jwt_secret = os.getenv("JWT_SECRET")
if not _jwt_secret:
    raise RuntimeError("FATAL: JWT_SECRET environment variable is not set. Refusing to start.")
JWT_SECRET = _jwt_secret
JWT_ALGORITHM = "HS256"

security = HTTPBearer()

# Statuses that block an account, kept in step with user-service.auth_helper.
DISABLED_STATUSES = {"inactive", "disabled", "suspended", "blocked", "deleted"}

# The user directory belongs to user-service. When it shares the MongoDB cluster
# with this service, set USER_SERVICE_DB_NAME (or MONGODB_DB_NAME, the name
# user-service itself uses) to enable the live revocation check. Without it the
# MAIN database is tried, which works for the shared-database deployment.
USER_SERVICE_DB_NAME = (
    os.getenv("USER_SERVICE_DB_NAME")
    or os.getenv("MONGODB_DB_NAME")
    or os.getenv("MONGODB_MAIN_DB")
)
_users_client = None


def _fresh_user(auth_id: str):
    """Reload the account so a disabled/deleted/demoted user loses access."""
    global _users_client
    if not auth_id:
        return None
    try:
        from .config import settings

        if _users_client is None:
            _users_client = MongoClient(
                settings.resolve_main_uri(), serverSelectionTimeoutMS=1000
            )
        db_name = USER_SERVICE_DB_NAME or settings.resolve_main_db()
        return _users_client[db_name]["users"].find_one(
            {"auth_id": auth_id},
            {"status": 1, "role_id": 1, "role": 1, "user_name": 1, "platform_admin": 1},
        )
    except Exception as exc:
        logger.warning("Could not reload user %r for revocation check: %s", auth_id, exc)
        return None


def create_access_token(data: dict, expires_in_hours: int = 24) -> str:
    """Create a signed JWT token containing user info."""
    payload = data.copy()
    now = datetime.now(timezone.utc)
    payload["exp"] = now + timedelta(hours=expires_in_hours)
    payload["iat"] = now
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> dict:
    """Verify and decode a JWT token."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired"
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
        )


def get_current_user(credentials=Security(security)) -> dict:
    """FastAPI dependency to validate JWT authorization header.

    The signature is checked first, then the account is reloaded from the user
    directory when that database is reachable. A disabled/deleted user is
    refused immediately (missing account -> 401, disabled -> 403) and a
    demotion takes effect on the next request. When no user database is
    configured the token snapshot is kept rather than locking every caller
    out because of a cross-service outage.
    """
    payload = verify_token(credentials.credentials)

    # Client (hotel/shop) sessions live in client_accounts, not users. Their
    # live state is reloaded by user-service.get_client_session at login.
    if payload.get("token_kind") == "client":
        return payload

    fresh = _fresh_user(payload.get("auth_id"))
    if fresh is None:
        if USER_SERVICE_DB_NAME:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Account no longer exists.",
            )
        return payload

    status_value = str(fresh.get("status") or "unset").strip().lower()
    if status_value in DISABLED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been deactivated. Contact an administrator.",
        )

    role = str(fresh.get("role_id") or fresh.get("role") or "").upper()
    if role:
        payload["role"] = role
    payload["status"] = fresh.get("status")
    payload["user_name"] = fresh.get("user_name") or payload.get("user_name")
    payload["platform_admin"] = bool(fresh.get("platform_admin")) or role == "PLATFORM_ADMIN"
    return payload


def require_role(allowed_roles: list):
    """FastAPI dependency factory class to validate appropriate roles."""

    def dependency(current_user: dict = Security(get_current_user)):
        role = str(current_user.get("role") or "").upper()
        allowed_upper = [r.upper() for r in allowed_roles]
        # A platform administrator is a super-account (support sessions): user-service
        # already treats PLATFORM_ADMIN as satisfying ADMIN, so we must agree here or
        # every ADMIN-only platform route 403s while the same token holds every
        # capability via require_capability. Only ADMIN-shaped checks inherit this;
        # a narrower list such as ["STAFF"] stays exact.
        is_platform_admin = role == "PLATFORM_ADMIN" and "ADMIN" in allowed_upper
        if role not in allowed_upper and not is_platform_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{role}' is not authorized to access this resource",
            )
        return current_user

    return dependency


ROLE_CAPABILITIES = {
    "ADMIN": [
        "gatepass:read", "gatepass:write",
        "delivery:read", "delivery:write",
        "dispatch:read", "dispatch:write",
        "loyalty:read", "loyalty:write",
        "bill:read", "bill:write",
        "payment:read", "payment:write",
        "dashboard:read", "report:read",
        "linen:read", "linen:write", "linen:scan",
    ],
    "MANAGER": [
        "gatepass:read", "gatepass:write",
        "delivery:read", "delivery:write",
        "dispatch:read", "dispatch:write",
        "loyalty:read", "loyalty:write",
        "bill:read", "bill:write",
        "payment:read", "payment:write",
        "dashboard:read", "report:read",
        "linen:read", "linen:write", "linen:scan",
    ],
    "STAFF": [
        "gatepass:read", "gatepass:write",
        "delivery:read", "delivery:write",
        "dispatch:read", "dispatch:write",
        "loyalty:read",
        "bill:read",
        "dashboard:read", "report:read",
        "linen:read", "linen:scan",
    ]
}

# Platform administrators hold every capability: support-entry sessions must
# be able to operate inside the entered laundry. Narrower roles are unchanged.
ROLE_CAPABILITIES["PLATFORM_ADMIN"] = list(ROLE_CAPABILITIES["ADMIN"])

def require_capability(capability: str):
    """FastAPI dependency to validate fine-grained capabilities."""
    def dependency(current_user: dict = Security(get_current_user)):
        role = str(current_user.get("role") or "").upper()
        capabilities = ROLE_CAPABILITIES.get(role, [])
        if capability not in capabilities:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{role}' is not authorized to perform this action (missing {capability})",
            )
        return current_user

    return dependency
