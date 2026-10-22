import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_") + "_apim_tests", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load("apim-release")
oidc = load("configure-github-oidc")

SHA = "a" * 40
SERVICE = (f"/subscriptions/{release.SUBSCRIPTION}/resourceGroups/rg-northwind-apps/providers/"
           "Microsoft.ApiManagement/service/apim-northwind")
PROVIDERS = f"/subscriptions/{release.SUBSCRIPTION}/resourceGroups/rg-northwind-apps/providers"
GUID = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"
SECRET = "SUPER-SECRET-VALUE-NEVER-PERSISTED"
API_KINDS = ("api", "voice", "mcp-tech-lookup", "mcp-work-order")


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    # GitHub Actions exports these for the whole job, including this test run.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("NORTHWIND_APIM_CONFIRM", raising=False)


def rid(path):
    return f"{SERVICE}/{path}"


def expected_ids():
    """Every object the live template creates for the two approved stacks (51 in total)."""
    ids = [rid("loggers/northwind-appinsights")]
    for stack in release.STACKS:
        for kind in API_KINDS:
            api = f"{stack}-{kind}"
            ids += [rid(f"apis/{api}"), rid(f"apis/{api}/diagnostics/applicationinsights")]
            ids.append(rid(f"apis/{api}/operations/onHandshake/policies/policy") if kind == "voice"
                       else rid(f"apis/{api}/policies/policy"))
            if kind.startswith("mcp"):
                ids += [rid(f"apis/{api}/operations/mcp-{method}") for method in ("post", "get", "delete")]
        ids += [
            rid(f"products/{stack}-web"), rid(f"products/{stack}-agents"), rid(f"subscriptions/{stack}-agents"),
            rid(f"products/{stack}-web/apis/{stack}-api"), rid(f"products/{stack}-web/apis/{stack}-voice"),
            rid(f"products/{stack}-agents/apis/{stack}-mcp-tech-lookup"),
            rid(f"products/{stack}-agents/apis/{stack}-mcp-work-order"),
        ]
    return ids


EXISTING = [
    SERVICE,
    *(f"{PROVIDERS}/Microsoft.App/containerApps/ca-{name}" for name in (
        "api", "web-tech", "web-voice", "mcp-tech-lookup", "mcp-work-order",
        "insurance-api", "insurance-lookup", "insurance-review", "insurance-portal", "insurance-voice")),
    f"{PROVIDERS}/Microsoft.Insights/components/appi-zurlo-northwind",
    f"{PROVIDERS}/microsoft.insights/actiongroups/Application Insights Smart Detection",
    f"{PROVIDERS}/Microsoft.ContainerRegistry/registries/acrzurlonorthwind",
]


def change(kind, identifier, secret=SECRET):
    return {"changeType": kind, "resourceId": identifier, "before": None, "delta": None,
            "after": {"properties": {"value": secret}} if kind != "Ignore" else None}


def whatif(*changes, **extra):
    return {"status": "Succeeded", "error": None, "diagnostics": None, "potentialChanges": None,
            "changes": list(changes), **extra}


def live_plan():
    return whatif(*(change("Ignore", identifier) for identifier in EXISTING),
                  *(change("Create", identifier) for identifier in expected_ids()))


def test_the_fixture_matches_the_shape_of_the_live_what_if():
    ids = expected_ids()
    assert len(ids) == 51 == len(set(ids))
    relative = [identifier.removeprefix(SERVICE + "/").split("/") for identifier in ids]
    assert sorted(parts[1] for parts in relative if parts[0] == "apis" and len(parts) == 2) == sorted(release.API_NAMES)


# ---------------------------------------------------------------- allow-list


@pytest.mark.parametrize("identifier", expected_ids() + [
    rid("apis/northwind-api/operations/get_health_health_get"), rid("apis/insurance-api/operations/a.b-c_d"),
    rid("apis/northwind-voice/operations/onHandshake"),
])
def test_allow_list_accepts_every_object_the_boundary_manages(identifier):
    assert release.allowed_change(identifier)
    assert release.allowed_change(identifier.upper()) and release.allowed_change(identifier.lower())


def test_allow_list_covers_exactly_the_approved_names():
    assert release.API_NAMES == (
        "northwind-api", "northwind-voice", "northwind-mcp-tech-lookup", "northwind-mcp-work-order",
        "insurance-api", "insurance-voice", "insurance-mcp-tech-lookup", "insurance-mcp-work-order",
    )
    assert release.PRODUCT_NAMES == ("northwind-web", "northwind-agents", "insurance-web", "insurance-agents")
    assert release.AGENT_SUBSCRIPTIONS == ("northwind-agents", "insurance-agents")
    assert release.STACKS == ("northwind", "insurance")


FORBIDDEN = {
    "service itself": SERVICE, "service with trailing slash": SERVICE + "/",
    "global policy": rid("policies/policy"), "global policy with other name": rid("policies/other"),
    "service diagnostics": rid("diagnostics/applicationinsights"), "service named values": rid("namedValues"),
    "named value": rid("namedValues/northwind-tenant-id"), "named value other": rid("namedValues/other"),
    "unknown api": rid("apis/echo-api"), "other stack api": rid("apis/contoso-api"),
    "api suffix": rid("apis/northwind-api2"), "api prefix": rid("apis/xnorthwind-api"),
    "unknown api kind": rid("apis/northwind-admin"), "revision suffix": rid("apis/northwind-api;rev=2"),
    "api with escaped slash": rid("apis/northwind-api%2Foperations"),
    "api policy on a voice api": rid("apis/northwind-voice/policies/policy"),
    "api policy on the other voice api": rid("apis/insurance-voice/policies/policy"),
    "api policy other name": rid("apis/northwind-api/policies/other"),
    "api diagnostics other": rid("apis/northwind-api/diagnostics/azuremonitor"),
    "api schema": rid("apis/northwind-api/schemas/default"), "api release": rid("apis/northwind-api/releases/r1"),
    "api tag": rid("apis/northwind-api/tags/t"), "api wiki": rid("apis/northwind-api/wikis/default"),
    "api issue": rid("apis/northwind-api/issues/1"), "api documentation": rid("apis/northwind-api/documentations/d"),
    "operation collection": rid("apis/northwind-api/operations"), "operation empty id": rid("apis/northwind-api/operations/"),
    "operation policy on rest api": rid("apis/northwind-api/operations/get_health/policies/policy"),
    "handshake policy on rest api": rid("apis/northwind-api/operations/onHandshake/policies/policy"),
    "handshake policy on mcp api": rid("apis/northwind-mcp-tech-lookup/operations/onHandshake/policies/policy"),
    "other operation policy on voice api": rid("apis/northwind-voice/operations/mcp-post/policies/policy"),
    "handshake policy other name": rid("apis/northwind-voice/operations/onHandshake/policies/other"),
    "handshake diagnostics": rid("apis/northwind-voice/operations/onHandshake/diagnostics/applicationinsights"),
    "handshake tags": rid("apis/northwind-voice/operations/onHandshake/tags/t"),
    "handshake deeper": rid("apis/northwind-voice/operations/onHandshake/policies/policy/x"),
    "unknown product": rid("products/starter"), "unlimited product": rid("products/unlimited"),
    "other product kind": rid("products/northwind-admin"), "other stack product": rid("products/contoso-web"),
    "product policy": rid("products/northwind-web/policies/policy"),
    "product group": rid("products/northwind-web/groups/administrators"),
    "product subscription": rid("products/northwind-web/subscriptions/x"),
    "product tag": rid("products/northwind-web/tags/x"),
    "product link to unknown api": rid("products/northwind-web/apis/echo-api"),
    "link on unknown product": rid("products/starter/apis/northwind-api"),
    "cross stack link": rid("products/northwind-web/apis/insurance-api"),
    "cross stack agents link": rid("products/insurance-agents/apis/northwind-mcp-work-order"),
    "master subscription": rid("subscriptions/master"), "web subscription": rid("subscriptions/northwind-web"),
    "other agents subscription": rid("subscriptions/contoso-agents"),
    "agents subscription suffix": rid("subscriptions/insurance-agents-2"),
    "other logger": rid("loggers/azuremonitor"), "logger prefix": rid("loggers/northwind-appinsights2"),
    "backend": rid("backends/northwind-api"), "certificate": rid("certificates/c1"), "user": rid("users/1"),
    "group": rid("groups/administrators"), "tag": rid("tags/northwind"),
    "portal settings": rid("portalsettings/delegation"), "policy fragment": rid("policyFragments/f"),
    "authorization server": rid("authorizationServers/a"), "identity provider": rid("identityProviders/aad"),
    "version set": rid("apiVersionSets/v"), "gateway": rid("gateways/g"), "tenant access": rid("tenant/access"),
    "other service": SERVICE + "-2/apis/northwind-api",
    "service prefix": SERVICE.replace("apim-northwind", "xapim-northwind") + "/apis/northwind-api",
    "other resource group": rid("apis/northwind-api").replace("rg-northwind-apps", "rg-northwind-data"),
    "resource group prefix": rid("apis/northwind-api").replace("rg-northwind-apps", "rg-northwind-apps2"),
    "other subscription": rid("apis/northwind-api").replace(release.SUBSCRIPTION, "00000000-0000-0000-0000-000000000000"),
    "other provider": rid("apis/northwind-api").replace("Microsoft.ApiManagement/service", "Microsoft.ApiManagement/Service2"),
    "container app": f"{PROVIDERS}/Microsoft.App/containerApps/ca-api",
    "app insights": f"{PROVIDERS}/Microsoft.Insights/components/appi-zurlo-northwind",
    "role assignment": f"{PROVIDERS}/Microsoft.Authorization/roleAssignments/x",
    "nested deployment": f"{PROVIDERS}/Microsoft.Resources/deployments/x",
    "empty": "", "relative": "apis/northwind-api", "no scope": "/apis/northwind-api",
    "leading space": " " + rid("apis/northwind-api"), "trailing space": rid("apis/northwind-api") + " ",
    "trailing newline": rid("apis/northwind-api") + "\n", "embedded newline": rid("apis/northwind-api/operations/a\nb"),
    "nul byte": rid("apis/northwind-api\x00"), "backslash": rid("apis/northwind-api\\operations\\x"),
    "double slash": rid("apis//northwind-api"), "double slash child": rid("apis/northwind-api//policies/policy"),
    "dot segment": rid("apis/northwind-api/./policies/policy"),
    "parent segment": rid("apis/northwind-api/../../policies/policy"),
    "parent segment to global policy": rid("apis/northwind-api/operations/../../../policies/policy"),
    "parent operation id": rid("apis/northwind-api/operations/.."), "dot operation id": rid("apis/northwind-api/operations/."),
    "operation parent then policy": rid("apis/northwind-api/operations/../policies/policy"),
    "operation deep traversal": rid("apis/northwind-api/operations/x/../../.."),
    "encoded parent operation id": rid("apis/northwind-api/operations/%2e%2e"),
    "encoded slash operation id": rid("apis/northwind-api/operations/a%2fb"),
    "operation with space": rid("apis/northwind-api/operations/a b"),
    "operation with query": rid("apis/northwind-api/operations/a?x=1"),
    "operation with hash": rid("apis/northwind-api/operations/a#b"), "operation with star": rid("apis/northwind-api/operations/*"),
    "operation too long": rid("apis/northwind-api/operations/" + "a" * 81),
    "api traversal": rid("apis/../policies/policy"), "apis traversal child": rid("apis/northwind-api/operations/x/policies/../.."),
    "handshake parent": rid("apis/northwind-voice/operations/../onHandshake/policies/policy"),
    "kelvin sign lookalike": rid("apis/northwind-mcp-tech-loo\u212aup"),
    "turkish dotless lookalike": rid("apis/northwind-api").replace("api", "ap\u0131"),
    "fullwidth lookalike": rid("apis/northwind-api").replace("apis", "\uff41pis"),
}


