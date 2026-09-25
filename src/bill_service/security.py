"""Security hardening middleware for Bill Service.

• SecurityHeadersMiddleware — injects strict response headers
• RateLimitMiddleware — sliding-window token bucket per client IP

Secrets are validated at import time in ``auth_helper`` / ``crypto_helper``:
the service refuses to start when JWT_SECRET or MASTER_KEY is unset, so there
is no insecure-default state for a health check to report.
"""
import time
import hashlib
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["X-Request-Id"] = request.headers.get("X-Request-Id") or _generate_req_id()
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, requests_per_minute: int = 300):
        super().__init__(app)
        self.limit = requests_per_minute
        self._buckets: dict[str, list[float]] = {}

    async def dispatch(self, request: Request, call_next):
        key = request.client.host if request.client else "unknown"
        now = time.time()
        window_start = now - 60.0
        hits = [t for t in self._buckets.get(key, []) if t >= window_start]
        self._buckets[key] = hits
        if len(hits) >= self.limit:
            return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded — try again shortly."})
        self._buckets[key] = [*hits, now]
        return await call_next(request)


def _generate_req_id() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:16]


def apply_security(app: FastAPI, rate_limit: int = 300, origins: list[str] | None = None) -> None:
    """Mount CORS, security headers, and rate limiting on the FastAPI app."""
    from fastapi.middleware.cors import CORSMiddleware
    from .config import settings

    if origins is None:
        origins = settings.cors_origins

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False if "*" in origins else True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RateLimitMiddleware, requests_per_minute=rate_limit)
