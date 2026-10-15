import importlib.util
import json
import os
import re
import shutil
import subprocess
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
    "api", "mcp-tech-lookup", "mcp-work-order", "web-tech", "web-voice"
}
GREENFIELD_MODULES = {
    "observability", "uami", "acr", "vnet", "storage", "search", "cosmos",
    "cosmos-private-endpoint", "foundry", "foundry-observability", "search-role",
    "containerapps",
}


def valid_environment():
    env = {key: "preserved" for key in module.PRESERVED_KEYS}
    endpoint = "https://aisvc-zurlo-northwind.services.ai.azure.com/api/projects/northwind-orders"
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
            "/accounts/aisvc-zurlo-northwind/projects/northwind-orders"
        ),
        "ORDER_PROJECT_NAME": "northwind-orders",
        "ORDER_AGENTS_HOSTED": "true",
        "FOUNDRY_PROJECT_ENDPOINT": endpoint,
        "AZURE_AI_PROJECT_ENDPOINT": endpoint,
        "FOUNDRY_PROJECT_ENDPOINTS": json.dumps({
            "order-concierge": endpoint,
            "employee-help-desk": endpoint.replace("northwind-orders", "nw-employee-help-desk"),
            "claims-warranty": endpoint.replace("northwind-orders", "nw-claims-warranty"),
            "contact-center": endpoint.replace("northwind-orders", "nw-contact-center"),
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
    env["NORTHWIND_BUSINESS_DOMAIN"] = "insurance"
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


def test_fresh_mode_requires_no_adoption_inputs():
    assert module.validate_environment({}) == []
    assert module.validate_environment({"NORTHWIND_REUSE_EXISTING_RESOURCES": "false"}) == []


@pytest.mark.parametrize("mode", ["", "TRUE", "False", "yes"])
def test_mode_must_be_an_explicit_lowercase_boolean(mode):
    errors = module.validate_environment({"NORTHWIND_REUSE_EXISTING_RESOURCES": mode})
    assert errors and "exactly true or false" in errors[0]


def test_reuse_preserves_orders_routing_with_independent_workspace_location():
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
@pytest.mark.parametrize(("domain", "ready"), [
    ("retail", "false"), ("insurance", "false"), ("insurance", "true"),
])
def test_cli_defaults_to_replacement_stack_without_requiring_or_using_legacy_label(
    legacy_label, domain, ready, monkeypatch, capsys,
):
    env = valid_environment()
    env.update({
        "NORTHWIND_BUSINESS_DOMAIN": domain,
        "NORTHWIND_INSURANCE_AGENTS_READY": ready,
    })
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
        ("northwind-orders", "nw-employee-help-desk"),
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
        ("ORDER_AGENTS_HOSTED", "yes", "exactly true or false"),
        ("AZURE_AI_PROJECT_ENDPOINT", "https://wrong.example/api/projects/northwind", "must agree"),
        ("ORDER_PROJECT_NAME", "northwind", "resolve ORDER_PROJECT_NAME"),
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


def test_department_map_cannot_change_the_orders_target():
    env = valid_environment()
    endpoints = json.loads(env["FOUNDRY_PROJECT_ENDPOINTS"])
    endpoints["order-concierge"] = endpoints["claims-warranty"]
    env["FOUNDRY_PROJECT_ENDPOINTS"] = json.dumps(endpoints)
    assert any("Orders project" in error for error in module.validate_environment(env))


def test_department_map_cannot_route_a_specialist_to_another_foundry_account():
    env = valid_environment()
    endpoints = json.loads(env["FOUNDRY_PROJECT_ENDPOINTS"])
    endpoints["claims-warranty"] = endpoints["claims-warranty"].replace(
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


def test_all_five_apps_have_explicit_routing_without_changing_agent_services():
    config = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
    apps = {
        name: service for name, service in config["services"].items()
        if service["host"] == "containerapp"
    }
    assert set(apps) == SERVICES
    for name, service in apps.items():
        assert service["resourceGroup"] == "${AZURE_APPS_RESOURCE_GROUP}"
        assert service["resourceName"] == "ca-" + name
    agents = [
        service for service in config["services"].values()
        if service["host"] == "azure.ai.agent"
    ]
    assert len(agents) == 5
    assert all("resourceGroup" not in service and "resourceName" not in service for service in agents)


def test_rai_policy_uses_configured_subscription_and_foundry_group():
    config = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
    policy = config["services"]["triage"]["policies"][0]["raiPolicyName"]
    assert policy == (
        "/subscriptions/${AZURE_SUBSCRIPTION_ID}/resourceGroups/${AZURE_FOUNDRY_RESOURCE_GROUP}"
        "/providers/Microsoft.CognitiveServices/accounts/aisvc-zurlo-northwind"
        "/raiPolicies/Microsoft.DefaultV2"
    )
    assert "rg-northwind-dev" not in json.dumps(config)


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
        "orderProject": "foundryResourceGroupName",
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
    assert {resource["name"] for resource in apps.values()} == {"ca-" + name for name in SERVICES}
    assert all(resource["resourceGroup"] == "[parameters('appsResourceGroupName')]" for resource in apps.values())
    assert "ca-cu-ingest" not in json.dumps(adoption)
    outputs = adoption["outputs"]
    reported_apps = outputs["containerAppResources"]["value"]
    assert {app["service"] for app in reported_apps} == SERVICES
    assert {app["name"] for app in reported_apps} == {resource["name"] for resource in apps.values()}
    for app in reported_apps:
        symbolic_name = next(name for name, resource in apps.items() if resource["name"] == app["name"])
        assert app["environmentId"] == f"[reference('{symbolic_name}').managedEnvironmentId]"
    settings = outputs["settings"]["value"]
    for output, resource in (
        ("API_URI", "api"),
        ("MCP_TECH_LOOKUP_URL", "mcpTechLookup"),
        ("MCP_WORK_ORDER_URL", "mcpWorkOrder"),
        ("WEB_TECH_URI", "webTech"),
        ("WEB_VOICE_URI", "webVoice"),
    ):
        assert f"'{output}', format(" in settings
        assert f"reference('{resource}').configuration.ingress.fqdn" in settings
    assert "reference('apps[" not in settings
    resource_ids = json.dumps(outputs["resourceIds"]["value"])
    assert "reference('containerAppsEnvironment').vnetConfiguration.infrastructureSubnetId" in resource_ids
    assert "reference('cosmosPrivateEndpoint').subnet.id" in resource_ids
    assert "containerAppsEnvironment" in outputs["resourceIds"]["value"]
    assert "vnet" in outputs["resourceIds"]["value"]
    assert "cosmosPrivateEndpoint" in outputs["resourceIds"]["value"]
    assert "NORTHWIND_EXISTING_RESOURCE_IDS" in template["outputs"]


@pytest.mark.parametrize("mode", ["", "TRUE", "False", "yes"])
def test_ingestion_opt_in_is_explicit_in_all_modes(mode):
    for reuse in ("false", "true"):
        env = valid_environment()
        env["NORTHWIND_REUSE_EXISTING_RESOURCES"] = reuse
        env["NORTHWIND_ENABLE_CU_INGEST"] = mode
        assert any("NORTHWIND_ENABLE_CU_INGEST must be exactly" in error for error in module.validate_environment(env))


def test_ingestion_is_greenfield_only_and_never_revived_by_reuse():
    assert module.validate_environment({"NORTHWIND_ENABLE_CU_INGEST": "true"}) == []
    env = valid_environment()
    env["NORTHWIND_ENABLE_CU_INGEST"] = "true"
    assert any("greenfield-only" in error for error in module.validate_environment(env))


def test_ingestion_greenfield_resource_is_default_disabled(template, parameter_defaults):
    assert template["parameters"]["enableCuIngest"]["defaultValue"] == "false"
    assert template["parameters"]["enableCuIngest"]["allowedValues"] == ["true", "false"]
    containerapps = next(resource for resource in template["resources"] if resource["name"] == "containerapps")
    assert containerapps["properties"]["parameters"]["enableCuIngest"]["value"] == (
        "[equals(parameters('enableCuIngest'), 'true')]"
    )
    nested = containerapps["properties"]["template"]
    assert nested["parameters"]["enableCuIngest"]["defaultValue"] is False
    worker = next(resource for resource in nested["resources"] if resource["name"] == "ca-cu-ingest")
    assert worker["condition"] == "[parameters('enableCuIngest')]"
    active_apps = [
        resource for resource in nested["resources"]
        if resource["type"] == "Microsoft.App/containerApps" and resource is not worker
    ]
    assert {resource["name"] for resource in active_apps} == {"ca-" + name for name in SERVICES}
    assert all("condition" not in resource for resource in active_apps)
    assert parameter_defaults["enableCuIngest"]["value"] == "false"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("NORTHWIND_BUSINESS_DOMAIN", ""),
        ("NORTHWIND_BUSINESS_DOMAIN", "Insurance"),
        ("NORTHWIND_INSURANCE_AGENTS_READY", ""),
        ("NORTHWIND_INSURANCE_AGENTS_READY", "TRUE"),
    ],
)
def test_insurance_activation_and_readiness_must_be_explicit_valid_values(key, value):
    env = valid_environment()
    env[key] = value
    assert any(key in error for error in module.validate_environment(env))


def test_insurance_readiness_is_not_inferred_by_reuse_or_hosted_agents():
    env = valid_environment()
    env["NORTHWIND_BUSINESS_DOMAIN"] = "insurance"
    assert module.validate_environment(env) == []
    assert "NORTHWIND_INSURANCE_AGENTS_READY" not in env
    env["NORTHWIND_INSURANCE_AGENTS_READY"] = "true"
    assert any("FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID" in error for error in module.validate_environment(env))
    env["FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID"] = "northwind-insurance-orchestrator:1"
    assert module.validate_environment(env) == []
    env["ORDER_AGENTS_HOSTED"] = "false"
    assert any("Insurance readiness requires" in error for error in module.validate_environment(env))


@pytest.mark.parametrize("manager_id", ["", "northwind-orchestrator:1", "agent_retail", " northwind-insurance-orchestrator:1"])
def test_insurance_readiness_never_reuses_a_retail_manager_id(manager_id):
    env = valid_environment()
    env.update({
        "NORTHWIND_BUSINESS_DOMAIN": "insurance",
        "NORTHWIND_INSURANCE_AGENTS_READY": "true",
        "FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID": manager_id,
    })
    assert any("FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID" in error for error in module.validate_environment(env))


def test_readiness_requires_a_deployed_project_even_in_greenfield_mode():
    env = {
        "NORTHWIND_BUSINESS_DOMAIN": "insurance",
        "NORTHWIND_INSURANCE_AGENTS_READY": "true",
        "FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID": "northwind-insurance-orchestrator:1",
    }
    assert any("deployed Orders project endpoint" in error for error in module.validate_environment(env))


@pytest.mark.parametrize("ready", ["false", "true"])
def test_insurance_never_targets_legacy_project_even_when_all_identifiers_agree(ready):
    env = valid_environment()
    env.update({
        "NORTHWIND_BUSINESS_DOMAIN": "insurance",
        "NORTHWIND_INSURANCE_AGENTS_READY": ready,
        "FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID": "northwind-insurance-orchestrator:1",
        "ORDER_PROJECT_NAME": "northwind",
    })
    for key in ("FOUNDRY_PROJECT_ENDPOINT", "AZURE_AI_PROJECT_ENDPOINT", "AZURE_AI_PROJECT_ID", "FOUNDRY_PROJECT_ENDPOINTS"):
        env[key] = env[key].replace("northwind-orders", "northwind")
    assert any("retain ORDER_PROJECT_NAME=northwind-orders" in error for error in module.validate_environment(env))


@pytest.mark.parametrize("service", module.AGENT_SERVICES)
def test_insurance_cannot_use_the_existing_retail_azd_agent_services(service):
    env = valid_environment()
    env["NORTHWIND_BUSINESS_DOMAIN"] = "insurance"
    assert any("must not overwrite existing retail agents" in error for error in module.validate_environment(env, service))
    assert any("--business-domain insurance" in error for error in module.validate_environment(env, service))
    assert module.validate_environment(env) == []  # Scoped Container Apps code deployment is still allowed.
    env["NORTHWIND_BUSINESS_DOMAIN"] = "retail"
    assert module.validate_environment(env, service) == []


@pytest.mark.parametrize("service", module.AGENT_SERVICES)
@pytest.mark.parametrize("name", ["${NORTHWIND_TRIAGE_AGENT_NAME}", "northwind-insurance-orchestrator", ""])
def test_azd_guard_rejects_unresolved_or_insurance_name_in_retail_manifest(service, name):
    assert module.validate_agent_manifest(service, {"name": name})
    assert module.validate_agent_manifest(service, {"name": module.AGENT_SERVICES[service]}) == []


@pytest.mark.parametrize("service", module.AGENT_SERVICES)
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("NORTHWIND_BUSINESS_DOMAIN", "insurance"),
        ("NORTHWIND_BUSINESS_DOMAIN", "${NORTHWIND_BUSINESS_DOMAIN}"),
        ("NORTHWIND_INSURANCE_AGENTS_READY", "true"),
    ],
)
def test_azd_retail_name_cannot_disguise_insurance_runtime_or_readiness(service, key, value):
    manifest = {
        "name": module.AGENT_SERVICES[service],
        "environment_variables": [{"name": key, "value": value}],
    }
    assert module.validate_agent_manifest(service, manifest)