@pytest.mark.parametrize("identifier", list(FORBIDDEN.values()), ids=list(FORBIDDEN))
def test_allow_list_rejects_everything_outside_the_boundary(identifier):
    assert not release.allowed_change(identifier)
    if identifier.isascii():
        assert not release.allowed_change(identifier.upper()) and not release.allowed_change(identifier.swapcase())


@pytest.mark.parametrize("value", [None, 5, 1.5, True, [], {}, ["a"], b"bytes", object()])
def test_allow_list_rejects_non_string_identifiers(value):
    assert release.allowed_change(value) is False


def test_voice_apis_take_policies_only_on_the_handshake_operation():
    for stack in release.STACKS:
        assert not release.allowed_change(rid(f"apis/{stack}-voice/policies/policy"))
        assert release.allowed_change(rid(f"apis/{stack}-voice/operations/onHandshake/policies/policy"))
        assert release.allowed_change(rid(f"apis/{stack}-voice/operations/ONHANDSHAKE/POLICIES/POLICY"))
        for kind in ("api", "mcp-tech-lookup", "mcp-work-order"):
            assert release.allowed_change(rid(f"apis/{stack}-{kind}/policies/policy"))
            assert not release.allowed_change(rid(f"apis/{stack}-{kind}/operations/onHandshake/policies/policy"))
            assert not release.allowed_change(rid(f"apis/{stack}-{kind}/operations/mcp-post/policies/policy"))


def test_products_only_expose_apis_of_their_own_stack():
    for stack in release.STACKS:
        for product in ("web", "agents"):
            for kind in API_KINDS:
                assert release.allowed_change(rid(f"products/{stack}-{product}/apis/{stack}-{kind}"))
                other = next(s for s in release.STACKS if s != stack)
                assert not release.allowed_change(rid(f"products/{stack}-{product}/apis/{other}-{kind}"))


# ---------------------------------------------------------------- plan allow-list


def test_plan_keeps_create_and_modify_lowercased_and_sorted_by_resource():
    identifiers = [rid("apis/NORTHWIND-API"), rid("apis/insurance-api"), rid("loggers/northwind-appinsights")]
    result = release.safe_plan(whatif(
        change("Create", identifiers[0]), change("Modify", identifiers[1]), change("Create", identifiers[2])))
    assert result == {"changes": [
        {"resource_id": rid("apis/insurance-api").lower(), "change_type": "Modify"},
        {"resource_id": rid("apis/northwind-api").lower(), "change_type": "Create"},
        {"resource_id": rid("loggers/northwind-appinsights").lower(), "change_type": "Create"},
    ]}


def test_plan_for_the_live_shape_skips_existing_resources_and_accepts_every_create():
    result = release.safe_plan(live_plan())
    assert len(result["changes"]) == 51 and {c["change_type"] for c in result["changes"]} == {"Create"}
    assert [c["resource_id"] for c in result["changes"]] == sorted(i.lower() for i in expected_ids())
    listed = {c["resource_id"] for c in result["changes"]}
    assert not any(identifier.lower() in listed for identifier in EXISTING)


@pytest.mark.parametrize("kind", ["Ignore", "NoChange"])
def test_ignore_and_no_change_are_skipped_even_for_resources_outside_the_boundary(kind):
    outside = [SERVICE, rid("policies/policy"), f"{PROVIDERS}/Microsoft.App/containerApps/ca-api", "not-an-id", "", None]
    assert release.safe_plan(whatif(*({"changeType": kind, "resourceId": identifier} for identifier in outside))) == {
        "changes": []}
    assert release.safe_plan({"changes": [{"changeType": kind}]}) == {"changes": []}


@pytest.mark.parametrize("kind", ["Delete", "Deploy", "Unsupported", "create", "MODIFY", "", None, 7])
def test_unsupported_change_types_are_rejected_for_allowed_resources(kind):
    with pytest.raises(ValueError, match="unsupported change"):
        release.safe_plan(whatif({"changeType": kind, "resourceId": rid("apis/northwind-api")}))


@pytest.mark.parametrize("identifier", [SERVICE, rid("policies/policy"), rid("apis/echo-api"),
                                        f"{PROVIDERS}/Microsoft.App/containerApps/ca-api", "", None, 5])
@pytest.mark.parametrize("kind", ["Create", "Modify"])
def test_creating_or_modifying_anything_else_is_rejected(kind, identifier):
    with pytest.raises(ValueError, match="unsupported change or resource"):
        release.safe_plan(whatif(change("Create", rid("apis/northwind-api")), {"changeType": kind, "resourceId": identifier}))


def test_a_single_bad_entry_rejects_a_plan_that_is_otherwise_valid():
    plan = live_plan()
    plan["changes"].append(change("Delete", rid("apis/northwind-api")))
    with pytest.raises(ValueError, match="unsupported change"):
        release.safe_plan(plan)


def test_rejection_names_the_offending_resource_without_property_values():
    with pytest.raises(ValueError) as error:
        release.safe_plan(whatif(change("Create", rid("policies/policy"))))
    assert "policies/policy" in str(error.value) and SECRET not in str(error.value)


@pytest.mark.parametrize("result", [
    {"status": "Failed", "changes": []}, {"status": "Canceled", "changes": []}, {"status": "Running", "changes": []},
    {"changes": "none"}, {"changes": None}, {"changes": {}}, {}, None, [], "plan",
    whatif(potentialChanges="maybe"), whatif(potentialChanges={"a": 1}),
    whatif(error={"code": "InvalidTemplate", "message": "failed"}), whatif(error="failed"),
])
def test_incomplete_or_malformed_plans_are_rejected(result):
    with pytest.raises(ValueError):
        release.safe_plan(result)


def test_status_may_be_absent_but_not_unsuccessful():
    assert release.safe_plan({"changes": []}) == {"changes": []}
    assert release.safe_plan({"status": "Succeeded", "changes": []}) == {"changes": []}


def test_what_if_entries_that_are_not_objects_are_rejected():
    for entry in (None, "Create", 5, ["Create"]):
        with pytest.raises(ValueError):
            release.safe_plan({"changes": [entry]})


