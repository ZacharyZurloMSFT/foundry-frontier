import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location(
    "validate_reuse", ROOT / "scripts" / "validate-reuse.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
SERVICES = {
    "api": "ca-insurance-api",
    "mcp-policy-lookup": "ca-insurance-lookup",
    "mcp-claim-review": "ca-insurance-review",
    "web-portal": "ca-insurance-portal",
    "web-voice": "ca-insurance-voice",
}
GREENFIELD_MODULES = {
    "observability", "uami", "acr", "vnet", "storage", "search", "cosmos",
    "cosmos-private-endpoint", "foundry", "foundry-observability", "search-role",
    "containerapps",
}


LIVE_PROJECT = "nw-insurance-claims"


def valid_environment(project=LIVE_PROJECT):
    env = {key: "preserved" for key in module.PRESERVED_KEYS}
    endpoint = f"https://aisvc-zurlo-northwind.services.ai.azure.com/api/projects/{project}"
    env.update({
        "NORTHWIND_REUSE_EXISTING_RESOURCES": "true",
        "AZURE_RESOURCE_GROUP": "rg-northwind-ai",
        "AZURE_APPS_RESOURCE_GROUP": "rg-northwind-apps",
        "AZURE_DATA_RESOURCE_GROUP": "rg-northwind-data",
        "AZURE_NETWORKING_RESOURCE_GROUP": "rg-northwind-networking",
        "AZURE_AI_RESOURCE_GROUP": "rg-northwind-ai",
        "AZURE_FOUNDRY_RESOURCE_GROUP": "rg-northwind-ai",
        "AZURE_OBSERVABILITY_RESOURCE_GROUP": "rg-northwind-apps",
        "AZURE_SUBSCRIPTION_ID": "137c905b-7734-479e-acc5-ab8222462936",
        "AZURE_AI_PROJECT_ID": (
            "/subscriptions/137c905b-7734-479e-acc5-ab8222462936"
            "/resourceGroups/rg-northwind-ai/providers/Microsoft.CognitiveServices"
            f"/accounts/aisvc-zurlo-northwind/projects/{project}"
        ),
        "INSURANCE_PROJECT_NAME": project,
        "HOSTED_AGENTS_ENABLED": "true",
        "FOUNDRY_PROJECT_ENDPOINT": endpoint,
        "AZURE_AI_PROJECT_ENDPOINT": endpoint,
        "FOUNDRY_PROJECT_ENDPOINTS": json.dumps({
            "claims-concierge": endpoint,
            "adjuster-support": endpoint.replace(project, "nw-insurance-support"),
            "claims-documents": endpoint.replace(project, "nw-insurance-documents"),
            "contact-center": endpoint.replace(project, "nw-insurance-contact-center"),
        }),
    })
    return env


@pytest.fixture(scope="module")
def template():
    return json.loads((ROOT / "infra" / "main.json").read_text(encoding="utf-8"))


def compile_parameters(values):
    bicep = shutil.which("bicep")
    if bicep is None:
        installed = Path.home() / ".azure" / "bin" / ("bicep.exe" if os.name == "nt" else "bicep")
        if not installed.is_file():
            pytest.skip("Bicep is not installed")
        bicep = str(installed)
    source = ROOT / "infra" / "main.bicepparam"
    keys = re.findall(r"readEnvironmentVariable\('([^']+)'", source.read_text())
    env = {key: value for key, value in os.environ.items() if key not in keys}
    env.update({"AZURE_ENV_NAME": "synthetic-parameter-test", **values})
    result = subprocess.run(
        [bicep, "build-params", str(source), "--stdout"],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stderr
    compiled = json.loads(result.stdout)
    if "parametersJson" in compiled:
        compiled = json.loads(compiled["parametersJson"])
    return compiled["parameters"]


@pytest.fixture(scope="module")
def parameter_defaults():
    return compile_parameters({})


@pytest.mark.parametrize("formatting", [
    {}, {"indent": 2}, {"separators": (",", ":"), "ensure_ascii": False},
])
def test_bicepparam_roundtrips_structured_endpoint_string_without_changing_raw_inputs(formatting):
    env = valid_environment()
    endpoints = json.loads(env["FOUNDRY_PROJECT_ENDPOINTS"])
    endpoints["synthetic-note"] = 'Quoted "value", backslash \\, newline\nUnicode é and ${NOT_AN_ENV_LOOKUP}'
    raw = json.dumps(endpoints, **formatting)
    env["FOUNDRY_PROJECT_ENDPOINTS"] = raw
    original = env.copy()
    assert module.validate_environment(env) == []
    parameters = compile_parameters(env)
    preserved = parameters["existingSettings"]["value"]
    assert preserved["FOUNDRY_PROJECT_ENDPOINTS"] == raw
    assert json.loads(preserved["FOUNDRY_PROJECT_ENDPOINTS"]) == endpoints
    assert all(preserved[key] == env[key] for key in module.PRESERVED_KEYS)
    assert parameters["reuseExistingResources"]["value"] == "true"
    assert parameters["foundryResourceGroupName"]["value"] == "rg-northwind-ai"
    assert env == original
    assert not (ROOT / "infra" / "main.parameters.json").exists()


def adoption_template(template):
    return next(
        resource["properties"]["template"]
        for resource in template["resources"]
        if resource["name"] == "reuse-existing"
    )


def test_business_api_is_opt_in_and_identity_configuration_roundtrips(parameter_defaults):
    assert parameter_defaults["businessApiEnabled"]["value"] == "false"
    assert parameter_defaults["insuranceAuthTenantId"]["value"] == ""
    parameters = compile_parameters({
        "NORTHWIND_BUSINESS_API_ENABLED": "true",
        "INSURANCE_AUTH_TENANT_ID": "11111111-1111-4111-8111-111111111111",
        "INSURANCE_AUTH_AUDIENCE": "22222222-2222-4222-8222-222222222222",
        "INSURANCE_AUTH_REQUIRED_SCOPE": "access_as_user",
        "INSURANCE_AUTH_CLIENT_IDS": "33333333-3333-4333-8333-333333333333",
    })
    assert parameters["businessApiEnabled"]["value"] == "true"
    assert parameters["insuranceAuthTenantId"]["value"] == "11111111-1111-4111-8111-111111111111"
    assert parameters["insuranceAuthAudience"]["value"] == "22222222-2222-4222-8222-222222222222"
    assert parameters["insuranceAuthClientIds"]["value"] == "33333333-3333-4333-8333-333333333333"


def test_customer_assistants_are_opt_in_and_do_not_change_reuse_resources(template, parameter_defaults):
    assert parameter_defaults["assistantsEnabled"]["value"] == "false"
    assert parameter_defaults["assistantSurfaces"]["value"] == ""
    assert parameter_defaults["assistantModel"]["value"] == ""
    configured = compile_parameters({
        "NORTHWIND_ASSISTANTS_ENABLED": "true", "NORTHWIND_ASSISTANT_SURFACES": "concierge,help_center",
        "NORTHWIND_ASSISTANT_MODEL": "gpt-5.4",
    })
    assert configured["assistantsEnabled"]["value"] == "true"
    assert configured["assistantSurfaces"]["value"] == "concierge,help_center"
    assert configured["assistantModel"]["value"] == "gpt-5.4"
    module = next(resource for resource in template["resources"] if resource["name"] == "containerapps")
    assert module["condition"] == "[not(variables('reuseExisting'))]"
    assert module["properties"]["parameters"]["assistantsEnabled"]["value"] == (
        "[equals(parameters('assistantsEnabled'), 'true')]"
    )
    nested = module["properties"]["template"]
    api = next(resource for resource in nested["resources"] if resource["name"] == "ca-insurance-api")
    env = api["properties"]["template"]["containers"][0]["env"]
    assert all(name in json.dumps(env) for name in (
        "NORTHWIND_ASSISTANTS_ENABLED", "NORTHWIND_ASSISTANT_SURFACES", "NORTHWIND_ASSISTANT_MODEL"))
    assert "NORTHWIND_ASSISTANTS_ENABLED" not in json.dumps(adoption_template(template))


def test_fresh_mode_requires_no_adoption_inputs():
    assert module.validate_environment({}) == []
    assert module.validate_environment({"NORTHWIND_REUSE_EXISTING_RESOURCES": "false"}) == []


@pytest.mark.parametrize("mode", ["", "TRUE", "False", "yes"])
def test_mode_must_be_an_explicit_lowercase_boolean(mode):
    errors = module.validate_environment({"NORTHWIND_REUSE_EXISTING_RESOURCES": mode})
    assert errors and "exactly true or false" in errors[0]


def test_reuse_preserves_insurance_routing_with_independent_workspace_location():
    env = valid_environment()
    assert module.validate_environment(env) == []
    env["AZURE_LOG_ANALYTICS_RESOURCE_GROUP"] = "rg-northwind-dev"
    assert module.validate_environment(env) == []


@pytest.mark.parametrize("explicit_transitional_groups", [False, True])
def test_moved_foundry_does_not_require_moving_app_environment_or_vnet(explicit_transitional_groups):
    env = valid_environment()
    if explicit_transitional_groups:
        env.update({
            "AZURE_CONTAINER_APPS_ENVIRONMENT_RESOURCE_GROUP": "rg-northwind-dev",
            "AZURE_VNET_RESOURCE_GROUP": "rg-northwind-dev",
            "AZURE_COSMOS_PRIVATE_ENDPOINT_NAME": "pe-cosmos-zurlo-northwind",
        })
    original = env.copy()
    assert module.validate_environment(env) == []
    assert env == original


def test_replacement_environment_vnet_and_endpoint_allow_explicit_selections():
    env = valid_environment()
    env.update({
        "AZURE_CONTAINER_APPS_ENVIRONMENT_RESOURCE_GROUP": "rg-northwind-apps",
        "AZURE_VNET_RESOURCE_GROUP": "rg-northwind-networking",
        "AZURE_COSMOS_PRIVATE_ENDPOINT_NAME": "pe-zz-cosmos-northwind",
    })
    assert module.validate_environment(env) == []


@pytest.mark.parametrize("legacy_label", [None, "", "rg-northwind-dev"])
@pytest.mark.parametrize("ready", ["false", "true"])
def test_cli_defaults_to_replacement_stack_without_requiring_or_using_legacy_label(
    legacy_label, ready, monkeypatch, capsys,
):
    env = valid_environment()
    env["NORTHWIND_INSURANCE_AGENTS_READY"] = ready
    if ready == "true":
        env["FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID"] = "northwind-insurance-orchestrator:1"
    if legacy_label is not None:
        env["AZURE_LEGACY_RESOURCE_GROUP"] = legacy_label
    original = env.copy()
    monkeypatch.setattr(module.os, "environ", env)
    assert module.main() == 0
    assert env == original
    output = capsys.readouterr().out
    assert "Agent target: rg-northwind-ai; existing app deploy target: rg-northwind-apps" in output
    assert "environment rg-northwind-apps; VNet rg-northwind-networking" in output
    assert "Cosmos private endpoint pe-zz-cosmos-northwind; Log Analytics rg-northwind-apps" in output
    assert "rg-northwind-dev" not in output
    assert "do not migrate app attachments or network dependencies" in output


def test_cli_honors_transitional_overrides_without_a_legacy_label(monkeypatch, capsys):
    env = valid_environment()
    env.update({
        "AZURE_CONTAINER_APPS_ENVIRONMENT_RESOURCE_GROUP": "rg-northwind-dev",
        "AZURE_VNET_RESOURCE_GROUP": "rg-northwind-dev",
        "AZURE_COSMOS_PRIVATE_ENDPOINT_NAME": "pe-cosmos-zurlo-northwind",
        "AZURE_LOG_ANALYTICS_RESOURCE_GROUP": "rg-northwind-dev",
    })
    monkeypatch.setattr(module.os, "environ", env)
    assert module.main() == 0
    output = capsys.readouterr().out
    assert "Agent target: rg-northwind-ai; existing app deploy target: rg-northwind-apps" in output
    assert "environment rg-northwind-dev; VNet rg-northwind-dev" in output
    assert "Cosmos private endpoint pe-cosmos-zurlo-northwind; Log Analytics rg-northwind-dev" in output
    assert "do not migrate app attachments or network dependencies" in output


def test_cli_rejects_stale_project_id_before_reporting_success(monkeypatch, capsys):
    env = valid_environment()
    env["AZURE_AI_PROJECT_ID"] = env["AZURE_AI_PROJECT_ID"].replace("rg-northwind-ai", "rg-northwind-dev")
    monkeypatch.setattr(module.os, "environ", env)
    assert module.main() == 1
    captured = capsys.readouterr()
    assert "AZURE_AI_PROJECT_ID must match" in captured.err
    assert not captured.out


@pytest.mark.parametrize(
    "key", module.GROUP_KEYS + module.PRESERVED_KEYS + module.IDENTITY_KEYS
)
def test_reuse_rejects_missing_inputs(key):
    env = valid_environment()
    del env[key]
    assert key in " ".join(module.validate_environment(env))
    env[key] = " "
    assert key in " ".join(module.validate_environment(env))


def test_foundry_cannot_accidentally_resolve_in_apps():
    env = valid_environment()
    env["AZURE_RESOURCE_GROUP"] = "rg-northwind-apps"
    assert any("do not route Foundry to Apps" in error for error in module.validate_environment(env))


def test_agent_target_cannot_remain_in_legacy_after_foundry_moved():
    env = valid_environment()
    env["AZURE_RESOURCE_GROUP"] = "rg-northwind-dev"
    assert any("agent target" in error for error in module.validate_environment(env))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("rg-northwind-ai", "rg-northwind-dev"),
        ("137c905b-7734-479e-acc5-ab8222462936", "00000000-0000-0000-0000-000000000000"),
        ("aisvc-zurlo-northwind", "aisvc-other"),
        ("nw-insurance-claims", "nw-insurance-support"),
        ("/projects/", "/wrongType/"),
    ],
)
def test_project_id_must_match_configured_account_subscription_group_and_project(old, new):
    env = valid_environment()
    env["AZURE_AI_PROJECT_ID"] = env["AZURE_AI_PROJECT_ID"].replace(old, new)
    assert any("AZURE_AI_PROJECT_ID must match" in error for error in module.validate_environment(env))


