"""The API surface left after the legacy department API was deleted.

Only /health and /build are served without authentication; every other route lives under
/api/v1 and depends on the verified Entra principal. Adding a route is a conscious decision
that updates this file.
"""
import httpx
import pytest
from app import main
from app.assistants.routes import router as assistants_router
from app.business.auth import authenticated_principal
from app.business.routes import router as business_router
from fastapi.routing import APIRoute
from starlette.routing import Mount, Route, WebSocketRoute

PUBLIC = {("GET", "/build"), ("GET", "/health")}
DOCUMENTATION = {"/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}
METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
ROUTERS = (business_router, assistants_router)
LEGACY_PATHS = (
    "/chat", "/qa", "/handoff", "/agent/run", "/hitl", "/hitl/decide", "/work-orders", "/voice/realtime",
    "/company", "/departments", "/bakeoff/run", "/safety/scan", "/traces/abc", "/insurance/claims",
    "/claims/samples", "/claims/extract", "/adjuster-support/chat", "/harness/runs", "/session/abc",
)


def dependency_calls(dependant):
    yield dependant.call
    for sub in dependant.dependencies:
        yield from dependency_calls(sub)


def operations():
    return {(method.upper(), path): operation for path, item in main.app.openapi()["paths"].items()
            for method, operation in item.items() if method in METHODS}


def test_the_routers_hold_only_http_routes_that_require_the_verified_principal():
    routes = [route for router in ROUTERS for route in router.routes]
    assert routes and all(isinstance(route, APIRoute) for route in routes)
    assert all(authenticated_principal in set(dependency_calls(route.dependant)) for route in routes)


def test_only_health_and_build_are_served_without_authentication():
    served = operations()
    assert {key for key, operation in served.items() if not operation.get("security")} == PUBLIC
    assert set(served) - PUBLIC == {(method, route.path) for router in ROUTERS
                                    for route in router.routes for method in route.methods}


def test_every_authenticated_route_is_versioned_under_api_v1():
    paths = {path for _, path in operations()} - {path for _, path in PUBLIC}
    assert paths and all(path.startswith("/api/v1/") for path in paths)


def test_nothing_else_is_mounted_and_there_is_no_websocket():
    assert not [route for route in main.app.routes if isinstance(route, (Mount, WebSocketRoute))]
    plain = {route.path for route in main.app.routes if isinstance(route, Route) and not isinstance(route, APIRoute)}
    assert plain <= DOCUMENTATION


@pytest.mark.asyncio
async def test_the_deleted_legacy_endpoints_answer_404():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        for path in LEGACY_PATHS:
            for method in ("GET", "POST"):
                response = await client.request(method, path, json={} if method == "POST" else None)
                assert response.status_code == 404, (method, path)