def test_potential_changes_are_held_to_the_same_allow_list():
    assert release.safe_plan(whatif(potentialChanges=[])) == {"changes": []}
    ok = release.safe_plan(whatif(potentialChanges=[change("Create", rid("apis/northwind-api"))]))
    assert [c["resource_id"] for c in ok["changes"]] == [rid("apis/northwind-api").lower()]
    assert release.safe_plan(whatif(potentialChanges=[change("Ignore", SERVICE)])) == {"changes": []}
    for bad in (change("Create", rid("policies/policy")), change("Delete", rid("apis/northwind-api")),
                {"changeType": "Deploy", "resourceId": rid("apis/northwind-api")}):
        with pytest.raises(ValueError, match="unsupported change"):
            release.safe_plan(whatif(potentialChanges=[bad]))


# ---------------------------------------------------------------- template boundary


def stack(name, prefix=""):
    return {"name": name, "apiApp": f"ca-{prefix}api", "techLookupApp": f"ca-{prefix}lookup",
            "workOrderApp": f"ca-{prefix}review", "webApp": f"ca-{prefix}portal", "voiceApp": f"ca-{prefix}voice"}


def template_resources():
    kinds = [
        "loggers", "products", "products", "apis", "apis/policies", "apis", "apis/operations/policies", "apis",
        "apis/operations", "apis/policies", "products/apis", "products/apis", "products/apis", "subscriptions",
        "apis/diagnostics", "apis/diagnostics", "apis/diagnostics",
    ]
    return [
        {"type": f"Microsoft.ApiManagement/service/{kind}", "apiVersion": "2024-05-01",
         "name": f"[format('{{0}}/{{1}}', parameters('apimName'), 'r{index}')]",
         "properties": {"value": "[variables('policy')]"},
         **({} if kind == "loggers" else {"copy": {"name": f"r{index}", "count": "[length(parameters('stacks'))]"}})}
        for index, kind in enumerate(kinds)
    ]


def compiled_template():
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0", "metadata": {"_generator": {"name": "bicep", "version": "0.46.1.21595"}},
        "parameters": {
            "apimName": {"type": "string", "defaultValue": "apim-northwind"},
            "appInsightsName": {"type": "string", "defaultValue": "appi-zurlo-northwind"},
            "stacks": {"type": "array", "defaultValue": [stack("northwind"), stack("insurance", "insurance-")]},
            "extraAllowedOrigins": {"type": "array", "defaultValue": ["http://localhost:5173", "http://localhost:5174"]},
            "gatewayAudience": {"type": "string", "defaultValue": ""},
            "businessAuth": {"type": "object", "defaultValue": {}},
            "webRateLimitPerMinute": {"type": "int", "defaultValue": 600, "minValue": 1, "maxValue": 100000},
            "agentsRateLimitPerMinute": {"type": "int", "defaultValue": 300, "minValue": 1, "maxValue": 100000},
        },
        "variables": {"policy": "<policies />"},
        "resources": template_resources(),
        "outputs": {"gatewayUrl": {"type": "string", "value": "[reference(parameters('apimName')).gatewayUrl]"}},
    }


def with_default(name, value):
    template = compiled_template()
    template["parameters"][name]["defaultValue"] = value
    return template


def test_synthetic_template_mirrors_the_live_compile_and_is_accepted():
    template = compiled_template()
    assert len(template["resources"]) == 17 and isinstance(template["resources"], list)
    release.validate_template(template)
    assert set(template["parameters"]) == set(release.PARAMETER_CHECKS)


def test_every_resource_type_the_live_template_declares_is_allowed():
    assert release.RESOURCE_TYPES == {f"microsoft.apimanagement/service/{child}".lower() for child in (
        "apis", "apis/operations", "apis/operations/policies", "apis/policies", "apis/diagnostics",
        "products", "products/apis", "subscriptions", "loggers")}
    assert {r["type"].lower() for r in template_resources()} == release.RESOURCE_TYPES


@pytest.mark.parametrize("candidate", [
    "Microsoft.ApiManagement/service", "microsoft.apimanagement/SERVICE", "Microsoft.ApiManagement/service/policies",
    "Microsoft.ApiManagement/service/namedValues", "microsoft.apimanagement/service/NAMEDVALUES",
    "Microsoft.ApiManagement/service/backends", "Microsoft.ApiManagement/service/certificates",
    "Microsoft.ApiManagement/service/users", "Microsoft.ApiManagement/service/groups",
    "Microsoft.ApiManagement/service/tags", "Microsoft.ApiManagement/service/diagnostics",
    "Microsoft.ApiManagement/service/portalsettings", "Microsoft.ApiManagement/service/apis/schemas",
    "Microsoft.ApiManagement/service/apis/releases", "Microsoft.ApiManagement/service/apis/operations/diagnostics",
    "Microsoft.ApiManagement/service/products/policies", "Microsoft.ApiManagement/service/products/groups",
    "Microsoft.ApiManagement/service/subscriptions/regeneratePrimaryKey", "Microsoft.ApiManagement/service/apiVersionSets",
    "Microsoft.App/containerApps", "Microsoft.Insights/components", "Microsoft.Authorization/roleAssignments",
    "Microsoft.Resources/deployments", "Microsoft.Network/privateEndpoints", "Microsoft.Storage/storageAccounts", "", "apis",
])
def test_template_cannot_declare_other_resource_types(candidate):
    template = compiled_template()
    template["resources"].append({"type": candidate, "apiVersion": "2024-05-01", "name": "x"})
    with pytest.raises(ValueError):
        release.validate_template(template)


def test_template_cannot_declare_the_api_management_service_even_with_an_existing_flag():
    for resource in (
        {"type": "Microsoft.ApiManagement/service", "apiVersion": "2024-05-01", "name": "[parameters('apimName')]"},
        {"type": "Microsoft.ApiManagement/service", "apiVersion": "2024-05-01", "name": "x", "existing": True},
    ):
        template = compiled_template()
        template["resources"].insert(0, resource)
        with pytest.raises(ValueError, match="service must be referenced as existing"):
            release.validate_template(template)


def test_template_must_have_a_resource_collection_with_object_entries():
    for resources in (None, "none", 5, {}, {"a": {}}, [], ["Microsoft.ApiManagement/service/apis"], [None]):
        template = compiled_template()
        template["resources"] = resources
        with pytest.raises(ValueError):
            release.validate_template(template)
    template = compiled_template()
    del template["resources"]
    with pytest.raises(ValueError):
        release.validate_template(template)


def test_nested_resources_and_cross_scope_properties_are_checked_too():
    template = compiled_template()
    template["resources"][3]["resources"] = [{"type": "Microsoft.ApiManagement/service/apis/schemas", "name": "x"}]
    with pytest.raises(ValueError):
        release.validate_template(template)
    template["resources"][3]["resources"] = [{"type": "Microsoft.ApiManagement/service/apis/operations", "name": "x"}]
    release.validate_template(template)
    template["resources"][3]["resources"] = "x"
    with pytest.raises(ValueError):
        release.validate_template(template)
    for key in ("scope", "subscriptionId", "resourceGroup"):
        template = compiled_template()
        template["resources"][0][key] = "elsewhere"
        with pytest.raises(ValueError, match="Cross-scope"):
            release.validate_template(template)


def test_template_must_be_a_resource_group_deployment_template():
    for schema in (None, "", "https://schema.management.azure.com/schemas/2018-05-01/subscriptionDeploymentTemplate.json#",
                   "https://schema.management.azure.com/schemas/2019-08-01/tenantDeploymentTemplate.json#"):
        template = compiled_template()
        template["$schema"] = schema
        with pytest.raises(ValueError, match="resource-group deployment template"):
            release.validate_template(template)
    for template in (None, [], "template"):
        with pytest.raises(ValueError):
            release.validate_template(template)


def test_symbolic_name_templates_may_reference_existing_resources_but_not_declare_them():
    template = compiled_template()
    template["languageVersion"] = "2.0"
    template["resources"] = {
        "apim": {"existing": True, "type": "Microsoft.ApiManagement/service", "apiVersion": "2024-05-01", "name": "x"},
        "apps": {"existing": True, "type": "Microsoft.App/containerApps", "apiVersion": "2024-03-01", "name": "x"},
        "insights": {"existing": True, "type": "Microsoft.Insights/components", "apiVersion": "2020-02-02", "name": "x"},
        **{f"r{index}": resource for index, resource in enumerate(template_resources())},
    }
    release.validate_template(template)
    template["resources"]["apim"]["existing"] = False
    with pytest.raises(ValueError, match="service must be referenced as existing"):
        release.validate_template(template)
    template["resources"]["apim"] = {"type": "Microsoft.App/containerApps", "name": "x"}
    with pytest.raises(ValueError, match="outside the API Management boundary"):
        release.validate_template(template)