def test_arm_id_and_group_comparisons_are_case_insensitive():
    env = valid_environment()
    env["AZURE_RESOURCE_GROUP"] = env["AZURE_RESOURCE_GROUP"].upper()
    env["AZURE_AI_PROJECT_ID"] = env["AZURE_AI_PROJECT_ID"].upper()
    assert module.validate_environment(env) == []


@pytest.mark.parametrize("key", module.OPTIONAL_GROUP_KEYS)
@pytest.mark.parametrize("value", ["", " ", " rg-northwind-apps", "rg-northwind-apps/other"])
def test_optional_resource_group_selections_cannot_silently_fall_back(key, value):
    env = valid_environment()
    env[key] = value
    assert any(key in error for error in module.validate_environment(env))


@pytest.mark.parametrize("value", [" ", " rg-northwind-dev", "rg-northwind-dev/other"])
def test_nonempty_legacy_compatibility_label_still_requires_a_group_name(value):
    env = valid_environment()
    env["AZURE_LEGACY_RESOURCE_GROUP"] = value
    assert any("AZURE_LEGACY_RESOURCE_GROUP" in error for error in module.validate_environment(env))


@pytest.mark.parametrize("value", ["", " ", "pe-zz/cosmos", "pe-zz-cosmos "])
def test_private_endpoint_selection_cannot_be_blank_or_an_id(value):
    env = valid_environment()
    env["AZURE_COSMOS_PRIVATE_ENDPOINT_NAME"] = value
    assert any("AZURE_COSMOS_PRIVATE_ENDPOINT_NAME" in error for error in module.validate_environment(env))


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("AZURE_LOG_ANALYTICS_RESOURCE_GROUP", "", "cannot be blank"),
        ("HOSTED_AGENTS_ENABLED", "yes", "exactly true or false"),
        ("AZURE_AI_PROJECT_ENDPOINT", "https://wrong.example/api/projects/northwind", "must agree"),
        ("INSURANCE_PROJECT_NAME", "northwind", "retain INSURANCE_PROJECT_NAME"),
        ("AZURE_SUBSCRIPTION_ID", "not-a-guid", "subscription GUID"),
        ("FOUNDRY_PROJECT_ENDPOINT", "https://other.services.ai.azure.com/api/projects/northwind-orders", "existing Foundry account"),
        ("FOUNDRY_PROJECT_ENDPOINT", "https://[", "existing Foundry account"),
        ("FOUNDRY_PROJECT_ENDPOINTS", "[]", "all four department"),
        ("FOUNDRY_PROJECT_ENDPOINTS", "{", "JSON object"),
        ("FOUNDRY_PROJECT_ENDPOINTS", "{}", "all four department"),
    ],
)
def test_reuse_rejects_inconsistent_inputs(key, value, message):
    env = valid_environment()
    env[key] = value
    assert any(message in error for error in module.validate_environment(env))


