import os
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPBearer
import jwt

import logging as _auth_log
_auth_logger = _auth_log.getLogger("auth")
_jwt_secret = os.getenv("JWT_SECRET")
if not _jwt_secret:
    _auth_logger.warning("JWT_SECRET not set — using insecure fallback. Set JWT_SECRET in production!")
_jwt_secret = _jwt_secret or "CHANGE-ME-IN-PRODUCTION-love-laundry-2026"
JWT_SECRET = _jwt_secret
JWT_ALGORITHM = "HS256"

security = HTTPBearer()


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
    """FastAPI dependency to validate JWT authorization header."""
    token = credentials.credentials
    return verify_token(token)


def require_role(allowed_roles: list):
    """FastAPI dependency factory class to validate appropriate roles."""

    def dependency(current_user: dict = Security(get_current_user)):
        role = str(current_user.get("role") or "").upper()
        allowed_upper = [r.upper() for r in allowed_roles]
        if role not in allowed_upper:
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
