"""Unit tests for scripts/configure-gateway-apps.py against an in-memory fake Container Apps ARM API."""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from argparse import Namespace
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("configure_gateway_apps", ROOT / "scripts" / "configure-gateway-apps.py")
module = importlib.util.module_from_spec(_spec)
sys.modules["configure_gateway_apps"] = module
_spec.loader.exec_module(module)

TENANT = module.TENANT
AUDIENCE = "22222222-2222-4222-8222-222222222222"
OID = "11111111-1111-4111-8111-111111111111"
GATEWAY = "https://apim-northwind.azure-api.net"
SECRET = "super-secret-subscription-key"
FQDN = "{}.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io"


def make_app(name, env=None, secrets=None, **overrides):
    app = {
        "etag": '"1"',
        "properties": {
            "provisioningState": "Succeeded", "latestRevisionName": f"{name}--old", "latestReadyRevisionName": f"{name}--old",
            "configuration": {"activeRevisionsMode": "Single", "ingress": {"fqdn": FQDN.format(name)},
                              "secrets": secrets},
            "template": {"containers": [{"name": "c", "image": "acr/x@sha256:" + "a" * 64, "env": env or [
                {"name": "AZURE_CLIENT_ID", "value": "x"}]}]},
        },
    }
    app["properties"].update(overrides)
    return app