def test_department_map_cannot_change_the_insurance_target():
    env = valid_environment()
    endpoints = json.loads(env["FOUNDRY_PROJECT_ENDPOINTS"])
    endpoints["claims-concierge"] = endpoints["claims-documents"]
    env["FOUNDRY_PROJECT_ENDPOINTS"] = json.dumps(endpoints)
    assert any("must resolve the insurance project" in error for error in module.validate_environment(env))


def test_department_map_cannot_route_a_specialist_to_another_foundry_account():
    env = valid_environment()
    endpoints = json.loads(env["FOUNDRY_PROJECT_ENDPOINTS"])
    endpoints["claims-documents"] = endpoints["claims-documents"].replace(
        "aisvc-zurlo-northwind", "aisvc-other"
    )
    env["FOUNDRY_PROJECT_ENDPOINTS"] = json.dumps(endpoints)
    assert any("All department endpoints" in error for error in module.validate_environment(env))


def test_existing_project_endpoint_trailing_slashes_are_preserved():
    env = valid_environment()
    env["FOUNDRY_PROJECT_ENDPOINT"] += "/"
    original = env.copy()
    assert module.validate_environment(env) == []
    assert env == original


def test_all_five_apps_have_explicit_routing_and_no_azd_hosted_agent_services():
    config = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
    assert set(config["services"]) == set(SERVICES)
    for name, service in config["services"].items():
        assert service["host"] == "containerapp"
        assert service["resourceGroup"] == "${AZURE_APPS_RESOURCE_GROUP}"
        assert service["resourceName"] == SERVICES[name]
    assert not [service for service in config["services"].values() if service["host"] == "azure.ai.agent"]


