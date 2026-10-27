"""Contract tests for the API Management definition (infra/live/apim-apis.bicep and its policy files)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "infra" / "live"
BICEP = LIVE / "apim-apis.bicep"
POLICIES = LIVE / "apim" / "policies"

ALLOWED_TYPES = {
    "Microsoft.ApiManagement/service/apis",
    "Microsoft.ApiManagement/service/apis/diagnostics",
    "Microsoft.ApiManagement/service/apis/operations",
    "Microsoft.ApiManagement/service/apis/operations/policies",
    "Microsoft.ApiManagement/service/apis/policies",
    "Microsoft.ApiManagement/service/loggers",
    "Microsoft.ApiManagement/service/products",
    "Microsoft.ApiManagement/service/products/apis",
    "Microsoft.ApiManagement/service/subscriptions",
}
PLACEHOLDER = re.compile(r"__[A-Z][A-Z_]*__")
SUFFIX = "gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io"
GUID = "00000000-0000-4000-8000-0000000000a1"


def find_bicep() -> str | None:
    found = shutil.which("bicep")
    if found:
        return found
    installed = Path.home() / ".azure" / "bin" / ("bicep.exe" if os.name == "nt" else "bicep")
    return str(installed) if installed.is_file() else None


@pytest.fixture(scope="module")
def template() -> dict:
    bicep = find_bicep()
    if bicep is None:
        pytest.skip("Bicep is not installed")
    result = subprocess.run([bicep, "build", str(BICEP), "--stdout"], capture_output=True, text=True,
                            encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def resources(template: dict) -> list[dict]:
    items = template["resources"]
    return list(items.values()) if isinstance(items, dict) else items


def read(name: str) -> str:
    return (POLICIES / name).read_text(encoding="utf-8")


def render(kind: str, *, audience: str = "", business_auth: bool = False) -> str:
    """Mirror of the replace() chains in apim-apis.bicep."""
    token = (read("fragment-gateway-token.xml").replace("__GATEWAY_AUDIENCE__", audience) if audience
             else read("fragment-gateway-token-off.xml"))
    if kind == "voice":
        return read("voice-api.xml").replace("__GATEWAY_TOKEN__", token)
    if kind == "mcp":
        return read("mcp-api.xml").replace("__RATE_LIMIT__", "300").replace("__GATEWAY_TOKEN__", token)
    jwt = ""
    if business_auth:
        jwt = (read("fragment-validate-jwt.xml").replace("__TENANT_ID__", GUID).replace("__AUDIENCE__", GUID)
               .replace("__SCOPE__", "access_as_user").replace("__CLIENT_ID_VALUES__", f"<value>{GUID}</value>"))
    origins = "".join(f"<origin>{o}</origin>" for o in (
        f"https://ca-insurance-portal.{SUFFIX}", f"https://ca-insurance-voice.{SUFFIX}", "http://localhost:5173"))
    return (read("rest-api.xml").replace("__RATE_LIMIT__", "600").replace("__JWT_VALIDATION__", jwt)
            .replace("__GATEWAY_TOKEN__", token).replace("__ALLOWED_ORIGINS__", origins))


RENDERINGS = {
    "rest-minimal": lambda: render("rest"),
    "rest-full": lambda: render("rest", audience=f"api://{GUID}", business_auth=True),
    "voice-off": lambda: render("voice"),
    "voice-on": lambda: render("voice", audience=GUID),
    "mcp-off": lambda: render("mcp"),
    "mcp-on": lambda: render("mcp", audience=GUID),
}


def test_template_declares_only_allowlisted_apim_child_types(template):
    types = {resource["type"] for resource in resources(template)}
    assert types <= ALLOWED_TYPES, types - ALLOWED_TYPES
    assert not any(t in ("Microsoft.ApiManagement/service", "Microsoft.Resources/deployments") for t in types)
    assert {
        "Microsoft.ApiManagement/service/apis", "Microsoft.ApiManagement/service/apis/policies",
        "Microsoft.ApiManagement/service/apis/operations/policies", "Microsoft.ApiManagement/service/products",
        "Microsoft.ApiManagement/service/subscriptions", "Microsoft.ApiManagement/service/loggers",
    } <= types


def test_definition_never_uses_preview_mcp_api_type_backends_or_service_scope_objects():
    text = BICEP.read_text(encoding="utf-8")
    assert "type: 'mcp'" not in text
    assert "service/backends" not in text and "service/policies" not in text and "service/diagnostics" not in text
    assert "Microsoft.ApiManagement/service@" in text and "existing" in text
    assert "@2025-09-01-preview" not in text, "use GA API versions only"


def test_parameter_defaults_describe_the_live_insurance_stack(template):
    params = template["parameters"]
    assert params["apimName"]["defaultValue"] == "apim-northwind"
    assert params["appInsightsName"]["defaultValue"] == "appi-zurlo-northwind"
    assert re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", params["gatewayAudience"]["defaultValue"]), (
        "the gateway token needs the northwind-gateway application's client ID; clearing it silently disables lock-down"
    )
    assert params["businessAuth"]["defaultValue"] == {}
    stacks = params["stacks"]["defaultValue"]
    assert [stack["name"] for stack in stacks] == ["insurance"]
    keys = {"name", "apiApp", "techLookupApp", "workOrderApp", "webApp", "voiceApp"}
    assert all(set(stack) == keys for stack in stacks)
    assert all(re.fullmatch(r"[a-z][a-z0-9]{1,30}", stack["name"]) for stack in stacks)
    apps = [value for stack in stacks for key, value in stack.items() if key != "name"]
    assert len(apps) == len(set(apps)) and all(re.fullmatch(r"ca-insurance-[a-z]+", app) for app in apps)


def test_repo_managed_stack_matches_azure_yaml_resource_names():
    services = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))["services"]
    declared = {name: service.get("resourceName") for name, service in services.items() if service.get("resourceName")}
    text = BICEP.read_text(encoding="utf-8")
    stack = re.search(r"name: 'insurance'(.*?)\n  }", text, re.S).group(1)
    for service, key in (("api", "apiApp"), ("mcp-policy-lookup", "techLookupApp"), ("mcp-claim-review", "workOrderApp"),
                         ("web-portal", "webApp"), ("web-voice", "voiceApp")):
        assert f"{key}: '{declared[service]}'" in stack


def test_every_placeholder_in_policy_files_is_substituted_by_the_bicep_file():
    text = BICEP.read_text(encoding="utf-8")
    used = set()
    for path in POLICIES.glob("*.xml"):
        used |= set(PLACEHOLDER.findall(path.read_text(encoding="utf-8")))
    replaced = set(re.findall(r"'(__[A-Z][A-Z_]*__)'", text))
    assert used and used <= replaced, used - replaced


@pytest.mark.parametrize("name", sorted(RENDERINGS))
def test_rendered_policies_are_strict_well_formed_xml_without_placeholders(name):
    xml = RENDERINGS[name]()
    assert not PLACEHOLDER.search(xml)
    root = ET.fromstring(xml)  # raises on nested/invalid comments or unquoted attributes
    assert root.tag == "policies"
    assert {child.tag for child in root} == {"inbound", "backend", "outbound", "on-error"}


def test_placeholder_names_never_appear_inside_xml_comments():
    for path in POLICIES.glob("*.xml"):
        for comment in re.findall(r"<!--(.*?)-->", path.read_text(encoding="utf-8"), re.S):
            assert not PLACEHOLDER.search(comment), (path.name, "substitution would nest comments")


@pytest.mark.parametrize("name", sorted(RENDERINGS))
def test_policies_never_buffer_streams_or_validate_bodies(name):
    xml = re.sub(r"<!--.*?-->", "", RENDERINGS[name](), flags=re.S)
    for forbidden in ("validate-content", "context.Response.Body", "context.Request.Body", "cache-lookup", "cache-store"):
        assert forbidden not in xml
    if name.startswith(("rest", "mcp")):
        assert 'buffer-response="false"' in xml


def test_rest_policy_is_the_single_cors_authority_with_an_explicit_header_list():
    root = ET.fromstring(render("rest"))
    cors = root.find("inbound/cors")
    assert cors is not None and cors.get("allow-credentials") == "false"
    headers = [h.text for h in cors.findall("allowed-headers/header")]
    assert "*" not in headers, "APIM rejects '*' combined with other headers and browsers never match Authorization with *"
    assert {"Authorization", "Content-Type", "X-Department", "X-Demo-Run", "Idempotency-Key", "X-Synthetic-Data"} <= set(headers)
    assert {"ETag", "Location", "X-Correlation-ID"} <= {h.text for h in cors.findall("expose-headers/header")}
    origins = [o.text for o in cors.findall("allowed-origins/origin")]
    assert origins and "*" not in origins and all(o.startswith(("https://", "http://localhost")) for o in origins)


def test_mcp_policy_rewrites_to_the_backend_slash_endpoint_and_never_forwards_the_gateway_key():
    root = ET.fromstring(render("mcp", audience=GUID))
    inbound = root.find("inbound")
    assert inbound.find("rewrite-uri").get("template") == "/mcp/"
    deleted = {(e.get("name")) for e in inbound.findall("set-header") if e.get("exists-action") == "delete"}
    assert "Ocp-Apim-Subscription-Key" in deleted
    assert inbound.find("set-query-parameter").get("name") == "subscription-key"
    assert root.find("backend/forward-request").get("buffer-response") == "false"


def test_gateway_token_fragment_never_replaces_the_callers_authorization_header():
    xml = render("rest", audience=GUID)
    inbound = ET.fromstring(xml).find("inbound")
    names = [e.tag for e in inbound]
    assert names.index("set-variable") < names.index("authentication-managed-identity")
    assert 'northwind-original-authorization' in xml
    identity = inbound.find("authentication-managed-identity")
    assert identity.get("output-token-variable-name") and identity.get("ignore-error") == "false"
    restored = inbound.find("choose/when/set-header")
    assert restored.get("name") == "Authorization" and restored.get("exists-action") == "override"
    assert inbound.find("choose/otherwise/set-header").get("exists-action") == "delete"
    assert any(e.get("name") == "X-Northwind-Gateway-Token" and e.get("exists-action") == "override"
               for e in inbound.findall("set-header"))


def test_off_fragment_strips_a_client_supplied_gateway_token():
    inbound = ET.fromstring(render("rest")).find("inbound")
    assert inbound.find("authentication-managed-identity") is None
    assert any(e.get("name") == "X-Northwind-Gateway-Token" and e.get("exists-action") == "delete"
               for e in inbound.findall("set-header"))


def test_business_api_jwt_prevalidation_is_scoped_to_api_v1_and_requires_scope_and_client():
    inbound = ET.fromstring(render("rest", business_auth=True)).find("inbound")
    when = next(w for w in inbound.findall("choose/when") if "validate-jwt" in [c.tag for c in w])
    assert "/api/v1/" in when.get("condition")
    jwt = when.find("validate-jwt")
    assert jwt.get("require-scheme") == "Bearer" and jwt.get("require-signed-tokens") == "true"
    claims = {c.get("name"): c for c in jwt.findall("required-claims/claim")}
    assert claims["scp"].get("separator") == " " and claims["scp"].find("value").text == "access_as_user"
    assert claims["azp"].find("value").text == GUID
    assert [i.text for i in jwt.findall("issuers/issuer")] == [f"https://login.microsoftonline.com/{GUID}/v2.0"]


def test_voice_policy_has_no_cors_or_validation_because_handshakes_do_not_support_them():
    root = ET.fromstring(render("voice", audience=GUID))
    forbidden = {"cors", "validate-jwt", "validate-content", "validate-headers", "validate-parameters"}
    assert not forbidden & {element.tag for element in root.iter()}
    assert root.find("inbound/rate-limit-by-key") is not None


def test_voice_api_is_attached_to_the_onhandshake_operation():
    text = BICEP.read_text(encoding="utf-8")
    assert "name: 'onHandshake'" in text
    assert "Microsoft.ApiManagement/service/apis/operations/policies@" in text


def served_routes() -> tuple[set[tuple[str, str]], set[str]]:
    """HTTP (method, path) pairs and WebSocket paths from the public `routes` of every router the app includes."""
    import sys

    from fastapi import APIRouter
    from fastapi.routing import APIRoute, APIWebSocketRoute

    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
    from apps.api.app import main

    routers = [main.app.router] + [value for value in vars(main).values() if isinstance(value, APIRouter)]
    http = {(method, route.path) for router in routers for route in router.routes if isinstance(route, APIRoute)
            for method in route.methods if method not in ("HEAD", "OPTIONS")}
    sockets = {route.path for router in routers for route in router.routes if isinstance(route, APIWebSocketRoute)}
    return http, sockets


def test_every_websocket_route_of_the_api_is_published_as_a_websocket_api():
    _, sockets = served_routes()
    text = BICEP.read_text(encoding="utf-8")
    assert sockets == {"/voice/realtime"}, "a new WebSocket route needs its own APIM WebSocket API"
    assert "path: '${stack.name}/voice/realtime'" in text
    assert "/voice/realtime'" in text and "type: 'websocket'" in text


def test_rest_api_is_imported_from_the_committed_export():
    text = BICEP.read_text(encoding="utf-8")
    assert "loadTextContent('apim/specs/northwind-api.openapi.json')" in text
    assert "format: 'openapi+json'" in text
    assert (LIVE / "apim" / "specs" / "northwind-api.openapi.json").is_file()
