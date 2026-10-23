"""Hosted deploy: gateway-aware runtime role grants and config validation (offline)."""
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("hosted_agents_deploy_gateway", SCRIPTS / "deploy-hosted-agents.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
common = importlib.import_module("ai_gateway_common")

SUBSCRIPTION = "11111111-2222-4333-8444-555555555555"
ACCOUNT = (f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-northwind-ai"
           "/providers/Microsoft.CognitiveServices/accounts/aisvc-zurlo-northwind")
PROJECT = f"{ACCOUNT}/projects/nw-insurance-claims"
GATEWAY = (f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-northwind-ai"
           "/providers/Microsoft.ApiManagement/service/ai-gateway-northwind")
PRINCIPAL = "99999999-aaaa-4bbb-8ccc-dddddddddddd"
FOUNDRY = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
OPENAI = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
CONSUMER = "eed3b665-ab3a-47b6-8f48-c9382fb1dad6"
MODELS = "a6b68e3d-a1f0-4c60-8678-237c050cf4ea"
TOOLS = "dce03f8d-857f-4e40-a90d-67770fa6ecd2"
GATEWAY_SETTINGS = {
    "AGENT_MODEL_GATEWAY_ENDPOINT": "https://ai-gateway-northwind.azure-api.net/default/models/openai/v1/",
    "AGENT_MODEL_GATEWAY_MODEL": "aisvc-zurlo-northwind/gpt-4o",
    "AGENT_MODEL_GATEWAY_DEPLOYMENT": "gpt-4o",
}
DEPRECATED_GATEWAY_SETTINGS = {
    "ORDERS_MODEL_GATEWAY_ENDPOINT": GATEWAY_SETTINGS["AGENT_MODEL_GATEWAY_ENDPOINT"],
    "ORDERS_MODEL_GATEWAY_MODEL": GATEWAY_SETTINGS["AGENT_MODEL_GATEWAY_MODEL"],
    "ORDERS_MODEL_GATEWAY_DEPLOYMENT": GATEWAY_SETTINGS["AGENT_MODEL_GATEWAY_DEPLOYMENT"],
}
LOOKUP_ROUTE = {"url": "https://ai-gateway-northwind.azure-api.net/default/toolservers/northwind-insurance-lookup/mcp",
                "tool_prefix": "lookup_", "connection": "nw-insurance-claims-gateway-mcp"}
KNOWLEDGE_ROUTE = {
    "url": "https://ai-gateway-northwind.azure-api.net/default/toolservers/northwind-insurance-knowledge/mcp",
    "tool_prefix": "knowledge_", "connection": "nw-insurance-claims-gateway-kb"}
MCP_ROUTES = json.dumps({"policy-claim-lookup": LOOKUP_ROUTE, "policy-wording": KNOWLEDGE_ROUTE})
DEPRECATED_MCP_ROUTES = json.dumps({"orderlookup": LOOKUP_ROUTE, "policylookup": KNOWLEDGE_ROUTE})
DIRECT = [(ACCOUNT, OPENAI), (PROJECT, FOUNDRY)]


def test_role_ids_and_gateway_resource_match_the_shared_contract():
    assert module.FOUNDRY_USER == FOUNDRY == common.ROLE_FOUNDRY_USER
    assert module.OPENAI_USER == OPENAI == common.ROLE_OPENAI_USER
    assert module.MODELS_USER == MODELS == common.ROLE_MODELS_USER
    assert not hasattr(module, "TOOLS_USER") and TOOLS == common.ROLE_TOOLS_USER
    assert module.GATEWAY_NAME == common.GATEWAY_NAME and module.GATEWAY_RESOURCE_GROUP == common.RESOURCE_GROUP
    assert module.gateway_scope(SUBSCRIPTION) == GATEWAY


@pytest.mark.parametrize("environment", [
    {}, {"AI_GATEWAY_REQUIRED": ""}, {"AI_GATEWAY_REQUIRED": "false", "AI_GATEWAY_MCP_REQUIRED": "0"},
    {"AI_GATEWAY_REQUIRED": "no", "AI_GATEWAY_MCP_REQUIRED": "no"},
    {"AI_GATEWAY_REQUIRED": "off", "AI_GATEWAY_MCP_REQUIRED": " OFF "},
])
@pytest.mark.parametrize("role", list(module.ROLES))
def test_without_gateway_flags_the_grants_are_the_direct_sequence(environment, role):
    expected = DIRECT + ([(PROJECT, CONSUMER)] if role == "orchestrator" else [])
    assert module.runtime_grants(environment, role, PROJECT, ACCOUNT) == expected


def test_direct_grants_never_need_a_parseable_subscription():
    assert module.runtime_grants({}, "triage", "p", "a") == [("a", OPENAI), ("p", FOUNDRY)]


@pytest.mark.parametrize("flag", ["true", "1", "yes", "YES", " true "])
@pytest.mark.parametrize("role", list(module.ROLES))
def test_required_models_replace_the_account_openai_grant_with_models_user_on_the_gateway(flag, role):
    grants = module.runtime_grants({"AI_GATEWAY_REQUIRED": flag}, role, PROJECT, ACCOUNT)
    expected = [(GATEWAY, MODELS), (PROJECT, FOUNDRY)] + ([(PROJECT, CONSUMER)] if role == "orchestrator" else [])
    assert grants == expected
    assert all(role_id != OPENAI for _, role_id in grants)
    assert all(role_id != TOOLS for _, role_id in grants)


@pytest.mark.parametrize("role", ["policy-claim-lookup", "policy-wording"])
def test_required_mcp_grants_no_gateway_role_because_the_runtime_key_authenticates_it(role):
    environment = {"AI_GATEWAY_REQUIRED": "true", "AI_GATEWAY_MCP_REQUIRED": "true"}
    grants = module.runtime_grants(environment, role, PROJECT, ACCOUNT)
    assert grants == [(GATEWAY, MODELS), (PROJECT, FOUNDRY)]
    assert all(role_id != TOOLS for _, role_id in grants)


@pytest.mark.parametrize("role", ["triage", "claim-action-draft", "orchestrator"])
def test_required_mcp_does_not_touch_the_other_roles(role):
    environment = {"AI_GATEWAY_REQUIRED": "true", "AI_GATEWAY_MCP_REQUIRED": "true"}
    assert module.runtime_grants(environment, role, PROJECT, ACCOUNT) == module.runtime_grants(
        {"AI_GATEWAY_REQUIRED": "true"}, role, PROJECT, ACCOUNT)
    assert module.runtime_grants({"AI_GATEWAY_MCP_REQUIRED": "true"}, role, PROJECT, ACCOUNT) == module.runtime_grants(
        {}, role, PROJECT, ACCOUNT)


def test_mcp_alone_keeps_the_direct_model_grants_and_adds_nothing():
    assert module.runtime_grants({"AI_GATEWAY_MCP_REQUIRED": "true"}, "policy-claim-lookup", PROJECT, ACCOUNT) == DIRECT


@pytest.mark.parametrize("legacy,canonical", list(module.ROLE_ALIASES.items()))
def test_deprecated_role_ids_receive_the_grants_of_their_canonical_role(legacy, canonical):
    for environment in ({}, {"AI_GATEWAY_REQUIRED": "true"}, {"AI_GATEWAY_MCP_REQUIRED": "true"}):
        assert module.runtime_grants(environment, legacy, PROJECT, ACCOUNT) == module.runtime_grants(
            environment, canonical, PROJECT, ACCOUNT)


def test_gateway_grants_need_a_subscription_scope_and_flags_must_not_be_typos():
    with pytest.raises(ValueError, match="gateway subscription"):
        module.runtime_grants({"AI_GATEWAY_REQUIRED": "true"}, "triage", PROJECT, "/not-a-subscription")
    with pytest.raises(ValueError, match="must be true, 1 or yes"):
        module.runtime_grants({"AI_GATEWAY_REQUIRED": "ture"}, "triage", PROJECT, ACCOUNT)
    with pytest.raises(ValueError, match="AI_GATEWAY_MCP_REQUIRED must be"):
        module.validate_gateway_environment({"AI_GATEWAY_MCP_REQUIRED": "enabled"}, "triage")


@pytest.mark.parametrize("missing", GATEWAY_SETTINGS)
@pytest.mark.parametrize("blank", ["", "   "])
def test_required_models_need_every_gateway_setting(missing, blank):
    environment = {"AI_GATEWAY_REQUIRED": "true", **GATEWAY_SETTINGS, missing: blank}
    with pytest.raises(ValueError, match=missing):
        module.validate_gateway_environment(environment, "triage")
    del environment[missing]
    with pytest.raises(ValueError, match=missing):
        module.validate_gateway_environment(environment, "triage")


def test_complete_gateway_settings_and_disabled_flags_validate():
    module.validate_gateway_environment({"AI_GATEWAY_REQUIRED": "true", **GATEWAY_SETTINGS}, "orchestrator")
    module.validate_gateway_environment({"AI_GATEWAY_REQUIRED": "true", **DEPRECATED_GATEWAY_SETTINGS}, "orchestrator")
    module.validate_gateway_environment({}, "orchestrator")
    module.validate_gateway_environment({"AI_GATEWAY_REQUIRED": "false"}, "orchestrator")


@pytest.mark.parametrize("canonical,deprecated", list(zip(GATEWAY_SETTINGS, DEPRECATED_GATEWAY_SETTINGS)))
def test_a_blank_deprecated_gateway_setting_is_reported_by_its_canonical_name(canonical, deprecated):
    assert module.SETTING_ALIASES[canonical] == (deprecated,)
    environment = {"AI_GATEWAY_REQUIRED": "true", **DEPRECATED_GATEWAY_SETTINGS, deprecated: " "}
    with pytest.raises(ValueError, match=canonical):
        module.validate_gateway_environment(environment, "triage")


@pytest.mark.parametrize("role", ["policy-claim-lookup", "policy-wording"])
def test_required_mcp_needs_a_route_for_the_lookup_role(role):
    environment = {"AI_GATEWAY_MCP_REQUIRED": "true", "AGENT_MCP_GATEWAY": MCP_ROUTES}
    module.validate_gateway_environment(environment, role)
    for bad in (None, "", "not json", "[]", "null", json.dumps({"triage": {"url": "https://x"}}),
                json.dumps({role: {"tool_prefix": "x_"}}), json.dumps({role: "https://x"}),
                json.dumps({role: {"url": "  "}}), {"policy-claim-lookup": {}}):
        broken = {"AI_GATEWAY_MCP_REQUIRED": "true"}
        if bad is not None:
            broken["AGENT_MCP_GATEWAY"] = bad
        with pytest.raises(ValueError, match="AGENT_MCP_GATEWAY"):
            module.validate_gateway_environment(broken, role)


@pytest.mark.parametrize("role,legacy", [("policy-claim-lookup", "orderlookup"), ("policy-wording", "policylookup")])
def test_the_deprecated_gateway_setting_and_role_keys_satisfy_the_mcp_requirement(role, legacy):
    for routes in (DEPRECATED_MCP_ROUTES, json.dumps({legacy: LOOKUP_ROUTE})):
        module.validate_gateway_environment({"AI_GATEWAY_MCP_REQUIRED": "true", "ORDERS_MCP_GATEWAY": routes}, role)
        module.validate_gateway_environment({"AI_GATEWAY_MCP_REQUIRED": "true", "AGENT_MCP_GATEWAY": "",
                                             "ORDERS_MCP_GATEWAY": routes}, role)
    # The canonical setting wins when both are present, and a role named both ways is ambiguous.
    with pytest.raises(ValueError, match=f"has no {role} entry"):
        module.validate_gateway_environment({"AI_GATEWAY_MCP_REQUIRED": "true", "ORDERS_MCP_GATEWAY": DEPRECATED_MCP_ROUTES,
                                             "AGENT_MCP_GATEWAY": json.dumps({"triage": {"url": "https://x"}})}, role)
    both = json.dumps({role: LOOKUP_ROUTE, legacy: LOOKUP_ROUTE})
    with pytest.raises(ValueError, match="more than once"):
        module.validate_gateway_environment({"AI_GATEWAY_MCP_REQUIRED": "true", "AGENT_MCP_GATEWAY": both}, role)


@pytest.mark.parametrize("role", ["triage", "claim-action-draft", "orchestrator"])
def test_required_mcp_is_not_checked_for_roles_without_gateway_mcp(role):
    module.validate_gateway_environment({"AI_GATEWAY_MCP_REQUIRED": "true"}, role)


def deploy_arguments(tmp_path, role, *, apply, grant_runtime_roles=False):
    return SimpleNamespace(
        output=tmp_path / f"{role}.json", apply=apply, config=tmp_path / "config.json", role=role,
        project_endpoint="https://existing.invalid/api/projects/nw-insurance-claims",
        project_arm_id=PROJECT, tenant="tenant-fixture", credential_source="cli", subscription=SUBSCRIPTION,
        requirements=ROOT / "agents" / "hosted" / "requirements.txt", timeout=30,
        grant_runtime_roles=grant_runtime_roles,
    )


def write_config(tmp_path, role, common_environment=None, role_environment=None):
    (tmp_path / "config.json").write_text(json.dumps({
        "common": {"AGENT_MODEL_DEPLOYMENT": "baseline", "AZURE_SEARCH_KNOWLEDGE_BASE": "northwind-insurance-kb",
                   "AZURE_SEARCH_INDEX": "northwind-insurance-knowledge",
                   "AGENT_KB_CONNECTION": "northwind-insurance-kb-mcp", **(common_environment or {})},
        role: role_environment or {},
    }), encoding="utf-8")


@pytest.fixture
def no_azure(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("A rejected or dry-run deployment must not reach Azure")

    monkeypatch.setattr(module, "deployment_credential", refuse)
    monkeypatch.setattr(module, "AIProjectClient", refuse)
    monkeypatch.setattr(module.httpx, "Client", refuse)


def test_deploy_rejects_incomplete_gateway_settings_before_any_network_call(tmp_path, no_azure):
    write_config(tmp_path, "triage", {"AI_GATEWAY_REQUIRED": "true"})
    for apply in (False, True):
        with pytest.raises(ValueError, match="AGENT_MODEL_GATEWAY_ENDPOINT"):
            module.deploy(deploy_arguments(tmp_path, "triage", apply=apply, grant_runtime_roles=True))


def test_deploy_rejects_a_lookup_role_without_its_mcp_route_before_any_network_call(tmp_path, no_azure):
    role = "policy-claim-lookup"
    write_config(tmp_path, role, {"AI_GATEWAY_MCP_REQUIRED": "true"})
    with pytest.raises(ValueError, match=f"no {role} entry|JSON object"):
        module.deploy(deploy_arguments(tmp_path, role, apply=True, grant_runtime_roles=True))
    write_config(tmp_path, role, {"AI_GATEWAY_MCP_REQUIRED": "true",
                                  "AGENT_MCP_GATEWAY": json.dumps({"policy-wording": {"url": "https://x"}})})
    with pytest.raises(ValueError, match=f"no {role} entry"):
        module.deploy(deploy_arguments(tmp_path, role, apply=False))


def test_dry_run_receipt_marks_gateway_routing_only_when_enabled(tmp_path, no_azure):
    write_config(tmp_path, "triage")
    arguments = deploy_arguments(tmp_path, "triage", apply=False)
    module.deploy(arguments)
    assert "ai_gateway" not in json.loads(arguments.output.read_text())
    write_config(tmp_path, "triage", {"AI_GATEWAY_REQUIRED": "true", **GATEWAY_SETTINGS})
    module.deploy(arguments)
    report = json.loads(arguments.output.read_text())
    assert report["ai_gateway"] == {"models_required": True, "mcp_required": False}
    assert report["roles_modified"] is False
    write_config(tmp_path, "policy-claim-lookup", {"AI_GATEWAY_MCP_REQUIRED": "true", "AGENT_MCP_GATEWAY": MCP_ROUTES})
    lookup = deploy_arguments(tmp_path, "policy-claim-lookup", apply=False)
    module.deploy(lookup)
    assert json.loads(lookup.output.read_text())["ai_gateway"] == {"models_required": False, "mcp_required": True}
    write_config(tmp_path, "triage", {"AI_GATEWAY_MCP_REQUIRED": "true"})
    module.deploy(arguments)
    assert "ai_gateway" not in json.loads(arguments.output.read_text())


def insurance_arguments(tmp_path, project, arm_project=None, *, apply=False):
    arguments = deploy_arguments(tmp_path, "triage", apply=apply)
    arguments.project_endpoint = f"https://aisvc-zurlo-northwind.services.ai.azure.com/api/projects/{project}"
    arguments.project_arm_id = f"{ACCOUNT}/projects/{arm_project or project}"
    return arguments


@pytest.mark.parametrize("project", ["northwind-orders", "nw-insurance-claims"])
def test_insurance_deploy_accepts_both_native_insurance_projects(tmp_path, no_azure, project):
    write_config(tmp_path, "triage")
    arguments = insurance_arguments(tmp_path, project)
    module.deploy(arguments)
    assert json.loads(arguments.output.read_text())["business_domain"] == "insurance"


@pytest.mark.parametrize("endpoint", [
    "https://aisvc-zurlo-northwind.services.ai.azure.com/api/projects/northwind",
    "https://aisvc-zurlo-northwind.services.ai.azure.com/api/projects/nw-insurance-support",
    "https://aisvc-zurlo-northwind.services.ai.azure.com/api/projects/nw-insurance-claims/extra",
    "http://aisvc-zurlo-northwind.services.ai.azure.com/api/projects/nw-insurance-claims",
    "https://aisvc-zurlo-northwind.services.ai.azure.com/api/projects/nw-insurance-claims?x=1",
    "https://user@aisvc-zurlo-northwind.services.ai.azure.com/api/projects/nw-insurance-claims",
])
def test_insurance_deploy_rejects_other_projects_and_unsafe_endpoints(tmp_path, no_azure, endpoint):
    write_config(tmp_path, "triage")
    arguments = insurance_arguments(tmp_path, "nw-insurance-claims")
    arguments.project_endpoint = endpoint
    with pytest.raises(ValueError, match="native insurance project"):
        module.deploy(arguments)


def test_insurance_apply_requires_the_arm_id_to_name_the_same_project(tmp_path, no_azure):
    write_config(tmp_path, "triage")
    arguments = insurance_arguments(tmp_path, "nw-insurance-claims", "northwind-orders", apply=True)
    with pytest.raises(ValueError, match="ARM ID must match the existing nw-insurance-claims project"):
        module.deploy(arguments)


class Version:
    def __init__(self, version, status):
        self.version = version
        self.metadata = {}
        self.status = status

    def as_dict(self):
        return {"status": self.status, "instance_identity": {"principal_id": PRINCIPAL}}


class Agents:
    def __init__(self):
        self.created, self.updated = [], []

    def list_versions(self, name):
        return []

    def create_version_from_code(self, **kwargs):
        self.created.append(kwargs)
        return Version("1", "creating")

    def get_version(self, name, version):
        return Version(version, "active")

    def update_details(self, **kwargs):
        self.updated.append(kwargs)


class Response:
    status_code = 201

    def json(self):
        return {}

    def raise_for_status(self):
        return None


class Http:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def put(self, url, params=None, headers=None, json=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "json": json})
        return Response()


def apply_deployment(tmp_path, monkeypatch, role, *, common_environment=None, grant_runtime_roles=True,
                     document=None):
    agents, calls = Agents(), []

    class Project:
        def __init__(self, **kwargs):
            self.agents = agents

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(module, "AIProjectClient", Project)
    monkeypatch.setattr(module, "deployment_credential", lambda source, tenant: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(module, "get_bearer_token_provider", lambda credential, scope: (lambda: "token-fixture"))
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: Http(calls))
    if document is None:
        write_config(tmp_path, role, common_environment)
    else:
        (tmp_path / "config.json").write_text(json.dumps(document), encoding="utf-8")
    arguments = deploy_arguments(tmp_path, role, apply=True, grant_runtime_roles=grant_runtime_roles)
    module.deploy(arguments)
    return agents, calls, json.loads(arguments.output.read_text())


def granted(calls):
    pairs = []
    for call in calls:
        scope = call["url"].removeprefix("https://management.azure.com").split(
            "/providers/Microsoft.Authorization/roleAssignments/")[0]
        properties = call["json"]["properties"]
        assert properties["principalId"] == PRINCIPAL and properties["principalType"] == "ServicePrincipal"
        assert properties["roleDefinitionId"].startswith(
            f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/")
        assert call["params"] == {"api-version": "2022-04-01"} and call["headers"] == {
            "Authorization": "Bearer token-fixture"}
        pairs.append((scope, properties["roleDefinitionId"].rsplit("/", 1)[1]))
    return pairs


@pytest.mark.parametrize("role", list(module.ROLES))
def test_apply_without_gateway_flags_issues_exactly_the_direct_grants(tmp_path, monkeypatch, role):
    agents, calls, report = apply_deployment(tmp_path, monkeypatch, role)
    expected = DIRECT + ([(PROJECT, CONSUMER)] if role == "orchestrator" else [])
    assert granted(calls) == expected
    assert report["roles_modified"] is True and report["principal_id"] == PRINCIPAL
    assert "ai_gateway" not in report
    assert len(agents.created) == 1 and len(agents.updated) == 1
    assert agents.created[0]["agent_name"] == module.ROLES[role]
    assert agents.created[0]["metadata"]["business_domain"] == "insurance"
    assert agents.created[0]["description"] == f"Hosted Northwind insurance {role}"


def test_apply_does_not_touch_roles_without_the_explicit_opt_in(tmp_path, monkeypatch):
    _, calls, report = apply_deployment(tmp_path, monkeypatch, "triage", grant_runtime_roles=False)
    assert calls == [] and report["roles_modified"] is False


def test_apply_with_gateway_models_grants_models_user_on_the_gateway_instead_of_openai_user(tmp_path, monkeypatch):
    environment = {"AI_GATEWAY_REQUIRED": "true", **GATEWAY_SETTINGS}
    agents, calls, report = apply_deployment(tmp_path, monkeypatch, "orchestrator", common_environment=environment)
    pairs = granted(calls)
    assert pairs == [(GATEWAY, MODELS), (PROJECT, FOUNDRY), (PROJECT, CONSUMER)]
    assert OPENAI not in [role for _, role in pairs]
    assert report["ai_gateway"]["runtime_grants"] == [
        {"scope": GATEWAY, "role_id": MODELS}, {"scope": PROJECT, "role_id": FOUNDRY},
        {"scope": PROJECT, "role_id": CONSUMER}]
    sent = agents.created[0]["definition"].as_dict()["environment_variables"]
    assert sent["AI_GATEWAY_REQUIRED"] == "true" and sent["AGENT_MODEL_GATEWAY_MODEL"] == "aisvc-zurlo-northwind/gpt-4o"
    assert sent["AGENT_ROLE"] == "orchestrator" and sent["AGENT_MODEL_DEPLOYMENT"] == "baseline"


def test_a_deprecated_config_is_sent_to_the_agent_with_canonical_names_only(tmp_path, monkeypatch):
    document = {
        "common": {"ORDERS_MODEL_DEPLOYMENT": "legacy-baseline", "AI_GATEWAY_REQUIRED": "true",
                   "AI_GATEWAY_MCP_REQUIRED": "true", "ORDERS_MCP_GATEWAY": DEPRECATED_MCP_ROUTES,
                   "ORDER_AGENT_ROLE": "manager", "NORTHWIND_BUSINESS_DOMAIN": "insurance",
                   **DEPRECATED_GATEWAY_SETTINGS},
        "orderlookup": {"MCP_TECH_LOOKUP_URL": "https://lookup.example/mcp"},
    }
    agents, calls, report = apply_deployment(tmp_path, monkeypatch, "orderlookup", document=document)
    sent = agents.created[0]["definition"].as_dict()["environment_variables"]
    assert sent == {
        "AGENT_ROLE": "policy-claim-lookup", "AGENT_MODEL_DEPLOYMENT": "legacy-baseline",
        "AI_GATEWAY_REQUIRED": "true", "AI_GATEWAY_MCP_REQUIRED": "true", "AGENT_MCP_GATEWAY": DEPRECATED_MCP_ROUTES,
        "MCP_POLICY_LOOKUP_URL": "https://lookup.example/mcp",
        "AGENT_MODEL_GATEWAY_ENDPOINT": GATEWAY_SETTINGS["AGENT_MODEL_GATEWAY_ENDPOINT"],
        "AGENT_MODEL_GATEWAY_MODEL": GATEWAY_SETTINGS["AGENT_MODEL_GATEWAY_MODEL"],
        "AGENT_MODEL_GATEWAY_DEPLOYMENT": GATEWAY_SETTINGS["AGENT_MODEL_GATEWAY_DEPLOYMENT"],
    }
    assert agents.created[0]["agent_name"] == "northwind-insurance-policy-claim-lookup"
    assert granted(calls) == [(GATEWAY, MODELS), (PROJECT, FOUNDRY)] and report["ai_gateway"]["mcp_required"] is True


@pytest.mark.parametrize("role,mcp_routed", [
    ("policy-claim-lookup", True), ("policy-wording", True), ("triage", False), ("claim-action-draft", False),
])
def test_apply_with_gateway_mcp_grants_no_tools_role_to_any_agent(tmp_path, monkeypatch, role, mcp_routed):
    environment = {"AI_GATEWAY_REQUIRED": "true", "AI_GATEWAY_MCP_REQUIRED": "true",
                   "AGENT_MCP_GATEWAY": MCP_ROUTES, **GATEWAY_SETTINGS}
    _, calls, report = apply_deployment(tmp_path, monkeypatch, role, common_environment=environment)
    assert granted(calls) == [(GATEWAY, MODELS), (PROJECT, FOUNDRY)]
    assert report["ai_gateway"]["mcp_required"] is mcp_routed


def test_gateway_grant_request_is_a_deterministic_role_assignment_on_the_gateway_resource(tmp_path, monkeypatch):
    environment = {"AI_GATEWAY_REQUIRED": "true", **GATEWAY_SETTINGS}
    _, calls, _ = apply_deployment(tmp_path, monkeypatch, "triage", common_environment=environment)
    first = calls[0]
    assert first["url"].startswith(f"https://management.azure.com{GATEWAY}/providers/Microsoft.Authorization/"
                                   "roleAssignments/")
    assert first["json"]["properties"]["roleDefinitionId"].endswith(f"/roleDefinitions/{MODELS}")