def test_azure_yaml_never_references_the_legacy_resource_group_or_a_business_domain():
    config = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
    serialized = json.dumps(config)
    assert "rg-northwind-dev" not in serialized
    assert "NORTHWIND_BUSINESS_DOMAIN" not in serialized


def test_azure_yaml_hooks_only_run_scripts_that_exist():
    config = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
    hooks = list(config["hooks"].values())
    for service in config["services"].values():
        hooks.extend(service.get("hooks", {}).values())
    referenced = set()
    for hook in hooks:
        for platform in ("windows", "posix"):
            referenced.update(re.findall(r"\./scripts/[\w.-]+", hook[platform]["run"]))
    assert "./scripts/validate-reuse.py" in referenced
    assert {path for path in referenced if not (ROOT / path).is_file()} == set()
    assert "postprovision" not in config["hooks"]


def test_deploy_and_provision_both_validate_reuse_before_any_changes():
    config = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
    for hook in ("predeploy", "preprovision"):
        for platform in ("windows", "posix"):
            assert config["hooks"][hook][platform]["run"] == "python ./scripts/validate-reuse.py"


def test_only_adoption_deployment_is_active_in_reuse(template):
    resources = template["resources"]
    adoption = next(resource for resource in resources if resource["name"] == "reuse-existing")
    assert adoption["type"] == "Microsoft.Resources/deployments"
    assert adoption["condition"] == "[variables('reuseExisting')]"
    others = [resource for resource in resources if resource is not adoption]
    assert len(others) == 1 + len(GREENFIELD_MODULES)
    assert all(resource["condition"] == "[not(variables('reuseExisting'))]" for resource in others)
    assert {
        resource["name"] for resource in others
        if resource["type"] == "Microsoft.Resources/deployments"
    } == GREENFIELD_MODULES
    for resource in others:
        for parameter in resource.get("properties", {}).get("parameters", {}).values():
            if "reference(" in json.dumps(parameter):
                assert isinstance(parameter, str)
                assert parameter.startswith(
                    "[if(variables('reuseExisting'), createObject('value', ''), "
                )