def test_an_existing_flag_is_ignored_outside_symbolic_name_templates():
    template = compiled_template()
    template["resources"].append({"type": "Microsoft.App/containerApps", "name": "x", "existing": True})
    with pytest.raises(ValueError, match="outside the API Management boundary"):
        release.validate_template(template)
    template = compiled_template()
    template["resources"] = {"a": template["resources"][0]}
    with pytest.raises(ValueError):
        release.validate_template(template)


def test_parameter_defaults_are_pinned_to_the_live_targets():
    for name, value in (("apimName", "apim-other"), ("apimName", "APIM-NORTHWIND"), ("apimName", "apim-northwind "),
                        ("apimName", ""), ("apimName", None), ("apimName", ["apim-northwind"]),
                        ("appInsightsName", "appi-other"), ("appInsightsName", "APPI-ZURLO-NORTHWIND"),
                        ("appInsightsName", None)):
        with pytest.raises(ValueError, match="differ from the approved"):
            release.validate_template(with_default(name, value))


@pytest.mark.parametrize("stacks", [
    [], [stack("northwind")], [stack("insurance", "insurance-")],
    [stack("northwind"), stack("insurance", "insurance-"), stack("contoso", "contoso-")],
    [stack("northwind"), stack("northwind")], [stack("northwind"), stack("contoso", "contoso-")],
    [stack("Northwind"), stack("insurance", "insurance-")], [stack("northwind "), stack("insurance", "insurance-")],
    [{**stack("northwind"), "name": None}, stack("insurance", "insurance-")],
    [{**stack("northwind"), "name": ["northwind"]}, stack("insurance", "insurance-")],
    "northwind", None, {"name": "northwind"}, [None, "x"], [stack("northwind"), "insurance"],
])
def test_stack_names_must_be_exactly_the_approved_stacks(stacks):
    with pytest.raises(ValueError):
        release.validate_template(with_default("stacks", stacks))


def test_stack_order_is_not_significant_but_membership_is():
    release.validate_template(with_default("stacks", [stack("insurance", "insurance-"), stack("northwind")]))


def broken_stack(**changes):
    return [{**stack("northwind"), **changes}, stack("insurance", "insurance-")]


def without(mapping, key):
    return {name: value for name, value in mapping.items() if name != key}


@pytest.mark.parametrize("stacks", [
    broken_stack(extraApp="ca-extra"), broken_stack(note="free text"),
    broken_stack(apps={"api": "ca-api"}), broken_stack(display="ca-api"),
    [without(stack("northwind"), "webApp"), stack("insurance", "insurance-")],
    [without(stack("northwind"), "voiceApp"), stack("insurance", "insurance-")],
    [without(stack("northwind"), "name"), stack("insurance", "insurance-")],
    [{"name": "northwind"}, stack("insurance", "insurance-")],
    [stack("northwind"), without(stack("insurance", "insurance-"), "techLookupApp")],
])
def test_each_stack_carries_exactly_the_approved_key_set(stacks):
    with pytest.raises(ValueError):
        release.validate_template(with_default("stacks", stacks))


@pytest.mark.parametrize("key", ["apiApp", "techLookupApp", "workOrderApp", "webApp", "voiceApp"])
@pytest.mark.parametrize("value", [
    "api", "ca-", "CA-api", "ca-API", "ca_api", "ca-api_1", "ca-api ", " ca-api", "ca-api\n", "ca-a b", "ca-api/x", "ca-../x",
    "https://ca-api", "ca-api.internal", "", None, 5, True, ["ca-api"], {"a": "ca-api"},
])
def test_stack_app_names_must_be_container_app_names(key, value):
    with pytest.raises(ValueError):
        release.validate_template(with_default("stacks", broken_stack(**{key: value})))


@pytest.mark.parametrize("key", ["apiApp", "techLookupApp", "workOrderApp", "webApp", "voiceApp"])
def test_stack_app_names_accept_digits_and_dashes(key):
    release.validate_template(with_default("stacks", broken_stack(**{key: "ca-web-2-tech"})))


@pytest.mark.parametrize("origins", [
    [], ["http://localhost:5173"], ["https://app.example.com"], ["http://a.b-c.example:8080", "https://x.y"],
])
def test_allowed_origins_accept_plain_http_origins(origins):
    release.validate_template(with_default("extraAllowedOrigins", origins))


@pytest.mark.parametrize("origins", [
    ["*"], ["https://*.example.com"], ["https://example.com/"], ["https://example.com/path"], ["https://example.com?x=1"],
    ["https://example.com#x"], ["https://user@example.com"], ["https://example.com:"], ["https://example.com:80a"],
    ["https://"], ["http://"], ["ftp://example.com"], ["javascript:alert(1)"], ["//example.com"], ["example.com"],
    ["https://example.com\n"], [" https://example.com"], ["https://example.com "], ["https://exa mple.com"],
    ["https://example.com</origin><origin>*"], ["https://exämple.com"], ["HTTPS://EXAMPLE.COM/x"], [""], [None], [5],
    "http://localhost:5173", None, {"a": "http://localhost"}, [["http://localhost"]],
])
def test_allowed_origins_reject_wildcards_paths_and_markup(origins):
    with pytest.raises(ValueError, match="extraAllowedOrigins"):
        release.validate_template(with_default("extraAllowedOrigins", origins))


@pytest.mark.parametrize("audience", ["", GUID, GUID.upper(), f"api://{GUID}", f"api://{GUID.upper()}"])
def test_gateway_audience_accepts_empty_guid_or_application_id_uri(audience):
    release.validate_template(with_default("gatewayAudience", audience))


@pytest.mark.parametrize("audience", [
    "api://not-a-guid", "not-a-guid", GUID + "x", "x" + GUID, f"api://{GUID}/", f"api://{GUID}/scope", f"https://{GUID}",
    f"API://{GUID}", f"api:/{GUID}", " ", GUID + "\n", f"api://{GUID}</audience>", "__GATEWAY_AUDIENCE__", None, 5, [GUID],
])
def test_gateway_audience_rejects_anything_else(audience):
    with pytest.raises(ValueError, match="gatewayAudience"):
        release.validate_template(with_default("gatewayAudience", audience))


def business_auth(**changes):
    return {"tenantId": GUID, "audience": GUID, "clientIds": [GUID, GUID.upper()], "scope": "access_as_user", **changes}


@pytest.mark.parametrize("value", [{}, business_auth(), business_auth(scope="Business.Read-Write_1"),
                                   business_auth(clientIds=[GUID])])
def test_business_auth_accepts_empty_or_fully_specified_guid_configuration(value):
    release.validate_template(with_default("businessAuth", value))


@pytest.mark.parametrize("value", [
    business_auth(tenantId="contoso.onmicrosoft.com"), business_auth(tenantId=""), business_auth(tenantId=None),
    business_auth(audience="api://" + GUID), business_auth(audience="not-a-guid"), business_auth(audience=5),
    business_auth(clientIds=[]), business_auth(clientIds=GUID), business_auth(clientIds=["nope"]),
    business_auth(clientIds=[GUID, "nope"]), business_auth(clientIds=[GUID, None]), business_auth(clientIds=None),
    business_auth(scope=""), business_auth(scope="two words"), business_auth(scope="a<b"), business_auth(scope="a/b"),
    business_auth(scope="x" * 65), business_auth(scope="a\n"), business_auth(scope=None), business_auth(scope=["a"]),
    business_auth(issuer="https://evil.example"), business_auth(extra=GUID),
    {key: value for key, value in business_auth().items() if key != "scope"},
    {key: value for key, value in business_auth().items() if key != "clientIds"},
    {"tenantId": GUID}, [], "x", None, 5,
])
def test_business_auth_rejects_partial_free_form_or_unknown_configuration(value):
    with pytest.raises(ValueError, match="businessAuth"):
        release.validate_template(with_default("businessAuth", value))


@pytest.mark.parametrize("name", ["webRateLimitPerMinute", "agentsRateLimitPerMinute"])
@pytest.mark.parametrize("value", [1, 300, 100000])
def test_rate_limits_accept_the_approved_range(name, value):
    release.validate_template(with_default(name, value))


@pytest.mark.parametrize("name", ["webRateLimitPerMinute", "agentsRateLimitPerMinute"])
@pytest.mark.parametrize("value", [0, -1, 100001, 10**9, "600", "", None, True, False, 1.5, 600.0, [600], {"a": 1}])
def test_rate_limits_reject_values_outside_the_approved_range(name, value):
    with pytest.raises(ValueError, match="Rate limits"):
        release.validate_template(with_default(name, value))


def test_template_parameters_are_exactly_the_approved_set():
    assert set(release.PARAMETER_CHECKS) == {
        "apimName", "appInsightsName", "stacks", "extraAllowedOrigins", "gatewayAudience", "businessAuth",
        "webRateLimitPerMinute", "agentsRateLimitPerMinute"}
    template = compiled_template()
    template["parameters"]["extra"] = {"type": "string", "defaultValue": "x"}
    with pytest.raises(ValueError, match="unexpected: extra"):
        release.validate_template(template)
    for name in release.PARAMETER_CHECKS:
        template = compiled_template()
        del template["parameters"][name]
        with pytest.raises(ValueError, match=f"missing: {name}"):
            release.validate_template(template)
    for parameters in (None, [], "x", {}):
        template = compiled_template()
        template["parameters"] = parameters
        with pytest.raises(ValueError):
            release.validate_template(template)


