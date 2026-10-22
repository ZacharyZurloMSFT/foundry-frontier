"""Preview or create the identities behind the APIM gateway token; only --apply changes anything.

1. apim-northwind needs a system-assigned managed identity: it requests the token APIM injects as
   X-Northwind-Gateway-Token (policy fragment-gateway-token.xml).
2. An Entra application `northwind-gateway` provides that token's audience. It has no secrets, scopes or
   permissions; backends accept a token only from the allow-listed object IDs (NORTHWIND_GATEWAY_ALLOWED_OIDS).
The output lists the values to copy into infra/live/apim-apis.bicep (gatewayAudience) and the backend environment.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time

import httpx

SUBSCRIPTION = "137c905b-7734-479e-acc5-ab8222462936"
TENANT = "043c812b-d828-4850-8da0-cfda17c314d4"
GROUP = "rg-northwind-apps"
APIM = "apim-northwind"
APP_NAME = "northwind-gateway"
APP_TAG = "northwind-gateway-audience"
SERVICE = (f"https://management.azure.com/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}"
           f"/providers/Microsoft.ApiManagement/service/{APIM}")
GRAPH = "https://graph.microsoft.com/v1.0"
GUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


class SetupError(RuntimeError):
    """A safe-to-display failure (never includes a response body)."""


def request(client: httpx.Client, method: str, url: str, token: str, **kwargs) -> dict:
    response = client.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
    if response.status_code == 404 and method == "GET":
        return {}
    if not response.is_success:
        raise SetupError(f"{method} {url.split('?')[0].rsplit('/', 2)[-2]} failed (HTTP {response.status_code})")
    return response.json() if response.content else {}


def apim_identity(client: httpx.Client, token: str) -> dict:
    body = request(client, "GET", SERVICE, token, params={"api-version": "2024-05-01"})
    if not body:
        raise SetupError("apim-northwind was not found in rg-northwind-apps")
    return body.get("identity") or {}


def find_app(client: httpx.Client, token: str) -> dict | None:
    body = request(client, "GET", f"{GRAPH}/applications", token, params={
        "$filter": f"displayName eq '{APP_NAME}'", "$select": "id,appId,displayName,tags,identifierUris"})
    apps = body.get("value", [])
    if len(apps) > 1:
        raise SetupError("More than one application is named northwind-gateway; resolve manually")
    if apps and APP_TAG not in apps[0].get("tags", []):
        raise SetupError("An application named northwind-gateway exists but was not created by this script; refusing to reuse it")
    return apps[0] if apps else None


def plan(identity: dict, app: dict | None) -> list[str]:
    actions = []
    if "SystemAssigned" not in identity.get("type", ""):
        actions.append("enable the system-assigned managed identity on apim-northwind")
    if app is None:
        actions += ["create the Entra application northwind-gateway (single tenant, no secrets or permissions)",
                    "set its identifier URI to api://<appId>", "create its service principal"]
    return actions


def enable_identity(client: httpx.Client, token: str, current: dict, *, sleep=time.sleep) -> dict:
    kind = "SystemAssigned,UserAssigned" if "UserAssigned" in current.get("type", "") else "SystemAssigned"
    request(client, "PATCH", SERVICE, token, params={"api-version": "2024-05-01"}, json={"identity": {"type": kind}})
    for _ in range(30):
        identity = apim_identity(client, token)
        if identity.get("principalId"):
            return identity
        sleep(5)
    raise SetupError("The managed identity did not become available")


def create_app(client: httpx.Client, token: str) -> dict:
    app = request(client, "POST", f"{GRAPH}/applications", token, json={
        "displayName": APP_NAME, "signInAudience": "AzureADMyOrg", "tags": [APP_TAG],
        "notes": "Audience for APIM-to-backend gateway tokens (X-Northwind-Gateway-Token). Managed by scripts/configure-gateway-identity.py."})
    request(client, "PATCH", f"{GRAPH}/applications/{app['id']}", token, json={"identifierUris": [f"api://{app['appId']}"]})
    request(client, "POST", f"{GRAPH}/servicePrincipals", token, json={"appId": app["appId"], "tags": [APP_TAG]})
    return app


def summary(identity: dict, app: dict | None, actions: list[str], applied: bool) -> dict:
    principal = identity.get("principalId")
    audience = app["appId"] if app else None
    return {
        "applied": applied, "pending_actions": [] if applied else actions, "tenant": TENANT,
        "apim_principal_id": principal, "gateway_audience": audience,
        "bicep_parameter": {"gatewayAudience": audience},
        "backend_environment": {
            "NORTHWIND_GATEWAY_TENANT_ID": TENANT, "NORTHWIND_GATEWAY_AUDIENCE": audience,
            "NORTHWIND_GATEWAY_ALLOWED_OIDS": principal, "NORTHWIND_GATEWAY_AUTH_REQUIRED": "false (set true only after the gateway is verified)",
        },
    }


def configure(arm_token: str, graph_token: str, apply: bool, client: httpx.Client | None = None, *, sleep=time.sleep) -> dict:
    http = client or httpx.Client(timeout=60)
    try:
        identity = apim_identity(http, arm_token)
        app = find_app(http, graph_token)
        actions = plan(identity, app)
        if apply and actions:
            if "SystemAssigned" not in identity.get("type", ""):
                identity = enable_identity(http, arm_token, identity, sleep=sleep)
            if app is None:
                app = create_app(http, graph_token)
        return summary(identity, app, actions, applied=apply)
    finally:
        if client is None:
            http.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="create/enable what is missing (default: preview only)")
    args = parser.parse_args(argv)
    from azure.identity import AzureCliCredential

    with AzureCliCredential(tenant_id=TENANT, process_timeout=120) as credential:
        try:
            result = configure(credential.get_token("https://management.azure.com/.default").token,
                               credential.get_token("https://graph.microsoft.com/.default").token, args.apply)
        except SetupError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