def test_adoption_has_only_existing_references_and_no_mutations(template):
    adoption = adoption_template(template)
    resources = adoption["resources"]
    assert resources
    assert all(resource.get("existing") is True for resource in resources.values())
    assert all("properties" not in resource for resource in resources.values())
    assert not any(
        resource["type"] in ("Microsoft.Resources/deployments", "Microsoft.Authorization/roleAssignments")
        for resource in resources.values()
    )
    serialized = json.dumps(adoption)
    assert "listKeys(" not in serialized
    assert "publicNetworkAccess" not in serialized
    assert "plannerModelCapacity" not in serialized
    settings = adoption["outputs"]["settings"]["value"]
    assert "union(parameters('preservedSettings')" in settings
    assert "reference('foundry').defaultProject" in settings
    assert "reference('identity').clientId" in settings
    assert "configuration.ingress.fqdn" in settings


def test_adoption_routes_each_dependency_independently(template):
    resources = adoption_template(template)["resources"]
    expected_groups = {
        "foundry": "foundryResourceGroupName",
        "insuranceProject": "foundryResourceGroupName",
        "aiGateway": "aiResourceGroupName",
        "search": "dataResourceGroupName",
        "cosmos": "dataResourceGroupName",
        "storage": "dataResourceGroupName",
        "identity": "appsResourceGroupName",
        "acr": "appsResourceGroupName",
        "appInsights": "observabilityResourceGroupName",
        "logAnalytics": "logAnalyticsResourceGroupName",
        "containerAppsEnvironment": "containerAppsEnvironmentResourceGroupName",
        "vnet": "vnetResourceGroupName",
        "cosmosPrivateEndpoint": "networkingResourceGroupName",
        "cosmosPrivateDnsZone": "networkingResourceGroupName",
    }
    for resource, group in expected_groups.items():
        assert resources[resource]["resourceGroup"] == f"[parameters('{group}')]"
    assert resources["cosmosPrivateEndpoint"]["name"] == "[parameters('cosmosPrivateEndpointName')]"


def test_replacement_selector_defaults_and_transitional_overrides_are_wired(template, parameter_defaults):
    for variable, parameter, default in (
        ("existingEnvironmentGroup", "containerAppsEnvironmentResourceGroupName", "appsResourceGroupName"),
        ("existingVnetGroup", "vnetResourceGroupName", "networkingResourceGroupName"),
        ("existingLogAnalyticsGroup", "logAnalyticsResourceGroupName", "appsResourceGroupName"),
    ):
        assert template["parameters"][parameter]["defaultValue"] == ""
        assert template["variables"][variable] == (
            f"[if(empty(parameters('{parameter}')), parameters('{default}'), parameters('{parameter}'))]"
        )
    outputs = template["outputs"]
    assert outputs["AZURE_RESOURCE_GROUP"]["value"].startswith(
        "[if(variables('reuseExisting'), parameters('foundryResourceGroupName'), "
    )
    parameters = parameter_defaults
    for parameter in (
        "containerAppsEnvironmentResourceGroupName",
        "vnetResourceGroupName",
        "logAnalyticsResourceGroupName",
    ):
        assert parameters[parameter]["value"] == ""
    assert parameters["cosmosPrivateEndpointName"]["value"] == "pe-zz-cosmos-northwind"
    assert template["parameters"]["cosmosPrivateEndpointName"]["defaultValue"] == "pe-zz-cosmos-northwind"
    assert template["parameters"]["legacyResourceGroupName"]["defaultValue"] == ""
    assert parameters["legacyResourceGroupName"]["value"] == ""
    assert "legacyResourceGroupName" not in json.dumps(template["variables"])
    assert "legacyResourceGroupName" not in json.dumps(template["resources"])
    assert outputs["AZURE_LEGACY_RESOURCE_GROUP"]["value"].startswith(
        "[if(variables('reuseExisting'), parameters('legacyResourceGroupName'), "
    )
    assert "rg-northwind-dev" not in json.dumps(template)