@pytest.mark.parametrize("name", sorted(release.PARAMETER_CHECKS))
def test_every_parameter_needs_a_reviewed_default(name):
    template = compiled_template()
    del template["parameters"][name]["defaultValue"]
    with pytest.raises(ValueError, match="reviewed default"):
        release.validate_template(template)
    template["parameters"][name] = "not-a-definition"
    with pytest.raises(ValueError, match="reviewed default"):
        release.validate_template(template)


def test_secure_or_expression_defaults_are_not_a_way_around_the_pins():
    for name, value in (("apimName", "[parameters('other')]"), ("stacks", "[json('[]')]"),
                        ("gatewayAudience", "[newGuid()]"), ("extraAllowedOrigins", "[createArray('*')]"),
                        ("webRateLimitPerMinute", "[int('1')]")):
        with pytest.raises(ValueError):
            release.validate_template(with_default(name, value))


# ---------------------------------------------------------------- envelope


def template_file(tmp_path, template=None):
    path = tmp_path / "apim-apis.json"
    path.write_text(json.dumps(template or compiled_template()), encoding="utf-8")
    return path


def test_envelope_binds_source_template_and_full_plan(tmp_path):
    path = template_file(tmp_path)
    result = live_plan()
    first = release.envelope(path, result, SHA)
    assert first == release.envelope(path, copy.deepcopy(result), SHA)
    assert set(first) == {"source_sha", "subscription", "resource_group", "template_sha256", "plan_sha256", "changes"}
    assert first["source_sha"] == SHA and first["subscription"] == release.SUBSCRIPTION
    assert first["resource_group"] == "rg-northwind-apps"
    assert first["template_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(first["changes"]) == 51 and first["changes"] == release.safe_plan(result)["changes"]
    assert first != release.envelope(path, result, "b" * 40)
    other_template = template_file(tmp_path, {**compiled_template(), "variables": {"policy": "<policies><x/></policies>"}})
    assert first["template_sha256"] != release.envelope(other_template, result, SHA)["template_sha256"]
    changed = copy.deepcopy(result)
    changed["changes"][-1]["after"]["properties"]["value"] = "different"
    assert first["plan_sha256"] != release.envelope(path, changed, SHA)["plan_sha256"]
    shorter = whatif(*result["changes"][:-1])
    assert first["plan_sha256"] != release.envelope(path, shorter, SHA)["plan_sha256"]


def test_envelope_hash_does_not_depend_on_what_if_listing_order(tmp_path):
    path = template_file(tmp_path)
    result = live_plan()
    reordered = copy.deepcopy(result)
    reordered["changes"].reverse()
    assert release.envelope(path, result, SHA) == release.envelope(path, reordered, SHA)
    potential = whatif(potentialChanges=[change("Create", rid("apis/northwind-api")), change("Create", rid("apis/insurance-api"))])
    flipped = copy.deepcopy(potential)
    flipped["potentialChanges"].reverse()
    assert release.plan_digest(potential) == release.plan_digest(flipped)
    assert release.plan_digest(potential) != release.plan_digest(whatif())


def test_envelope_never_contains_property_values_and_refuses_unsafe_plans(tmp_path):
    path = template_file(tmp_path)
    assert SECRET not in json.dumps(release.envelope(path, live_plan(), SHA))
    for plan in (whatif(change("Delete", rid("apis/northwind-api"))), whatif(change("Create", rid("policies/policy"))),
                 {"status": "Failed", "changes": []}):
        with pytest.raises(ValueError):
            release.envelope(path, plan, SHA)


# ---------------------------------------------------------------- Azure CLI wrapper


def test_command_requires_the_azure_cli(monkeypatch):
    monkeypatch.setattr(release.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="Azure CLI is required"):
        release.command("deployment", "group", "what-if")


def test_command_runs_az_with_json_output_and_parses_it(monkeypatch):
    captured = {}

    def run(arguments, **options):
        captured.update(arguments=arguments, options=options)
        return SimpleNamespace(returncode=0, stdout='{"status": "Succeeded"}', stderr="")

    monkeypatch.setattr(release.shutil, "which", lambda _: "az-path")
    monkeypatch.setattr(release.subprocess, "run", run)
    assert release.command("rest", "--method", "get") == {"status": "Succeeded"}
    assert captured["arguments"] == ["az-path", "rest", "--method", "get", "--output", "json", "--only-show-errors"]
    assert captured["options"]["check"] is False and captured["options"]["capture_output"] is True


def test_command_failures_never_echo_azure_output(monkeypatch):
    monkeypatch.setattr(release.shutil, "which", lambda _: "az")
    monkeypatch.setattr(release.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=SECRET, stderr=SECRET))
    with pytest.raises(RuntimeError, match="No successful deployment is implied") as error:
        release.command("deployment", "group", "create")
    assert SECRET not in str(error.value)


# ---------------------------------------------------------------- post-apply verification


def listing(*names, next_link=None):
    page = {"value": [{"name": name, "type": "Microsoft.ApiManagement/service/apis"} for name in names]}
    if next_link:
        page["nextLink"] = next_link
    return page


def test_verification_queries_the_apis_collection_and_expects_all_eight(monkeypatch):
    calls = []
    monkeypatch.setattr(release, "command", lambda *args: calls.append(args) or listing(*release.API_NAMES, "echo-api"))
    assert release.verify_apis() == sorted(release.API_NAMES) and len(release.API_NAMES) == 8
    assert calls == [("rest", "--method", "get", "--url",
                      f"https://management.azure.com/subscriptions/{release.SUBSCRIPTION}/resourceGroups/rg-northwind-apps"
                      "/providers/Microsoft.ApiManagement/service/apim-northwind/apis",
                      "--url-parameters", "api-version=2024-05-01")]


def test_verification_is_case_insensitive_and_reports_every_missing_api(monkeypatch):
    monkeypatch.setattr(release, "command", lambda *args: listing(*(n.upper() for n in release.API_NAMES)))
    assert release.verify_apis() == sorted(release.API_NAMES)
    monkeypatch.setattr(release, "command", lambda *args: listing(*release.API_NAMES[:6]))
    with pytest.raises(RuntimeError, match="insurance-mcp-tech-lookup, insurance-mcp-work-order"):
        release.verify_apis()
    monkeypatch.setattr(release, "command", lambda *args: {"value": []})
    with pytest.raises(RuntimeError, match="missing after deployment"):
        release.verify_apis()
    monkeypatch.setattr(release, "command", lambda *args: {})
    with pytest.raises(RuntimeError, match="missing after deployment"):
        release.verify_apis()


@pytest.mark.parametrize("page", [[], "text", None, 5])
def test_verification_rejects_malformed_listings(monkeypatch, page):
    monkeypatch.setattr(release, "command", lambda *args: page)
    with pytest.raises(RuntimeError, match="unexpected API listing"):
        release.verify_apis()


def test_verification_follows_continuation_links_within_the_same_collection(monkeypatch):
    calls = []
    link = release.APIS_URL + "?api-version=2024-05-01&$skip=4"
    pages = [listing(*release.API_NAMES[:4], next_link=link), listing(*release.API_NAMES[4:])]
    monkeypatch.setattr(release, "command", lambda *args: calls.append(args) or pages[len(calls) - 1])
    assert release.verify_apis() == sorted(release.API_NAMES)
    assert calls[1] == ("rest", "--method", "get", "--url", link)


@pytest.mark.parametrize("link", [
    "https://evil.example/apis?x=1", "https://management.azure.com/subscriptions/other/apis", "http://management.azure.com/",
    5, ["x"],
])
def test_verification_never_follows_foreign_continuation_links(monkeypatch, link):
    calls = []
    monkeypatch.setattr(release, "command", lambda *args: calls.append(args) or listing(*release.API_NAMES[:2], next_link=link))
    with pytest.raises(RuntimeError, match="unexpected continuation link"):
        release.verify_apis()
    assert len(calls) == 1


def test_verification_gives_up_after_a_bounded_number_of_pages(monkeypatch):
    calls = []
    link = release.APIS_URL + "?$skip=1"
    monkeypatch.setattr(release, "command", lambda *args: calls.append(args) or listing("echo-api", next_link=link))
    with pytest.raises(RuntimeError, match="missing after deployment"):
        release.verify_apis()
    assert len(calls) == 20


# ---------------------------------------------------------------- plan and apply


