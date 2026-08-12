"""Vercel ASGI entrypoint for bill-service."""

try:
    from src.bill_service.main import app as app
except Exception as exc:  # pragma: no cover - bootstrap diagnostics only
    import traceback

    from fastapi import FastAPI

    _boot_error = f"{type(exc).__name__}: {exc}"
    _boot_trace = traceback.format_exc()
    app = FastAPI(title="bill-service-boot-error")

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    @app.api_route("/", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def _boot_failure(path: str = ""):
        return {
            "status": "boot_error",
            "service": "bill-service",
            "error": _boot_error,
            "traceback": _boot_trace,
        }