def test_live_app_attachments_and_urls_use_named_resources_not_positions(template):
    adoption = adoption_template(template)
    apps = {
        name: resource for name, resource in adoption["resources"].items()
        if resource["type"] == "Microsoft.App/containerApps"
    }
    assert {resource["name"] for resource in apps.values()} == set(SERVICES.values())
    assert all(resource["resourceGroup"] == "[parameters('appsResourceGroupName')]" for resource in apps.values())
    assert "ca-cu-ingest" not in json.dumps(adoption)
    outputs = adoption["outputs"]
    reported_apps = outputs["containerAppResources"]["value"]
    assert {app["service"] for app in reported_apps} == set(SERVICES)
    assert {app["name"] for app in reported_apps} == {resource["name"] for resource in apps.values()}
    assert {app["service"]: app["name"] for app in reported_apps} == SERVICES
    for app in reported_apps:
        symbolic_name = next(name for name, resource in apps.items() if resource["name"] == app["name"])
        assert app["environmentId"] == f"[reference('{symbolic_name}').managedEnvironmentId]"
    settings = outputs["settings"]["value"]
    for output, resource in (
        ("API_URI", "api"),
        ("MCP_POLICY_LOOKUP_URL", "mcpPolicyLookup"),
        ("MCP_CLAIM_REVIEW_URL", "mcpClaimReview"),
        ("WEB_PORTAL_URI", "webPortal"),
        ("WEB_VOICE_URI", "webVoice"),
    ):
        assert f"'{output}', format(" in settings
        assert f"reference('{resource}').configuration.ingress.fqdn" in settings
    assert "MCP_TECH_LOOKUP_URL" not in settings and "MCP_WORK_ORDER_URL" not in settings
    assert "reference('apps[" not in settings
    resource_ids = json.dumps(outputs["resourceIds"]["value"])
    assert "reference('containerAppsEnvironment').vnetConfiguration.infrastructureSubnetId" in resource_ids
    assert "reference('cosmosPrivateEndpoint').subnet.id" in resource_ids
    assert "containerAppsEnvironment" in outputs["resourceIds"]["value"]
    assert "vnet" in outputs["resourceIds"]["value"]
    assert "cosmosPrivateEndpoint" in outputs["resourceIds"]["value"]
    assert "NORTHWIND_EXISTING_RESOURCE_IDS" in template["outputs"]


def test_content_understanding_ingest_worker_is_gone(template, parameter_defaults):
    assert "enableCuIngest" not in template["parameters"]
    assert "enableCuIngest" not in parameter_defaults
    assert "ca-cu-ingest" not in json.dumps(template)
    assert "NORTHWIND_ENABLE_CU_INGEST" not in (ROOT / "infra" / "main.bicepparam").read_text(encoding="utf-8")
    containerapps = next(resource for resource in template["resources"] if resource["name"] == "containerapps")
    nested = containerapps["properties"]["template"]
    assert "enableCuIngest" not in nested["parameters"]
    apps = [resource for resource in nested["resources"] if resource["type"] == "Microsoft.App/containerApps"]
    assert {resource["name"] for resource in apps} == set(SERVICES.values())
    assert all("condition" not in resource for resource in apps)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("NORTHWIND_INSURANCE_AGENTS_READY", ""),
        ("NORTHWIND_INSURANCE_AGENTS_READY", "TRUE"),
    ],
)
def test_insurance_readiness_must_be_an_explicit_valid_value(key, value):
    env = valid_environment()
    env[key] = value
    assert any(key in error for error in module.validate_environment(env))


def test_insurance_readiness_is_not_inferred_by_reuse_or_hosted_agents():
    env = valid_environment()
    assert module.validate_environment(env) == []
    assert "NORTHWIND_INSURANCE_AGENTS_READY" not in env
    env["NORTHWIND_INSURANCE_AGENTS_READY"] = "true"
    assert any("FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID" in error for error in module.validate_environment(env))
    env["FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID"] = "northwind-insurance-orchestrator:1"
    assert module.validate_environment(env) == []
    env["HOSTED_AGENTS_ENABLED"] = "false"
    assert any("Insurance readiness requires" in error for error in module.validate_environment(env))


@pytest.mark.parametrize("manager_id", ["", "orchestrator:1", "agent_manager", " northwind-insurance-orchestrator:1"])
def test_insurance_readiness_requires_an_insurance_manager_id(manager_id):
    env = valid_environment()
    env.update({
        "NORTHWIND_INSURANCE_AGENTS_READY": "true",
        "FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID": manager_id,
    })
    assert any("FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID" in error for error in module.validate_environment(env))


def test_readiness_requires_a_deployed_project_even_in_greenfield_mode():
    env = {
        "NORTHWIND_INSURANCE_AGENTS_READY": "true",
        "FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID": "northwind-insurance-orchestrator:1",
    }
    assert any("deployed insurance project endpoint" in error for error in module.validate_environment(env))


