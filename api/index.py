from src.bill_service.main import app

# Vercel loads the FastAPI ASGI app from the top-level `app` variable.
# Do NOT assign `handler = app` — `handler` is reserved for BaseHTTPRequestHandler.
