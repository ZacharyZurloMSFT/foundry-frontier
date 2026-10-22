"""Unit tests for scripts/configure-gateway-identity.py with fake ARM and Graph endpoints (no network)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("configure_gateway_identity", ROOT / "scripts" / "configure-gateway-identity.py")
module = importlib.util.module_from_spec(_spec)
sys.modules["configure_gateway_identity"] = module
_spec.loader.exec_module(module)

PRINCIPAL = "11111111-1111-4111-8111-111111111111"
APP_ID = "22222222-2222-4222-8222-222222222222"


class Fake:
    def __init__(self, identity=None, apps=None):
        self.identity, self.apps, self.calls = identity, apps if apps is not None else [], []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, method = str(request.url), request.method
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, url.split("?")[0], body))
        if "ApiManagement/service" in url:
            if method == "PATCH":
                self.identity = {"type": body["identity"]["type"], "principalId": PRINCIPAL, "tenantId": module.TENANT}
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"identity": self.identity})
        if url.startswith(f"{module.GRAPH}/applications") and method == "GET":
            return httpx.Response(200, json={"value": self.apps})
        if url.endswith("/applications") and method == "POST":
            self.apps = [{"id": "obj-1", "appId": APP_ID, "tags": [module.APP_TAG]}]
            return httpx.Response(201, json=self.apps[0])
        return httpx.Response(200, json={})


def run(fake, apply):
    with httpx.Client(transport=httpx.MockTransport(fake)) as client:
        return module.configure("arm", "graph", apply, client, sleep=lambda _: None)


def writes(fake):
    return [call for call in fake.calls if call[0] != "GET"]


def test_preview_lists_actions_and_changes_nothing():
    fake = Fake()
    result = run(fake, apply=False)
    assert not writes(fake)
    assert result["applied"] is False and len(result["pending_actions"]) == 4
    assert result["gateway_audience"] is None and result["apim_principal_id"] is None


def test_apply_enables_identity_creates_the_app_and_reports_the_values():
    fake = Fake()
    result = run(fake, apply=True)
    assert [(m, u.rsplit("/", 1)[-1]) for m, u, _ in writes(fake)] == [
        ("PATCH", "apim-northwind"), ("POST", "applications"), ("PATCH", "obj-1"), ("POST", "servicePrincipals")]
    assert writes(fake)[0][2] == {"identity": {"type": "SystemAssigned"}}
    assert writes(fake)[2][2] == {"identifierUris": [f"api://{APP_ID}"]}
    assert result["applied"] and result["pending_actions"] == []
    assert result["gateway_audience"] == APP_ID and result["apim_principal_id"] == PRINCIPAL
    env = result["backend_environment"]
    assert env["NORTHWIND_GATEWAY_AUDIENCE"] == APP_ID and env["NORTHWIND_GATEWAY_ALLOWED_OIDS"] == PRINCIPAL
    assert env["NORTHWIND_GATEWAY_AUTH_REQUIRED"].startswith("false")
    for value in (module.TENANT, APP_ID, PRINCIPAL):
        assert module.GUID.fullmatch(value)


def test_existing_resources_are_reused_without_writes():
    fake = Fake(identity={"type": "SystemAssigned", "principalId": PRINCIPAL},
                apps=[{"id": "obj-1", "appId": APP_ID, "tags": [module.APP_TAG]}])
    result = run(fake, apply=True)
    assert not writes(fake) and result["gateway_audience"] == APP_ID and result["pending_actions"] == []


def test_user_assigned_identity_is_preserved_when_enabling_system_assigned():
    fake = Fake(identity={"type": "UserAssigned"}, apps=[{"id": "o", "appId": APP_ID, "tags": [module.APP_TAG]}])
    run(fake, apply=True)
    assert writes(fake)[0][2] == {"identity": {"type": "SystemAssigned,UserAssigned"}}


def test_an_application_not_created_by_this_script_is_never_reused():
    fake = Fake(identity={"type": "SystemAssigned", "principalId": PRINCIPAL}, apps=[{"id": "o", "appId": APP_ID, "tags": []}])
    with pytest.raises(module.SetupError, match="not created by this script"):
        run(fake, apply=True)
    assert not writes(fake)


def test_duplicate_applications_are_refused():
    fake = Fake(identity={"type": "SystemAssigned"}, apps=[{"tags": [module.APP_TAG]}, {"tags": [module.APP_TAG]}])
    with pytest.raises(module.SetupError, match="More than one"):
        run(fake, apply=False)


def test_missing_apim_is_reported():
    def gone(request):
        return httpx.Response(404, json={})
    with httpx.Client(transport=httpx.MockTransport(gone)) as client, pytest.raises(module.SetupError, match="not found"):
        module.configure("arm", "graph", False, client)


def test_errors_never_include_response_bodies():
    def broken(request):
        return httpx.Response(403, json={"error": "secret-details"})
    with httpx.Client(transport=httpx.MockTransport(broken)) as client, pytest.raises(module.SetupError) as error:
        module.configure("arm", "graph", False, client)
    assert "secret-details" not in str(error.value)