@pytest.mark.parametrize("ready", ["false", "true"])
def test_reuse_never_targets_another_project_even_when_all_identifiers_agree(ready):
    env = valid_environment()
    env.update({
        "NORTHWIND_INSURANCE_AGENTS_READY": ready,
        "FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID": "northwind-insurance-orchestrator:1",
        "INSURANCE_PROJECT_NAME": "northwind",
    })
    for key in ("FOUNDRY_PROJECT_ENDPOINT", "AZURE_AI_PROJECT_ENDPOINT", "AZURE_AI_PROJECT_ID", "FOUNDRY_PROJECT_ENDPOINTS"):
        env[key] = env[key].replace(LIVE_PROJECT, "northwind")
    assert any("retain INSURANCE_PROJECT_NAME=nw-insurance-claims or northwind-orders" in error
               for error in module.validate_environment(env))


def test_the_live_project_and_the_original_native_project_both_validate_in_reuse_mode():
    assert module.validate_environment(valid_environment()) == []
    assert module.validate_environment(valid_environment("northwind-orders")) == []


def test_greenfield_may_name_the_insurance_project_but_reuse_pins_the_live_name():
    assert module.validate_environment({"INSURANCE_PROJECT_NAME": "a-new-insurance-project"}) == []
    env = valid_environment()
    env["INSURANCE_PROJECT_NAME"] = "a-new-insurance-project"
    assert any("retain INSURANCE_PROJECT_NAME" in error for error in module.validate_environment(env))


def test_deprecated_setting_names_still_validate_and_the_canonical_name_wins():
    env = valid_environment()
    for canonical in ("INSURANCE_PROJECT_NAME", "HOSTED_AGENTS_ENABLED", "AGENT_OPENAI_ENDPOINT", "AGENT_KB_CONNECTION_NAME"):
        env[module.DEPRECATED_ALIASES[canonical]] = env.pop(canonical)
    assert module.validate_environment(env) == []
    env["HOSTED_AGENTS_ENABLED"] = "yes"
    assert any("HOSTED_AGENTS_ENABLED must be exactly" in error for error in module.validate_environment(env))
    env["HOSTED_AGENTS_ENABLED"] = ""
    assert module.validate_environment(env) == []
    env["ORDER_AGENTS_HOSTED"] = "yes"
    assert any("HOSTED_AGENTS_ENABLED must be exactly" in error for error in module.validate_environment(env))


def test_deprecated_department_slugs_in_an_existing_endpoint_map_are_still_accepted():
    env = valid_environment()
    endpoints = json.loads(env["FOUNDRY_PROJECT_ENDPOINTS"])
    renamed = {
        "order-concierge": endpoints["claims-concierge"],
        "employee-help-desk": endpoints["adjuster-support"],
        "claims-warranty": endpoints["claims-documents"],
        "contact-center": endpoints["contact-center"],
    }
    env["FOUNDRY_PROJECT_ENDPOINTS"] = json.dumps(renamed)
    assert module.validate_environment(env) == []
    del renamed["contact-center"]
    env["FOUNDRY_PROJECT_ENDPOINTS"] = json.dumps(renamed)
    assert any("all four department" in error for error in module.validate_environment(env))


def test_the_retired_northwind_gateway_stack_is_rejected():
    assert module.validate_environment({"NORTHWIND_GATEWAY_STACK": "insurance"}) == []
    errors = module.validate_environment({"NORTHWIND_GATEWAY_STACK": "northwind"})
    assert any("NORTHWIND_GATEWAY_STACK must be exactly insurance" in error for error in errors)


def test_greenfield_defaults_are_insurance_native_and_readiness_is_explicitly_false(template, parameter_defaults):
    for retired in ("businessDomain", "orderProjectName", "orderAgentsHosted", "bakeoffDeployments", "enableCuIngest"):
        assert retired not in template["parameters"]
        assert retired not in parameter_defaults
    assert template["parameters"]["insuranceProjectName"]["defaultValue"] == "nw-insurance-claims"
    assert template["parameters"]["hostedAgentsEnabled"]["defaultValue"] == "true"
    assert template["parameters"]["additionalModelDeployments"]["defaultValue"] == []
    assert template["parameters"]["gatewayStack"]["allowedValues"] == ["insurance"]
    assert template["parameters"]["insuranceAgentsReady"]["defaultValue"] == "false"
    parameters = parameter_defaults
    assert parameters["insuranceProjectName"]["value"] == "nw-insurance-claims"
    assert parameters["hostedAgentsEnabled"]["value"] == "true"
    assert parameters["gatewayStack"]["value"] == "insurance"
    assert parameters["insuranceAgentsReady"]["value"] == "false"
    assert parameters["insuranceOrchestratorAgentId"]["value"] == ""
    assert template["parameters"]["insuranceOrchestratorAgentId"]["defaultValue"] == ""
    assert "FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID" not in template["outputs"]
    assert "NORTHWIND_INSURANCE_AGENTS_READY" not in template["outputs"]
    assert len(template["outputs"]) <= 64
    containerapps = next(resource for resource in template["resources"] if resource["name"] == "containerapps")
    assert containerapps["properties"]["parameters"]["insuranceAgentsReady"]["value"] == (
        "[equals(parameters('insuranceAgentsReady'), 'true')]"
    )
    serialized = json.dumps(containerapps["properties"]["template"])
    assert "NORTHWIND_BUSINESS_DOMAIN" not in serialized
    assert "FOUNDRY_BAKEOFF_DEPLOYMENTS" not in serialized
    assert "INSURANCE_PROJECT_NAME" in serialized
    assert "HOSTED_AGENTS_ENABLED" in serialized
    assert "NORTHWIND_INSURANCE_AGENTS_READY" in serialized
    assert "FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID" in serialized