class FakeArm:
    def __init__(self, apps, guard_effective=True, patch_status=200):
        self.apps, self.guard_effective, self.patch_status = apps, guard_effective, patch_status
        self.calls, self.guard_on = [], {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, method = request.url, request.method
        self.calls.append((method, url.path, request.content))
        host = url.host
        if host == "management.azure.com":
            name = re.search(r"containerApps/([^/]+)", url.path).group(1)
            app = self.apps[name]
            if "/revisions/" in url.path:
                return httpx.Response(200, json={"properties": {"healthState": "Healthy", "runningState": "Running", "trafficWeight": 100}})
            if method == "GET":
                return httpx.Response(200, json={k: v for k, v in app.items() if k != "etag"}, headers={"etag": app["etag"]})
            if method == "PATCH":
                if self.patch_status != 200:
                    return httpx.Response(self.patch_status, json={})
                body = json.loads(request.content)["properties"]
                props = app["properties"]
                props["template"] = body["template"]
                if "configuration" in body:
                    props["configuration"]["secrets"] = [{"name": s["name"]} for s in body["configuration"]["secrets"]]
                revision = f"{name}--{body['template']['revisionSuffix']}"
                props["latestRevisionName"] = props["latestReadyRevisionName"] = revision
                env = {e["name"]: e.get("value") for e in body["template"]["containers"][0]["env"]}
                self.guard_on[name] = env.get("NORTHWIND_GATEWAY_AUTH_REQUIRED") == "true" and self.guard_effective
                return httpx.Response(200, json={})
        name = host.split(".")[0]
        if url.path in ("/health", "/healthz"):
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(401 if self.guard_on.get(name) else 200, json={})


def args(**overrides):
    base = dict(app=None, stack=None, guard=None, tenant_id=TENANT, gateway_audience=AUDIENCE, allowed_oid=[OID],
                mcp_gateway_base=None, mcp_key_env="NORTHWIND_MCP_KEY")
    return Namespace(**{**base, **overrides})


def run(fake, app, changes, tmp_path, apply=True, **kwargs):
    with httpx.Client(transport=httpx.MockTransport(fake)) as http:
        return module.configure_app(app, changes, http, lambda: "token", apply=apply, rollback_dir=tmp_path,
                                    sleep=lambda _: None, **kwargs)


def patches(fake):
    return [json.loads(content) for method, _, content in fake.calls if method == "PATCH"]


def enable(app):
    return module.build_changes(args(guard="enable"), app)


def records(tmp_path, app):
    return sorted(tmp_path.glob(f"gateway-rollback-{app}-gw*.json"))


def test_preview_changes_nothing_and_reports_previous_values(tmp_path):
    fake = FakeArm({"ca-api": make_app("ca-api", env=[{"name": "NORTHWIND_GATEWAY_AUTH_REQUIRED", "value": "false"}])})
    result = run(fake, "ca-api", enable("ca-api"), tmp_path, apply=False)
    assert result["state"] == "dry-run" and not patches(fake) and not list(tmp_path.iterdir())
    assert result["previous"]["NORTHWIND_GATEWAY_AUTH_REQUIRED"] == "false"
    assert result["changes"]["NORTHWIND_GATEWAY_ALLOWED_OIDS"] == OID


@pytest.mark.parametrize("app", sorted(module.APPS))
def test_enabling_the_guard_sets_the_four_variables_and_confirms_the_state(tmp_path, app):
    fake = FakeArm({app: make_app(app)})
    result = run(fake, app, enable(app), tmp_path)
    assert result["state"] == "ready" and result["revision"].startswith(f"{app}--gw")
    env = {e["name"]: e["value"] for e in patches(fake)[0]["properties"]["template"]["containers"][0]["env"]}
    assert env["NORTHWIND_GATEWAY_AUTH_REQUIRED"] == "true" and env["NORTHWIND_GATEWAY_TENANT_ID"] == TENANT
    assert env["NORTHWIND_GATEWAY_AUDIENCE"] == AUDIENCE and env["NORTHWIND_GATEWAY_ALLOWED_OIDS"] == OID
    assert env["AZURE_CLIENT_ID"] == "x", "unrelated variables are preserved"
    record = json.loads(records(tmp_path, app)[0].read_text())
    assert record["previous"]["NORTHWIND_GATEWAY_AUTH_REQUIRED"] is None


def test_an_image_without_the_guard_is_detected(tmp_path):
    fake = FakeArm({"ca-api": make_app("ca-api")}, guard_effective=False)
    with pytest.raises(module.ConfigError, match="Guard state not confirmed"):
        run(fake, "ca-api", enable("ca-api"), tmp_path)
    assert records(tmp_path, "ca-api")


def test_disabling_is_confirmed_by_a_non_401(tmp_path):
    fake = FakeArm({"ca-mcp-work-order": make_app("ca-mcp-work-order", env=[
        {"name": "NORTHWIND_GATEWAY_AUTH_REQUIRED", "value": "true"}])})
    result = run(fake, "ca-mcp-work-order", module.build_changes(args(guard="disable"), "ca-mcp-work-order"), tmp_path)
    assert result["state"] == "ready" and result["previous"]["NORTHWIND_GATEWAY_AUTH_REQUIRED"] == "true"


def test_api_apps_can_be_pointed_at_the_gateway_with_the_key_stored_as_a_secret(tmp_path):
    changes = module.build_changes(args(mcp_gateway_base=f"{GATEWAY}/insurance/"), "ca-insurance-api",
                                   {"NORTHWIND_MCP_KEY": SECRET})
    assert changes["MCP_WORK_ORDER_URL"] == f"{GATEWAY}/insurance/mcp/work-order"
    fake = FakeArm({"ca-insurance-api": make_app("ca-insurance-api", env=[
        {"name": "MCP_WORK_ORDER_URL", "value": "https://ca-insurance-review.example/mcp/"}])})
    result = run(fake, "ca-insurance-api", changes, tmp_path, secret_value=SECRET)
    body = patches(fake)[0]["properties"]
    assert body["configuration"] == {"secrets": [{"name": "mcp-gateway-key", "value": SECRET}]}
    env = {e["name"]: e for e in body["template"]["containers"][0]["env"]}
    assert env["MCP_GATEWAY_SUBSCRIPTION_KEY"] == {"name": "MCP_GATEWAY_SUBSCRIPTION_KEY", "secretRef": "mcp-gateway-key"}
    assert env["MCP_TECH_LOOKUP_URL"]["value"] == f"{GATEWAY}/insurance/mcp/tech-lookup"
    for artifact in (json.dumps(result), records(tmp_path, "ca-insurance-api")[0].read_text()):
        assert SECRET not in artifact
    assert result["previous"]["MCP_WORK_ORDER_URL"] == "https://ca-insurance-review.example/mcp/"


def test_mcp_changes_require_the_key_the_matching_stack_and_api_apps(monkeypatch):
    with pytest.raises(module.ConfigError, match="Set NORTHWIND_MCP_KEY"):
        module.build_changes(args(mcp_gateway_base=f"{GATEWAY}/northwind"), "ca-api", {})
    with pytest.raises(module.ConfigError, match="must end with /northwind"):
        module.build_changes(args(mcp_gateway_base=f"{GATEWAY}/insurance"), "ca-api", {"NORTHWIND_MCP_KEY": "k"})
    assert "MCP_WORK_ORDER_URL" not in module.build_changes(args(mcp_gateway_base=f"{GATEWAY}/northwind"), "ca-mcp-work-order", {})


def test_other_secrets_are_never_replaced(tmp_path):
    fake = FakeArm({"ca-api": make_app("ca-api", secrets=[{"name": "unrelated"}])})
    changes = module.build_changes(args(mcp_gateway_base=f"{GATEWAY}/northwind"), "ca-api", {"NORTHWIND_MCP_KEY": "k"})
    with pytest.raises(module.ConfigError, match="other secrets"):
        run(fake, "ca-api", changes, tmp_path, secret_value="k")
    assert not patches(fake)


@pytest.mark.parametrize("app,changes,message", [
    ("ca-api", {"AZURE_CLIENT_ID": "x"}, "may not be changed"),
    ("ca-api", {"NORTHWIND_GATEWAY_AUTH_REQUIRED": "yes"}, "Invalid value"),
    ("ca-api", {"NORTHWIND_GATEWAY_AUDIENCE": "not-a-guid"}, "Invalid value"),
    ("ca-api", {"NORTHWIND_GATEWAY_ALLOWED_OIDS": f"{OID},bad"}, "Invalid value"),
    ("ca-api", {"MCP_WORK_ORDER_URL": "https://evil.example/mcp/work-order"}, "Invalid value"),
    ("ca-api", {"MCP_WORK_ORDER_URL": f"{GATEWAY}/insurance/mcp/work-order"}, "Invalid value"),
    ("ca-api", {"NORTHWIND_GATEWAY_AUTH_REQUIRED": "true"}, "also requires"),
    ("ca-mcp-tech-lookup", {"MCP_TECH_LOOKUP_URL": f"{GATEWAY}/northwind/mcp/tech-lookup"}, "may not be changed"),
    ("ca-web-tech", {"NORTHWIND_GATEWAY_AUTH_REQUIRED": "true"}, "Unknown Container App"),
])
def test_only_allowlisted_names_and_values_are_accepted(tmp_path, app, changes, message):
    with pytest.raises(module.ConfigError, match=message):
        module.validate(app, changes)


def test_revert_restores_previous_values_including_direct_urls(tmp_path):
    direct = "https://ca-mcp-work-order.gentlemushroom-296b38f8.canadacentral.azurecontainerapps.io/mcp/"
    fake = FakeArm({"ca-api": make_app("ca-api", env=[{"name": "MCP_WORK_ORDER_URL", "value": f"{GATEWAY}/northwind/mcp/work-order"},
                                                      {"name": "NORTHWIND_GATEWAY_AUTH_REQUIRED", "value": "true"}])})
    restore = {"MCP_WORK_ORDER_URL": direct, "NORTHWIND_GATEWAY_AUTH_REQUIRED": None}
    with pytest.raises(module.ConfigError):
        module.validate("ca-api", restore)
    result = run(fake, "ca-api", restore, tmp_path, restoring=True)
    env = {e["name"]: e["value"] for e in patches(fake)[0]["properties"]["template"]["containers"][0]["env"]}
    assert env == {"MCP_WORK_ORDER_URL": direct} and result["state"] == "ready"


def test_drift_unstable_and_multi_container_apps_are_refused(tmp_path):
    for overrides, message in (({"provisioningState": "Updating"}, "not in a stable state"),
                               ({"latestRevisionName": "ca-api--pending"}, "deployment in progress")):
        with pytest.raises(module.ConfigError, match=message):
            run(FakeArm({"ca-api": make_app("ca-api", **overrides)}), "ca-api", enable("ca-api"), tmp_path)
    multi = make_app("ca-api")
    multi["properties"]["template"]["containers"].append({"name": "sidecar"})
    with pytest.raises(module.ConfigError, match="multi-container"):
        run(FakeArm({"ca-api": multi}), "ca-api", enable("ca-api"), tmp_path)
    with pytest.raises(module.ConfigError, match="HTTP 412"):
        run(FakeArm({"ca-api": make_app("ca-api")}, patch_status=412), "ca-api", enable("ca-api"), tmp_path)


def test_the_etag_is_sent_so_concurrent_changes_are_not_overwritten(tmp_path):
    seen = {}
    fake = FakeArm({"ca-api": make_app("ca-api")})

    def spy(request):
        if request.method == "PATCH":
            seen["if-match"] = request.headers.get("if-match")
        return fake(request)

    with httpx.Client(transport=httpx.MockTransport(spy)) as http:
        module.configure_app("ca-api", enable("ca-api"), http, lambda: "t", apply=True, rollback_dir=tmp_path, sleep=lambda _: None)
    assert seen["if-match"] == '"1"'


def test_target_selection():
    assert module.targets(args(stack="insurance")) == ["ca-insurance-api", "ca-insurance-lookup", "ca-insurance-review"]
    assert len(module.targets(args(stack="all"))) == 6
    assert module.targets(args(app=["ca-api", "ca-api"])) == ["ca-api"]
    with pytest.raises(module.ConfigError):
        module.targets(args())


def test_every_target_has_a_health_probe_and_the_six_apps_cover_both_stacks():
    assert {stack for stack, _ in module.APPS.values()} == {"northwind", "insurance"}
    assert {kind for _, kind in module.APPS.values()} == set(module.HEALTH)