class FakeAz:
    def __init__(self, plan=None, apis=None):
        self.plan = live_plan() if plan is None else plan
        self.apis = list(release.API_NAMES) if apis is None else apis
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[:3] == ("deployment", "group", "what-if"):
            return copy.deepcopy(self.plan)
        if args[:3] == ("deployment", "group", "create"):
            return {"properties": {"provisioningState": "Succeeded", "outputs": {"leak": {"value": SECRET}}}}
        if args[0] == "rest":
            return listing(*self.apis)
        raise AssertionError(f"Unexpected Azure call: {args}")

    @property
    def verbs(self):
        return [" ".join(call[:3]) if call[0] == "deployment" else call[0] for call in self.calls]


@pytest.fixture
def azure(monkeypatch):
    fake = FakeAz()
    monkeypatch.setattr(release, "command", fake)
    return fake


def run_main(tmp_path, *extra, template=None, sha=SHA):
    compiled = template or template_file(tmp_path)
    output = tmp_path / "out" / "result.json"
    release.main(["--compiled-template", str(compiled), "--output", str(output), "--source-sha", sha, *extra])
    return json.loads(output.read_text(encoding="utf-8"))


def approve(tmp_path, plan=None, sha=SHA):
    approved = tmp_path / "approved.json"
    approved.write_text(json.dumps(release.envelope(template_file(tmp_path), live_plan() if plan is None else plan, sha)),
                        encoding="utf-8")
    return approved


def test_plan_runs_only_what_if_and_publishes_ids_and_change_types(tmp_path, azure, capsys, monkeypatch):
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    result = run_main(tmp_path)
    assert azure.verbs == ["deployment group what-if"]
    call = azure.calls[0]
    assert call[call.index("--mode") + 1] == "Incremental" and call[call.index("--validation-level") + 1] == "ProviderNoRbac"
    assert "--no-pretty-print" in call and call[call.index("--resource-group") + 1] == "rg-northwind-apps"
    assert call[call.index("--subscription") + 1] == release.SUBSCRIPTION and "--parameters" not in call
    assert "applied" not in result and len(result["changes"]) == 51 and result["source_sha"] == SHA
    printed = capsys.readouterr().out
    markdown = summary_file.read_text(encoding="utf-8")
    assert json.loads(printed) == result
    assert "plan only; nothing was changed" in markdown and "| Create | `apis/northwind-api` |" in markdown
    assert "| Create | `apis/insurance-voice/operations/onhandshake/policies/policy` |" in markdown
    for identifier in (c["resource_id"] for c in result["changes"]):
        assert identifier.removeprefix(release.SERVICE_SCOPE) in markdown
    for text in (printed, markdown, json.dumps(result)):
        assert SECRET not in text


def test_plan_without_a_step_summary_environment_writes_only_the_output_file(tmp_path, azure):
    run_main(tmp_path)
    assert sorted(path.name for path in tmp_path.rglob("*") if path.is_file()) == ["apim-apis.json", "result.json"]