def test_template_outputs_use_the_canonical_insurance_names(template):
    outputs = template["outputs"]
    for canonical in (
        "INSURANCE_PROJECT_NAME", "HOSTED_AGENTS_ENABLED", "AGENT_OPENAI_ENDPOINT", "AGENT_KB_CONNECTION_NAME",
        "FOUNDRY_DEFAULT_PROJECT", "MCP_POLICY_LOOKUP_URL", "MCP_CLAIM_REVIEW_URL",
    ):
        assert canonical in outputs
    retired = (
        "ORDER_", "ORDERS_", "NORTHWIND_BUSINESS_DOMAIN", "FOUNDRY_BAKEOFF_DEPLOYMENTS", "FOUNDRY_AGENT_",
        "FOUNDRY_ORCHESTRATOR_AGENT_ID", "MCP_TECH_LOOKUP_URL", "MCP_WORK_ORDER_URL", "AZURE_SEARCH_INDEX",
        "AZURE_SEARCH_KNOWLEDGE",
    )
    assert [name for name in outputs if name.startswith(retired)] == []
    foundry = next(resource for resource in template["resources"] if resource["name"] == "foundry")
    nested = foundry["properties"]["template"]
    assert "additionalModelDeployments" in nested["parameters"]
    assert "bakeoffDeployments" not in nested["parameters"]


def test_bicepparam_reads_canonical_names_and_falls_back_to_the_deprecated_ones():
    parameters = compile_parameters({"ORDER_PROJECT_NAME": "legacy-project", "ORDER_AGENTS_HOSTED": "false",
                                     "ORDERS_OPENAI_ENDPOINT": "https://legacy/", "ORDERS_KB_CONNECTION_NAME": "legacy-kb"})
    assert parameters["insuranceProjectName"]["value"] == "legacy-project"
    assert parameters["hostedAgentsEnabled"]["value"] == "false"
    assert parameters["existingSettings"]["value"]["AGENT_OPENAI_ENDPOINT"] == "https://legacy/"
    assert parameters["existingSettings"]["value"]["AGENT_KB_CONNECTION_NAME"] == "legacy-kb"
    parameters = compile_parameters({
        "ORDER_PROJECT_NAME": "legacy-project", "INSURANCE_PROJECT_NAME": "canonical-project",
        "ORDER_AGENTS_HOSTED": "false", "HOSTED_AGENTS_ENABLED": "true",
        "ORDERS_OPENAI_ENDPOINT": "https://legacy/", "AGENT_OPENAI_ENDPOINT": "https://canonical/",
        "ORDERS_KB_CONNECTION_NAME": "legacy-kb", "AGENT_KB_CONNECTION_NAME": "canonical-kb",
    })
    assert parameters["insuranceProjectName"]["value"] == "canonical-project"
    assert parameters["hostedAgentsEnabled"]["value"] == "true"
    assert parameters["existingSettings"]["value"]["AGENT_OPENAI_ENDPOINT"] == "https://canonical/"
    assert parameters["existingSettings"]["value"]["AGENT_KB_CONNECTION_NAME"] == "canonical-kb"


def test_reuse_constraints_match_required_preserved_inputs(template, parameter_defaults):
    adoption = adoption_template(template)
    definition = adoption["definitions"]["preservedConfiguration"]
    constrained = {
        key for key, value in definition["properties"].items()
        if value.get("minLength") == 1
    }
    assert constrained == set(module.PRESERVED_KEYS)
    preserved = parameter_defaults["existingSettings"]["value"]
    assert set(preserved) == set(definition["properties"])
    assert all(not value.get("nullable") for value in definition["properties"].values())
    assert template["parameters"]["reuseExistingResources"]["defaultValue"] == "false"
    assert template["parameters"]["reuseExistingResources"]["allowedValues"] == ["true", "false"]
    assert template["outputs"]["AZURE_APPS_RESOURCE_GROUP"]["value"].startswith(
        "[if(variables('reuseExisting'), parameters('appsResourceGroupName'), "
    )


def test_reuse_validation_script_rejects_an_invalid_mode_without_azure_access():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate-reuse.py")],
        env={**{key: value for key, value in os.environ.items() if key in ("PATH", "SYSTEMROOT")},
             "NORTHWIND_REUSE_EXISTING_RESOURCES": "yes"},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1
    assert "must be exactly true or false" in result.stderr


def test_reuse_validation_script_succeeds_without_azure_access_for_a_fresh_environment():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate-reuse.py")],
        env={key: value for key, value in os.environ.items() if key in ("PATH", "SYSTEMROOT")},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