def test_current_agent_manifests_are_compatible_with_the_retail_azd_contract():
    for service in module.AGENT_SERVICES:
        manifest = yaml.safe_load((ROOT / "agents" / service / "agent.yaml").read_text())
        assert module.validate_agent_manifest(service, manifest) == []


def test_greenfield_retains_retail_defaults_and_explicit_readiness_false(template, parameter_defaults):
    assert template["parameters"]["businessDomain"]["defaultValue"] == "retail"
    assert template["parameters"]["insuranceAgentsReady"]["defaultValue"] == "false"
    parameters = parameter_defaults
    assert parameters["businessDomain"]["value"] == "retail"
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
    assert "NORTHWIND_BUSINESS_DOMAIN" in serialized
    assert "NORTHWIND_INSURANCE_AGENTS_READY" in serialized
    assert "FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID" in serialized


def test_manifest_preserves_retail_agent_names_and_blocks_insurance_packaging_and_deploy(template):
    config = yaml.safe_load((ROOT / "azure.yaml").read_text())
    retail_names = {
        "orchestrator": "northwind-orchestrator",
        "triage": "northwind-triage",
        "orderlookup": "northwind-order-lookup",
        "policylookup": "northwind-policy-lookup",
        "resolutiondraft": "northwind-resolution-draft",
    }
    for service, retail_name in retail_names.items():
        agent = config["services"][service]
        assert agent["name"] == retail_name
        hook = agent["hooks"]["prepackage"]["run"]
        assert hook.index("--agent-service " + service) < hook.index("orders_agent_package.py")
        predeploy = agent["hooks"]["predeploy"]
        assert "--agent-service " + service in predeploy["run"]
        assert "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }" in predeploy["run"]
        assert predeploy["continueOnError"] is False
        environment = {item["name"]: item["value"] for item in agent["environmentVariables"]}
        assert environment["NORTHWIND_BUSINESS_DOMAIN"] == "${NORTHWIND_BUSINESS_DOMAIN}"
        assert environment["NORTHWIND_INSURANCE_AGENTS_READY"] == "false"
    assert template["parameters"]["orderProjectName"]["defaultValue"] == "northwind-orders"


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


