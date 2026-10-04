import json
from pathlib import Path

from fastapi.routing import APIRoute
from starlette.routing import Match

from bill_service.main import app
from bill_service.routers import monthly, notifications, returns


def _resolve_endpoint(routes, path: str, method: str):
    scope = {"type": "http", "path": path, "method": method}
    for route in routes:
        if isinstance(route, APIRoute) and route.matches(scope)[0] is Match.FULL:
            return route.endpoint
    return None


def test_vercel_routes_all_paths_to_fastapi_entrypoint():
    config_path = Path(__file__).parents[1] / "vercel.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert config["builds"] == [{"src": "api/index.py", "use": "@vercel/python"}]
    assert config["routes"] == [{"src": "/(.*)", "dest": "api/index.py"}]


def test_expected_production_paths_resolve_to_their_handlers():
    assert _resolve_endpoint(
        returns.router.routes, "/returns/pending-resent", "GET"
    ) is returns.pending_resent_items
    assert _resolve_endpoint(
        notifications.router.routes, "/notifications/gatepass-pending", "GET"
    ) is notifications.gatepass_pending
    assert (
        _resolve_endpoint(
            monthly.router.routes,
            "/monthly/receiving/Amagi Beach Resort/2026/8/day/1/confirm",
            "POST",
        )
        is monthly.confirm_monthly_day
    )
    assert "/returns/pending-resent" in app.openapi()["paths"]
    assert "/notifications/gatepass-pending" in app.openapi()["paths"]
    assert (
        "/monthly/{kind}/{client_name}/{year}/{month}/day/{day}/confirm"
        in app.openapi()["paths"]
    )