def test_step_summary_is_appended_not_overwritten(tmp_path, azure, monkeypatch):
    summary_file = tmp_path / "summary.md"
    summary_file.write_text("earlier step\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    run_main(tmp_path)
    assert summary_file.read_text(encoding="utf-8").startswith("earlier step\n## API Management plan")


def test_an_empty_plan_is_reported_as_no_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(release, "command", FakeAz(whatif(*(change("Ignore", i) for i in EXISTING))))
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    assert run_main(tmp_path)["changes"] == []
    assert "No API Management changes." in summary_file.read_text(encoding="utf-8")


@pytest.mark.parametrize("sha", ["", "a" * 39, "a" * 41, "A" * 40, "g" * 40, " " + "a" * 39, "a" * 40 + "\n", "main", "HEAD"])
def test_a_full_lowercase_commit_sha_is_required(tmp_path, azure, sha):
    with pytest.raises(ValueError, match="exact reviewed Git commit SHA"):
        run_main(tmp_path, "--apply", sha=sha)
    with pytest.raises(ValueError, match="exact reviewed Git commit SHA"):
        run_main(tmp_path, sha=sha)
    assert not azure.calls


def test_apply_requires_an_approved_plan_before_any_azure_call(tmp_path, azure, monkeypatch):
    monkeypatch.setenv("NORTHWIND_APIM_CONFIRM", SHA)
    with pytest.raises(ValueError, match="explicit confirmation"):
        run_main(tmp_path, "--apply")
    assert not azure.calls and not (tmp_path / "out").exists()


@pytest.mark.parametrize("confirmation", [None, "", "b" * 40, SHA[:12], SHA.upper(), SHA + "\n", " " + SHA])
def test_apply_requires_the_environment_to_repeat_the_reviewed_sha(tmp_path, azure, monkeypatch, confirmation):
    approved = approve(tmp_path)
    if confirmation is not None:
        monkeypatch.setenv("NORTHWIND_APIM_CONFIRM", confirmation)
    with pytest.raises(ValueError, match="explicit confirmation"):
        run_main(tmp_path, "--apply", "--approved-plan", str(approved))
    assert not azure.calls and not (tmp_path / "out").exists()


def test_confirming_through_the_infrastructure_variable_is_not_enough(tmp_path, azure, monkeypatch):
    monkeypatch.setenv("NORTHWIND_INFRA_CONFIRM", SHA)
    with pytest.raises(ValueError, match="explicit confirmation"):
        run_main(tmp_path, "--apply", "--approved-plan", str(approve(tmp_path)))
    assert not azure.calls


def mutate(plan, edit):
    plan = copy.deepcopy(plan)
    edit(plan)
    return plan


@pytest.mark.parametrize("approved_plan", [
    mutate(live_plan(), lambda p: p["changes"].pop()),
    mutate(live_plan(), lambda p: p["changes"].append(change("Create", rid("apis/northwind-api/operations/get_health")))),
    mutate(live_plan(), lambda p: p["changes"][-1]["after"]["properties"].update(value="a different property")),
    mutate(live_plan(), lambda p: p["changes"].__setitem__(-1, {**p["changes"][-1], "changeType": "Modify"})),
    whatif(*(change("Ignore", identifier) for identifier in EXISTING)),
])
def test_apply_refuses_when_the_fresh_plan_differs_from_the_reviewed_one(tmp_path, azure, monkeypatch, approved_plan):
    approved = approve(tmp_path, approved_plan)
    monkeypatch.setenv("NORTHWIND_APIM_CONFIRM", SHA)
    with pytest.raises(ValueError, match="changed after review"):
        run_main(tmp_path, "--apply", "--approved-plan", str(approved))
    assert azure.verbs == ["deployment group what-if"] and not (tmp_path / "out").exists()


def test_apply_refuses_an_approved_plan_for_a_different_commit_or_template(tmp_path, azure, monkeypatch):
    monkeypatch.setenv("NORTHWIND_APIM_CONFIRM", SHA)
    other_sha = approve(tmp_path, sha="b" * 40)
    with pytest.raises(ValueError, match="changed after review"):
        run_main(tmp_path, "--apply", "--approved-plan", str(other_sha))
    approved = approve(tmp_path)
    changed_template = template_file(tmp_path, {**compiled_template(), "variables": {"policy": "<policies><new/></policies>"}})
    with pytest.raises(ValueError, match="changed after review"):
        run_main(tmp_path, "--apply", "--approved-plan", str(approved), template=changed_template)
    assert azure.verbs == ["deployment group what-if"] * 2


def test_apply_refuses_tampered_envelopes(tmp_path, azure, monkeypatch):
    monkeypatch.setenv("NORTHWIND_APIM_CONFIRM", SHA)
    envelope = json.loads(approve(tmp_path).read_text(encoding="utf-8"))
    for edit in (lambda e: e.update(applied=True), lambda e: e.update(extra="x"), lambda e: e.pop("plan_sha256"),
                 lambda e: e["changes"].pop(), lambda e: e.update(resource_group="rg-northwind-data"),
                 lambda e: e.update(subscription="0" * 8)):
        candidate = copy.deepcopy(envelope)
        edit(candidate)
        path = tmp_path / "tampered.json"
        path.write_text(json.dumps(candidate), encoding="utf-8")
        with pytest.raises(ValueError, match="changed after review"):
            run_main(tmp_path, "--apply", "--approved-plan", str(path))
    assert "deployment group create" not in azure.verbs


@pytest.mark.parametrize("content", [None, "not json", "", "[]", "null", '"approved"', "{}"])
def test_apply_refuses_a_missing_or_unreadable_approved_plan(tmp_path, azure, monkeypatch, content):
    approved = tmp_path / "missing.json"
    if content is not None:
        approved.write_text(content, encoding="utf-8")
    monkeypatch.setenv("NORTHWIND_APIM_CONFIRM", SHA)
    with pytest.raises(ValueError, match="missing or unreadable|changed after review"):
        run_main(tmp_path, "--apply", "--approved-plan", str(approved))
    assert azure.verbs == ["deployment group what-if"]


def test_apply_deploys_incrementally_after_a_fresh_plan_then_verifies_the_apis(tmp_path, azure, monkeypatch):
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    monkeypatch.setenv("NORTHWIND_APIM_CONFIRM", SHA)
    approved = approve(tmp_path)
    result = run_main(tmp_path, "--apply", "--approved-plan", str(approved))
    assert azure.verbs == ["deployment group what-if", "deployment group create", "rest"]
    create = azure.calls[1]
    assert create[create.index("--mode") + 1] == "Incremental"
    assert create[create.index("--subscription") + 1] == release.SUBSCRIPTION
    assert create[create.index("--resource-group") + 1] == "rg-northwind-apps"
    assert create[create.index("--name") + 1] == f"northwind-apim-{SHA[:12]}"
    assert create[create.index("--template-file") + 1].endswith("apim-apis.json")
    assert not any(argument.startswith("--parameters") or argument == "--what-if" for argument in create)
    assert azure.calls[2][:4] == ("rest", "--method", "get", "--url") and azure.calls[2][4] == release.APIS_URL
    assert result["applied"] is True and result["verified_apis"] == sorted(release.API_NAMES)
    assert len(result["verified_apis"]) == 8
    without_apply_fields = {k: v for k, v in result.items() if k not in {"applied", "verified_apis"}}
    assert without_apply_fields == json.loads(approved.read_text(encoding="utf-8"))
    markdown = summary_file.read_text(encoding="utf-8")
    assert markdown.startswith("## API Management apply") and "Result: applied" in markdown
    assert "`northwind-api`" in markdown and "`insurance-mcp-work-order`" in markdown
    assert SECRET not in markdown and SECRET not in json.dumps(result)


def test_apply_accepts_a_fresh_plan_listed_in_a_different_order(tmp_path, monkeypatch):
    reordered = live_plan()
    reordered["changes"].reverse()
    fake = FakeAz(reordered)
    monkeypatch.setattr(release, "command", fake)
    monkeypatch.setenv("NORTHWIND_APIM_CONFIRM", SHA)
    result = run_main(tmp_path, "--apply", "--approved-plan", str(approve(tmp_path)))
    assert result["applied"] is True and fake.verbs[1] == "deployment group create"


def test_apply_never_deploys_when_the_fresh_plan_contains_an_unsafe_change(tmp_path, monkeypatch):
    approved = approve(tmp_path)
    monkeypatch.setenv("NORTHWIND_APIM_CONFIRM", SHA)
    for unsafe in (change("Delete", rid("apis/northwind-api")), change("Create", rid("policies/policy")),
                   change("Modify", SERVICE), {"changeType": "Deploy", "resourceId": rid("apis/northwind-api")}):
        fake = FakeAz(mutate(live_plan(), lambda p: p["changes"].append(unsafe)))
        monkeypatch.setattr(release, "command", fake)
        with pytest.raises(ValueError, match="unsupported change"):
            run_main(tmp_path, "--apply", "--approved-plan", str(approved))
        assert fake.verbs == ["deployment group what-if"]


def test_apply_never_runs_azure_when_the_template_leaves_the_boundary(tmp_path, azure, monkeypatch):
    monkeypatch.setenv("NORTHWIND_APIM_CONFIRM", SHA)
    template = compiled_template()
    template["resources"].append({"type": "Microsoft.ApiManagement/service", "apiVersion": "2024-05-01", "name": "x"})
    with pytest.raises(ValueError, match="service must be referenced as existing"):
        run_main(tmp_path, "--apply", "--approved-plan", str(approve(tmp_path)), template=template_file(tmp_path, template))
    assert not azure.calls
    with pytest.raises(ValueError, match="service must be referenced as existing"):
        run_main(tmp_path, template=template_file(tmp_path, template))
    assert not azure.calls


def test_failed_verification_fails_the_run_but_keeps_evidence_that_the_deployment_ran(tmp_path, monkeypatch):
    fake = FakeAz(apis=list(release.API_NAMES[:7]))
    monkeypatch.setattr(release, "command", fake)
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    monkeypatch.setenv("NORTHWIND_APIM_CONFIRM", SHA)
    with pytest.raises(RuntimeError, match="insurance-mcp-work-order"):
        run_main(tmp_path, "--apply", "--approved-plan", str(approve(tmp_path)))
    assert fake.verbs == ["deployment group what-if", "deployment group create", "rest"]
    recorded = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    assert recorded["applied"] is True and "verified_apis" not in recorded
    assert "not confirmed" in summary_file.read_text(encoding="utf-8")


def test_a_failed_deployment_leaves_no_applied_record(tmp_path, monkeypatch):
    class Failing(FakeAz):
        def __call__(self, *args):
            if args[:3] == ("deployment", "group", "create"):
                self.calls.append(args)
                raise RuntimeError("Azure operation failed. No successful deployment is implied.")
            return super().__call__(*args)

    fake = Failing()
    monkeypatch.setattr(release, "command", fake)
    monkeypatch.setenv("NORTHWIND_APIM_CONFIRM", SHA)
    with pytest.raises(RuntimeError, match="No successful deployment is implied"):
        run_main(tmp_path, "--apply", "--approved-plan", str(approve(tmp_path)))
    assert fake.verbs == ["deployment group what-if", "deployment group create"]
    assert not (tmp_path / "out").exists()


def test_the_approved_plan_is_ignored_when_not_applying(tmp_path, azure):
    run_main(tmp_path, "--approved-plan", str(tmp_path / "does-not-exist.json"))
    assert azure.verbs == ["deployment group what-if"]


def test_target_constants_cannot_be_overridden_from_the_command_line(tmp_path, azure):
    for option in ("--resource-group", "--subscription", "--apim-name", "--parameters"):
        with pytest.raises(SystemExit):
            release.main(["--compiled-template", "x", "--output", "y", "--source-sha", SHA, option, "other"])
    assert not azure.calls


def test_boundary_constants_are_pinned():
    assert (release.SUBSCRIPTION, release.GROUP, release.APIM_NAME) == (
        "137c905b-7734-479e-acc5-ab8222462936", "rg-northwind-apps", "apim-northwind")
    assert release.TEMPLATE == ROOT / "infra" / "live" / "apim-apis.bicep"
    assert release.__doc__.strip() == (
        "Plan or explicitly apply only the approved API Management configuration boundary for apim-northwind.")


# ---------------------------------------------------------------- workflows


def workflow(name):
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def steps_text(job):
    return json.dumps(job.get("steps", []))


def uses(flow):
    return {step["uses"] for job in flow["jobs"].values() for step in job.get("steps", []) if "uses" in step}


def test_apim_workflow_is_gated_and_never_applies_from_a_push_or_a_workflow_run():
    flow = workflow("deploy-apim.yml")
    jobs = flow["jobs"]
    triggers = flow.get("on", flow.get(True))
    assert flow["name"] == "Scoped API Management"
    assert triggers["workflow_run"] == {"workflows": ["CI"], "branches": ["main"], "types": ["completed"]}
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"operation", "expected_sha", "plan_run_id", "confirm_apply"}
    assert inputs["operation"]["options"] == ["plan", "apply"] and inputs["operation"]["default"] == "plan"
    assert inputs["expected_sha"]["required"] is True
    assert set(triggers) == {"workflow_run", "workflow_dispatch"}
    assert flow["permissions"] == {"contents": "read", "actions": "read"}
    assert flow["concurrency"] == {"group": "northwind-apim-${{ github.ref }}", "cancel-in-progress": False}
    assert set(jobs) == {"source", "plan", "apply", "smoke"}
    gate = jobs["source"]["if"]
    for fragment in ("github.repository == 'ZacharyZurloMSFT/foundry-frontier'", "vars.NORTHWIND_APIM_ENABLED == 'true'",
                     "github.event.workflow_run.conclusion == 'success'", "github.event.workflow_run.event == 'push'",
                     "github.event.workflow_run.head_repository.id == github.repository_id"):
        assert fragment in gate
    assert "NORTHWIND_INFRA_ENABLED" not in json.dumps(flow) and "NORTHWIND_DEPLOY_ENABLED" not in json.dumps(flow)
    assert "workflow_dispatch" in jobs["apply"]["if"] and "'apply'" in jobs["apply"]["if"]
    assert "'plan'" in jobs["plan"]["if"] and "workflow_dispatch" not in jobs["plan"]["if"]
    assert not any("azd " in str(step.get("run", "")) for job in jobs.values() for step in job.get("steps", []))


def test_apim_source_job_re_verifies_main_ci_and_the_same_source_plan_run():
    jobs = workflow("deploy-apim.yml")["jobs"]
    source = json.dumps(jobs["source"])
    for fragment in ("getBranch", "listWorkflowRuns", "getWorkflowRun", "refs/heads/main", "^[a-f0-9]{40}$",
                     "sha !== main", "workflow_id:'ci.yml'", "process.env.CONFIRM !== sha",
                     ".github/workflows/deploy-apim.yml", "run.head_sha !== sha", "run.conclusion !== 'success'",
                     "run.head_branch !== 'main'"):
        assert fragment in source
    assert "deploy-infra.yml" not in source
    assert "id-token" not in jobs["source"].get("permissions", {}) and "azure/login" not in source
    assert jobs["source"]["outputs"] == {"sha": "${{ steps.source.outputs.sha }}",
                                          "operation": "${{ steps.source.outputs.operation }}"}