def test_bootstrap_guards_precede_any_dependency_or_azure_operation():
    windows = (ROOT / "scripts" / "postprovision.ps1").read_text(encoding="utf-8")
    posix = (ROOT / "scripts" / "postprovision.sh").read_text(encoding="utf-8")
    assert windows.index("NORTHWIND_REUSE_EXISTING_RESOURCES -ceq 'true'") < windows.index("exit 0")
    assert windows.index("exit 0") < windows.index("$root =") < windows.index("pip install")
    assert posix.index('case "${NORTHWIND_REUSE_EXISTING_RESOURCES-false}"') < posix.index("exit 0")
    assert posix.index("exit 0") < posix.index("ROOT=") < posix.index("enable-public-access.sh")
    assert windows.index("NORTHWIND_BUSINESS_DOMAIN -ceq 'insurance'") < windows.index("$root =")
    assert posix.index('case "${NORTHWIND_BUSINESS_DOMAIN-retail}"') < posix.index("ROOT=")


@pytest.mark.parametrize("mode", ["true", "TRUE", "False", "yes"])
def test_windows_postprovision_never_runs_bootstrap_in_reuse_or_invalid_modes(mode):
    shell = shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is not installed")
    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(ROOT / "scripts" / "postprovision.ps1")],
        env={**os.environ, "NORTHWIND_REUSE_EXISTING_RESOURCES": mode},
        capture_output=True, text=True, timeout=30,
    )
    if mode == "true":
        assert result.returncode == 0
        assert "skipping dependency installation" in result.stdout
    else:
        assert result.returncode != 0
        assert "must be exactly true or false" in result.stderr
    assert "post-provision starting" not in result.stdout


@pytest.mark.parametrize("domain", ["insurance", "INSURANCE", "yes"])
def test_windows_insurance_or_invalid_domain_never_runs_legacy_bootstrap_even_without_reuse(domain):
    shell = shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is not installed")
    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(ROOT / "scripts" / "postprovision.ps1")],
        env={**os.environ, "NORTHWIND_REUSE_EXISTING_RESOURCES": "false", "NORTHWIND_BUSINESS_DOMAIN": domain},
        capture_output=True, text=True, timeout=30,
    )
    if domain == "insurance":
        assert result.returncode == 0
        assert "skipping all legacy bootstrap" in result.stdout
    else:
        assert result.returncode != 0
        assert "must be exactly retail or insurance" in result.stderr
    assert "post-provision starting" not in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell validation runs on POSIX")
@pytest.mark.parametrize("domain", ["insurance", "", "INSURANCE", "yes"])
def test_posix_insurance_or_invalid_domain_never_runs_legacy_bootstrap_even_without_reuse(domain):
    result = subprocess.run(
        ["sh", str(ROOT / "scripts" / "postprovision.sh")],
        env={**os.environ, "NORTHWIND_REUSE_EXISTING_RESOURCES": "false", "NORTHWIND_BUSINESS_DOMAIN": domain},
        capture_output=True, text=True, timeout=30,
    )
    if domain == "insurance":
        assert result.returncode == 0
        assert "skipping all legacy bootstrap" in result.stdout
    else:
        assert result.returncode != 0
        assert "must be exactly retail or insurance" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell validation runs on POSIX")
@pytest.mark.parametrize("mode", ["true", "", "TRUE", "False", "yes"])
def test_posix_postprovision_never_runs_bootstrap_in_reuse_or_invalid_modes(mode):
    result = subprocess.run(
        ["sh", str(ROOT / "scripts" / "postprovision.sh")],
        env={**os.environ, "NORTHWIND_REUSE_EXISTING_RESOURCES": mode},
        capture_output=True, text=True, timeout=30,
    )
    if mode == "true":
        assert result.returncode == 0
        assert "skipping dependency installation" in result.stdout
    else:
        assert result.returncode != 0
        assert "must be exactly true or false" in result.stderr
    assert "post-provision starting" not in result.stdout