def step(job, action):
    matches = [item for item in job["steps"] if str(item.get("uses", "")).startswith(action)]
    assert len(matches) == 1, action
    return matches[0]


def test_apim_plan_and_apply_use_separate_environments_identities_and_artifacts():
    jobs = workflow("deploy-apim.yml")["jobs"]
    plan, apply = jobs["plan"], jobs["apply"]
    assert plan["needs"] == apply["needs"] == "source"
    assert plan["environment"] == "northwind-apim-plan" and apply["environment"] == "northwind-apim-apply"
    assert plan["environment"] != apply["environment"]
    assert step(plan, "azure/login")["with"]["client-id"] == "${{ vars.AZURE_APIM_PLAN_CLIENT_ID }}"
    assert step(apply, "azure/login")["with"]["client-id"] == "${{ vars.AZURE_APIM_APPLY_CLIENT_ID }}"
    assert "AZURE_INFRA" not in json.dumps(jobs) and "AZURE_APPS_CLIENT_ID" not in json.dumps(jobs)
    assert plan["permissions"] == {"contents": "read", "id-token": "write"}
    assert apply["permissions"] == {"contents": "read", "actions": "read", "id-token": "write"}
    assert apply["concurrency"] == {"group": "northwind-live-mutations", "cancel-in-progress": False}
    assert "concurrency" not in plan
    assert "--apply" not in steps_text(plan) and "NORTHWIND_APIM_CONFIRM" not in steps_text(plan)
    assert "--approved-plan approved/apim-plan.json" in steps_text(apply) and "--apply" in steps_text(apply)
    assert "NORTHWIND_APIM_CONFIRM" in steps_text(apply) and "${{ inputs.confirm_apply }}" in steps_text(apply)
    assert "getBranch" in steps_text(apply) and "Source changed while waiting" in steps_text(apply)
    assert step(plan, "actions/upload-artifact")["with"] == {
        "name": "apim-plan-${{ needs.source.outputs.sha }}", "path": "release/apim-plan.json",
        "retention-days": 14, "if-no-files-found": "error",
    }
    assert step(apply, "actions/download-artifact")["with"] == {
        "name": "apim-plan-${{ needs.source.outputs.sha }}", "run-id": "${{ inputs.plan_run_id }}",
        "github-token": "${{ github.token }}", "path": "approved",
    }
    result = step(apply, "actions/upload-artifact")
    assert result["if"] == "always()" and result["with"]["name"] == "apim-result-${{ github.run_id }}"
    assert result["with"]["path"] == "release/apim-result.json"


def test_apim_jobs_compile_and_run_only_the_apim_boundary():
    jobs = workflow("deploy-apim.yml")["jobs"]
    for name in ("plan", "apply"):
        text = steps_text(jobs[name])
        assert "az bicep install --version v0.46.1" in text
        assert "az bicep build --file infra/live/apim-apis.bicep --outfile /tmp/apim-apis.json" in text
        assert "python scripts/apim-release.py --compiled-template /tmp/apim-apis.json" in text
        assert '--source-sha \\"$SOURCE_SHA\\"' in text
        assert "storage-networking" not in text and "infra-release" not in text
    assert "--output release/apim-plan.json" in steps_text(jobs["plan"])
    assert "--output release/apim-result.json" in steps_text(jobs["apply"])


def test_apim_smoke_runs_only_after_a_successful_apply_without_credentials():
    jobs = workflow("deploy-apim.yml")["jobs"]
    smoke = jobs["smoke"]
    assert smoke["needs"] == ["source", "apply"] and smoke["if"] == "needs.apply.result == 'success'"
    assert smoke["permissions"] == {"contents": "read"} and "environment" not in smoke
    text = steps_text(smoke)
    assert "python scripts/apim-smoke.py --gateway-url https://apim-northwind.azure-api.net --public-checks" in text
    assert "azure/login" not in text and "id-token" not in json.dumps(smoke) and "secrets." not in text
    assert "vars." not in text and "needs.source.outputs.sha" in text


def test_apim_workflow_needs_and_environments_are_consistent_with_the_oidc_setup():
    jobs = workflow("deploy-apim.yml")["jobs"]
    for job in jobs.values():
        needs = job.get("needs", [])
        assert set([needs] if isinstance(needs, str) else needs) <= set(jobs)
    assert jobs["plan"]["environment"] == oidc.IDENTITIES["apim-plan"][1]
    assert jobs["apply"]["environment"] == oidc.IDENTITIES["apim-apply"][1]
    assert oidc.IDENTITIES["apim-plan"][2] in steps_text(jobs["plan"])
    assert oidc.IDENTITIES["apim-apply"][2] in steps_text(jobs["apply"])
    assert oidc.static_variables("apim") == {"NORTHWIND_APIM_ENABLED": "true"}
    assert "NORTHWIND_APIM_ENABLED" in jobs["source"]["if"]


def test_apim_workflow_uses_only_the_action_versions_of_the_infrastructure_workflow():
    apim, infra = workflow("deploy-apim.yml"), workflow("deploy-infra.yml")
    assert uses(apim) <= uses(infra)
    for job in apim["jobs"].values():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/checkout"):
                assert step["with"]["persist-credentials"] is False


def test_ci_compiles_the_apim_template_and_runs_the_gateway_contract_tests():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "az bicep build --file infra/live/apim-apis.bicep --outfile /tmp/apim-apis.json" in text
    for name in ("test_apim_release.py", "test_apim_definition.py", "test_export_openapi.py", "test_gateway_apps.py",
                 "test_apim_smoke.py", "test_gateway_identity.py", "test_oidc_apim.py", "test_actions_infra.py"):
        assert f"scripts/tests/{name}" in text


# ---------------------------------------------------------------- real template (optional)


def bicep_executable():
    if not release.TEMPLATE.exists():
        pytest.skip("infra/live/apim-apis.bicep is not present")
    bicep = shutil.which("bicep")
    if not bicep:
        path = Path.home() / ".azure" / "bin" / ("bicep.exe" if os.name == "nt" else "bicep")
        if not path.exists():
            pytest.skip("Existing Bicep CLI is required for compiled-template tests")
        bicep = str(path)
    return bicep


@pytest.fixture(scope="module")
def compiled_text():
    result = subprocess.run([bicep_executable(), "build", str(release.TEMPLATE), "--stdout"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture(scope="module")
def live_template(compiled_text):
    return json.loads(compiled_text)


def test_live_template_compiles_inside_the_management_boundary(live_template):
    release.validate_template(live_template)
    resources = live_template["resources"]
    assert isinstance(resources, list) and resources
    assert {r["type"].lower() for r in resources} <= release.RESOURCE_TYPES
    assert not any(r["type"].lower() == release.SERVICE_TYPE.lower() for r in resources)
    assert set(live_template["parameters"]) == set(release.PARAMETER_CHECKS)


def test_live_template_defaults_name_exactly_the_approved_stacks(live_template):
    defaults = live_template["parameters"]["stacks"]["defaultValue"]
    assert [item["name"] for item in defaults] == list(release.STACKS)
    assert all(set(item) == set(release.STACK_KEYS) for item in defaults)
    assert live_template["parameters"]["apimName"]["defaultValue"] == release.APIM_NAME
    assert live_template["parameters"]["appInsightsName"]["defaultValue"] == release.APP_INSIGHTS_NAME


def test_live_template_compiles_deterministically(compiled_text):
    again = subprocess.run([bicep_executable(), "build", str(release.TEMPLATE), "--stdout"],
                           capture_output=True, text=True, check=False)
    assert again.returncode == 0 and again.stdout == compiled_text


@pytest.mark.parametrize("mutation", ["service", "backend", "global_policy", "extra_stack", "origin", "audience", "app_name"])
def test_live_template_changes_cannot_silently_expand_the_boundary(live_template, mutation):
    candidate = copy.deepcopy(live_template)
    parameters = candidate["parameters"]
    if mutation == "service":
        candidate["resources"].append({"type": "Microsoft.ApiManagement/service", "apiVersion": "2024-05-01", "name": "x"})
    elif mutation == "backend":
        candidate["resources"].append({"type": "Microsoft.ApiManagement/service/backends", "name": "x"})
    elif mutation == "global_policy":
        candidate["resources"].append({"type": "Microsoft.ApiManagement/service/policies", "name": "x"})
    elif mutation == "extra_stack":
        parameters["stacks"]["defaultValue"].append({**parameters["stacks"]["defaultValue"][0], "name": "contoso"})
    elif mutation == "origin":
        parameters["extraAllowedOrigins"]["defaultValue"] = ["*"]
    elif mutation == "audience":
        parameters["gatewayAudience"]["defaultValue"] = "api://not-a-guid"
    else:
        parameters["stacks"]["defaultValue"][0]["apiApp"] = "other-api"
    with pytest.raises(ValueError):
        release.validate_template(candidate)
